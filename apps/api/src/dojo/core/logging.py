"""Структурные логи.

JSON с первого дня, а не «человекочитаемый вывод, потом переделаем»: события
обучения всё равно поедут в Kafka и ClickHouse (M6), и формат логов должен
совпадать с форматом событий, иначе придётся держать два сериализатора.

Локально JSON нечитаем глазами, поэтому при DOJO_ENV=local включается
цветной консольный рендер — но структура полей одна и та же.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.StackInfoRenderer(),
    # UTC и ISO-8601: смешение локальных зон в логах превращает разбор
    # инцидента в угадайку.
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Настраивает structlog и подчиняет ему stdlib logging.

    Библиотеки (uvicorn, asyncpg) пишут через stdlib, и без ProcessorFormatter
    их строки шли бы мимо JSON — в наблюдаемости это самая частая дыра.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn ставит свои хендлеры — снимаем, иначе каждая строка задваивается.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
