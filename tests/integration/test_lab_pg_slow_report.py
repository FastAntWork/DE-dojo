"""Лаба pg-slow-report на живом PostgreSQL.

Здесь проверяется то, что нельзя проверить рассуждением: что лаба **решаема** и
что чекер **честен**.

Решаемость: описанное в разборе исправление действительно переводит стенд в
зачёт. Задача, которую нельзя решить заявленным способом, хуже отсутствия
задачи — человек потратит на неё вечер и решит, что дело в нём.

Честность: сломанный стенд обязан давать незачёт, а обход задачи удалением
данных — тем более. Чекер, который пропускает мусор, превращает лабу в
декорацию.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from dojo.runner.lab import Lab, load_lab, parse_check_output, reset_stand, run_check

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"

# Исправление из разбора для каждого варианта. Тест проверяет именно их:
# если разбор врёт, тест покраснеет.
#
# У варианта 2 команда именно VACUUM ANALYZE, а не ANALYZE: после массового
# UPDATE устаревшая статистика — только половина беды, вторая половина —
# мёртвые строки. Отдельный тест ниже фиксирует, что половинчатый ответ не
# проходит, потому что запрос от него действительно не ускоряется.
FIXES = {
    1: ["CREATE INDEX events_tenant_created_idx ON events (tenant_id, created_at)"],
    2: ["VACUUM ANALYZE events"],
    3: ["CREATE INDEX events_tenant_created_idx ON events (tenant_id, created_at)"],
}


# VACUUM нельзя выполнить внутри транзакции, а asyncpg на нескольких
# командах в одном execute открывает неявную. Поэтому команды подаются
# по одной.
async def apply(dsn: str, statements: list[str]) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def lab() -> Lab:
    return load_lab(CONTENT, "pg-slow-report")


async def prepare(dsn: str, lab: Lab, variant: int) -> str:
    return await reset_stand(dsn, lab, variant)


class TestStandPreparation:
    async def test_all_variants_build(self, postgres_dsn: str, lab: Lab) -> None:
        for variant in range(1, lab.variants + 1):
            lab_dsn = await prepare(postgres_dsn, lab, variant)
            conn = await asyncpg.connect(lab_dsn)
            try:
                rows = await conn.fetchval("SELECT count(*) FROM events")
            finally:
                await conn.close()
            assert rows == 200_000, f"вариант {variant}: строк {rows}"

    async def test_unknown_variant_rejected(self, postgres_dsn: str, lab: Lab) -> None:
        from dojo.runner.lab import LabError

        with pytest.raises(LabError, match="вне диапазона"):
            await prepare(postgres_dsn, lab, 99)


class TestBrokenStandFails:
    @pytest.mark.parametrize("variant", [1, 2, 3])
    async def test_untouched_stand_does_not_pass(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await prepare(postgres_dsn, lab, variant)

        result = run_check(lab, lab_dsn, variant)

        assert result.passed is False, f"вариант {variant} сдаётся без единого действия"
        failed = {c.name for c in result.checks if not c.ok}
        assert failed == {"reads_few_pages"}, (
            "сломан должен быть ровно объём чтения: данные на месте, отчёт верен. "
            f"{[(c.name, c.detail) for c in result.checks]}"
        )


class TestDocumentedFixWorks:
    @pytest.mark.parametrize("variant", [1, 2, 3])
    async def test_fix_from_solution_passes(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        lab_dsn = await prepare(postgres_dsn, lab, variant)
        await apply(lab_dsn, FIXES[variant])

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, (
            f"вариант {variant}: исправление из разбора не сдаёт лабу. "
            f"{[(c.name, c.detail) for c in result.checks if not c.ok]}"
        )
        assert result.score == 1.0

    @pytest.mark.parametrize("variant", [1, 2, 3])
    async def test_covering_index_also_passes(
        self, postgres_dsn: str, lab: Lab, variant: int
    ) -> None:
        """Разбор обещает Index Only Scan как лучший ответ — проверяем обещание.

        Заодно это тест на то, что критерий не диктует единственный способ:
        покрывающий индекс проходит во всех вариантах, включая тот, где
        задумывалась совсем другая причина.
        """
        lab_dsn = await prepare(postgres_dsn, lab, variant)
        await apply(
            lab_dsn,
            [
                "CREATE INDEX events_report_idx ON events (tenant_id, created_at) INCLUDE (kind)",
                "VACUUM ANALYZE events",
            ],
        )

        result = run_check(lab, lab_dsn, variant)

        assert result.passed, [(c.name, c.detail) for c in result.checks if not c.ok]


class TestCheckerIsHonest:
    async def test_deleting_data_does_not_pass(self, postgres_dsn: str, lab: Lab) -> None:
        """Ускорить отчёт удалением данных — так инциденты не чинят."""
        lab_dsn = await prepare(postgres_dsn, lab, 1)
        await apply(lab_dsn, ["DELETE FROM events WHERE tenant_id <> 42", "ANALYZE events"])

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False
        failed = {c.name for c in result.checks if not c.ok}
        assert "data_intact" in failed

    @pytest.mark.parametrize(
        "index",
        [
            "CREATE INDEX events_kind_idx ON events (kind)",
            # Ровно то, чем сломан вариант 3: колонка есть в фильтре, но
            # неселективная. Такой индекс план возьмёт — и всё равно
            # перечитает таблицу.
            "CREATE INDEX events_created_idx ON events (created_at)",
        ],
    )
    async def test_wrong_index_does_not_pass(self, postgres_dsn: str, lab: Lab, index: str) -> None:
        """Индекс не по тем колонкам не должен приниматься за решение."""
        lab_dsn = await prepare(postgres_dsn, lab, 1)
        await apply(lab_dsn, [index, "ANALYZE events"])

        result = run_check(lab, lab_dsn, 1)

        assert result.passed is False, f"{index} сдал лабу: {[c.detail for c in result.checks]}"

    async def test_analyze_alone_does_not_pass_variant_2(self, postgres_dsn: str, lab: Lab) -> None:
        """Половинчатый ответ в варианте 2 обязан оставаться незачётом.

        ANALYZE чинит статистику, и план после него выглядит правильным:
        оценка совпадает с фактом, используется нужный индекс. Но запрос
        по-прежнему читает всю таблицу, потому что живые строки размазаны
        среди мёртвых. Разбор обещает, что этого не хватит, — фиксируем.
        """
        lab_dsn = await prepare(postgres_dsn, lab, 2)
        await apply(lab_dsn, ["ANALYZE events"])

        result = run_check(lab, lab_dsn, 2)

        assert result.passed is False, "ANALYZE без VACUUM сдал вариант 2 — разбор врёт"

    async def test_stand_reset_undoes_previous_fix(self, postgres_dsn: str, lab: Lab) -> None:
        """Повторный запуск обязан вернуть стенд в сломанное состояние."""
        lab_dsn = await prepare(postgres_dsn, lab, 1)
        await apply(lab_dsn, FIXES[1])
        assert run_check(lab, lab_dsn, 1).passed is True

        await prepare(postgres_dsn, lab, 1)

        assert run_check(lab, lab_dsn, 1).passed is False


class TestOutputParsing:
    def test_reads_last_line_as_json(self) -> None:
        # Чекер мог что-то напечатать по дороге; контракт требует, чтобы JSON
        # был последним, а не единственным.
        out = 'что-то в лог\n{"passed": true, "score": 1.0, "checks": []}'

        assert parse_check_output(out).passed is True

    def test_non_json_reported_clearly(self) -> None:
        result = parse_check_output("Traceback (most recent call last):")

        assert result.passed is False
        assert result.error is not None
        assert "не JSON" in result.error

    def test_empty_output_reported(self) -> None:
        result = parse_check_output("", "ImportError: no module named asyncpg")

        assert result.passed is False
        assert "ImportError" in (result.error or "")
