"""Индекс контента в памяти.

Контент — файлы в git, и на каждый запрос перечитывать десятки YAML незачем.
Индекс собирается один раз при старте приложения и живёт в app.state.

Перезагрузка делается явно (`reload`), а не по таймеру: контент меняется
коммитом, а не сам по себе, и фоновое подглядывание за файловой системой
породило бы гонки на ровном месте.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dojo.content.loader import SkillSpec, TaskSpec, load_skills
from dojo.content.quiz import Quiz, QuizError, parse_quiz
from dojo.core.logging import get_logger
from dojo.runner.kata import Kata, KataError, load_kata
from dojo.runner.sql_check import SqlTaskError, SqlTaskFile, parse_task_file

logger = get_logger(__name__)


class SkillNotFoundError(LookupError):
    def __init__(self, skill_id: str) -> None:
        super().__init__(f"узел {skill_id!r} не найден")
        self.skill_id = skill_id


@dataclass(slots=True)
class ContentIndex:
    content_root: Path
    repo_root: Path
    skills: dict[str, SkillSpec] = field(default_factory=dict)
    quizzes: dict[str, Quiz] = field(default_factory=dict)
    sql_tasks: dict[str, SqlTaskFile] = field(default_factory=dict)
    katas: dict[str, Kata] = field(default_factory=dict)

    def reload(self) -> None:
        skills = load_skills(self.content_root, self.repo_root)
        self.skills = {skill.id: skill for skill in skills}

        quizzes: dict[str, Quiz] = {}
        sql_tasks: dict[str, SqlTaskFile] = {}
        katas: dict[str, Kata] = {}

        for skill in skills:
            quiz_task = self.quiz_task(skill)
            if quiz_task is not None:
                try:
                    quizzes[skill.id] = parse_quiz(
                        skill.id, self.content_root / quiz_task.spec["file"]
                    )
                except QuizError:
                    # Валидатор контента ловит это в CI; здесь молчаливый пропуск
                    # лучше падения приложения из-за одного битого файла.
                    logger.warning("quiz.parse.failed", skill_id=skill.id)

            sql_task = self.sql_task(skill)
            if sql_task is not None:
                try:
                    sql_tasks[skill.id] = parse_task_file(
                        skill.id, self.content_root / sql_task.spec["file"]
                    )
                except SqlTaskError:
                    logger.warning("sql_tasks.parse.failed", skill_id=skill.id)

            kata_task = self.kata_task(skill)
            if kata_task is not None:
                try:
                    name = str(kata_task.spec["path"]).removeprefix("katas/")
                    katas[skill.id] = load_kata(self.content_root, name)
                except KataError:
                    logger.warning("kata.load.failed", skill_id=skill.id)

        self.quizzes = quizzes
        self.sql_tasks = sql_tasks
        self.katas = katas

        logger.info(
            "content.loaded",
            skills=len(self.skills),
            quizzes=len(self.quizzes),
            sql_tasks=len(self.sql_tasks),
            katas=len(self.katas),
        )

    @staticmethod
    def quiz_task(skill: SkillSpec) -> TaskSpec | None:
        return next((task for task in skill.tasks if task.type == "quiz"), None)

    @staticmethod
    def sql_task(skill: SkillSpec) -> TaskSpec | None:
        return next((task for task in skill.tasks if task.type == "sql"), None)

    @staticmethod
    def kata_task(skill: SkillSpec) -> TaskSpec | None:
        return next((task for task in skill.tasks if task.type == "kata"), None)

    def skill(self, skill_id: str) -> SkillSpec:
        try:
            return self.skills[skill_id]
        except KeyError as exc:
            raise SkillNotFoundError(skill_id) from exc

    def theory(self, skill_id: str) -> str | None:
        """Исходный markdown теории или None, если файла нет."""
        skill = self.skill(skill_id)
        if not skill.theory_path:
            return None
        path = self.content_root / skill.theory_path
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def is_stub_theory(self, skill_id: str) -> bool:
        """Заглушка ли теория. Интерфейс обязан говорить об этом честно."""
        text = self.theory(skill_id)
        return text is not None and "ЗАГЛУШКА" in text

    def unlocked(self, closed: set[str]) -> set[str]:
        """Узлы, доступные к изучению: все жёсткие предпосылки закрыты."""
        return {
            skill.id
            for skill in self.skills.values()
            if skill.id not in closed and all(pid in closed for pid, hard in skill.prereq if hard)
        }


def find_content_root(start: Path | None = None) -> Path:
    """Ищет каталог content/, поднимаясь вверх по дереву.

    Тот же приём, что и у раннера миграций: модуль работает и из репозитория,
    и из контейнера, куда content смонтирован томом.
    """
    origin = (start or Path(__file__).resolve()).parent
    for candidate in [origin, *origin.parents]:
        found = candidate / "content" / "schemas"
        if found.is_dir():
            return candidate / "content"
    msg = f"каталог content/ не найден вверх от {origin}"
    raise FileNotFoundError(msg)


def load_index(content_root: Path, repo_root: Path) -> ContentIndex:
    index = ContentIndex(content_root=content_root, repo_root=repo_root)
    index.reload()
    return index
