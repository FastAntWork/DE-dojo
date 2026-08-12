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
import typer

from dojo_cli.stack import (
    is_port_free,
    preflight_ports,
    read_env,
    retarget_url,
    serve,
    set_env_value,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    lines = [f"{key}={value}" for key, value in ports.items()]
    lines += [
        f"DATABASE_URL=postgresql://dojo:secret@127.0.0.1:{ports['POSTGRES_HOST_PORT']}/dojo",
        f"REDIS_URL=redis://127.0.0.1:{ports['REDIS_HOST_PORT']}/0",
    ]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def busy_port() -> Iterator[int]:
    """Занятый порт, освобождаемый после теста."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield int(sock.getsockname()[1])


class TestEnvFile:
    def test_reads_values(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("REDIS_HOST_PORT=6379\n", encoding="utf-8")

        assert read_env(tmp_path)["REDIS_HOST_PORT"] == "6379"

    def test_ignores_comments_and_blanks(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("# коммент\n\nA=1\n", encoding="utf-8")

        assert read_env(tmp_path) == {"A": "1"}

    def test_updates_existing_key_in_place(self, project: Path) -> None:
        untouched = read_env(project)["POSTGRES_HOST_PORT"]

        set_env_value(project, "REDIS_HOST_PORT", "16380")

        env = read_env(project)
        assert env["REDIS_HOST_PORT"] == "16380"
        # Соседние строки правка одного ключа задевать не должна.
        assert env["POSTGRES_HOST_PORT"] == untouched

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


class TestServeRefusesBusyPort:
    """Регрессия из жизни, обнаруженная при сквозной проверке лаб.

    Занятый порт здесь опаснее, чем кажется: uvicorn не сможет его занять и
    завершится, а отвечать на этом адресе продолжит СТАРЫЙ процесс с прежним
    кодом. Приложение выглядит работающим, но новых узлов и лаб в нём нет —
    и понять это по внешнему виду невозможно.
    """

    def test_exits_with_message_instead_of_silent_start(
        self, busy_port: int, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(typer.Exit) as info:
            serve(port=busy_port, open_window=False)

        assert info.value.exit_code == 1
        output = capsys.readouterr().out
        assert str(busy_port) in output
        assert "занят" in output

    def test_free_port_is_not_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Проверка не должна мешать нормальному запуску.

        Сам uvicorn не поднимаем: тест про решение «запускать или нет», а не
        про сервер.
        """
        started: list[int] = []
        monkeypatch.setattr(
            "uvicorn.run", lambda *_args, **kwargs: started.append(int(kwargs["port"]))
        )

        serve(repo=REPO_ROOT, port=free_port(), open_window=False)

        assert len(started) == 1


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


class TestConnectionStringFollowsPort:
    """Регрессия, найденная на живом стенде и стоившая бы дорого.

    Порт хранилища задан в двух местах: compose публикует его по HOST_PORT, а
    приложение ходит по URL. Первая версия сдвигала только порт публикации — и
    приложение продолжало стучаться на прежний адрес, где сидел ЧУЖОЙ сервер:
    ровно тот, из-за которого порт и оказался занят. Стек при этом
    поднимался, приложение подключалось, всё выглядело работающим, а данные
    уезжали в чужую базу.
    """

    @pytest.mark.parametrize(
        ("port_key", "url_key"),
        [("POSTGRES_HOST_PORT", "DATABASE_URL"), ("REDIS_HOST_PORT", "REDIS_URL")],
    )
    def test_url_moves_with_port(
        self, project: Path, busy_port: int, port_key: str, url_key: str
    ) -> None:
        set_env_value(project, port_key, str(busy_port))

        preflight_ports(project)

        env = read_env(project)
        assert f":{env[port_key]}" in env[url_key], (
            f"{url_key} остался на старом порту: {env[url_key]}"
        )
        assert f":{busy_port}/" not in env[url_key]

    def test_untouched_url_keeps_credentials_and_database(self, project: Path) -> None:
        before = read_env(project)["DATABASE_URL"]

        preflight_ports(project)

        assert read_env(project)["DATABASE_URL"] == before

    def test_retarget_keeps_everything_but_port(self) -> None:
        moved = retarget_url("postgresql://dojo:p%40ss@127.0.0.1:5432/dojo?sslmode=disable", 5433)

        assert moved == "postgresql://dojo:p%40ss@127.0.0.1:5433/dojo?sslmode=disable"
