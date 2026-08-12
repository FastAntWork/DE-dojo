"""Лабораторные стенды.

Лаба — это сломанное окружение и вопрос «почини». В отличие от каты, где
проверяется написанный код, здесь проверяется **состояние системы после
вмешательства**: появился ли индекс, ушло ли распухание, стал ли план вменяемым.

Контракт из ТЗ соблюдён: единственный источник истины о зачёте — `check.py`,
печатающий строгий JSON. Он запускается как отдельный процесс, а не
импортируется: так лабу можно написать на чём угодно, и она не сможет случайно
повлиять на приложение своим состоянием.

Отступление от ТЗ одно и сделано осознанно. Там на каждую лабу предполагался
свой docker-compose. Для лаб на PostgreSQL это означало бы второй экземпляр
СУБД рядом с рабочим — гигабайт памяти при бюджете в десять. Поэтому лаба
объявляет тип стенда: `shared-postgres` получает отдельную БАЗУ в уже поднятом
экземпляре, `compose` — собственный стенд. Второй понадобится для Kafka и
ClickHouse, где своё окружение действительно необходимо.

check.py запускается НЕ в песочнице: это наш код из репозитория, а не решение
пользователя. Песочница нужна там, где исполняется чужое.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import yaml

from dojo.core.logging import get_logger

logger = get_logger(__name__)

LAB_FILES: Final = ("brief.md", "check.py", "hints.yaml", "solution.md")
SAFE_NAME: Final = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
CHECK_TIMEOUT_SEC: Final = 120


class LabError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Hint:
    level: int
    text: str
    penalty: float


@dataclass(frozen=True, slots=True)
class Lab:
    name: str
    path: Path
    brief: str
    solution: str
    hints: tuple[Hint, ...]
    stand: str
    variants: int

    @property
    def database(self) -> str:
        return f"dojo_lab_{self.name.replace('-', '_')}"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LabResult:
    passed: bool
    score: float
    checks: tuple[Check, ...] = ()
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def load_lab(content_root: Path, name: str) -> Lab:
    if not SAFE_NAME.match(name):
        msg = f"недопустимое имя лабы: {name!r}"
        raise LabError(msg)

    directory = content_root / "labs" / name
    missing = [f for f in LAB_FILES if not (directory / f).is_file()]
    if missing:
        msg = f"лаба {name}: не хватает файлов: {', '.join(missing)}"
        raise LabError(msg)

    meta_path = directory / "lab.yaml"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        meta = loaded if isinstance(loaded, dict) else {}

    raw_hints = yaml.safe_load((directory / "hints.yaml").read_text(encoding="utf-8")) or {}
    hints = tuple(
        Hint(
            level=int(item["level"]),
            text=str(item["text"]),
            penalty=float(item["penalty"]),
        )
        for item in raw_hints.get("hints", [])
    )

    return Lab(
        name=name,
        path=directory,
        brief=(directory / "brief.md").read_text(encoding="utf-8"),
        solution=(directory / "solution.md").read_text(encoding="utf-8"),
        hints=hints,
        stand=str(meta.get("stand", "shared-postgres")),
        variants=int(meta.get("variants", 1)),
    )


def lab_dsn(base_dsn: str, database: str) -> str:
    parts = urlsplit(base_dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def reset_stand(base_dsn: str, lab: Lab, variant: int) -> str:
    """Приводит стенд в сломанное состояние. Возвращает DSN лабы.

    Стенд пересоздаётся полностью при каждом запуске, а не чинится: человек
    мог сделать со своей базой что угодно, включая то, что мы не предусмотрели.
    Гарантировать исходное состояние можно только сборкой с нуля.
    """
    if not 1 <= variant <= lab.variants:
        msg = f"вариант {variant} вне диапазона 1..{lab.variants}"
        raise LabError(msg)

    seed = lab.path / f"seed.{variant}.sql"
    if not seed.is_file():
        msg = f"лаба {lab.name}: нет файла {seed.name}"
        raise LabError(msg)

    await _recreate_database(base_dsn, lab.database)
    dsn = lab_dsn(base_dsn, lab.database)

    body, maintenance = split_maintenance(seed.read_text(encoding="utf-8"))

    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    try:
        if body.strip():
            await conn.execute(body)
        for statement in maintenance:
            await conn.execute(statement)
    finally:
        await conn.close()

    logger.info("lab.stand.ready", lab=lab.name, variant=variant)
    return dsn


def split_maintenance(sql: str) -> tuple[str, list[str]]:
    """Делит seed на основную часть и обслуживающие команды.

    Несколько команд в одном `execute` асинхронный драйвер отправляет простым
    протоколом, и PostgreSQL оборачивает их в неявную транзакцию. VACUUM внутри
    транзакции запрещён — а он нужен, например, лабе про Index Only Scan: без
    него карта видимости остаётся пустой, и стенд получается не тем, который
    задумывался.

    Поэтому seed может объявить хвост, выполняемый по одной команде:

        -- dojo:no-transaction
        VACUUM ANALYZE orders;

    Тот же приём и тот же маркер, что у раннера миграций. В этой части
    допустимы только простые однострочные команды: она делится по `;`, и
    долларовые кавычки в ней не поддерживаются.
    """
    marker = "-- dojo:no-transaction"
    head, separator, tail = sql.partition(marker)
    if not separator:
        return sql, []

    statements = [part.strip() for part in tail.split(";")]
    return head, [part for part in statements if part and not part.startswith("--")]


async def _recreate_database(base_dsn: str, database: str) -> None:
    parts = urlsplit(base_dsn)
    admin = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))

    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin)
    try:
        # Обрываем чужие сеансы: без этого DROP DATABASE не выполнится, если
        # человек оставил открытым psql.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await conn.execute(f'CREATE DATABASE "{database}"')
    finally:
        await conn.close()


def run_check(lab: Lab, dsn: str, variant: int) -> LabResult:
    """Запускает check.py лабы и разбирает его JSON.

    Единственный источник истины о зачёте — этот скрипт. Приложение не знает,
    что именно проверяется, и не может «договориться» с проверкой.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(lab.path / "check.py")],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SEC,
            check=False,
            env={"DOJO_LAB_DSN": dsn, "DOJO_LAB_VARIANT": str(variant), "PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired:
        return LabResult(passed=False, score=0.0, error="Проверка не уложилась во время.")
    except OSError as exc:
        return LabResult(passed=False, score=0.0, error=f"Не удалось запустить проверку: {exc}")

    return parse_check_output(completed.stdout, completed.stderr)


def parse_check_output(stdout: str, stderr: str = "") -> LabResult:
    """Разбирает вывод check.py. Чистая функция — проверяется без стенда."""
    text = stdout.strip()
    if not text:
        return LabResult(
            passed=False,
            score=0.0,
            error=f"Проверка ничего не вывела. {stderr.strip()[-500:]}",
        )

    # Берём последнюю строку: скрипт мог что-то напечатать по дороге, а
    # контракт требует JSON — пусть он будет последним, а не единственным.
    last = text.splitlines()[-1]
    try:
        data = json.loads(last)
    except json.JSONDecodeError:
        return LabResult(
            passed=False,
            score=0.0,
            error=f"Проверка вернула не JSON: {last[:300]}",
        )

    checks = tuple(
        Check(
            name=str(item.get("name", "?")),
            ok=bool(item.get("ok", False)),
            detail=str(item.get("detail", "")),
        )
        for item in data.get("checks", [])
    )
    return LabResult(
        passed=bool(data.get("passed", False)),
        score=float(data.get("score", 0.0)),
        checks=checks,
        raw=data,
    )
