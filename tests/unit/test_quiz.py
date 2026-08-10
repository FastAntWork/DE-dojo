"""Тесты проверки квизов.

Зачёт по квизу ставит этот код, а не человек и не модель, поэтому его правила
должны быть закреплены явно — принцип №1 из ТЗ.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from dojo.content.quiz import Answer, Option, Question, Quiz, QuizError, grade, parse_quiz

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_QUIZZES = REPO_ROOT / "content" / "quizzes"


def question(*correct: int, kind: str = "single", count: int = 4) -> Question:
    return Question(
        id="q1",
        kind=kind,
        prompt="Вопрос",
        options=tuple(Option(text=f"вариант {i}", correct=i in correct) for i in range(count)),
        explanation="Разбор",
    )


class TestGrading:
    def test_exact_match_is_correct(self) -> None:
        answer = Answer(question=question(2), chosen=frozenset({2}))

        assert answer.correct is True

    def test_wrong_choice_is_incorrect(self) -> None:
        answer = Answer(question=question(2), chosen=frozenset({0}))

        assert answer.correct is False

    def test_partial_answer_is_not_credited(self) -> None:
        # Частично верный ответ = неверный: «почти назвал причину» на
        # собеседовании тоже не считается.
        answer = Answer(question=question(1, 2, kind="multiple"), chosen=frozenset({1}))

        assert answer.correct is False

    def test_extra_choice_is_not_credited(self) -> None:
        answer = Answer(question=question(1, kind="multiple"), chosen=frozenset({1, 3}))

        assert answer.correct is False

    def test_empty_answer_is_incorrect(self) -> None:
        assert Answer(question=question(1), chosen=frozenset()).correct is False


class TestResult:
    def test_score_is_share_of_correct(self) -> None:
        answers = [
            Answer(question=question(0), chosen=frozenset({0})),
            Answer(question=question(0), chosen=frozenset({0})),
            Answer(question=question(0), chosen=frozenset({1})),
            Answer(question=question(0), chosen=frozenset({1})),
        ]

        result = grade(answers)

        assert result.right == 2
        assert result.total == 4
        assert result.score == 0.5

    def test_perfect_run_scores_one(self) -> None:
        answers = [Answer(question=question(0), chosen=frozenset({0})) for _ in range(3)]

        assert grade(answers).score == 1.0

    def test_wrong_answers_are_listed_for_review(self) -> None:
        good = Answer(question=question(0), chosen=frozenset({0}))
        bad = Answer(question=question(0), chosen=frozenset({2}))

        assert grade([good, bad]).wrong == (bad,)


class TestShuffle:
    def test_shuffle_keeps_correct_option_correct(self) -> None:
        # Перемешивание обязано двигать варианты вместе с их правильностью:
        # ошибка здесь означала бы, что квиз врёт при каждом прохождении.
        original = question(2)
        correct_text = original.options[2].text

        shuffled = original.shuffled(random.Random(42))

        assert len(shuffled.correct_indices) == 1
        index = next(iter(shuffled.correct_indices))
        assert shuffled.options[index].text == correct_text

    def test_shuffle_preserves_all_options(self) -> None:
        original = question(1, count=5)

        shuffled = original.shuffled(random.Random(7))

        assert sorted(o.text for o in shuffled.options) == sorted(o.text for o in original.options)

    def test_question_order_changes(self) -> None:
        quiz = Quiz(
            skill_id="s",
            path=Path("q.yaml"),
            questions=tuple(
                Question(
                    id=f"q{i}",
                    kind="single",
                    prompt="p",
                    options=(Option("a", True), Option("b", False)),
                    explanation="e",
                )
                for i in range(8)
            ),
        )

        order = [q.id for q in quiz.shuffled(random.Random(1)).questions]

        assert order != [q.id for q in quiz.questions]


class TestParsing:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(QuizError, match="не удалось прочитать"):
            parse_quiz("s", tmp_path / "нет-такого.yaml")

    def test_file_without_questions_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "q.yaml"
        path.write_text("что-то: другое\n", encoding="utf-8")

        with pytest.raises(QuizError, match="нет ключа questions"):
            parse_quiz("s", path)

    @pytest.mark.parametrize("path", sorted(REAL_QUIZZES.glob("*.yaml")), ids=lambda p: p.stem)
    def test_real_quizzes_parse_and_have_one_answer(self, path: Path) -> None:
        quiz = parse_quiz(path.stem, path)

        assert quiz.questions
        for item in quiz.questions:
            if item.kind in {"single", "find-error"}:
                assert len(item.correct_indices) == 1, item.id
            assert item.explanation
