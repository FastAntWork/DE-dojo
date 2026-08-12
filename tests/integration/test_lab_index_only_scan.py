"""Лаба index-only-scan на живом PostgreSQL.

Проверяется решаемость и честность чекера. Ключевой предмет — вторая половина
Index Only Scan, о которой забывают: он пропускает таблицу только там, где
карта видимости говорит, что страница видна всем, а заполняет её VACUUM.

Поэтому отдельный тест фиксирует, что покрывающий индекс БЕЗ VACUUM лабу не
сдаёт: план при этом уже показывает Index Only Scan, и без проверки Heap
Fetches задача считалась бы решённой.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from dojo.runner.lab import Lab, load_lab, reset_stand, run_check

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"

VARIANTS = [1, 2, 3]

COVERING_INDEX = "CREATE INDEX orders_report_idx ON orders (tenant_id, created_at) INCLUDE (status)"
DROP_OLD = "DROP INDEX IF EXISTS orders_tenant_created_idx"


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "index-only-scan")


async def run_sql(dsn: str, *statements: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def solve(dsn: str, variant: int) -> None:
    """Исправление из разбора для конкретного варианта."""
    if variant == 1:
        await run_sql(dsn, COVERING_INDEX, "VACUUM ANALYZE orders")
    elif variant == 2:
        # Индекс уже правильный: не хватает только карты видимости.
        await run_sql(dsn, "VACUUM ANALYZE orders")
    else:
        await run_sql(dsn, DROP_OLD, COVERING_INDEX, "VACUUM ANALYZE orders")


class TestStandPreparation:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_stand_builds(self, postgres_dsn: str, lab: Lab, variant: int) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        conn = await asyncpg.connect(lab_dsn)
        try:
            rows = await conn.fetchval("SELECT count(*) FROM orders")
            target = await conn.fetchval(
                "SELECT count(*) FROM orders WHERE tenant_id = 42 "
                "AND created_at >= TIMESTAMPTZ '2026-02-01' "
                "AND created_at < TIMESTAMPTZ '2026-03-01'"
            )
        finally:
            await conn.close()

        assert rows == 300_000
        assert target > 100, f"вариант {variant}: в окне отчёта всего {target} строк"


class TestBrokenStandFails:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_untouched_stand_does_not_pass(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed is False, f"вариант {variant} сдаётся без единого действия"
        failed = {c.name for c in result.checks if not c.ok}
        assert "data_intact" not in failed
        assert "report_correct" not in failed, "запрос обязан возвращать верный результат и так"

    async def test_variant_2_has_the_right_index_and_still_no_plan(
        self, postgres_dsn: str, lab: Lab
    ) -> None:
        """Главная ловушка варианта 2: индекс правильный, а плана нет.

        Проверено замером: при пустой карте видимости планировщик оценивает
        Index Only Scan как более дорогой и не выбирает его вовсе — в плане
        Bitmap Heap Scan. Интуитивное «план будет index-only, но с большим
        Heap Fetches» здесь неверно, и тест фиксирует именно наблюдаемое.
        """
        lab_dsn = await reset_stand(postgres_dsn, lab, 2)

        conn = await asyncpg.connect(lab_dsn)
        try:
            covering = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'orders' "
                "AND indexdef LIKE '%INCLUDE%'"
            )
        finally:
            await conn.close()
        assert covering == 1, "в варианте 2 покрывающий индекс обязан быть с самого начала"

        result = run_check(lab, lab_dsn, 2)

        by_name = {c.name: c.ok for c in result.checks}
        assert by_name["index_only_scan"] is False, "без VACUUM план не должен быть index-only"


class TestDocumentedFixWorks:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_fix_from_solution_passes(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)
        await solve(lab_dsn, variant)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]
        assert result.score == 1.0


class TestCheckerIsHonest:
    async def test_more_indexes_without_vacuum_does_not_pass(
        self, postgres_dsn: str, lab: Lab
    ) -> None:
        """В варианте 2 добавление индексов не помогает — не хватает VACUUM.

        Это самый вероятный неверный ход: раз плана нет, значит дело в индексе.
        Лаба обязана оставаться несданной, пока карта видимости пуста.
        """
        lab_dsn = await reset_stand(postgres_dsn, lab, 2)
        await run_sql(lab_dsn, "CREATE INDEX ix ON orders (tenant_id, created_at, status)")

        result = run_check(lab, lab_dsn, 2)

        assert result.passed is False, "лишний индекс без VACUUM сдал лабу"
        failed = {c.name for c in result.checks if not c.ok}
        assert "index_only_scan" in failed

    async def test_index_without_include_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Индекс по колонкам фильтра не делает план index-only."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(
            lab_dsn,
            "CREATE INDEX ix ON orders (tenant_id, created_at)",
            "VACUUM ANALYZE orders",
        )

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "index_only_scan" in failed

    async def test_piling_up_indexes_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Насыпать индексов — тоже способ получить нужный план, но не решение."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(
            lab_dsn,
            COVERING_INDEX,
            "CREATE INDEX ix1 ON orders (status)",
            "CREATE INDEX ix2 ON orders (amount)",
            "CREATE INDEX ix3 ON orders (created_at)",
            "CREATE INDEX ix4 ON orders (tenant_id)",
            "VACUUM ANALYZE orders",
        )

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"index_count_reasonable"}

    async def test_deleting_data_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await solve(lab_dsn, 1)
        await run_sql(lab_dsn, "DELETE FROM orders WHERE tenant_id <> 42")

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "data_intact" in failed

    async def test_stand_reset_undoes_fix(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await solve(lab_dsn, 1)
        assert run_check(lab, lab_dsn, 1).passed is True

        await reset_stand(postgres_dsn, lab, 1)

        assert run_check(lab, lab_dsn, 1).passed is False
