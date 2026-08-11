"""Команды `dojo` для управления стеком.

Существуют ради переносимости. Makefile прекрасен на Linux и macOS, но на
Windows его нет из коробки, а ставить ради него MSYS или choco — навязывать
пользователю лишний инструмент. Python в системе уже есть: он нужен самому
Dojo. Поэтому оркестрация живёт здесь, а Makefile остаётся тонкой обёрткой.

Свободная память проверяется через psutil, а не чтением /proc/meminfo:
последнего нет ни на Windows, ни на macOS.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Final

import httpx
import psutil
import typer
from rich.console import Console

app = typer.Typer(help="Управление стеком.", no_args_is_help=True)
console = Console()

GIB: Final = 1024**3

# Сколько памяти нужно профилю сверх уже занятой, в мегабайтах.
PROFILE_MEMORY_MB: Final[dict[str, int]] = {
    "": 2300,
    "ai": 9400,
    "analytics": 5900,
    "storage": 3400,
    "full": 11800,
}

ALL_PROFILES: Final = ("ai", "analytics", "storage")


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "docker-compose.yml").is_file():
            return candidate
    msg = "docker-compose.yml не найден — запусти команду из каталога проекта"
    raise typer.BadParameter(msg)


def profile_args(profile: str) -> list[str]:
    if profile == "full":
        return [arg for name in ALL_PROFILES for arg in ("--profile", name)]
    if not profile:
        return []
    return ["--profile", profile]


def compose(
    root: Path, args: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        console.print("[red]docker не найден.[/red] Установи Docker Desktop или docker-ce.")
        raise typer.Exit(code=2)

    # Аргументы формируются кодом, пользовательский ввод сюда не попадает.
    return subprocess.run(  # noqa: S603
        ["docker", "compose", *args],  # noqa: S607
        cwd=root,
        text=True,
        capture_output=capture,
        check=False,
    )


def warn_if_low_memory(profile: str) -> None:
    needed = PROFILE_MEMORY_MB.get(profile, PROFILE_MEMORY_MB[""])
    available = int(psutil.virtual_memory().available / 1024 / 1024)
    if available >= needed:
        return
    console.print(
        f"\n  [yellow]Свободно {available} МБ, профилю нужно ~{needed} МБ.[/yellow]\n"
        "  Стек может уйти в своп или получить OOM.\n"
        "  Погаси лишние профили или увеличь память, отданную Docker.\n"
    )


def ensure_env(root: Path) -> None:
    env = root / ".env"
    example = root / ".env.example"
    if env.exists() or not example.is_file():
        return
    env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    console.print("  создан .env из .env.example")


@app.command("up")
def up(
    profile: Annotated[str, typer.Option(help="ai | analytics | storage | full")] = "",
    repo: Annotated[Path | None, typer.Option(help="Корень проекта.")] = None,
) -> None:
    """Поднять контейнеры."""
    root = repo.resolve() if repo else find_repo_root()
    ensure_env(root)
    warn_if_low_memory(profile)

    # --build обязателен: без него правка кода не попадает в контейнер.
    result = compose(root, [*profile_args(profile), "up", "-d", "--build", "--quiet-pull"])
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
    ps(repo=root)


@app.command("down")
def down(repo: Annotated[Path | None, typer.Option()] = None) -> None:
    """Остановить и удалить контейнеры. Тома остаются."""
    root = repo.resolve() if repo else find_repo_root()
    compose(root, [*profile_args("full"), "down"])


@app.command("ps")
def ps(repo: Annotated[Path | None, typer.Option()] = None) -> None:
    """Что сейчас запущено."""
    root = repo.resolve() if repo else find_repo_root()
    compose(
        root, [*profile_args("full"), "ps", "--format", "table {{.Name}}\t{{.State}}\t{{.Status}}"]
    )


@app.command("logs")
def logs(
    service: Annotated[str, typer.Argument(help="Имя сервиса, например api")] = "",
    repo: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Хвост логов."""
    root = repo.resolve() if repo else find_repo_root()
    args = [*profile_args("full"), "logs", "-f", "--tail", "100"]
    if service:
        args.append(service)
    compose(root, args)


@app.command("start")
def start(
    profile: Annotated[str, typer.Option(help="ai | analytics | storage | full")] = "",
    repo: Annotated[Path | None, typer.Option()] = None,
    port: Annotated[int, typer.Option(help="Порт API на хосте.")] = 8000,
    open_window: Annotated[
        bool, typer.Option("--app/--no-app", help="Открыть отдельным окном по готовности.")
    ] = True,
) -> None:
    """Поднять стек, дождаться готовности, применить миграции и залить контент.

    Одна команда вместо четырёх: именно её человек выполняет каждый день.
    """
    root = repo.resolve() if repo else find_repo_root()
    up(profile=profile, repo=root)

    base = f"http://127.0.0.1:{port}"
    console.print("\n  жду готовности API", end="")
    for _ in range(60):
        try:
            if httpx.get(f"{base}/readyz", timeout=2).status_code == 200:
                console.print(" — [green]готов[/green]")
                break
        except httpx.HTTPError:
            pass
        console.print(".", end="")
        time.sleep(2)
    else:
        console.print("\n  [red]API не поднялся за две минуты.[/red] Смотри: dojo stack logs api")
        raise typer.Exit(code=1)

    from dojo_cli import content as content_cmd

    console.print()
    _migrate()
    content_cmd.sync(dry_run=False, database_url=None, repo=root)

    console.print(f"\n  [bold]Готово.[/bold] Приложение: {base}")
    console.print(f"  Документация API: {base}/docs")

    if open_window:
        from dojo_cli.desktop import launch

        launch(base)


def _migrate() -> None:
    import asyncio

    from dojo.core.config import get_settings
    from dojo.core.migrate import migrate

    applied = asyncio.run(migrate(get_settings().database_url))
    console.print(f"  миграции: применено {len(applied)}")


@app.command("app")
def open_app(
    port: Annotated[int, typer.Option(help="Порт API на хосте.")] = 8000,
) -> None:
    """Открыть Dojo отдельным окном, без вкладок и адресной строки."""
    from dojo_cli.desktop import launch

    url = f"http://127.0.0.1:{port}"
    if launch(url):
        console.print(f"  [green]Окно открыто:[/green] {url}")
    console.print(
        "  [dim]Совет: в самом окне можно нажать «Установить» — тогда Dojo\n"
        "  появится в меню «Пуск» и будет запускаться без этой команды.[/dim]"
    )


@app.command("stop")
def stop(repo: Annotated[Path | None, typer.Option()] = None) -> None:
    """Остановить контейнеры, не удаляя их."""
    root = repo.resolve() if repo else find_repo_root()
    compose(root, [*profile_args("full"), "stop"])


@app.command("info")
def info() -> None:
    """Где мы и чем работаем — для отчётов об ошибках."""
    console.print(f"  платформа:  {sys.platform}")
    console.print(f"  python:     {sys.version.split()[0]}")
    console.print(f"  память:     {psutil.virtual_memory().total / GIB:.1f} ГиБ")
    docker = shutil.which("docker")
    console.print(f"  docker:     {docker or 'не найден'}")
    if docker:
        result = compose(Path.cwd(), ["version", "--short"], capture=True)
        console.print(f"  compose:    {result.stdout.strip() or 'неизвестно'}")
