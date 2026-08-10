"""Чтение учебного контента из файлов в доменные объекты.

Здесь нет ни строчки про БД: загрузчик превращает YAML в структуры, а запись
живёт в sync.py. Разделение нужно, чтобы разбор контента тестировался без
поднятой базы — а это самая объёмная часть логики.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

# Значения по умолчанию для полей, которых может не быть в YAML.
DEFAULT_DIFFICULTY: Final = 0.5
DEFAULT_TIMEOUT_SEC: Final = 900
DEFAULT_REVIEW_DAYS: Final = (1, 4, 12, 30)

# Ключи, по которым строится устойчивый идентификатор задания.
_NATURAL_KEY: Final[dict[str, str]] = {
    "flashcard": "deck",
    "quiz": "file",
    "sql": "file",
    "kata": "path",
    "lab": "path",
    "review": "rubric",
    "design": "rubric",
    "interview": "rubric",
}


class ContentError(RuntimeError):
    """Контент не удалось разобрать. Валидатор должен был поймать это раньше."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    skill_id: str
    type: str
    ordinal: int
    difficulty: float
    variants: int
    timeout_sec: int
    spec: dict[str, Any]
    hints: list[dict[str, Any]]
    source_path: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SkillSpec:
    id: str
    title: str
    track: str
    level: int
    estimated_hours: float
    objectives: list[str]
    job_tags: list[str]
    theory_path: str | None
    rag_scope: list[str]
    review_after_days: list[int]
    # (id предпосылки, жёсткая ли она). Мягкая не блокирует выдачу узла.
    prereq: list[tuple[str, bool]]
    source_path: str
    content_hash: str
    tasks: list[TaskSpec] = field(default_factory=list)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_slug(task: dict[str, Any]) -> str:
    """Устойчивая часть идентификатора задания.

    Порядковый номер в списке для этого не годится: перестановка заданий
    местами переименовала бы их все, а вместе с идентификатором потерялась бы
    и привязка истории попыток. Поэтому берём естественный ключ — путь к файлу
    или каталогу, а для текстовых заданий короткий хеш формулировки.
    """
    kind = task["type"]
    key = _NATURAL_KEY.get(kind)
    if key is not None and key in task:
        return Path(str(task[key])).stem

    # design/review/interview без рубрики и capstone: ключ — сама формулировка.
    text = str(task.get("repo_task") or task.get("prompt") or "")
    if not text:
        raise ContentError(f"нечем идентифицировать задание типа {kind}")
    return sha256_text(text)[:12]


def _timeout_sec(task: dict[str, Any]) -> int:
    if "timeout_sec" in task:
        return int(task["timeout_sec"])
    if "timeout_min" in task:
        return int(task["timeout_min"]) * 60
    if "duration_min" in task:
        return int(task["duration_min"]) * 60
    return DEFAULT_TIMEOUT_SEC


def parse_task(skill_id: str, task: dict[str, Any], ordinal: int, source_path: str) -> TaskSpec:
    kind = str(task["type"])
    # spec хранит тип-специфичную часть: общие поля вынесены в колонки, а всё
    # остальное у девяти типов почти не пересекается — в отдельных колонках
    # таблица стала бы решетом из NULL.
    spec = {
        key: value
        for key, value in task.items()
        if key not in {"type", "difficulty", "hints", "variants", "timeout_sec", "timeout_min"}
    }

    return TaskSpec(
        id=f"{skill_id}::{kind}::{_stable_slug(task)}",
        skill_id=skill_id,
        type=kind,
        ordinal=ordinal,
        difficulty=float(task.get("difficulty", DEFAULT_DIFFICULTY)),
        variants=int(task.get("variants", 1)),
        timeout_sec=_timeout_sec(task),
        spec=spec,
        hints=list(task.get("hints", [])),
        source_path=source_path,
        # Хеш по каноническому виду задания: пересортировка ключей в YAML
        # не должна выглядеть как изменение содержимого.
        content_hash=sha256_text(yaml.safe_dump(task, sort_keys=True, allow_unicode=True)),
    )


def parse_skill(path: Path, repo_root: Path) -> SkillSpec:
    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    if not isinstance(data, dict):
        raise ContentError(f"{path}: ожидался объект верхнего уровня")

    skill_id = str(data["id"])
    source_path = str(path.relative_to(repo_root))
    theory = data.get("theory") or {}

    prereq: list[tuple[str, bool]] = [(str(item), True) for item in data.get("prereq", [])]
    prereq += [(str(item), False) for item in data.get("soft_prereq", [])]

    tasks = [
        parse_task(skill_id, task, ordinal, source_path)
        for ordinal, task in enumerate(data.get("tasks", []))
    ]

    return SkillSpec(
        id=skill_id,
        title=str(data["title"]),
        track=str(data["track"]),
        level=int(data["level"]),
        estimated_hours=float(data["estimated_hours"]),
        objectives=[str(item) for item in data["objectives"]],
        job_tags=[str(item) for item in data.get("job_tags", [])],
        theory_path=theory.get("read"),
        rag_scope=[str(item) for item in theory.get("rag_scope", [])],
        review_after_days=[int(day) for day in data.get("review_after_days", DEFAULT_REVIEW_DAYS)],
        prereq=prereq,
        source_path=source_path,
        # Хеш по тексту файла: любое изменение узла делает его «изменённым»
        # и заставляет sync переписать строку. job_weight в файле не хранится,
        # поэтому перевзвешивание вакансиями хеш не трогает.
        content_hash=sha256_text(raw_text),
        tasks=tasks,
    )


def load_skills(content_root: Path, repo_root: Path) -> list[SkillSpec]:
    """Читает все узлы из content/tracks, отсортированные по id."""
    skills = [
        parse_skill(path, repo_root) for path in sorted((content_root / "tracks").rglob("*.yaml"))
    ]
    skills.sort(key=lambda skill: skill.id)
    return skills
