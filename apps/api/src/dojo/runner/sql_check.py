"""Проверка SQL-заданий.

Зачёт ставит сравнение результирующих наборов, а не человек и не модель —
принцип №1. Ожидаемый результат не хранится в файле руками, а вычисляется
запуском эталонного решения на том же датасете: иначе правка датасета
потребовала бы переписать все ожидаемые строки, и они бы разъехались.

Безопасность запуска чужого SQL держится на трёх независимых рубежах:

1. Отдельная база данных. Даже успешная порча ничего важного не заденет.
2. Транзакция READ ONLY — запись отвергается сервером независимо от прав.
3. Расширенный протокол (fetch, а не execute) — в одном запросе физически
   не может оказаться второго оператора через точку с запятой.

Плюс statement_timeout, чтобы бесконечный запрос не занимал соединение.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    import asyncpg

# Пять секунд — с запасом для учебного датасета в десятки строк. Всё, что
# дольше, это либо декартово произведение, либо бесконечная рекурсия.
STATEMENT_TIMEOUT_MS = 5000

# Показывать в отчёте не больше нескольких строк расхождения: вывалить сотню
# лишних строк значит спрятать в них суть.
MAX_DIFF_ROWS = 5


class SqlTaskError(RuntimeError):
    """Файл заданий не разобран."""


@dataclass(frozen=True, slots=True)
class SqlTask:
    id: str
    title: str
    prompt: str
    solution: str
    ordered: bool = False
    hint: str | None = None
    max_seq_scans: int | None = None


@dataclass(frozen=True, slots=True)
class SqlTaskFile:
    skill_id: str
    dataset: str
    path: Path
    tasks: tuple[SqlTask, ...]

    def task(self, task_id: str) -> SqlTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        msg = f"задача {task_id!r} не найдена в {self.path.name}"
        raise SqlTaskError(msg)


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    message: str
    expected_columns: tuple[str, ...] = ()
    actual_columns: tuple[str, ...] = ()
    missing_rows: tuple[tuple[Any, ...], ...] = ()
    unexpected_rows: tuple[tuple[Any, ...], ...] = ()
    actual_rows: tuple[tuple[Any, ...], ...] = ()
    seq_scans: int | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def parse_task_file(skill_id: str, path: Path) -> SqlTaskFile:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        msg = f"{path}: не читается: {exc}"
        raise SqlTaskError(msg) from exc

    if not isinstance(raw, dict) or "tasks" not in raw or "dataset" not in raw:
        msg = f"{path}: нужны ключи dataset и tasks"
        raise SqlTaskError(msg)

    tasks = tuple(
        SqlTask(
            id=str(item["id"]),
            title=str(item["title"]),
            prompt=str(item["prompt"]).rstrip(),
            solution=str(item["solution"]).strip().rstrip(";"),
            ordered=bool(item.get("ordered", False)),
            hint=item.get("hint"),
            max_seq_scans=item.get("max_seq_scans"),
        )
        for item in raw["tasks"]
    )
    return SqlTaskFile(skill_id=skill_id, dataset=str(raw["dataset"]), path=path, tasks=tasks)


# ── Сравнение результатов ────────────────────────────────────────────────────


def normalize_value(value: Any) -> Any:
    """Приводит значение к виду, устойчивому к разнице типов.

    Постоянный источник ложных расхождений: 3500 из count() и Decimal('3500.00')
    из sum() — это одно и то же число, записанное по-разному. Пользователь не
    должен угадывать, каким типом мы его ждём.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float | Decimal):
        try:
            return Decimal(str(value)).normalize()
        except InvalidOperation:
            return value
    return value


def normalize_rows(rows: list[Any]) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(normalize_value(v) for v in row) for row in rows)


def compare(
    expected_columns: tuple[str, ...],
    expected_rows: tuple[tuple[Any, ...], ...],
    actual_columns: tuple[str, ...],
    actual_rows: tuple[tuple[Any, ...], ...],
    *,
    ordered: bool,
) -> Verdict:
    """Сравнивает наборы. Чистая функция — тестируется без базы."""
    if [c.lower() for c in expected_columns] != [c.lower() for c in actual_columns]:
        return Verdict(
            passed=False,
            message=(
                f"Не те колонки. Ожидались: {', '.join(expected_columns)}. "
                f"Получены: {', '.join(actual_columns) or '—'}. "
                "Проверь список SELECT и псевдонимы."
            ),
            expected_columns=expected_columns,
            actual_columns=actual_columns,
            actual_rows=actual_rows[:MAX_DIFF_ROWS],
        )

    if ordered:
        if list(expected_rows) == list(actual_rows):
            return Verdict(passed=True, message="Верно.", actual_columns=actual_columns)
        return Verdict(
            passed=False,
            message=(
                f"Порядок или состав строк не совпал. Ожидалось строк: "
                f"{len(expected_rows)}, получено: {len(actual_rows)}."
            ),
            expected_columns=expected_columns,
            actual_columns=actual_columns,
            actual_rows=actual_rows[:MAX_DIFF_ROWS],
        )

    # Мультимножество, а не множество: дубликаты — часть результата, и
    # размножение строк лишним соединением обязано считаться ошибкой.
    from collections import Counter

    expected_count = Counter(expected_rows)
    actual_count = Counter(actual_rows)
    if expected_count == actual_count:
        return Verdict(passed=True, message="Верно.", actual_columns=actual_columns)

    missing = tuple((expected_count - actual_count).elements())
    unexpected = tuple((actual_count - expected_count).elements())

    parts = []
    if missing:
        parts.append(f"не хватает строк: {len(missing)}")
    if unexpected:
        parts.append(f"лишних строк: {len(unexpected)}")

    return Verdict(
        passed=False,
        message="Результат не совпал — " + ", ".join(parts) + ".",
        expected_columns=expected_columns,
        actual_columns=actual_columns,
        missing_rows=missing[:MAX_DIFF_ROWS],
        unexpected_rows=unexpected[:MAX_DIFF_ROWS],
        actual_rows=actual_rows[:MAX_DIFF_ROWS],
    )


# ── Выполнение ───────────────────────────────────────────────────────────────

SEQ_SCAN_RE = re.compile(r"Seq Scan", re.IGNORECASE)


async def run_query(
    conn: asyncpg.Connection[asyncpg.Record], query: str
) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    """Выполняет запрос в транзакции только для чтения.

    fetch, а не execute: расширенный протокол не допускает нескольких
    операторов в одном запросе, поэтому «; DROP TABLE» физически не пройдёт.
    """
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        records = await conn.fetch(query)

    columns = tuple(records[0].keys()) if records else ()
    return columns, normalize_rows(records)


async def count_seq_scans(conn: asyncpg.Connection[asyncpg.Record], query: str) -> int:
    """Сколько последовательных сканов в плане.

    Нужно там, где проверяется не только результат, но и способ его получения:
    правильный ответ, полученный полным перебором, на проде правильным не
    является.
    """
    async with conn.transaction(readonly=True):
        rows = await conn.fetch(f"EXPLAIN (FORMAT TEXT) {query}")
    return sum(1 for row in rows if SEQ_SCAN_RE.search(str(row[0])))


async def check(conn: asyncpg.Connection[asyncpg.Record], task: SqlTask, answer: str) -> Verdict:
    """Полная проверка одного ответа."""
    cleaned = answer.strip().rstrip(";")
    if not cleaned:
        return Verdict(passed=False, message="Пустой запрос.")

    try:
        expected_columns, expected_rows = await run_query(conn, task.solution)
    except Exception as exc:
        return Verdict(
            passed=False,
            message="Эталонное решение не выполняется — это ошибка в задании.",
            error=str(exc),
        )

    try:
        actual_columns, actual_rows = await run_query(conn, cleaned)
    except Exception as exc:
        return Verdict(
            passed=False,
            message="Запрос не выполнился.",
            error=_clean_error(str(exc)),
        )

    verdict = compare(
        expected_columns, expected_rows, actual_columns, actual_rows, ordered=task.ordered
    )

    if not verdict.passed or task.max_seq_scans is None:
        return verdict

    seq_scans = await count_seq_scans(conn, cleaned)
    if seq_scans > task.max_seq_scans:
        return Verdict(
            passed=False,
            message=(
                f"Результат верный, но план плохой: последовательных сканов "
                f"{seq_scans}, допустимо {task.max_seq_scans}. "
                "Посмотри EXPLAIN и подумай, что мешает использовать индекс."
            ),
            actual_columns=actual_columns,
            seq_scans=seq_scans,
        )

    return Verdict(
        passed=True, message="Верно.", actual_columns=actual_columns, seq_scans=seq_scans
    )


def _clean_error(text: str) -> str:
    """Убирает из ошибки СУБД служебный шум, оставляя суть."""
    first = text.strip().splitlines()[0] if text.strip() else text
    return first[:400]
