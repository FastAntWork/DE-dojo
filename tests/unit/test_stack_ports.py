"""Тесты подбора портов.

Регрессия из жизни: у человека одновременно работали docker-ce в WSL и Docker
Desktop на Windows, оба пытались занять 6379, и запуск падал с сообщением
«Only one usage of each socket address», по которому непонятно ни кто держит
порт, ни что делать.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from dojo_cli.stack import is_port_free, preflight_ports, read_env, set_env_value


def free_port() -> int:
    """Порт, который сейчас свободен."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Проект со ЗАВЕДОМО свободными портами.

    Значения по умолчанию (5432, 6379, 8000) сюда не годятся: на машине
    разработчика они почти наверняка заняты своим же поднятым стеком, и тест
    падал бы в зависимости от того, запущено ли приложение. Тест обязан
    проверять логику, а не состояние окружения.
    """
    ports = {
        "POSTGRES_HOST_PORT": free_port(),
        "REDIS_HOST_PORT": free_port(),
        "API_HOST_PORT": free_port(),
    }
    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in ports.items()), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def busy_port() -> Iterator[int]:
    """Занятый порт, освобождаемый после теста."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield int(sock.getsockname()[1])


class TestEnvFile:
    def test_reads_values(self, project: Path) -> None:
        assert read_env(project)["REDIS_HOST_PORT"] == "6379"

    def test_ignores_comments_and_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("# коммент\n\nA=1\n", encoding="utf-8")

        assert read_env(tmp_path) == {"A": "1"}

    def test_updates_existing_key_in_place(self, project: Path) -> None:
        set_env_value(project, "REDIS_HOST_PORT", "6380")

        env = read_env(project)
        assert env["REDIS_HOST_PORT"] == "6380"
        # Остальные значения не должны пострадать.
        assert env["POSTGRES_HOST_PORT"] == "5432"

    def test_appends_missing_key(self, project: Path) -> None:
        set_env_value(project, "NEW_KEY", "42")

        assert read_env(project)["NEW_KEY"] == "42"


class TestPortProbe:
    def test_busy_port_detected(self, busy_port: int) -> None:
        assert is_port_free(busy_port) is False

    def test_free_port_detected(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        assert is_port_free(port) is True


class TestPreflight:
    def test_keeps_ports_when_free(self, project: Path) -> None:
        before = read_env(project)

        api_port = preflight_ports(project)

        assert str(api_port) == before["API_HOST_PORT"]
        assert read_env(project) == before, "свободные порты трогать нельзя"

    def test_moves_api_off_busy_port_and_returns_new_one(
        self, project: Path, busy_port: int
    ) -> None:
        set_env_value(project, "API_HOST_PORT", str(busy_port))

        api_port = preflight_ports(project)

        # Возвращённый порт обязан совпадать с записанным в .env: окно
        # открывается именно по нему, и разойтись они не должны.
        assert api_port != busy_port
        assert str(api_port) == read_env(project)["API_HOST_PORT"]
        assert is_port_free(api_port)

    def test_moves_dependency_off_busy_port(self, project: Path, busy_port: int) -> None:
        set_env_value(project, "REDIS_HOST_PORT", str(busy_port))

        preflight_ports(project)

        assert read_env(project)["REDIS_HOST_PORT"] != str(busy_port)
