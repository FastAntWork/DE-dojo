"""Эталонное решение. Пользователю не показывается."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MISSING = {"", "null", "na", "n/a", "-", "не указано"}


@dataclass(frozen=True)
class Rejected:
    line_no: int
    reason: str


@dataclass(frozen=True)
class ParseResult:
    rows: list[dict[str, Any]]
    rejected: list[Rejected]


REQUIRED = ("order_id", "amount", "city")


def _is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in MISSING


def parse_orders(lines: Iterable[str]) -> ParseResult:
    rows: list[dict[str, Any]] = []
    rejected: list[Rejected] = []

    # Модуль csv, а не split: он знает про кавычки, экранирование и запятые
    # внутри значений. DictReader берёт порядок колонок из заголовка.
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        return ParseResult(rows=rows, rejected=rejected)

    header = {name.strip() for name in reader.fieldnames}
    missing_columns = [name for name in REQUIRED if name not in header]
    if missing_columns:
        rejected.append(
            Rejected(line_no=1, reason=f"в заголовке нет колонок: {', '.join(missing_columns)}")
        )
        return ParseResult(rows=rows, rejected=rejected)

    # Заголовок — первая строка файла, поэтому данные начинаются со второй.
    for line_no, raw in enumerate(reader, start=2):
        record = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw.items()
        }

        raw_id = record.get("order_id")
        if _is_missing(raw_id):
            rejected.append(Rejected(line_no=line_no, reason="order_id пуст"))
            continue
        try:
            order_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            rejected.append(Rejected(line_no=line_no, reason=f"order_id не число: {raw_id!r}"))
            continue

        raw_amount = record.get("amount")
        if _is_missing(raw_amount):
            rejected.append(Rejected(line_no=line_no, reason="amount пуст"))
            continue
        try:
            amount = Decimal(raw_amount)  # type: ignore[arg-type]
        except (TypeError, InvalidOperation):
            rejected.append(Rejected(line_no=line_no, reason=f"amount не число: {raw_amount!r}"))
            continue

        raw_city = record.get("city")
        city = None if _is_missing(raw_city) else raw_city

        rows.append({"order_id": order_id, "amount": amount, "city": city})

    return ParseResult(rows=rows, rejected=rejected)


__all__ = ["MISSING", "ParseResult", "Rejected", "parse_orders"]
