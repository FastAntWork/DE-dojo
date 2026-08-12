"""Лаба scd2-broken на живом PostgreSQL.

Проверяется решаемость и честность чекера. Особое внимание — двум ловушкам,
на которых лаба и построена:

* закрытие интервала «датой следующей версии минус один день» выглядит
  правильным и даёт разрыв в один день; чекер обязан его увидеть;
* удаление лишних версий вместо пересчёта интервалов «чинит» пересечения и
  теряет историю — то, ради чего SCD и заводят.
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

# Решение из разбора: valid_to равен valid_from следующей версии.
FIX = """
UPDATE dim_customer d
SET valid_to   = s.next_from,
    is_current = s.next_from IS NULL
FROM (
    SELECT version_id,
           lead(valid_from) OVER (PARTITION BY customer_id ORDER BY valid_from) AS next_from
    FROM dim_customer
) s
WHERE d.version_id = s.version_id
"""

# Та же логика, но с закрытым интервалом: минус день. Выглядит аккуратно и
# оставляет однодневные дыры, в которые проваливаются события.
FIX_OFF_BY_ONE = """
UPDATE dim_customer d
SET valid_to   = s.next_from - 1,
    is_current = s.next_from IS NULL
FROM (
    SELECT version_id,
           lead(valid_from) OVER (PARTITION BY customer_id ORDER BY valid_from) AS next_from
    FROM dim_customer
) s
WHERE d.version_id = s.version_id
"""

# «Починка» удалением: пересечений нет, потому что версий не осталось.
WIPE_HISTORY = """
DELETE FROM dim_customer d
WHERE EXISTS (
    SELECT 1 FROM dim_customer o
    WHERE o.customer_id = d.customer_id AND o.valid_from > d.valid_from
);
UPDATE dim_customer SET valid_to = NULL, is_current = true
"""


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "scd2-broken")


async def apply(dsn: str, statement: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


class TestStandPreparation:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_stand_builds_with_history(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        conn = await asyncpg.connect(lab_dsn)
        try:
            versions = await conn.fetchval("SELECT count(*) FROM dim_customer")
            customers = await conn.fetchval("SELECT count(DISTINCT customer_id) FROM dim_customer")
            multi = await conn.fetchval(
                "SELECT count(*) FROM (SELECT customer_id FROM dim_customer "
                "GROUP BY customer_id HAVING count(*) > 1) x"
            )
            snapshot = await conn.fetchval("SELECT count(*) FROM _seed_versions")
        finally:
            await conn.close()

        assert customers == 2000
        assert versions == snapshot, "снимок обязан совпадать с таблицей на старте"
        assert multi > 500, "у большинства клиентов должно быть несколько версий"

    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_stand_is_actually_broken(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        """Каждый вариант обязан ломаться хотя бы одним из проверяемых свойств."""
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed is False, f"вариант {variant} сдаётся без единого действия"
        failed = {c.name for c in result.checks if not c.ok}
        assert "history_preserved" not in failed, "историю на старте никто не трогал"


class TestDocumentedFixWorks:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_fix_from_solution_passes(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)
        await apply(lab_dsn, FIX)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]
        assert result.score == 1.0


class TestCheckerIsHonest:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_off_by_one_is_caught(self, postgres_dsn: str, lab: Lab, variant: int) -> None:
        """Закрытие «минус день» выглядит правильным и оставляет разрывы."""
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)
        await apply(lab_dsn, FIX_OFF_BY_ONE)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed is False, "интервал, закрытый на день раньше, принят за решение"
        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"no_gaps"}, f"ожидался ровно разрыв, получено: {failed}"

    async def test_deleting_history_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Оставить по одной версии — не починка, а потеря истории."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, WIPE_HISTORY)

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "history_preserved" in failed

    async def test_changing_valid_from_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """valid_from — источник истины, подгонять его под интервалы нельзя."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)
        await apply(lab_dsn, "UPDATE dim_customer SET valid_from = valid_from + 1")

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "history_preserved" in failed

    async def test_flag_without_intervals_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Поправить только флаг мало: интервалы остаются сломанными."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 2)
        await apply(
            lab_dsn,
            """
            UPDATE dim_customer d
            SET is_current = s.rn = 1
            FROM (
                SELECT version_id,
                       row_number() OVER (
                           PARTITION BY customer_id ORDER BY valid_from DESC
                       ) AS rn
                FROM dim_customer
            ) s
            WHERE d.version_id = s.version_id
            """,
        )

        result = run_check(lab, lab_dsn, 2)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "no_gaps" in failed

    async def test_stand_reset_undoes_fix(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)
        assert run_check(lab, lab_dsn, 1).passed is True

        await reset_stand(postgres_dsn, lab, 1)

        assert run_check(lab, lab_dsn, 1).passed is False


class TestVariantsDifferInSymptoms:
    async def test_variant_1_overlaps(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        failed = {c.name for c in run_check(lab, lab_dsn, 1).checks if not c.ok}

        assert "no_overlaps" in failed

    async def test_variant_2_gaps_and_flags(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 2)

        failed = {c.name for c in run_check(lab, lab_dsn, 2).checks if not c.ok}

        assert "no_gaps" in failed
        assert "one_current_per_customer" in failed

    async def test_variant_3_has_closed_current_versions(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 3)

        conn = await asyncpg.connect(lab_dsn)
        try:
            closed_current = await conn.fetchval(
                "SELECT count(*) FROM dim_customer WHERE is_current AND valid_to IS NOT NULL"
            )
        finally:
            await conn.close()

        assert closed_current > 0, "в варианте 3 часть текущих версий должна быть закрыта датой"
