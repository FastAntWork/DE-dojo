"""Лаба в интерфейсе: сквозной путь от брифа до разбора.

Главное, что здесь проверяется, — **разбор не утекает раньше времени**. Это не
косметика: лаба, чей ответ лежит в исходном коде страницы, перестаёт быть
задачей, а человек об этом даже не узнает — он просто увидит подсказку в
инструментах разработчика и решит, что так и надо.

Остальное — что страница вообще собирается, что стенд поднимается через кнопку,
что проверка честно отвечает «не сходится» на нетронутом стенде и «сдано» после
починки, и что результат попадает в историю попыток.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

from dojo.content.loader import load_skills
from dojo.content.sync import sync_skills
from dojo.core.config import Settings
from dojo.core.migrate import migrate
from dojo.web.app import create_app

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

SKILL = "sql.query-plans"
LAB_URL = f"/skills/{SKILL}/lab"

# Кусок разбора, которого не должно быть в ответе до сдачи. Взят из solution.md
# намеренно дословно: если разбор перепишут, а утечка появится, тест это
# заметит только пока строка совпадает — поэтому строка выбрана из той части,
# которая объясняет суть, а не из вводных фраз.
SOLUTION_MARKER = "Разбор"
SOLUTION_TEXT = "VACUUM ANALYZE events"


@pytest.fixture
def client(postgres_dsn: str, redis_url: str) -> TestClient:
    settings = Settings.model_validate({"DATABASE_URL": postgres_dsn, "REDIS_URL": redis_url})
    return TestClient(create_app(settings))


def dsn_from_panel(html: str) -> str:
    match = re.search(r"postgres(?:ql)?://[^\s<]+", html)
    assert match, f"в панели нет строки подключения: {html[:400]}"
    return match.group(0)


class TestLabPage:
    def test_brief_is_shown_without_solution(self, client: TestClient) -> None:
        with client:
            page = client.get(LAB_URL)

        assert page.status_code == 200
        assert "Инцидент" in page.text
        # Разбора нет ни в каком виде: ни в свёрнутом блоке, ни скрытым стилями.
        assert SOLUTION_TEXT not in page.text
        assert "Стенд ещё не поднят" in page.text

    def test_unknown_lab_is_404(self, client: TestClient) -> None:
        with client:
            assert client.get("/skills/python.core/lab").status_code == 404


class TestLabFlow:
    def test_start_check_fix_check(self, client: TestClient, postgres_dsn: str) -> None:
        """Полный путь. Один тест, потому что шаги связаны состоянием стенда."""
        with client:
            started = client.post(LAB_URL + "/start", data={"variant": "1"})
            assert started.status_code == 200
            assert "Стенд поднят" in started.text
            lab_dsn = dsn_from_panel(started.text)

            failed = client.post(LAB_URL + "/check")
            assert failed.status_code == 200
            assert "Ещё не сходится" in failed.text
            assert SOLUTION_TEXT not in failed.text, "разбор показан при незачёте"

            fix_index(lab_dsn)

            passed = client.post(LAB_URL + "/check")
            assert passed.status_code == 200, passed.text[:400]
            assert "Лаба сдана" in passed.text
            # А вот теперь разбор обязан появиться.
            assert SOLUTION_MARKER in passed.text
            assert SOLUTION_TEXT in passed.text

    def test_check_without_stand_explains_itself(self, client: TestClient) -> None:
        with client:
            response = client.post(LAB_URL + "/check")

        assert response.status_code == 200
        assert "Стенд не поднят" in response.text

    def test_restart_returns_stand_to_broken_state(self, client: TestClient) -> None:
        with client:
            started = client.post(LAB_URL + "/start", data={"variant": "1"})
            lab_dsn = dsn_from_panel(started.text)

            fix_index(lab_dsn)
            assert "Лаба сдана" in client.post(LAB_URL + "/check").text

            client.post(LAB_URL + "/start", data={"variant": "1"})

            assert "Ещё не сходится" in client.post(LAB_URL + "/check").text

    def test_variant_out_of_range_is_clamped(self, client: TestClient) -> None:
        """Число из формы приходит от кого угодно — оно не должно ронять сервер."""
        with client:
            response = client.post(LAB_URL + "/start", data={"variant": "999"})

        assert response.status_code == 200
        assert "Стенд поднят" in response.text


class TestHints:
    def test_hint_is_hidden_until_asked(self, client: TestClient) -> None:
        with client:
            started = client.post(LAB_URL + "/start", data={"variant": "1"})
            assert "EXPLAIN (ANALYZE, BUFFERS)" not in started.text

            opened = client.post(LAB_URL + "/hint")

        assert "EXPLAIN (ANALYZE, BUFFERS)" in opened.text
        assert "Уровень 1" in opened.text

    def test_hints_run_out(self, client: TestClient) -> None:
        with client:
            client.post(LAB_URL + "/start", data={"variant": "1"})
            for _ in range(5):
                last = client.post(LAB_URL + "/hint")

        assert "Все подсказки открыты" in last.text


class TestAttemptRecorded:
    def test_passed_lab_lands_in_history(self, client: TestClient, postgres_dsn: str) -> None:
        prepare_database(postgres_dsn)

        with client:
            started = client.post(LAB_URL + "/start", data={"variant": "1"})
            lab_dsn = dsn_from_panel(started.text)
            fix_index(lab_dsn)
            client.post(LAB_URL + "/check")

            progress = client.get("/progress")

        rows = attempts(postgres_dsn)
        assert len(rows) == 1
        assert rows[0]["skill_id"] == SKILL
        assert rows[0]["status"] == "passed"
        assert progress.status_code == 200

    def test_hint_reduces_score(self, client: TestClient, postgres_dsn: str) -> None:
        """Подсказка стоит баллов — иначе она бесплатна и берётся не думая."""
        prepare_database(postgres_dsn)

        with client:
            started = client.post(LAB_URL + "/start", data={"variant": "1"})
            lab_dsn = dsn_from_panel(started.text)
            client.post(LAB_URL + "/hint")
            fix_index(lab_dsn)
            client.post(LAB_URL + "/check")

        rows = attempts(postgres_dsn)
        assert rows[0]["hints_used"] == 1
        # score в базе numeric, а значит Decimal: сравнивать с float напрямую нельзя.
        assert float(rows[0]["score"]) == pytest.approx(0.9), "подсказка 1 уровня стоит 10%"


# ── Синхронные помощники ─────────────────────────────────────────────────────
#
# Тесты синхронные: TestClient крутит собственный цикл событий, и работать с
# asyncpg из того же теста напрямую нельзя. Поэтому каждый поход в базу
# выполняется в своём коротком цикле.


def sql(dsn: str, statement: str) -> list[asyncpg.Record]:
    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(dsn)
        try:
            return list(await conn.fetch(statement))
        finally:
            await conn.close()

    return asyncio.run(go())


def fix_index(dsn: str) -> None:
    """Починка варианта 1 из разбора."""
    sql(dsn, "CREATE INDEX events_tenant_created_idx ON events (tenant_id, created_at)")


def attempts(dsn: str) -> list[asyncpg.Record]:
    return sql(dsn, "SELECT skill_id, status, score, hints_used FROM attempts ORDER BY id")


def prepare_database(dsn: str) -> None:
    """Миграции и проекция контента — то же, что делает `dojo migrate && sync`.

    Синхронизация обязательна: попытка ссылается на задачу внешним ключом, а
    строки задач появляются в базе именно из контента. Без неё попытка не
    запишется — приложение это переживает и сдачу не роняет, но история
    останется пустой.
    """

    async def go() -> None:
        await migrate(dsn)
        conn = await asyncpg.connect(dsn)
        try:
            async with conn.transaction():
                await sync_skills(conn, load_skills(REPO_ROOT / "content", REPO_ROOT))
        finally:
            await conn.close()

    asyncio.run(go())
