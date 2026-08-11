"""Квизы: загрузка и проверка ответов.

Ни строчки про ввод-вывод: здесь только структуры и арифметика. Интерактив
живёт в CLI, а проверка правильности обязана быть тестируемой без человека
за клавиатурой — по принципу №1 зачёт ставит детерминированный код, а не
интерпретация чьего-то ответа.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class QuizError(RuntimeError):
    """Квиз не найден или не разобран."""


@dataclass(frozen=True, slots=True)
class Option:
    text: str
    correct: bool

    @property
    def id(self) -> str:
        """Устойчивый идентификатор варианта — хеш его текста.

        Нужен, чтобы отдавать варианты клиенту, не раскрывая правильности:
        браузер получает id и текст, признак correct остаётся на сервере.
        Позиционный номер для этого не годится — варианты перемешиваются.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    kind: str
    prompt: str
    options: tuple[Option, ...]
    explanation: str
    source: str | None = None

    @property
    def correct_indices(self) -> frozenset[int]:
        return frozenset(i for i, option in enumerate(self.options) if option.correct)

    @property
    def correct_option_ids(self) -> frozenset[str]:
        return frozenset(option.id for option in self.options if option.correct)

    def grade_ids(self, chosen_ids: set[str]) -> bool:
        """Проверяет ответ, пришедший от клиента набором идентификаторов."""
        return frozenset(chosen_ids) == self.correct_option_ids

    @property
    def multiple(self) -> bool:
        return self.kind == "multiple"

    def shuffled(self, rng: random.Random) -> Question:
        """Копия с перемешанными вариантами.

        Порядок в файле не случаен — верный вариант часто оказывается первым.
        Без перемешивания повторное прохождение проверяло бы память на
        позицию, а не на материал.
        """
        options = list(self.options)
        rng.shuffle(options)
        return Question(
            id=self.id,
            kind=self.kind,
            prompt=self.prompt,
            options=tuple(options),
            explanation=self.explanation,
            source=self.source,
        )


@dataclass(frozen=True, slots=True)
class Quiz:
    skill_id: str
    path: Path
    questions: tuple[Question, ...]

    def shuffled(self, rng: random.Random) -> Quiz:
        questions = [q.shuffled(rng) for q in self.questions]
        rng.shuffle(questions)
        return Quiz(skill_id=self.skill_id, path=self.path, questions=tuple(questions))


def parse_quiz(skill_id: str, path: Path) -> Quiz:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        msg = f"{path}: не удалось прочитать квиз: {exc}"
        raise QuizError(msg) from exc

    if not isinstance(raw, dict) or "questions" not in raw:
        msg = f"{path}: в файле нет ключа questions"
        raise QuizError(msg)

    questions = tuple(
        Question(
            id=str(item["id"]),
            kind=str(item["kind"]),
            prompt=str(item["prompt"]).rstrip(),
            options=tuple(
                Option(text=str(o["text"]), correct=bool(o.get("correct", False)))
                for o in item["options"]
            ),
            explanation=str(item["explanation"]).rstrip(),
            source=item.get("source"),
        )
        for item in raw["questions"]
    )
    if not questions:
        msg = f"{path}: квиз без вопросов"
        raise QuizError(msg)

    return Quiz(skill_id=skill_id, path=path, questions=questions)


@dataclass(frozen=True, slots=True)
class Answer:
    question: Question
    chosen: frozenset[int]

    @property
    def correct(self) -> bool:
        # Частично верный ответ засчитывается как неверный: на собеседовании
        # «почти назвал причину» тоже не считается.
        return self.chosen == self.question.correct_indices


@dataclass(frozen=True, slots=True)
class Result:
    answers: tuple[Answer, ...]

    @property
    def total(self) -> int:
        return len(self.answers)

    @property
    def right(self) -> int:
        return sum(1 for a in self.answers if a.correct)

    @property
    def score(self) -> float:
        """Доля верных ответов в [0, 1] — то, что уйдёт в attempts.score."""
        if not self.answers:
            return 0.0
        return round(self.right / self.total, 2)

    @property
    def wrong(self) -> tuple[Answer, ...]:
        return tuple(a for a in self.answers if not a.correct)


def grade(answers: list[Answer]) -> Result:
    return Result(answers=tuple(answers))


def find_quiz(content_root: Path, skill: Any) -> Quiz:
    """Достаёт квиз узла по его спецификации заданий."""
    for task in skill.tasks:
        if task.type == "quiz":
            return parse_quiz(skill.id, content_root / task.spec["file"])
    msg = f"у узла {skill.id} нет задания типа quiz"
    raise QuizError(msg)
