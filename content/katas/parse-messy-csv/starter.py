"""Разбор кривой выгрузки заказов.

Заготовка: классы менять не нужно, замени тело parse_orders своим решением.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Значения, означающие «нет данных». Приходят от источника в таком виде.
MISSING = {"", "null", "na", "n/a", "-", "не указано"}


@dataclass(frozen=True)
class Rejected:
    """Строка, которую не удалось разобрать."""

    line_no: int
    reason: str


@dataclass(frozen=True)
class ParseResult:
    rows: list[dict[str, Any]]
    rejected: list[Rejected]


def parse_orders(lines: Iterable[str]) -> ParseResult:
    """Разбирает CSV с заголовком в записи заказов.

    Колонки: order_id (целое), amount (Decimal), city (строка или None).
    Порядок колонок берётся из заголовка.

    Строка, которую разобрать не удалось, попадает в rejected с номером
    строки в файле (заголовок — строка 1) и причиной. Разбор при этом
    продолжается.
    """
    raise NotImplementedError


__all__ = ["MISSING", "ParseResult", "Rejected", "parse_orders"]
