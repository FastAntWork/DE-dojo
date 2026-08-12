"""Лаба idempotent-load на живом PostgreSQL.

Проверяется то, чего не проверить рассуждением: что лаба **решаема** описанным
в разборе способом и что чекер **честен** — не пропускает решения, которые
выглядят рабочими и таковыми не являются.

Последнее здесь важнее обычного. У этой лабы есть два соблазнительных
неверных ответа, каждый из которых проходит наивную проверку:

* `ON CONFLICT DO NOTHING` — идемпотентно, но не подхватывает опоздавшие
  строки, то есть витрина навсегда расходится с источником;
* `DELETE FROM orders_daily` без условия — тоже идемпотентно, но стирает
  соседние дни.

Если чекер принимает хотя бы одно из них, лаба учит неправильному.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import asyncpg
import pytest

from dojo.runner.lab import Lab, load_lab, reset_stand, run_check

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"

VARIANTS = [1, 2, 3]

# Исправление из разбора. Одно на все варианты намеренно: правильное решение
# универсально, а различаются только симптомы поломки.
FIX = """
CREATE OR REPLACE PROCEDURE load_day(p_day date)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM orders_daily WHERE day = p_day;

    INSERT INTO orders_daily (day, orders, revenue)
    SELECT p_day, count(*), coalesce(sum(amount), 0)
    FROM raw_orders
    WHERE created_at = p_day;
END;
$$;
"""

# Второй верный способ из разбора: слияние по ключу. Работает только там, где
# есть уникальный индекс по дню, — то есть в третьем варианте.
FIX_UPSERT = """
CREATE OR REPLACE PROCEDURE load_day(p_day date)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO orders_daily (day, orders, revenue)
    SELECT p_day, count(*), coalesce(sum(amount), 0)
    FROM raw_orders
    WHERE created_at = p_day
    ON CONFLICT (day) DO UPDATE
    SET orders = excluded.orders, revenue = excluded.revenue;
END;
$$;
"""

# Идемпотентно и разрушительно: загрузка одного дня стирает все остальные.
WIPE_EVERYTHING = """
CREATE OR REPLACE PROCEDURE load_day(p_day date)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM orders_daily;

    INSERT INTO orders_daily (day, orders, revenue)
    SELECT p_day, count(*), coalesce(sum(amount), 0)
    FROM raw_orders
    WHERE created_at = p_day;
END;
$$;
"""


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "idempotent-load")


async def apply(dsn: str, statement: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


class TestStandPreparation:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_stand_builds(self, postgres_dsn: str, lab: Lab, variant: int) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        conn = await asyncpg.connect(lab_dsn)
        try:
            rows = await conn.fetchval("SELECT count(*) FROM raw_orders")
            mart = await conn.fetchval("SELECT count(*) FROM orders_daily")
            proc = await conn.fetchval("SELECT count(*) FROM pg_proc WHERE proname = 'load_day'")
        finally:
            await conn.close()

        assert rows == 200_000, f"вариант {variant}: строк в источнике {rows}"
        assert mart == 0, "витрина должна начинаться пустой"
        assert proc == 1, "процедура load_day обязана существовать"

    async def test_every_day_has_data(self, postgres_dsn: str, lab: Lab) -> None:
        """Проверяемые дни не должны оказаться пустыми — иначе лаба бессмысленна."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        conn = await asyncpg.connect(lab_dsn)
        try:
            for day in (date(2026, 3, 10), date(2026, 3, 11), date(2026, 3, 12)):
                count = await conn.fetchval(
                    "SELECT count(*) FROM raw_orders WHERE created_at = $1", day
                )
                assert count > 100, f"{day}: строк всего {count}"
        finally:
            await conn.close()


class TestBrokenStandFails:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_untouched_stand_does_not_pass(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed is False, f"вариант {variant} сдаётся без единого действия"
        failed = {c.name for c in result.checks if not c.ok}
        assert "source_intact" not in failed, "источник трогать никто не должен был"

    async def test_variant_1_duplicates(self, postgres_dsn: str, lab: Lab) -> None:
        """Вариант 1 обязан ломаться именно на повторе."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)

        result = run_check(lab, lab_dsn, 1)

        failed = {c.name for c in result.checks if not c.ok}
        assert "rerun_is_noop" in failed

    async def test_variant_3_ignores_late_data(self, postgres_dsn: str, lab: Lab) -> None:
        """Вариант 3 идемпотентен — и обязан провалиться на опоздавших строках.

        Это главная мысль лабы: идемпотентность без правильности бесполезна.
        """
        lab_dsn = await reset_stand(postgres_dsn, lab, 3)

        result = run_check(lab, lab_dsn, 3)

        by_name = {c.name: c.ok for c in result.checks}
        assert by_name["rerun_is_noop"] is True, "вариант 3 не должен задваивать"
        assert by_name["late_data_picked_up"] is False, "вариант 3 обязан терять опоздавшие"


class TestDocumentedFixWorks:
    @pytest.mark.parametrize("variant", VARIANTS)
    async def test_delete_insert_passes(self, postgres_dsn: str, lab: Lab, variant: int) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, variant)
        await apply(lab_dsn, FIX)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]
        assert result.score == 1.0

    async def test_upsert_also_passes(self, postgres_dsn: str, lab: Lab) -> None:
        """Разбор обещает два рабочих способа — проверяем второй."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 3)
        await apply(lab_dsn, FIX_UPSERT)

        result = run_check(lab, lab_dsn, 3)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]


class TestCheckerIsHonest:
    async def test_wiping_the_mart_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Идемпотентно и разрушительно: соседние дни исчезают."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, WIPE_EVERYTHING)

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False, "удаление всей витрины принято за решение"
        failed = {c.name for c in result.checks if not c.ok}
        assert "neighbours_untouched" in failed

    async def test_deleting_source_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Подогнать витрину под источник, урезав источник, — не починка."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)
        await apply(lab_dsn, "DELETE FROM raw_orders WHERE created_at = DATE '2026-03-11'")

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "source_intact" in failed

    async def test_stand_reset_undoes_fix(self, postgres_dsn: str, lab: Lab) -> None:
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)
        assert run_check(lab, lab_dsn, 1).passed is True

        await reset_stand(postgres_dsn, lab, 1)

        assert run_check(lab, lab_dsn, 1).passed is False


class TestCheckerCleansUpAfterItself:
    async def test_source_row_count_unchanged_by_check(self, postgres_dsn: str, lab: Lab) -> None:
        """Чекер дописывает в источник опоздавшую строку — и обязан её убрать.

        Иначе второй прогон проверки провалит source_intact, и человек получит
        незачёт за то, чего не делал.
        """
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)

        first = run_check(lab, lab_dsn, 1)
        second = run_check(lab, lab_dsn, 1)

        assert first.passed and second.passed, "повторная проверка изменила приговор"
        conn = await asyncpg.connect(lab_dsn)
        try:
            total = await conn.fetchval("SELECT count(*) FROM raw_orders")
        finally:
            await conn.close()
        assert total == 200_000


class TestConcurrencyAssumption:
    async def test_check_runs_in_reasonable_time(self, postgres_dsn: str, lab: Lab) -> None:
        """Проверка вызывает процедуру шесть раз — она обязана быть быстрой."""
        lab_dsn = await reset_stand(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIX)

        started = asyncio.get_running_loop().time()
        run_check(lab, lab_dsn, 1)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 60, f"проверка заняла {elapsed:.1f} с — человек столько не ждёт"
