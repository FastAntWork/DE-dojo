"""Тесты сравнения результатов SQL.

Это судья, который ставит зачёт по заданию. Ошибка здесь означает, что
пользователю засчитывают неверное решение или отвергают верное — хуже
последствий у бага в этом проекте нет.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dojo.runner.sql_check import (
    SqlTaskError,
    compare,
    normalize_value,
    parse_task_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_TASKS = REPO_ROOT / "content" / "sql"


def verdict(
    expected: list[tuple[Any, ...]],
    actual: list[tuple[Any, ...]],
    *,
    columns: tuple[str, ...] = ("id", "name"),
    actual_columns: tuple[str, ...] | None = None,
    ordered: bool = False,
) -> Any:
    return compare(
        columns,
        tuple(expected),
        actual_columns or columns,
        tuple(actual),
        ordered=ordered,
    )


class TestNormalisation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (3500, Decimal("3500.00")),
            (Decimal("0"), 0),
            (1.5, Decimal("1.50")),
            (Decimal("4200.000"), 4200),
        ],
    )
    def test_numbers_of_different_types_are_equal(self, left: Any, right: Any) -> None:
        # count() отдаёт int, sum() — Decimal. Пользователь не должен угадывать,
        # каким типом мы ждём одно и то же число.
        assert normalize_value(left) == normalize_value(right)

    def test_null_stays_distinct_from_zero(self) -> None:
        assert normalize_value(None) != normalize_value(0)

    def test_text_untouched(self) -> None:
        assert normalize_value("Иванов") == "Иванов"


class TestComparison:
    def test_identical_sets_pass(self) -> None:
        rows = [(1, "Иванов"), (2, "Петров")]

        assert verdict(rows, rows).passed is True

    def test_order_ignored_by_default(self) -> None:
        result = verdict([(1, "Иванов"), (2, "Петров")], [(2, "Петров"), (1, "Иванов")])

        assert result.passed is True

    def test_order_enforced_when_requested(self) -> None:
        result = verdict(
            [(1, "Иванов"), (2, "Петров")],
            [(2, "Петров"), (1, "Иванов")],
            ordered=True,
        )

        assert result.passed is False

    def test_duplicates_are_significant(self) -> None:
        # Размножение строк лишним соединением — самая частая ошибка в JOIN,
        # и засчитывать её как верный ответ нельзя.
        result = verdict([(1, "Иванов")], [(1, "Иванов"), (1, "Иванов")])

        assert result.passed is False
        assert len(result.unexpected_rows) == 1

    def test_missing_row_reported(self) -> None:
        result = verdict([(1, "Иванов"), (2, "Петров")], [(1, "Иванов")])

        assert result.passed is False
        assert result.missing_rows == ((2, "Петров"),)

    def test_column_names_must_match(self) -> None:
        result = verdict(
            [(1, "Иванов")],
            [(1, "Иванов")],
            columns=("id", "revenue"),
            actual_columns=("id", "coalesce"),
        )

        assert result.passed is False
        assert "колонки" in result.message.lower()

    def test_column_names_compared_case_insensitively(self) -> None:
        result = verdict(
            [(1, "Иванов")], [(1, "Иванов")], columns=("ID", "Name"), actual_columns=("id", "name")
        )

        assert result.passed is True

    def test_empty_expected_and_actual_pass(self) -> None:
        assert verdict([], [], columns=()).passed is True

    def test_extra_rows_when_nothing_expected(self) -> None:
        assert verdict([], [(1, "Иванов")]).passed is False


class TestTaskFiles:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SqlTaskError, match="не читается"):
            parse_task_file("s", tmp_path / "нет.yaml")

    def test_file_without_dataset_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "t.yaml"
        path.write_text("tasks: []\n", encoding="utf-8")

        with pytest.raises(SqlTaskError, match="dataset"):
            parse_task_file("s", path)

    @pytest.mark.parametrize("path", sorted(REAL_TASKS.glob("*.yaml")), ids=lambda p: p.stem)
    def test_real_task_files_parse(self, path: Path) -> None:
        parsed = parse_task_file(path.stem, path)

        assert parsed.tasks
        for task in parsed.tasks:
            assert task.solution, task.id
            assert task.prompt, task.id
            # Точка с запятой в эталоне сломала бы расширенный протокол.
            assert not task.solution.rstrip().endswith(";"), task.id

    def test_task_ids_unique_within_file(self) -> None:
        for path in REAL_TASKS.glob("*.yaml"):
            parsed = parse_task_file(path.stem, path)
            ids = [task.id for task in parsed.tasks]
            assert len(ids) == len(set(ids)), path.name
