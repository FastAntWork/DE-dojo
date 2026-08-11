#!/usr/bin/env python3
"""Валидация учебного контента.

Запускается в CI и в pre-commit. Битый prereq, цикл в графе или лаба без
check.py должны ронять сборку здесь — то есть за секунды и с указанием файла,
а не всплывать в момент, когда планировщик выдал пользователю задание.

    python tools/content_validate.py [--content DIR]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Final

import yaml
from jsonschema import Draft202012Validator

# Контракт лабы. Без любого из этих файлов стенд невозможно ни собрать, ни
# честно проверить.
#
# Отличие от ТЗ: вместо docker-compose.yml и seed.sh здесь lab.yaml с типом
# стенда и seed.N.sql на каждый вариант поломки. Причина в том, что лабе на
# PostgreSQL свой контейнер не нужен — ей достаточно отдельной базы в уже
# поднятом экземпляре, а второй PostgreSQL стоил бы гигабайт при бюджете в
# десять. Лабы с типом стенда compose приносят свой docker-compose.yml, и он
# проверяется отдельно.
LAB_REQUIRED: Final = (
    "lab.yaml",
    "brief.md",
    "check.py",
    "hints.yaml",
    "solution.md",
)

# Эталонное решение обязательно: сгенерированная задача сначала прогоняется
# на нём в sandbox, и не прошедшая отбраковывается, не доходя до пользователя.
KATA_REQUIRED: Final = (
    "task.md",
    "starter.py",
    "tests_public.py",
    "tests_hidden.py",
    "solution.py",
)


class Color(IntEnum):
    """Разметка вершин при поиске циклов обходом в глубину."""

    WHITE = 0  # не посещалась
    GREY = 1  # в текущем стеке обхода
    BLACK = 2  # полностью обработана


@dataclass(frozen=True, slots=True)
class Problem:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    path: Path
    data: dict[str, Any]

    @property
    def prereq(self) -> list[str]:
        return [*self.data.get("prereq", []), *self.data.get("soft_prereq", [])]


def load_yaml(path: Path) -> tuple[dict[str, Any] | None, Problem | None]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, Problem(str(path), f"невалидный YAML: {exc}")
    except OSError as exc:
        return None, Problem(str(path), f"не читается: {exc}")

    if not isinstance(raw, dict):
        return None, Problem(str(path), "ожидался объект верхнего уровня")
    return raw, None


def validate_schema(skill: Skill, validator: Draft202012Validator, rel: Path) -> list[Problem]:
    problems: list[Problem] = []
    for error in sorted(validator.iter_errors(skill.data), key=lambda e: list(e.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "<корень>"
        problems.append(Problem(f"{rel}:{location}", error.message))
    return problems


def validate_naming(skill: Skill, rel: Path) -> list[Problem]:
    """id обязан совпадать с именем файла, track — с каталогом.

    Иначе файл невозможно найти по идентификатору из графа, а именно так его
    ищут и человек, и `dojo content sync`.
    """
    problems: list[Problem] = []
    if skill.path.stem != skill.id:
        problems.append(
            Problem(str(rel), f"id={skill.id!r} не совпадает с именем файла {skill.path.stem!r}")
        )
    track = skill.data.get("track")
    if track is not None and skill.path.parent.name != track:
        problems.append(
            Problem(
                str(rel), f"track={track!r} не совпадает с каталогом {skill.path.parent.name!r}"
            )
        )
    return problems


def validate_references(
    skill: Skill, content_root: Path, repo_root: Path, rel: Path
) -> list[Problem]:
    """Проверяет, что всё, на что ссылается узел, существует на диске."""
    problems: list[Problem] = []

    def require_file(relative: str, what: str) -> None:
        if not (content_root / relative).is_file():
            problems.append(Problem(str(rel), f"{what} не найден: content/{relative}"))

    def require_dir_with(relative: str, names: tuple[str, ...], what: str) -> None:
        directory = content_root / relative
        if not directory.is_dir():
            problems.append(Problem(str(rel), f"{what} не найден: content/{relative}"))
            return
        missing = [name for name in names if not (directory / name).exists()]
        if missing:
            problems.append(
                Problem(
                    str(rel), f"в content/{relative} нет обязательных файлов: {', '.join(missing)}"
                )
            )

    theory = skill.data.get("theory")
    if isinstance(theory, dict) and "read" in theory:
        require_file(theory["read"], "файл теории")

    for index, task in enumerate(skill.data.get("tasks", [])):
        if not isinstance(task, dict):
            continue
        kind = task.get("type")
        at = f"tasks[{index}] ({kind})"

        match kind:
            case "flashcard":
                require_file(task["deck"], f"{at}: колода")
            case "quiz" | "sql":
                require_file(task["file"], f"{at}: файл заданий")
            case "kata":
                require_dir_with(task["path"], KATA_REQUIRED, f"{at}: каталог каты")
            case "lab":
                require_dir_with(task["path"], LAB_REQUIRED, f"{at}: каталог лабы")
                problems += _validate_lab_variants(content_root, task["path"], rel, at)
            case "review" | "design" | "interview":
                require_file(task["rubric"], f"{at}: рубрика")
            case "capstone":
                for test in task.get("tests", []):
                    if not (repo_root / test).is_file():
                        problems.append(Problem(str(rel), f"{at}: тест не найден: {test}"))

    return problems


def validate_quizzes(content_root: Path, repo_root: Path) -> list[Problem]:
    """Проверяет квизы схемой и правилами, которые схемой не выражаются.

    Число верных вариантов зависит от kind, а JSON Schema такое условие
    описывает только через громоздкий if/then. Проще и понятнее — кодом.
    """
    schema_path = content_root / "schemas" / "quiz.schema.json"
    if not schema_path.is_file():
        return [Problem(str(schema_path), "схема квиза не найдена")]

    schema_data, problem = load_yaml(schema_path)
    if schema_data is None:
        return [problem] if problem else []
    validator = Draft202012Validator(schema_data)

    problems: list[Problem] = []

    for path in sorted((content_root / "quizzes").glob("*.yaml")):
        rel = path.relative_to(repo_root)
        data, problem = load_yaml(path)
        if data is None:
            if problem:
                problems.append(problem)
            continue

        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            location = ".".join(str(part) for part in error.absolute_path) or "<корень>"
            problems.append(Problem(f"{rel}:{location}", error.message))

        seen: set[str] = set()
        for index, question in enumerate(data.get("questions", [])):
            if not isinstance(question, dict):
                continue
            question_id = question.get("id", f"<без id, индекс {index}>")

            if question_id in seen:
                problems.append(Problem(str(rel), f"дублирующийся id вопроса: {question_id}"))
            seen.add(question_id)

            correct = sum(1 for option in question.get("options", []) if option.get("correct"))
            kind = question.get("kind")
            expected: str | None = None
            if kind in {"single", "find-error"} and correct != 1:
                expected = "ровно один верный вариант"
            elif kind == "multiple" and correct < 2:
                expected = "минимум два верных варианта"
            if expected is not None:
                problems.append(
                    Problem(
                        str(rel),
                        f"{question_id}: kind={kind} требует {expected}, найдено {correct}",
                    )
                )

    return problems


def _validate_lab_variants(content_root: Path, relative: str, rel: Path, at: str) -> list[Problem]:
    """Каждый заявленный вариант поломки обязан иметь свой seed.

    Иначе лаба с variants: 3 отдаёт человеку вариант, которого нет, и падает
    уже в момент подготовки стенда — то есть после того, как он её открыл.
    """
    directory = content_root / relative
    meta_path = directory / "lab.yaml"
    if not meta_path.is_file():
        return []

    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [Problem(str(rel), f"{at}: lab.yaml не читается: {exc}")]

    problems: list[Problem] = []
    stand = str(meta.get("stand", "shared-postgres"))
    variants = int(meta.get("variants", 1))

    if stand == "compose" and not (directory / "docker-compose.yml").is_file():
        problems.append(Problem(str(rel), f"{at}: стенд compose требует docker-compose.yml"))

    missing = [n for n in range(1, variants + 1) if not (directory / f"seed.{n}.sql").is_file()]
    if missing:
        names = ", ".join(f"seed.{n}.sql" for n in missing)
        problems.append(
            Problem(str(rel), f"{at}: объявлено вариантов {variants}, нет файлов: {names}")
        )

    return problems


def validate_graph(skills: dict[str, Skill]) -> list[Problem]:
    """Проверяет, что prereq разрешимы и в графе нет циклов."""
    problems: list[Problem] = []

    for skill in skills.values():
        for prereq in skill.prereq:
            if prereq not in skills:
                problems.append(
                    Problem(skill.id, f"prereq указывает на несуществующий узел: {prereq}")
                )

    # Встреча с GREY означает цикл, а стек обхода даёт его конкретный состав:
    # сообщение «цикл существует» без пути чинить невозможно.
    color: dict[str, Color] = dict.fromkeys(skills, Color.WHITE)
    stack: list[str] = []
    reported: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        color[node] = Color.GREY
        stack.append(node)
        for prereq in skills[node].prereq:
            if prereq not in skills:
                continue
            if color[prereq] is Color.GREY:
                cycle = [*stack[stack.index(prereq) :], prereq]
                signature = frozenset(cycle)
                if signature not in reported:
                    reported.add(signature)
                    problems.append(Problem("граф", "цикл в prereq: " + " -> ".join(cycle)))
            elif color[prereq] is Color.WHITE:
                visit(prereq)
        stack.pop()
        color[node] = Color.BLACK

    for node in sorted(skills):
        if color[node] is Color.WHITE:
            visit(node)

    return problems


def validate(content_root: Path, repo_root: Path) -> list[Problem]:
    schema_path = content_root / "schemas" / "skill.schema.json"
    if not schema_path.is_file():
        return [Problem(str(schema_path), "схема узла не найдена")]

    schema_data, problem = load_yaml(schema_path)  # JSON — валидный YAML
    if schema_data is None:
        return [problem] if problem else []
    validator = Draft202012Validator(schema_data)

    problems: list[Problem] = []
    skills: dict[str, Skill] = {}
    seen_paths: dict[str, Path] = {}

    for path in sorted((content_root / "tracks").rglob("*.yaml")):
        rel = path.relative_to(repo_root)
        data, problem = load_yaml(path)
        if data is None:
            if problem:
                problems.append(problem)
            continue

        skill_id = data.get("id")
        if not isinstance(skill_id, str):
            problems.append(Problem(str(rel), "нет поля id"))
            continue

        skill = Skill(id=skill_id, path=path, data=data)
        problems += validate_schema(skill, validator, rel)
        problems += validate_naming(skill, rel)
        problems += validate_references(skill, content_root, repo_root, rel)

        if skill_id in seen_paths:
            problems.append(
                Problem(
                    str(rel),
                    f"дублирующийся id {skill_id!r}, уже объявлен в {seen_paths[skill_id]}",
                )
            )
        else:
            seen_paths[skill_id] = rel
            skills[skill_id] = skill

    problems += validate_graph(skills)
    problems += validate_quizzes(content_root, repo_root)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Валидирует учебный контент DE Dojo.")
    parser.add_argument("--content", type=Path, default=None, help="Каталог content/.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    content_root = args.content.resolve() if args.content else repo_root / "content"

    if not content_root.is_dir():
        print(f"каталог не найден: {content_root}", file=sys.stderr)
        return 2

    problems = validate(content_root, repo_root)
    node_count = len(list((content_root / "tracks").rglob("*.yaml")))

    if problems:
        print(f"Контент невалиден. Проблем: {len(problems)}\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"Контент валиден: узлов {node_count}, проблем нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
