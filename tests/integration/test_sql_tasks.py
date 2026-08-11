"""Проверка SQL-заданий на живой базе.

Здесь проверяются три разные вещи, и все три обязательны:

1. Датасет грузится, а эталонные решения действительно выполняются и дают
   тот результат, который мы объявляем правильным.
2. Неверный ответ отвергается — судья не пропускает мусор.
3. Чужой SQL не может ничего испортить. Это единственное место, где
   выполняется код, написанный человеком, и цена ошибки здесь наибольшая.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from dojo.runner.datasets import ensure_dataset, load_dataset
from dojo.runner.sql_check import (
    SqlTask,
    SqlTaskFile,
    check,
    count_seq_scans,
    parse_task_file,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"

TASK_FILE = parse_task_file("sql.joins", CONTENT / "sql" / "sql.joins.yaml")

# Все файлы задач проверяются автоматически: новый файл не должен требовать
# правки тестов, иначе его однажды добавят непроверенным.
ALL_TASK_FILES = [parse_task_file(p.stem, p) for p in sorted((CONTENT / "sql").glob("*.yaml"))]
ALL_TASKS = [(f, task) for f in ALL_TASK_FILES for task in f.tasks]


@pytest.fixture
async def shop(postgres_dsn: str) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    async for conn in _dataset_conn(postgres_dsn, "shop"):
        yield conn


async def _dataset_conn(
    postgres_dsn: str, name: str
) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
    dsn = await ensure_dataset(postgres_dsn, load_dataset(CONTENT, name))
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


class TestDataset:
    async def test_loads_all_tables(self, shop: asyncpg.Connection[asyncpg.Record]) -> None:
        tables = {
            row["tablename"]
            for row in await shop.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }

        assert {"customers", "products", "orders", "order_items", "tickets"} <= tables

    async def test_contains_the_tricky_rows(self, shop: asyncpg.Connection[asyncpg.Record]) -> None:
        # Датасет обязан содержать ловушки, иначе задачи ничего не проверяют.
        without_orders = await shop.fetchval(
            "SELECT count(*) FROM customers c "
            "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"
        )
        null_city = await shop.fetchval("SELECT count(*) FROM customers WHERE city IS NULL")
        empty_orders = await shop.fetchval(
            "SELECT count(*) FROM orders o "
            "WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id)"
        )

        assert without_orders >= 1, "нужен клиент без заказов"
        assert null_city >= 1, "нужен клиент с NULL в городе"
        assert empty_orders >= 1, "нужен заказ без позиций"


class TestReferenceSolutions:
    """Эталон обязан проходить собственную проверку — во всех файлах задач."""

    @pytest.mark.parametrize(
        ("task_file", "task"), ALL_TASKS, ids=lambda x: x.id if isinstance(x, SqlTask) else ""
    )
    async def test_solution_passes(
        self, task_file: SqlTaskFile, task: SqlTask, postgres_dsn: str
    ) -> None:
        async for conn in _dataset_conn(postgres_dsn, task_file.dataset):
            result = await check(conn, task, task.solution)

        assert result.passed, f"{task.id}: {result.message} {result.error or ''}"

    @pytest.mark.parametrize(
        ("task_file", "task"), ALL_TASKS, ids=lambda x: x.id if isinstance(x, SqlTask) else ""
    )
    async def test_solution_returns_rows(
        self, task_file: SqlTaskFile, task: SqlTask, postgres_dsn: str
    ) -> None:
        # Задача, эталон которой возвращает пустоту, проверяет нечто странное:
        # её пройдёт любой запрос, ничего не находящий.
        async for conn in _dataset_conn(postgres_dsn, task_file.dataset):
            rows = await conn.fetch(task.solution)

        assert rows, f"{task.id}: эталон вернул пустой результат"

    @pytest.mark.parametrize(
        ("task_file", "task"),
        [(f, t) for f, t in ALL_TASKS if t.max_seq_scans is not None],
        ids=lambda x: x.id if isinstance(x, SqlTask) else "",
    )
    async def test_plan_limit_is_achievable(
        self, task_file: SqlTaskFile, task: SqlTask, postgres_dsn: str
    ) -> None:
        """Требование к плану должно быть выполнимым.

        Задача, где мы требуем индексный доступ, а планировщик на реальных
        данных всё равно выбирает Seq Scan, невыполнима в принципе — и
        обнаружить это должен тест, а не человек, потративший на неё час.
        """
        async for conn in _dataset_conn(postgres_dsn, task_file.dataset):
            seq_scans = await count_seq_scans(conn, task.solution)

        assert seq_scans <= (task.max_seq_scans or 0), (
            f"{task.id}: эталон даёт {seq_scans} последовательных сканов "
            f"при лимите {task.max_seq_scans} — требование невыполнимо"
        )


class TestRejectsWrongAnswers:
    async def test_inner_join_instead_of_left_is_rejected(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        # Классическая ошибка: фильтр статуса уехал в WHERE, клиенты без
        # оплаченных заказов пропали из результата.
        task = TASK_FILE.task("joins-paid-orders-per-customer")
        wrong = """
            SELECT c.id, c.name, count(o.id) AS paid_orders
            FROM customers c
            LEFT JOIN orders o ON o.customer_id = c.id
            WHERE o.status = 'paid'
            GROUP BY c.id, c.name
        """

        result = await check(shop, task, wrong)

        assert result.passed is False
        assert result.missing_rows

    async def test_row_multiplication_is_rejected(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        # Соединение двух веток «один-ко-многим» размножает строки.
        task = TASK_FILE.task("joins-orders-and-tickets")
        wrong = """
            SELECT c.id, c.name
            FROM customers c
            JOIN orders o ON o.customer_id = c.id
            JOIN tickets t ON t.customer_id = c.id
        """

        result = await check(shop, task, wrong)

        assert result.passed is False

    async def test_wrong_column_alias_is_rejected(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        task = TASK_FILE.task("joins-revenue-per-customer")
        wrong = task.solution.replace("AS revenue", "")

        result = await check(shop, task, wrong)

        assert result.passed is False
        assert "колонки" in result.message.lower()

    async def test_broken_sql_reports_database_error(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        task = TASK_FILE.tasks[0]

        result = await check(shop, task, "SELECT * FROM no_such_table")

        assert result.passed is False
        assert result.error
        assert "no_such_table" in result.error

    async def test_empty_answer_rejected(self, shop: asyncpg.Connection[asyncpg.Record]) -> None:
        result = await check(shop, TASK_FILE.tasks[0], "   ")

        assert result.passed is False


class TestSandboxSafety:
    """Самое важное: чужой SQL не должен ничего испортить."""

    async def test_write_is_rejected(self, shop: asyncpg.Connection[asyncpg.Record]) -> None:
        task = TASK_FILE.tasks[0]

        result = await check(shop, task, "DELETE FROM customers")

        assert result.passed is False
        assert result.error is not None
        assert "read-only" in result.error.lower() or "only" in result.error.lower()

    async def test_data_survives_write_attempt(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        before = await shop.fetchval("SELECT count(*) FROM customers")

        await check(shop, TASK_FILE.tasks[0], "DROP TABLE order_items")
        await check(shop, TASK_FILE.tasks[0], "TRUNCATE customers")
        await check(shop, TASK_FILE.tasks[0], "UPDATE customers SET name = 'x'")

        after = await shop.fetchval("SELECT count(*) FROM customers")
        assert after == before
        assert await shop.fetchval("SELECT count(*) FROM order_items") > 0

    async def test_second_statement_is_rejected(
        self, shop: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        # Расширенный протокол не допускает нескольких операторов в запросе —
        # инъекция через точку с запятой невозможна физически.
        result = await check(shop, TASK_FILE.tasks[0], "SELECT 1; DROP TABLE customers")

        assert result.passed is False
        assert await shop.fetchval("SELECT to_regclass('customers')") is not None
