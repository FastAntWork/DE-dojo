"""Лаба partition-pruning на живом PostgreSQL.

Проверяется решаемость и честность чекера. Особое внимание — требованию,
ради которого лаба существует: секция не должна содержать данных больше чем
одного месяца. Секционирование, при котором месяц нельзя отцепить, решает
только половину задачи, а выглядит полным решением.
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

MONTHS = (
    ("events_2026_01", "2026-01-01", "2026-02-01"),
    ("events_2026_02", "2026-02-01", "2026-03-01"),
    ("events_2026_03", "2026-03-01", "2026-04-01"),
)


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "partition-pruning")


async def run_sql(dsn: str, *statements: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def rebuild(partitions: tuple[tuple[str, str, str], ...] = MONTHS) -> list[str]:
    """Решение из разбора: новая секционированная таблица и перенос данных."""
    statements = [
        """
        CREATE TABLE events_new (
            event_id   bigint GENERATED ALWAYS AS IDENTITY,
            tenant_id  integer NOT NULL,
            kind       text NOT NULL,
            created_at timestamptz NOT NULL,
            payload    text NOT NULL
        ) PARTITION BY RANGE (created_at)
        """
    ]
    statements += [
        f"CREATE TABLE {name} PARTITION OF events_new FOR VALUES FROM ('{start}') TO ('{end}')"
        for name, start, end in partitions
    ]
    statements += [
        "INSERT INTO events_new (tenant_id, kind, created_at, payload) "
        "SELECT tenant_id, kind, created_at, payload FROM events",
        "DROP TABLE events",
        "ALTER TABLE events_new RENAME TO events",
        "CREATE INDEX ON events (created_at)",
        "ANALYZE events",
    ]
    return statements


class TestStandPreparation:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_stand_builds(self, postgres_dsn: str, lab: Lab, variant: int) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        conn = await asyncpg.connect(lab_dsn)
        try:
            rows = await conn.fetchval("SELECT count(*) FROM events")
            months = await conn.fetchval(
                "SELECT count(DISTINCT date_trunc('month', created_at)) FROM events"
            )
        finally:
            await conn.close()

        assert rows == 400_000
        assert months == 3, "нужны данные за три месяца, иначе отсекать нечего"


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
        assert "report_correct" not in failed

    async def test_variant_1_is_not_partitioned(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        failed = {c.name for c in run_check(lab, lab_dsn, 1).checks if not c.ok}

        assert "partitioned_by_time" in failed

    async def test_variant_2_partitioned_by_wrong_key(self, postgres_dsn: str, lab: Lab) -> None:
        """Секционирование есть, ключ не тот: отсечения по времени нет."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 2)

        failed = {c.name for c in run_check(lab, lab_dsn, 2).checks if not c.ok}

        assert "partitioned_by_time" in failed
        assert "month_is_droppable" in failed, "в хеш-секциях лежат все месяцы сразу"

    async def test_variant_3_has_right_key_but_one_partition(
        self, postgres_dsn: str, lab: Lab
    ) -> None:
        """Ключ верный, а секция одна — отсекать нечего."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 3)

        by_name = {c.name: c.ok for c in run_check(lab, lab_dsn, 3).checks}

        assert by_name["partitioned_by_time"] is True, "в варианте 3 ключ правильный"
        assert by_name["month_is_droppable"] is False, "всё лежит в одной секции DEFAULT"


class TestDocumentedFixWorks:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_rebuild_from_solution_passes(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)
        await run_sql(lab_dsn, *rebuild())

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]
        assert result.score == 1.0

    async def test_finer_granularity_also_passes(self, postgres_dsn: str, lab: Lab) -> None:
        """Гранулярность не диктуется: недельные секции тоже решают задачу."""
        weeks = tuple(
            (f"events_w{i}", f"2026-01-{1 + i * 7:02d}", f"2026-01-{8 + i * 7:02d}")
            for i in range(3)
        )
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(
            lab_dsn,
            *rebuild(
                (
                    *weeks,
                    ("events_rest_01", "2026-01-22", "2026-02-01"),
                    ("events_2026_02", "2026-02-01", "2026-03-01"),
                    ("events_2026_03", "2026-03-01", "2026-04-01"),
                )
            ),
        )

        result = run_check(lab, lab_dsn, 1)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]


class TestCheckerIsHonest:
    async def test_quarterly_partition_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Одна секция на весь квартал: отсечение работает, а месяц не отцепить."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(lab_dsn, *rebuild((("events_q1", "2026-01-01", "2026-04-01"),)))

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False, "квартальная секция принята за решение"
        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"month_is_droppable"}, f"ожидалась только неотцепляемость: {failed}"

    async def test_losing_rows_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Секции только за нужный месяц — остальные данные потеряны."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(lab_dsn, *rebuild())
        await run_sql(lab_dsn, "DROP TABLE events_2026_01")

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "data_intact" in failed

    async def test_stand_reset_undoes_fix(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await run_sql(lab_dsn, *rebuild())
        assert run_check(lab, lab_dsn, 1).passed is True

        await reset_stand(postgres_dsn, lab, 1)

        assert run_check(lab, lab_dsn, 1).passed is False
