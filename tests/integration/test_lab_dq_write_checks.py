"""Лаба dq-write-checks на живом PostgreSQL.

Эта лаба устроена наоборот остальных: сломанного стенда нет, человек пишет
проверки, а чекер портит данные и смотрит, сработают ли они. Отсюда и предмет
теста — не «решаема ли задача», а **честен ли чекер в обе стороны**:

* набор из разбора проходит целиком;
* проверка вида `SELECT 1`, срабатывающая всегда, НЕ засчитывается, хотя
  формально «ловит» все шесть дефектов;
* пустой реестр не проходит;
* чекер не оставляет следов: он портит данные в откатываемых транзакциях, и
  после проверки стенд обязан быть прежним.

Последнее особенно важно: чекер здесь единственный, кто пишет в таблицы, и
незакрытая транзакция испортила бы стенд человеку молча.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from dojo.runner.lab import Lab, load_lab, reset_stand, run_check

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"

# Полный набор из разбора.
SOLUTION_CHECKS: dict[str, str] = {
    "orders_id_unique": "SELECT order_id FROM orders GROUP BY order_id HAVING count(*) > 1",
    "orders_customer_not_null": "SELECT order_id FROM orders WHERE customer_id IS NULL",
    "orders_amount_positive": "SELECT order_id FROM orders WHERE amount <= 0",
    "orders_status_known": (
        "SELECT order_id FROM orders WHERE status NOT IN ('new', 'paid', 'shipped', 'cancelled')"
    ),
    "orders_customer_exists": (
        "SELECT o.order_id FROM orders o "
        "LEFT JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE o.customer_id IS NOT NULL AND c.customer_id IS NULL"
    ),
    "orders_fresh": (
        "SELECT max(created_at) FROM orders HAVING max(created_at) < now() - interval '2 days'"
    ),
}

ALWAYS_FIRES = "SELECT 1"
NEVER_FIRES = "SELECT order_id FROM orders WHERE false"


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "dq-write-checks")


async def add_checks(dsn: str, checks: dict[str, str]) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for name, query in checks.items():
            await conn.execute(
                "INSERT INTO dq_checks (name, query) VALUES ($1, $2) "
                "ON CONFLICT (name) DO UPDATE SET query = excluded.query",
                name,
                query,
            )
    finally:
        await conn.close()


async def counts(dsn: str) -> tuple[int, int]:
    conn = await asyncpg.connect(dsn)
    try:
        orders = int(await conn.fetchval("SELECT count(*) FROM orders"))
        customers = int(await conn.fetchval("SELECT count(*) FROM customers"))
    finally:
        await conn.close()
    return orders, customers


class TestStandPreparation:
    async def test_stand_is_clean(self, postgres_dsn: str, lab: Lab) -> None:
        """Стенд начинается с чистых данных: ломать будет чекер, а не seed."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        conn = await asyncpg.connect(lab_dsn)
        try:
            orders = await conn.fetchval("SELECT count(*) FROM orders")
            dupes = await conn.fetchval(
                "SELECT count(*) FROM (SELECT order_id FROM orders "
                "GROUP BY order_id HAVING count(*) > 1) d"
            )
            nulls = await conn.fetchval("SELECT count(*) FROM orders WHERE customer_id IS NULL")
            registry = await conn.fetchval("SELECT count(*) FROM dq_checks")
        finally:
            await conn.close()

        assert orders == 50_000
        assert dupes == 0, "в исходных данных не должно быть дубликатов"
        assert nulls == 0, "в исходных данных не должно быть пропусков"
        assert registry == 1, "в реестре должен лежать ровно один пример"

    async def test_example_check_is_valid(self, postgres_dsn: str, lab: Lab) -> None:
        """Пример из реестра обязан работать и молчать — он задаёт формат."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        conn = await asyncpg.connect(lab_dsn)
        try:
            query = await conn.fetchval("SELECT query FROM dq_checks LIMIT 1")
            rows = await conn.fetch(query)
        finally:
            await conn.close()

        assert rows == []


class TestEmptyRegistryFails:
    async def test_untouched_stand_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False, "пустой реестр сдал лабу"
        failed = {c.name for c in result.checks if not c.ok}
        assert "enough_checks" in failed


class TestSolutionPasses:
    async def test_full_set_from_solution_passes(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, SOLUTION_CHECKS)

        result = run_check(lab, lab_dsn, 1)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]
        assert result.score == 1.0

    async def test_each_defect_is_reported_separately(self, postgres_dsn: str, lab: Lab) -> None:
        """Убрав одну проверку, обязаны потерять ровно один дефект.

        Это делает обратную связь полезной: человек видит, ЧТО именно он не
        поймал, а не «лаба не сдана».
        """
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        without_freshness = {
            name: query for name, query in SOLUTION_CHECKS.items() if name != "orders_fresh"
        }
        await add_checks(lab_dsn, without_freshness)

        result = run_check(lab, lab_dsn, 1)

        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"catches_stale_data"}, f"неожиданный набор провалов: {failed}"


class TestCheckerIsHonest:
    async def test_always_firing_check_is_rejected(self, postgres_dsn: str, lab: Lab) -> None:
        """`SELECT 1` «ловит» все шесть дефектов и не значит ничего."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, {f"cheat_{i}": ALWAYS_FIRES for i in range(6)})

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False, "проверка, срабатывающая всегда, сдала лабу"
        failed = {c.name for c in result.checks if not c.ok}
        assert "no_false_positives" in failed

    async def test_never_firing_checks_are_rejected(self, postgres_dsn: str, lab: Lab) -> None:
        """Шесть проверок, которые молчат всегда, тоже не решение."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, {f"silent_{i}": NEVER_FIRES for i in range(6)})

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "no_false_positives" not in failed, "молчащие проверки ложных тревог не дают"
        assert len(failed) == len([c for c in result.checks if c.name.startswith("catches_")]), (
            "не пойман ни один дефект"
        )

    async def test_broken_sql_is_reported(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, {**SOLUTION_CHECKS, "broken": "SELECT * FROM no_such_table"})

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "queries_valid" in failed

    async def test_removing_example_is_reported(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, SOLUTION_CHECKS)
        conn = await asyncpg.connect(lab_dsn)
        try:
            await conn.execute("DELETE FROM dq_checks WHERE name = 'example_status_not_null'")
        finally:
            await conn.close()

        result = run_check(lab, lab_dsn, 1)

        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"example_kept"}


class TestCheckerLeavesNoTrace:
    async def test_data_unchanged_after_check(self, postgres_dsn: str, lab: Lab) -> None:
        """Чекер портит данные в откатываемых транзакциях — следов быть не должно."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, SOLUTION_CHECKS)
        before = await counts(lab_dsn)

        run_check(lab, lab_dsn, 1)

        assert await counts(lab_dsn) == before

    async def test_repeated_check_gives_same_verdict(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await add_checks(lab_dsn, SOLUTION_CHECKS)

        first = run_check(lab, lab_dsn, 1)
        second = run_check(lab, lab_dsn, 1)

        assert first.passed and second.passed, "повторная проверка изменила приговор"
