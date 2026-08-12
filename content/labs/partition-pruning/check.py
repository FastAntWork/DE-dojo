#!/usr/bin/env python3
"""Проверка лабы partition-pruning.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Проверяются свойства хранения, а не способ, которым их добились: годится и
пересоздание таблицы с переносом данных, и присоединение готовых таблиц
секциями. Гранулярность тоже не диктуется — требуется лишь, чтобы ни одна
секция не содержала данных больше чем одного месяца. Иначе удалить месяц
отцеплением секции невозможно, а ради этого всё и затевалось.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

EXPECTED_ROWS = 400_000

REPORT = """
SELECT kind, count(*)
FROM events
WHERE created_at >= TIMESTAMPTZ '2026-02-01'
  AND created_at <  TIMESTAMPTZ '2026-03-01'
GROUP BY kind
"""

# Сколько секций допустимо в плане месячного отчёта. Одна — та, что содержит
# нужный месяц. Секция по умолчанию, если она осталась пустой, планировщик
# всё равно может оставить в плане, поэтому граница не жёстче необходимого.
MAX_PARTITIONS_IN_PLAN = 2


def scanned_relations(node: dict[str, Any], found: set[str]) -> set[str]:
    """Имена таблиц, к которым план реально обращается."""
    name = node.get("Relation Name")
    if name:
        found.add(str(name))
    for child in node.get("Plans", []):
        scanned_relations(child, found)
    return found


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        # ── 1. Данные на месте ────────────────────────────────────────────
        total = int(await conn.fetchval("SELECT count(*) FROM events"))
        add(
            "data_intact",
            total == EXPECTED_ROWS,
            f"строк в events: {total}, ожидалось {EXPECTED_ROWS}",
        )
        if total != EXPECTED_ROWS:
            return checks

        # ── 2. Отчёт возвращает верный результат ──────────────────────────
        expected = await conn.fetch(
            "SELECT kind, count(*) FROM events "
            "WHERE created_at >= TIMESTAMPTZ '2026-02-01' "
            "  AND created_at <  TIMESTAMPTZ '2026-03-01' "
            "GROUP BY kind ORDER BY kind"
        )
        actual = await conn.fetch(f"{REPORT} ORDER BY kind")
        same = [tuple(r) for r in expected] == [tuple(r) for r in actual]
        add(
            "report_correct",
            same,
            "результат совпадает с эталоном" if same else "результат отчёта изменился",
        )

        # ── 3. Таблица секционирована по времени ──────────────────────────
        # relkind имеет тип "char", и драйвер отдаёт его байтами: сравнение со
        # строкой молча всегда ложно. Приводим к тексту в самом запросе.
        row = await conn.fetchrow(
            "SELECT relkind::text AS kind, pg_get_partkeydef(oid) AS key "
            "FROM pg_class WHERE relname = 'events' AND relkind IN ('r', 'p')"
        )
        kind = str(row["kind"]) if row else ""
        key = row["key"] if row else None
        by_time = kind == "p" and key is not None and "created_at" in str(key)
        add(
            "partitioned_by_time",
            by_time,
            f"ключ секционирования: {key}" if kind == "p" else "таблица events не секционирована",
        )

        # ── 4. Отсечение работает ─────────────────────────────────────────
        raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {REPORT}")
        plan = json.loads(raw)[0]["Plan"] if isinstance(raw, str) else raw[0]["Plan"]
        touched = sorted(scanned_relations(plan, set()))
        add(
            "pruning_works",
            len(touched) <= MAX_PARTITIONS_IN_PLAN,
            f"в плане участвуют: {', '.join(touched) or 'ничего'}",
        )

        # ── 5. Месяц можно удалить отцеплением секции ─────────────────────
        # Секция, в которой лежат данные двух месяцев, делает дешёвую чистку
        # невозможной: отцепив её, потеряешь и нужное.
        mixed = await conn.fetch(
            """
            SELECT c.relname, count(DISTINCT date_trunc('month', e.created_at)) AS months
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            CROSS JOIN LATERAL (
                SELECT created_at FROM events e2
                WHERE e2.tableoid = c.oid
            ) AS e
            WHERE p.relname = 'events'
            GROUP BY c.relname
            HAVING count(DISTINCT date_trunc('month', e.created_at)) > 1
            """
        )
        add(
            "month_is_droppable",
            not mixed,
            "каждая секция содержит один месяц"
            if not mixed
            else "секции с данными нескольких месяцев: "
            + ", ".join(f"{r['relname']} ({r['months']})" for r in mixed[:3]),
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
                    "checks": [{"name": "run", "ok": False, "detail": str(exc)[:300]}],
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
