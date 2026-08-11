"""Подготовка учебных датасетов.

Каждый датасет живёт в СВОЕЙ базе данных, а не в схеме рабочей. Причина
простая: чужой SQL выполняется на нём, и цена ошибки должна быть нулевой.
Испорченный датасет восстанавливается пересозданием за секунду, а рабочая
база с историей обучения к этому не располагает.

Датасет перезагружается, когда меняются его файлы: состояние отслеживается
по контрольной сумме, а не по факту существования таблиц. Иначе правка
seed.sql молча не доехала бы до занимающегося.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from dojo.core.logging import get_logger

logger = get_logger(__name__)

# Имена баз строятся из имени датасета, поэтому оно обязано быть безобидным.
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")

META_TABLE = "_dojo_dataset"


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    schema_sql: str
    seed_sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256((self.schema_sql + self.seed_sql).encode("utf-8")).hexdigest()

    @property
    def database(self) -> str:
        # Дефис в имени базы потребовал бы кавычек в каждом обращении.
        return f"dojo_ds_{self.name.replace('-', '_')}"


def load_dataset(content_root: Path, name: str) -> Dataset:
    if not SAFE_NAME.match(name):
        msg = f"недопустимое имя датасета: {name!r}"
        raise DatasetError(msg)

    directory = content_root / "sql" / "datasets" / name
    schema = directory / "schema.sql"
    seed = directory / "seed.sql"
    if not schema.is_file() or not seed.is_file():
        msg = f"датасет {name}: нужны schema.sql и seed.sql в {directory}"
        raise DatasetError(msg)

    return Dataset(
        name=name,
        schema_sql=schema.read_text(encoding="utf-8"),
        seed_sql=seed.read_text(encoding="utf-8"),
    )


def dataset_dsn(base_dsn: str, database: str) -> str:
    parts = urlsplit(base_dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def ensure_dataset(base_dsn: str, dataset: Dataset) -> str:
    """Создаёт базу датасета и загружает данные, если нужно. Возвращает DSN."""
    await _ensure_database(base_dsn, dataset.database)
    dsn = dataset_dsn(base_dsn, dataset.database)

    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn)
    try:
        current = await _stored_checksum(conn)
        if current == dataset.checksum:
            return dsn

        logger.info(
            "dataset.loading", dataset=dataset.name, reason="изменился" if current else "новый"
        )
        # Полная пересборка вместо инкрементальных правок: датасет маленький,
        # а частичное обновление рано или поздно разъедется с файлами.
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await conn.execute(dataset.schema_sql)
        await conn.execute(dataset.seed_sql)
        # Имя таблицы литералом, а не подстановкой: так очевидно, что никакой
        # склейки запроса из переменных здесь нет.
        await conn.execute(
            "CREATE TABLE _dojo_dataset ("
            "  checksum  text NOT NULL,"
            "  loaded_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )
        await conn.execute("INSERT INTO _dojo_dataset (checksum) VALUES ($1)", dataset.checksum)
        logger.info("dataset.loaded", dataset=dataset.name)
    finally:
        await conn.close()

    return dsn


async def _ensure_database(base_dsn: str, database: str) -> None:
    parts = urlsplit(base_dsn)
    admin_dsn = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))

    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if not exists:
            # CREATE DATABASE нельзя выполнить внутри транзакции, а имя нельзя
            # передать параметром — поэтому оно проверено регуляркой выше.
            await conn.execute(f'CREATE DATABASE "{database}"')
            logger.info("dataset.database.created", database=database)
    finally:
        await conn.close()


async def _stored_checksum(conn: asyncpg.Connection[asyncpg.Record]) -> str | None:
    exists = await conn.fetchval("SELECT to_regclass($1)", META_TABLE)
    if exists is None:
        return None
    value = await conn.fetchval("SELECT checksum FROM _dojo_dataset LIMIT 1")
    return str(value) if value is not None else None
