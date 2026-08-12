#!/usr/bin/env python3
"""Проверка лабы scd2-broken.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Проверяются свойства измерения, а не способ, которым их добились: годится и
одна UPDATE с оконной функцией, и пересборка таблицы целиком, и цикл на
plpgsql. Требование одно — история должна совпасть со снимком `_seed_versions`.

Интервал считается полуоткрытым: [valid_from, valid_to). Именно поэтому
«нет разрывов» означает valid_to предыдущей версии РАВЕН valid_from следующей,
а не «на день раньше».
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

# Сколько примеров нарушений показывать: список из тысячи строк бесполезен.
SAMPLE = 3


async def scalar(conn: asyncpg.Connection[asyncpg.Record], sql: str) -> int:
    return int(await conn.fetchval(sql))


async def sample_ids(conn: asyncpg.Connection[asyncpg.Record], sql: str) -> str:
    rows = await conn.fetch(sql)
    return ", ".join(str(row[0]) for row in rows[:SAMPLE])


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        # ── 1. История не потеряна и не дописана ──────────────────────────
        # Симметричная разность двух наборов: и удаление версии, и появление
        # лишней, и правка valid_from или атрибута дадут ненулевой результат.
        drift = await scalar(
            conn,
            """
            SELECT count(*) FROM (
                SELECT customer_id, valid_from, city, plan FROM dim_customer
                EXCEPT ALL
                SELECT customer_id, valid_from, city, plan FROM _seed_versions
                UNION ALL
                SELECT customer_id, valid_from, city, plan FROM _seed_versions
                EXCEPT ALL
                SELECT customer_id, valid_from, city, plan FROM dim_customer
            ) d
            """,
        )
        add(
            "history_preserved",
            drift == 0,
            "версии и их атрибуты совпадают со снимком"
            if drift == 0
            else f"расхождений со снимком версий: {drift}",
        )

        # ── 2. Интервалы не пересекаются ──────────────────────────────────
        overlaps = await scalar(
            conn,
            """
            SELECT count(*) FROM dim_customer a
            JOIN dim_customer b
              ON b.customer_id = a.customer_id
             AND b.version_id <> a.version_id
             AND b.valid_from >= a.valid_from
             AND b.valid_from < coalesce(a.valid_to, DATE '9999-12-31')
            """,
        )
        add(
            "no_overlaps",
            overlaps == 0,
            "пересечений нет"
            if overlaps == 0
            else f"пересекающихся пар версий: {overlaps} — соединение задвоит факты",
        )

        # ── 3. Разрывов нет ───────────────────────────────────────────────
        gaps = await scalar(
            conn,
            """
            SELECT count(*) FROM (
                SELECT valid_to,
                       lead(valid_from) OVER (PARTITION BY customer_id ORDER BY valid_from) AS nxt
                FROM dim_customer
            ) s
            WHERE nxt IS NOT NULL AND (valid_to IS NULL OR valid_to <> nxt)
            """,
        )
        add(
            "no_gaps",
            gaps == 0,
            "история непрерывна"
            if gaps == 0
            else f"версий, чей valid_to не равен началу следующей: {gaps}",
        )

        # ── 4. Ровно одна актуальная версия на клиента ────────────────────
        bad_current = await scalar(
            conn,
            """
            SELECT count(*) FROM (
                SELECT customer_id FROM dim_customer
                GROUP BY customer_id
                HAVING count(*) FILTER (WHERE is_current) <> 1
            ) x
            """,
        )
        examples = await sample_ids(
            conn,
            """
            SELECT customer_id FROM dim_customer
            GROUP BY customer_id
            HAVING count(*) FILTER (WHERE is_current) <> 1
            ORDER BY customer_id
            """,
        )
        add(
            "one_current_per_customer",
            bad_current == 0,
            "у каждого клиента ровно одна актуальная версия"
            if bad_current == 0
            else f"клиентов с неверным числом актуальных версий: {bad_current} ({examples})",
        )

        # ── 5. Актуальная версия — последняя, и её интервал открыт ────────
        wrong_flag = await scalar(
            conn,
            """
            SELECT count(*) FROM (
                SELECT is_current,
                       valid_to,
                       row_number() OVER (
                           PARTITION BY customer_id ORDER BY valid_from DESC
                       ) AS rn
                FROM dim_customer
            ) s
            WHERE (rn = 1) <> is_current
               OR (is_current AND valid_to IS NOT NULL)
               OR (NOT is_current AND valid_to IS NULL)
            """,
        )
        add(
            "current_is_open_and_last",
            wrong_flag == 0,
            "флаг стоит на последней версии, её интервал открыт"
            if wrong_flag == 0
            else f"версий с неверным флагом или интервалом: {wrong_flag}",
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
