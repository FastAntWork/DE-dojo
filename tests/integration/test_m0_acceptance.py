"""Приёмка M0.

Проверяются не отдельные функции, а критерии готовности вехи: на чистой БД
миграции применяются, контент проецируется, повторный прогон ничего не меняет,
а приложение отвечает готовностью. Если этот файл зелёный на пустом томе —
`make up && make migrate && make sync` отработает на чистой машине.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

from dojo.content.loader import load_skills
from dojo.content.sync import sync_skills
from dojo.core.config import Settings
from dojo.core.migrate import find_migrations_dir, migrate
from dojo.web.app import create_app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_SKILLS = 10
EXPECTED_EDGES = 10
EXPECTED_TASKS = 10


async def connect(dsn: str) -> asyncpg.Connection[asyncpg.Record]:
    return await asyncpg.connect(dsn)


async def count(conn: asyncpg.Connection[asyncpg.Record], table: str) -> int:
    value = await conn.fetchval(f"SELECT count(*) FROM {table}")
    return int(value)


class TestMigrations:
    async def test_apply_to_empty_database(self, postgres_dsn: str) -> None:
        applied = await migrate(postgres_dsn)

        assert [m.version for m in applied] == ["001", "002"]

    async def test_second_run_is_noop(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)

        assert await migrate(postgres_dsn) == []

    async def test_schema_has_expected_tables(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)

        conn = await connect(postgres_dsn)
        try:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        finally:
            await conn.close()

        tables = {row["tablename"] for row in rows}
        assert {
            "skills",
            "skill_edges",
            "tasks",
            "attempts",
            "skill_states",
            "reviews_schedule",
            "review_log",
            "job_postings",
            "job_posting_skills",
            "job_skill_stats",
            "sessions",
            "event_outbox",
            "schema_migrations",
        } <= tables

    async def test_migrations_directory_is_discoverable(self) -> None:
        # Раннер ищет каталог вверх по дереву, чтобы работать и из репозитория,
        # и из установленного пакета.
        assert find_migrations_dir().is_dir()


class TestContentSync:
    async def test_projects_whole_graph(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                report = await sync_skills(conn, skills)

            assert report.inserted == EXPECTED_SKILLS
            assert await count(conn, "skills") == EXPECTED_SKILLS
            assert await count(conn, "skill_edges") == EXPECTED_EDGES
            assert await count(conn, "tasks") == EXPECTED_TASKS
            # Строка состояния заводится на каждый живой узел, чтобы
            # планировщику не приходилось различать «не начат» и «строки нет».
            assert await count(conn, "skill_states") == EXPECTED_SKILLS
        finally:
            await conn.close()

    async def test_repeat_changes_nothing(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, skills)
            before = await conn.fetchval("SELECT max(updated_at) FROM skills")

            async with conn.transaction():
                report = await sync_skills(conn, skills)
            after = await conn.fetchval("SELECT max(updated_at) FROM skills")
        finally:
            await conn.close()

        assert report.unchanged == EXPECTED_SKILLS
        assert report.inserted == 0
        assert report.changed is False
        # Самая надёжная проверка идемпотентности: строки физически не трогали.
        assert before == after

    async def test_missing_node_is_deprecated_not_deleted(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)
        # Убираем лист графа: на него никто не ссылается как на предпосылку,
        # иначе sync справедливо упадёт с UnknownPrereqError.
        without_leaf = [s for s in skills if s.id != "testing.unit"]

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, skills)
            async with conn.transaction():
                report = await sync_skills(conn, without_leaf)

            deprecated_at = await conn.fetchval(
                "SELECT deprecated_at FROM skills WHERE id = 'testing.unit'"
            )
            still_there = await count(conn, "skills")
        finally:
            await conn.close()

        assert report.deprecated == 1
        assert deprecated_at is not None
        # Строка осталась: на неё ссылается история попыток.
        assert still_there == EXPECTED_SKILLS

    async def test_returned_node_is_restored(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)
        without_leaf = [s for s in skills if s.id != "testing.unit"]

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, skills)
            async with conn.transaction():
                await sync_skills(conn, without_leaf)
            async with conn.transaction():
                report = await sync_skills(conn, skills)

            deprecated_at = await conn.fetchval(
                "SELECT deprecated_at FROM skills WHERE id = 'testing.unit'"
            )
        finally:
            await conn.close()

        assert report.restored == 1
        assert deprecated_at is None

    async def test_event_written_to_outbox(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)

        conn = await connect(postgres_dsn)
        try:
            await conn.execute("TRUNCATE event_outbox")
            async with conn.transaction():
                await sync_skills(conn, skills)
            row = await conn.fetchrow(
                "SELECT topic, payload FROM event_outbox ORDER BY id DESC LIMIT 1"
            )
        finally:
            await conn.close()

        assert row is not None
        assert row["topic"] == "dojo.content.synced"


class TestReadiness:
    """Проба готовности — единственное, на что смотрит compose и оркестратор."""

    def test_readyz_green_when_dependencies_are_up(self, postgres_dsn: str, redis_url: str) -> None:
        settings = Settings.model_validate({"DATABASE_URL": postgres_dsn, "REDIS_URL": redis_url})

        # Контекстный менеджер TestClient прогоняет lifespan приложения,
        # то есть открывает и закрывает пул ровно как в бою.
        with TestClient(create_app(settings)) as client:
            response = client.get("/readyz")

        assert response.status_code == 200
        assert response.json() == {"ready": True, "checks": {"postgres": True, "redis": True}}

    def test_readyz_red_when_postgres_is_unreachable(self, redis_url: str) -> None:
        settings = Settings.model_validate(
            {
                # Порт, на котором заведомо никого нет.
                "DATABASE_URL": "postgresql://dojo:dojo@127.0.0.1:1/dojo",
                "REDIS_URL": redis_url,
            }
        )

        with TestClient(create_app(settings)) as client:
            response = client.get("/readyz")
            liveness = client.get("/healthz")

        assert response.status_code == 503
        assert response.json()["checks"] == {"postgres": False, "redis": True}
        # Живость при этом зелёная: перезапуск процесса недоступную БД не лечит.
        assert liveness.status_code == 200


class TestGraphIntegrity:
    async def test_every_edge_points_at_existing_skill(self, postgres_dsn: str) -> None:
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, skills)
            orphans = await conn.fetchval(
                """
                SELECT count(*) FROM skill_edges e
                WHERE NOT EXISTS (SELECT 1 FROM skills s WHERE s.id = e.parent_id)
                   OR NOT EXISTS (SELECT 1 FROM skills s WHERE s.id = e.child_id)
                """
            )
        finally:
            await conn.close()

        assert orphans == 0

    async def test_graph_has_roots(self, postgres_dsn: str) -> None:
        """Без узла без предпосылок обучение невозможно начать в принципе."""
        await migrate(postgres_dsn)
        skills = load_skills(REPO_ROOT / "content", REPO_ROOT)

        conn = await connect(postgres_dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, skills)
            roots = await conn.fetch(
                """
                SELECT s.id FROM skills s
                WHERE NOT EXISTS (SELECT 1 FROM skill_edges e WHERE e.child_id = s.id)
                ORDER BY s.id
                """
            )
        finally:
            await conn.close()

        assert [row["id"] for row in roots] == ["python.core", "sql.core.select"]
