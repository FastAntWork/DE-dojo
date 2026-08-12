#!/usr/bin/env python3
"""Проверка лабы idempotent-load.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Проверка не читает текст процедуры и ничего не знает о том, как человек её
переписал. Она ВЫЗЫВАЕТ её в тех сценариях, ради которых лаба существует, и
сравнивает состояние витрины с независимым расчётом. Поэтому засчитывается
любое верное решение: delete-insert, MERGE, upsert, подмена секции.

Отдельно проверяется, что решение не свелось к «удалять всё и грузить заново»:
такая процедура идемпотентна и разрушительна одновременно.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

EXPECTED_SOURCE_ROWS = 200_000

# Три соседних дня: загружаем средний повторно и смотрим, не пострадали ли
# крайние. День выбран из середины периода, чтобы соседи заведомо существовали.
DAY_BEFORE = date(2026, 3, 10)
DAY = date(2026, 3, 11)
DAY_AFTER = date(2026, 3, 12)

REFERENCE = """
SELECT count(*) AS orders, coalesce(sum(amount), 0) AS revenue
FROM raw_orders WHERE created_at = $1
"""


async def snapshot(conn: asyncpg.Connection[asyncpg.Record]) -> list[tuple[Any, ...]]:
    rows = await conn.fetch("SELECT day, orders, revenue FROM orders_daily ORDER BY day, orders")
    return [tuple(row) for row in rows]


async def reference(conn: asyncpg.Connection[asyncpg.Record], day: date) -> tuple[int, Decimal]:
    row = await conn.fetchrow(REFERENCE, day)
    return int(row["orders"]), Decimal(row["revenue"])


async def mart_row(
    conn: asyncpg.Connection[asyncpg.Record], day: date
) -> list[tuple[int, Decimal]]:
    rows = await conn.fetch(
        "SELECT orders, revenue FROM orders_daily WHERE day = $1 ORDER BY orders", day
    )
    return [(int(r["orders"]), Decimal(r["revenue"])) for r in rows]


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        # ── 1. Источник не тронут ─────────────────────────────────────────
        total = int(await conn.fetchval("SELECT count(*) FROM raw_orders"))
        add(
            "source_intact",
            total == EXPECTED_SOURCE_ROWS,
            f"строк в raw_orders: {total}, ожидалось {EXPECTED_SOURCE_ROWS}",
        )
        if total != EXPECTED_SOURCE_ROWS:
            # Дальше проверять бессмысленно: эталон считается по источнику.
            return checks

        # Начинаем с чистой витрины: состояние, оставшееся от экспериментов,
        # не должно влиять на приговор.
        await conn.execute("TRUNCATE orders_daily")
        for day in (DAY_BEFORE, DAY, DAY_AFTER):
            await conn.execute("CALL load_day($1)", day)

        after_first = await snapshot(conn)

        # ── 2. Повторный вызов ничего не меняет ───────────────────────────
        await conn.execute("CALL load_day($1)", DAY)
        after_second = await snapshot(conn)
        same = after_first == after_second
        add(
            "rerun_is_noop",
            same,
            "повторный вызов оставил витрину прежней"
            if same
            else f"строк в витрине было {len(after_first)}, стало {len(after_second)}",
        )

        # ── 3. Соседние дни не пострадали ─────────────────────────────────
        neighbours_ok = True
        details: list[str] = []
        for day in (DAY_BEFORE, DAY_AFTER):
            rows = await mart_row(conn, day)
            expected = await reference(conn, day)
            if rows != [expected]:
                neighbours_ok = False
                details.append(f"{day}: в витрине {rows}, ожидалось [{expected}]")
        add(
            "neighbours_untouched",
            neighbours_ok,
            "; ".join(details) if details else "соседние дни на месте и верны",
        )

        # ── 4. Опоздавшая строка подхватывается ───────────────────────────
        # Источник дописывает запись задним числом — ровно то, ради чего
        # повторный запуск и существует.
        late_id = await conn.fetchval(
            "INSERT INTO raw_orders (created_at, customer_id, amount) "
            "VALUES ($1, 999999, 1234.56) RETURNING id",
            DAY,
        )
        try:
            await conn.execute("CALL load_day($1)", DAY)
            rows = await mart_row(conn, DAY)
            expected = await reference(conn, DAY)
            picked = rows == [expected]
            add(
                "late_data_picked_up",
                picked,
                "опоздавшая строка учтена"
                if picked
                else f"в витрине {rows}, ожидалось [{expected}] — повтор не обновил день",
            )
        finally:
            # Убираем за собой и возвращаем витрину в согласованное состояние.
            await conn.execute("DELETE FROM raw_orders WHERE id = $1", late_id)
            await conn.execute("CALL load_day($1)", DAY)

        # ── 5. Числа совпадают с независимым расчётом ─────────────────────
        wrong: list[str] = []
        for day in (DAY_BEFORE, DAY, DAY_AFTER):
            rows = await mart_row(conn, day)
            expected = await reference(conn, day)
            if rows != [expected]:
                wrong.append(f"{day}: {rows} против [{expected}]")
        add(
            "numbers_correct",
            not wrong,
            "; ".join(wrong) if wrong else "все три дня совпадают с расчётом по источнику",
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
