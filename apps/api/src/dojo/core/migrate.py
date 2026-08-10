"""Раннер SQL-миграций.

Почему свой, а не alembic: миграции здесь пишутся руками на чистом SQL —
это учебный материал, и autogenerate, ради которого alembic обычно и берут,
тут не нужен. Оставшаяся часть alembic — «применить файлы по порядку и
запомнить, что применено» — умещается в этот модуль, зато не тянет
SQLAlchemy в проект, где ORM сознательно не используется. См. docs/adr/0003.

Гарантии:

* **Порядок.** Файлы применяются по возрастанию числового префикса.
* **Атомарность.** Каждая миграция идёт в своей транзакции. Файл, который
  нельзя выполнить в транзакции (CREATE INDEX CONCURRENTLY), помечается
  строкой `-- dojo:no-transaction` в первых строках.
* **Единственность.** Advisory-lock не даёт двум процессам мигрировать
  одновременно — иначе параллельный `make up` и CI подрались бы за схему.
* **Неизменность применённого.** Хранится контрольная сумма; правка уже
  применённого файла роняет запуск, а не тихо расходится с БД.
* **Без дыр.** Миграция с номером меньше максимального применённого — ошибка:
  так ловится ветка, отставшая от main.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

logger = logging.getLogger("dojo.migrate")

# Произвольная, но фиксированная константа: важно лишь, чтобы все процессы
# Dojo брали один и тот же ключ.
ADVISORY_LOCK_KEY = 4_242_424_242

FILENAME_RE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")
NO_TRANSACTION_MARKER = "-- dojo:no-transaction"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Базовая ошибка миграций."""


class ChecksumMismatchError(MigrationError):
    """Применённая миграция изменилась на диске."""


class OutOfOrderError(MigrationError):
    """Появилась миграция с номером ниже уже применённого."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str
    transactional: bool


def _normalize(text: str) -> str:
    """Приводит текст к виду, устойчивому к косметическим правкам.

    Контрольная сумма считается по нормализованному тексту: иначе хук
    trailing-whitespace, прошедший по репозиторию, сломал бы сверку для
    миграций, применённых месяц назад.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


def find_migrations_dir(start: Path | None = None) -> Path:
    """Ищет `migrations/versions`, поднимаясь вверх по дереву.

    Так модуль работает и из репозитория, и из установленного пакета, куда
    каталог миграций смонтирован рядом.
    """
    origin = (start or Path(__file__).resolve()).parent
    for candidate in [origin, *origin.parents]:
        found = candidate / "migrations" / "versions"
        if found.is_dir():
            return found
    msg = f"каталог migrations/versions не найден вверх от {origin}"
    raise FileNotFoundError(msg)


def load_migrations(directory: Path) -> list[Migration]:
    """Читает и валидирует все файлы миграций из каталога."""
    migrations: list[Migration] = []
    seen: dict[str, Path] = {}

    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if match is None:
            msg = (
                f"{path.name}: имя не по формату. Ожидается NNN_snake_case.sql, "
                f"например 002_add_lab_variants.sql"
            )
            raise MigrationError(msg)

        version = match["version"]
        if version in seen:
            msg = f"дублирующийся номер {version}: {seen[version].name} и {path.name}"
            raise MigrationError(msg)
        seen[version] = path

        raw = path.read_text(encoding="utf-8")
        normalized = _normalize(raw)
        head = "\n".join(normalized.splitlines()[:5])

        migrations.append(
            Migration(
                version=version,
                name=match["name"],
                path=path,
                sql=normalized,
                checksum=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                transactional=NO_TRANSACTION_MARKER not in head,
            )
        )

    migrations.sort(key=lambda m: int(m.version))
    return migrations


async def _fetch_applied(conn: asyncpg.Connection[asyncpg.Record]) -> dict[str, str]:
    rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
    return {row["version"]: row["checksum"] for row in rows}


def select_pending(
    migrations: list[Migration],
    applied: dict[str, str],
) -> list[Migration]:
    """Отбирает неприменённые, попутно проверяя целостность истории."""
    for migration in migrations:
        known = applied.get(migration.version)
        if known is not None and known != migration.checksum:
            msg = (
                f"{migration.path.name} изменился после применения "
                f"(в БД {known[:12]}…, на диске {migration.checksum[:12]}…). "
                f"Применённые миграции править нельзя — добавь новую."
            )
            raise ChecksumMismatchError(msg)

    pending = [m for m in migrations if m.version not in applied]
    if applied and pending:
        highest_applied = max(int(v) for v in applied)
        stragglers = [m for m in pending if int(m.version) < highest_applied]
        if stragglers:
            names = ", ".join(m.path.name for m in stragglers)
            msg = (
                f"миграции с номером ниже применённого {highest_applied:03d}: {names}. "
                f"Скорее всего ветка отстала от main — перенумеруй файлы."
            )
            raise OutOfOrderError(msg)
    return pending


async def _apply_one(conn: asyncpg.Connection[asyncpg.Record], migration: Migration) -> int:
    """Применяет одну миграцию и возвращает длительность в миллисекундах."""
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def body() -> None:
        await conn.execute(migration.sql)

    if migration.transactional:
        async with conn.transaction():
            await body()
    else:
        # Файл сам отвечает за свою атомарность.
        await body()

    elapsed_ms = int((loop.time() - started) * 1000)

    await conn.execute(
        """
        INSERT INTO schema_migrations (version, name, checksum, duration_ms)
        VALUES ($1, $2, $3, $4)
        """,
        migration.version,
        migration.name,
        migration.checksum,
        elapsed_ms,
    )
    return elapsed_ms


async def migrate(
    database_url: str,
    *,
    directory: Path | None = None,
    dry_run: bool = False,
) -> list[Migration]:
    """Применяет все неприменённые миграции. Возвращает применённое."""
    directory = directory or find_migrations_dir()
    migrations = load_migrations(directory)
    logger.info("найдено миграций: %d в %s", len(migrations), directory)

    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        # Блокировка сессионная: держится до закрытия соединения, поэтому
        # упавший процесс не оставляет схему заблокированной навсегда.
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)

        applied = await _fetch_applied(conn)
        pending = select_pending(migrations, applied)

        if not pending:
            logger.info("нечего применять, схема на версии %s", max(applied, default="—"))
            return []

        if dry_run:
            for migration in pending:
                logger.info("[dry-run] применил бы %s_%s", migration.version, migration.name)
            return pending

        for migration in pending:
            elapsed = await _apply_one(conn, migration)
            logger.info("применено %s_%s за %d мс", migration.version, migration.name, elapsed)

        return pending
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Применяет SQL-миграции DE Dojo.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="DSN Postgres. По умолчанию берётся из настроек (DATABASE_URL).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что было бы применено, и ничего не менять.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    database_url = args.database_url
    if database_url is None:
        from dojo.core.config import get_settings

        database_url = get_settings().database_url

    try:
        applied = asyncio.run(migrate(database_url, dry_run=args.dry_run))
    except MigrationError:
        logger.exception("миграции не применены")
        return 1
    except OSError:
        logger.exception("не удалось подключиться к Postgres")
        return 2

    logger.info("готово, применено миграций: %d", len(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
