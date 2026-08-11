#!/usr/bin/env python3
"""Единственный источник истины о зачёте по лабе pg-slow-report.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Запускается приложением как отдельный процесс: так проверка не зависит от
внутренностей Dojo, а лабу можно переписать на чём угодно, не трогая раннер.

Проверок три, и третья не менее важна первых двух: сделать отчёт быстрым,
удалив половину таблицы, технически можно — и именно так «чинят» инциденты
те, кого потом ищут.

Про критерий скорости. Наивная проверка «в плане нет Seq Scan» здесь была бы
ложью, и это выяснилось на живом стенде: в двух вариантах из трёх план идёт
через индекс и всё равно читает таблицу целиком — в одном случае потому, что
индекс не по тем колонкам, в другом потому, что таблица распухла от мёртвых
строк. Поэтому меряется то, что меряет DBA: сколько страниц запрос прочитал.
Заодно критерий перестаёт диктовать способ починки — годится любой, лишь бы
отчёт перестал перечитывать всю таблицу.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

REPORT = """
SELECT kind, count(*) AS events
FROM events
WHERE tenant_id = 42
  AND created_at >= TIMESTAMPTZ '2026-01-01'
  AND created_at <  TIMESTAMPTZ '2026-02-01'
GROUP BY kind
ORDER BY kind
"""

# Столько строк засеяно в каждом варианте. Потеря даже одной означает, что
# «оптимизация» свелась к удалению данных.
EXPECTED_ROWS = 200_000

# Бюджет страниц (буферов) на отчёт. Таблица занимает около 2500 страниц, и
# любое из трёх сломанных состояний читает их все — 2559, 2670 и 2565. Любая
# осмысленная починка укладывается в 535 и меньше, а индекс, покрывающий все
# три колонки, доводит счёт до семи. Порог стоит посередине с запасом в обе
# стороны, поэтому не зависит от числа ядер, размера кэша и версии сервера.
PAGE_BUDGET = 1000


def _access_to_events(node: dict[str, Any]) -> str:
    """Каким узлом план добирается до events. Нужен только для сообщения."""
    if node.get("Relation Name") == "events":
        return str(node.get("Node Type", "?"))
    for child in node.get("Plans", []):
        found = _access_to_events(child)
        if found != "не найден":
            return found
    return "не найден"


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    try:
        checks: list[dict[str, Any]] = []

        # ── 1. Данные на месте ────────────────────────────────────────────
        total = await conn.fetchval("SELECT count(*) FROM events")
        checks.append(
            {
                "name": "data_intact",
                "ok": total == EXPECTED_ROWS,
                "detail": (
                    f"строк в events: {total}, ожидалось {EXPECTED_ROWS}"
                    if total != EXPECTED_ROWS
                    else f"все {total} строк на месте"
                ),
            }
        )

        # ── 2. Отчёт возвращает верный результат ──────────────────────────
        # Эталон считается независимым запросом, который не может
        # воспользоваться индексом по тем же колонкам, — так проверяется
        # именно содержимое, а не то, что оба запроса одинаково ошиблись.
        expected = await conn.fetch(
            "SELECT kind, count(*) AS events FROM events "
            "WHERE tenant_id::text = '42' "
            "  AND created_at >= TIMESTAMPTZ '2026-01-01' "
            "  AND created_at <  TIMESTAMPTZ '2026-02-01' "
            "GROUP BY kind ORDER BY kind"
        )
        actual = await conn.fetch(REPORT)
        same = [tuple(r) for r in expected] == [tuple(r) for r in actual]
        checks.append(
            {
                "name": "report_correct",
                "ok": same,
                "detail": (
                    "результат совпадает с эталоном"
                    if same
                    else f"строк в отчёте {len(actual)}, ожидалось {len(expected)}"
                ),
            }
        )

        # ── 3. Отчёт не перечитывает таблицу целиком ──────────────────────
        raw_plan = await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {REPORT}")
        plan = json.loads(raw_plan)[0]["Plan"] if isinstance(raw_plan, str) else raw_plan[0]["Plan"]
        # На верхнем узле счётчики буферов накопительные: в них уже сложено
        # всё дерево вместе с параллельными воркерами.
        pages = int(plan.get("Shared Hit Blocks", 0)) + int(plan.get("Shared Read Blocks", 0))
        access = _access_to_events(plan)
        checks.append(
            {
                "name": "reads_few_pages",
                "ok": pages <= PAGE_BUDGET,
                "detail": (
                    f"прочитано страниц: {pages} при бюджете {PAGE_BUDGET}"
                    f" (доступ к events: {access})"
                ),
            }
        )

        return checks
    finally:
        await conn.close()


def main() -> int:
    dsn = os.environ.get("DOJO_LAB_DSN")
    if not dsn:
        print(
            json.dumps({"passed": False, "score": 0.0, "checks": [], "error": "нет DOJO_LAB_DSN"})
        )
        return 2

    try:
        checks = asyncio.run(collect(dsn))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "score": 0.0,
                    "checks": [{"name": "connection", "ok": False, "detail": str(exc)[:300]}],
                },
                ensure_ascii=False,
            )
        )
        return 1

    passed = all(c["ok"] for c in checks)
    score = round(sum(1 for c in checks if c["ok"]) / len(checks), 2)
    print(json.dumps({"passed": passed, "score": score, "checks": checks}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
