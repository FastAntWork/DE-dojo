#!/usr/bin/env python3
"""Проверка лабы index-only-scan.

Печатает в stdout строго одну строку JSON:

    {"passed": bool, "score": float, "checks": [{"name", "ok", "detail"}]}

Проверяется не наличие конкретного индекса, а свойство плана: запрос обязан
выполняться без обращений к таблице. Поэтому засчитывается любой способ,
которым этого добились, — важно, что в плане Index Only Scan и Heap Fetches
равен нулю.

Ограничение на число индексов стоит намеренно: насыпать десяток «на всякий
случай» тоже приводит к нужному плану, но за каждый индекс платит каждая
вставка.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

EXPECTED_ROWS = 300_000
MAX_INDEXES = 3

REPORT = """
SELECT status, count(*)
FROM orders
WHERE tenant_id = 42
  AND created_at >= TIMESTAMPTZ '2026-02-01'
  AND created_at <  TIMESTAMPTZ '2026-03-01'
GROUP BY status
"""


def find_node(node: dict[str, Any], relation: str) -> dict[str, Any] | None:
    """Узел плана, читающий указанную таблицу."""
    if node.get("Relation Name") == relation:
        return node
    for child in node.get("Plans", []):
        found = find_node(child, relation)
        if found is not None:
            return found
    return None


async def collect(dsn: str) -> list[dict[str, Any]]:
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        # ── 1. Данные на месте ────────────────────────────────────────────
        total = int(await conn.fetchval("SELECT count(*) FROM orders"))
        add(
            "data_intact",
            total == EXPECTED_ROWS,
            f"строк в orders: {total}, ожидалось {EXPECTED_ROWS}",
        )
        if total != EXPECTED_ROWS:
            return checks

        # ── 2. Результат верен ────────────────────────────────────────────
        # Эталон считается запросом, который заведомо не воспользуется тем же
        # индексом: так проверяется содержимое, а не согласованность двух
        # одинаково неверных путей.
        expected = await conn.fetch(
            "SELECT status, count(*) FROM orders "
            "WHERE tenant_id::text = '42' "
            "  AND created_at >= TIMESTAMPTZ '2026-02-01' "
            "  AND created_at <  TIMESTAMPTZ '2026-03-01' "
            "GROUP BY status ORDER BY status"
        )
        actual = await conn.fetch(f"{REPORT} ORDER BY status")
        same = [tuple(r) for r in expected] == [tuple(r) for r in actual]
        add(
            "report_correct",
            same,
            "результат совпадает с эталоном"
            if same
            else f"строк в результате {len(actual)}, ожидалось {len(expected)}",
        )

        # ── 3. Index Only Scan и ноль обращений к таблице ─────────────────
        raw = await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {REPORT}")
        plan = json.loads(raw)[0]["Plan"] if isinstance(raw, str) else raw[0]["Plan"]
        node = find_node(plan, "orders")
        node_type = str(node.get("Node Type", "не найден")) if node else "не найден"
        heap_fetches = int(node.get("Heap Fetches", -1)) if node else -1

        add(
            "index_only_scan",
            node_type == "Index Only Scan",
            f"способ доступа к orders: {node_type}",
        )
        add(
            "no_heap_fetches",
            node_type == "Index Only Scan" and heap_fetches == 0,
            f"Heap Fetches: {heap_fetches}"
            if node_type == "Index Only Scan"
            else "Heap Fetches считается только при Index Only Scan",
        )

        # ── 4. Индексов не насыпано ───────────────────────────────────────
        indexes = int(
            await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE tablename = 'orders'")
        )
        names = ", ".join(
            str(row[0])
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'orders' ORDER BY indexname"
            )
        )
        add(
            "index_count_reasonable",
            indexes <= MAX_INDEXES,
            f"индексов на orders: {indexes} (не больше {MAX_INDEXES}) — {names}",
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
