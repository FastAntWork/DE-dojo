"""Тесты валидатора контента.

Проверяем именно то, ради чего он существует: что битый граф ломает сборку
на этапе валидации, а не всплывает у пользователя посреди занятия.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.content_validate import Problem, validate

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCHEMAS = REPO_ROOT / "content" / "schemas"

# Минимальный валидный квиз: файлы в quizzes/ проверяются схемой, поэтому
# заглушки «questions: []» здесь уже недостаточно.
SAMPLE_QUIZ = """
questions:
  - id: sample-question
    kind: single
    prompt: "Вопрос-заглушка для тестов валидатора"
    options:
      - text: "Верный"
        correct: true
      - text: "Неверный"
    explanation: "Объяснение-заглушка достаточной длины для схемы"
"""


@pytest.fixture
def content(tmp_path: Path) -> Path:
    """Пустой каталог content/ с настоящими схемами внутри."""
    root = tmp_path / "content"
    (root / "schemas").mkdir(parents=True)
    (root / "tracks").mkdir()
    (root / "quizzes").mkdir()
    for schema in REAL_SCHEMAS.glob("*.schema.json"):
        shutil.copy(schema, root / "schemas" / schema.name)
    (root / "quizzes" / "sample.yaml").write_text(SAMPLE_QUIZ, encoding="utf-8")
    return root


def skill_dict(skill_id: str, track: str, prereq: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": skill_id,
        "title": f"Узел {skill_id}",
        "track": track,
        "level": 1,
        "estimated_hours": 4,
        "prereq": prereq or [],
        "objectives": ["Объяснить, как это работает, и когда применять"],
        "tasks": [{"type": "quiz", "file": "quizzes/sample.yaml"}],
    }


def write_skill(content: Path, skill_id: str, track: str, prereq: list[str] | None = None) -> Path:
    directory = content / "tracks" / track
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{skill_id}.yaml"
    path.write_text(
        yaml.safe_dump(skill_dict(skill_id, track, prereq), allow_unicode=True), "utf-8"
    )
    return path


def messages(problems: list[Problem]) -> str:
    return "\n".join(str(p) for p in problems)


class TestHappyPath:
    def test_empty_content_is_valid(self, content: Path) -> None:
        assert validate(content, content.parent) == []

    def test_minimal_node_is_valid(self, content: Path) -> None:
        write_skill(content, "sql.joins", "sql")
        assert validate(content, content.parent) == []

    def test_chain_of_prereqs_is_valid(self, content: Path) -> None:
        write_skill(content, "sql.core", "sql")
        write_skill(content, "sql.joins", "sql", prereq=["sql.core"])
        write_skill(content, "sql.windows", "sql", prereq=["sql.joins"])
        assert validate(content, content.parent) == []


class TestGraph:
    def test_dangling_prereq_is_reported(self, content: Path) -> None:
        write_skill(content, "sql.joins", "sql", prereq=["sql.does-not-exist"])

        problems = validate(content, content.parent)

        assert "несуществующий узел: sql.does-not-exist" in messages(problems)

    def test_cycle_is_reported_with_path(self, content: Path) -> None:
        write_skill(content, "a.one", "sql", prereq=["a.three"])
        write_skill(content, "a.two", "sql", prereq=["a.one"])
        write_skill(content, "a.three", "sql", prereq=["a.two"])

        text = messages(validate(content, content.parent))

        # Важен не факт цикла, а его состав: без пути чинить нечего.
        assert "цикл в prereq" in text
        for node in ("a.one", "a.two", "a.three"):
            assert node in text

    def test_self_reference_is_a_cycle(self, content: Path) -> None:
        write_skill(content, "sql.joins", "sql", prereq=["sql.joins"])

        assert "цикл в prereq" in messages(validate(content, content.parent))

    def test_cycle_reported_once(self, content: Path) -> None:
        write_skill(content, "a.one", "sql", prereq=["a.two"])
        write_skill(content, "a.two", "sql", prereq=["a.one"])

        cycles = [p for p in validate(content, content.parent) if "цикл" in p.message]

        assert len(cycles) == 1


class TestNaming:
    def test_id_must_match_filename(self, content: Path) -> None:
        path = write_skill(content, "sql.joins", "sql")
        path.rename(path.with_name("sql.wrong-name.yaml"))

        assert "не совпадает с именем файла" in messages(validate(content, content.parent))

    def test_track_must_match_directory(self, content: Path) -> None:
        data = skill_dict("sql.joins", "postgres")
        directory = content / "tracks" / "sql"
        directory.mkdir(parents=True)
        (directory / "sql.joins.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

        assert "не совпадает с каталогом" in messages(validate(content, content.parent))


class TestSchema:
    def test_unknown_field_is_rejected(self, content: Path) -> None:
        data = skill_dict("sql.joins", "sql")
        data["typo_field"] = 1
        directory = content / "tracks" / "sql"
        directory.mkdir(parents=True)
        (directory / "sql.joins.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

        assert validate(content, content.parent) != []

    def test_node_without_tasks_is_rejected(self, content: Path) -> None:
        data = skill_dict("sql.joins", "sql")
        data["tasks"] = []
        directory = content / "tracks" / "sql"
        directory.mkdir(parents=True)
        (directory / "sql.joins.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

        assert validate(content, content.parent) != []


class TestReferences:
    def test_missing_quiz_file_is_reported(self, content: Path) -> None:
        (content / "quizzes" / "sample.yaml").unlink()
        write_skill(content, "sql.joins", "sql")

        assert "не найден: content/quizzes/sample.yaml" in messages(
            validate(content, content.parent)
        )

    def test_lab_without_checker_is_reported(self, content: Path) -> None:
        lab = content / "labs" / "kafka-consumer-lag"
        lab.mkdir(parents=True)
        for name in ("docker-compose.yml", "brief.md", "seed.sh", "hints.yaml", "solution.md"):
            (lab / name).write_text("", encoding="utf-8")
        # check.py намеренно отсутствует — он единственный источник истины
        # о зачёте, и лаба без него непроверяема.

        data = skill_dict("kafka.groups", "kafka")
        data["tasks"] = [{"type": "lab", "path": "labs/kafka-consumer-lag", "variants": 3}]
        directory = content / "tracks" / "kafka"
        directory.mkdir(parents=True)
        (directory / "kafka.groups.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

        assert "check.py" in messages(validate(content, content.parent))


class TestDuplicates:
    def test_duplicate_id_across_tracks_is_reported(self, content: Path) -> None:
        write_skill(content, "sql.joins", "sql")
        second = content / "tracks" / "postgres"
        second.mkdir(parents=True)
        data = skill_dict("sql.joins", "postgres")
        (second / "sql.joins.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

        assert "дублирующийся id" in messages(validate(content, content.parent))
