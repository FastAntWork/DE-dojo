"""Сторож упаковки.

Регрессия, которая уже случилась: корень workspace объявлен виртуальным
(`package = false`) и не ссылался на свои же пакеты. Из-за этого обычный
`uv sync` ставил только зависимости корня, скрипт `dojo` в окружение не
попадал, и на чистой машине запуск падал с «Failed to spawn: dojo».

В WSL это не проявлялось, потому что окружение там давно синхронизировано с
`--all-packages`. Ошибка вылезла только у человека, поставившего проект с
нуля — то есть в единственном сценарии, который и важен для установки.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def root_pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestWorkspaceEntryPoints:
    def test_root_depends_on_cli_package(self, root_pyproject: dict[str, Any]) -> None:
        # Без этой зависимости `uv run dojo` не находит скрипт на чистой машине.
        dependencies = root_pyproject["project"]["dependencies"]

        assert "dojo-cli" in dependencies

    def test_workspace_sources_declared(self, root_pyproject: dict[str, Any]) -> None:
        # Пакеты должны браться из workspace, а не искаться на PyPI, где их нет.
        sources = root_pyproject["tool"]["uv"]["sources"]

        for name in root_pyproject["project"]["dependencies"]:
            assert sources.get(name, {}).get("workspace") is True, name

    def test_cli_declares_dojo_script(self) -> None:
        cli = tomllib.loads(
            (REPO_ROOT / "apps" / "cli" / "pyproject.toml").read_text(encoding="utf-8")
        )

        assert cli["project"]["scripts"]["dojo"] == "dojo_cli.__main__:app"


class TestLauncherScripts:
    """Скрипты запуска — единственное, что человек трогает при установке."""

    def test_windows_launcher_exists(self) -> None:
        assert (REPO_ROOT / "Dojo.cmd").is_file()

    def test_unix_launcher_is_executable(self) -> None:
        launcher = REPO_ROOT / "dojo.sh"

        assert launcher.is_file()
        assert launcher.stat().st_mode & 0o111, "dojo.sh должен быть исполняемым"

    @pytest.mark.parametrize("name", ["Dojo.cmd", "dojo.sh"])
    def test_launcher_uses_short_command(self, name: str) -> None:
        # Длинная форма `uv run --package dojo-cli dojo` в лаунчере означала бы,
        # что корневая зависимость снова сломана, а симптом замаскирован.
        text = (REPO_ROOT / name).read_text(encoding="utf-8")

        assert "uv run dojo start" in text
        assert "--package" not in text
