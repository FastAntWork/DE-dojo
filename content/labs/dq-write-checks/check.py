#!/usr/bin/env python3
"""Проверка лабы dq-write-checks.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Устройство. Чекер читает реестр `dq_checks`, затем по очереди портит данные
шестью способами — каждый внутри транзакции, которая откатывается, — и
смотрит, вернула ли хоть одна пользовательская проверка строки.

Порядок важен: сначала проверяется отсутствие ложных тревог на чистых данных.
Проверка вида `SELECT 1` поймала бы все шесть дефектов и не значила бы ничего,
поэтому она обязана отсеиваться раньше остальных выводов.

Пользовательские запросы выполняются с ограничением времени: неудачный запрос
не должен подвешивать проверку.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

STATEMENT_TIMEOUT_MS = 5000

# Как ломаем данные и что при этом обязана заметить хотя бы одна проверка.
# Каждая порча применяется в откатываемой транзакции, поэтому стенд остаётся
# нетронутым независимо от результата.
DEFECTS: dict[str, tuple[str, str]] = {
    "catches_duplicate_key": (
        "задвоение ключа",
        "INSERT INTO orders (order_id, customer_id, status, amount, created_at) "
        "SELECT order_id, customer_id, status, amount, created_at FROM orders LIMIT 1",
    ),
    "catches_null_in_required": (
        "пропуск в обязательном поле",
        "UPDATE orders SET customer_id = NULL "
        "WHERE order_id IN (SELECT order_id FROM orders LIMIT 20)",
    ),
    "catches_negative_amount": (
        "значение вне допустимого диапазона",
        "UPDATE orders SET amount = -100 WHERE order_id IN (SELECT order_id FROM orders LIMIT 5)",
    ),
    "catches_unknown_status": (
        "статус вне списка допустимых",
        "UPDATE orders SET status = 'refunded_v2' "
        "WHERE order_id IN (SELECT order_id FROM orders LIMIT 5)",
    ),
    "catches_broken_reference": (
        "ссылка на несуществующего клиента",
        "UPDATE orders SET customer_id = 999999999 "
        "WHERE order_id IN (SELECT order_id FROM orders LIMIT 5)",
    ),
    "catches_stale_data": (
        "устаревание: свежих данных нет",
        "DELETE FROM orders WHERE created_at > now() - interval '2 days'",
    ),
}

MIN_CHECKS = 6
EXAMPLE_CHECK = "example_status_not_null"


async def load_checks(conn: asyncpg.Connection[asyncpg.Record]) -> list[tuple[str, str]]:
    rows = await conn.fetch("SELECT name, query FROM dq_checks ORDER BY name")
    return [(str(r["name"]), str(r["query"])) for r in rows]


async def run_user_check(conn: asyncpg.Connection[asyncpg.Record], query: str) -> int | None:
    """Число плохих строк, найденных проверкой. None — запрос не выполнился."""
    try:
        rows = await conn.fetch(query)
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return None
    return len(rows)


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        user_checks = await load_checks(conn)

        # ── 1. Проверок достаточно ────────────────────────────────────────
        add(
            "enough_checks",
            len(user_checks) >= MIN_CHECKS,
            f"проверок в реестре: {len(user_checks)}, нужно не меньше {MIN_CHECKS}",
        )

        # ── 2. Пример на месте ────────────────────────────────────────────
        names = {name for name, _ in user_checks}
        add(
            "example_kept",
            EXAMPLE_CHECK in names,
            f"проверка {EXAMPLE_CHECK} на месте"
            if EXAMPLE_CHECK in names
            else f"проверка {EXAMPLE_CHECK} удалена из реестра",
        )

        # ── 3. Все запросы выполняются и молчат на чистых данных ──────────
        broken: list[str] = []
        noisy: list[str] = []
        for name, query in user_checks:
            found = await run_user_check(conn, query)
            if found is None:
                broken.append(name)
            elif found > 0:
                noisy.append(f"{name} ({found})")

        add(
            "queries_valid",
            not broken,
            f"не выполнились: {', '.join(broken)}" if broken else "все запросы выполняются",
        )
        add(
            "no_false_positives",
            not noisy,
            f"сработали на чистых данных: {', '.join(noisy)}"
            if noisy
            else "на чистых данных все проверки молчат",
        )

        # Дальше сравнивать бессмысленно: проверка, орущая всегда, «поймает»
        # любую порчу, и результат ничего не будет значить.
        if noisy or broken or not user_checks:
            for name, (title, _) in DEFECTS.items():
                add(name, False, f"{title}: проверки сначала должны молчать на чистых данных")
            return checks

        working = [(name, query) for name, query in user_checks]

        # ── 4. Каждая порча замечена ──────────────────────────────────────
        for check_name, (title, defect_sql) in DEFECTS.items():
            transaction = conn.transaction()
            await transaction.start()
            try:
                await conn.execute(defect_sql)
                caught_by = [
                    name
                    for name, query in working
                    if (found := await run_user_check(conn, query)) is not None and found > 0
                ]
            finally:
                await transaction.rollback()

            add(
                check_name,
                bool(caught_by),
                f"{title}: поймана проверкой {', '.join(caught_by)}"
                if caught_by
                else f"{title}: ни одна проверка не сработала",
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
