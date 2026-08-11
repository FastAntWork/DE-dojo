"""Команды `dojo` для управления стеком.

Существуют ради переносимости. Makefile прекрасен на Linux и macOS, но на
Windows его нет из коробки, а ставить ради него MSYS или choco — навязывать
пользователю лишний инструмент. Python в системе уже есть: он нужен самому
Dojo. Поэтому оркестрация живёт здесь, а Makefile остаётся тонкой обёрткой.

Свободная память проверяется через psutil, а не чтением /proc/meminfo:
последнего нет ни на Windows, ни на macOS.
"""

from __future__ import annotations

import os
import shutil
import socket
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


def docker_available() -> tuple[bool, str]:
    """Работает ли докер. Возвращает (готов, причина).

    Проверяется именно демон, а не наличие бинаря: на Windows CLI ставится
    вместе с Docker Desktop и остаётся на месте, даже когда сам Desktop не
    запущен. Ошибка при этом выглядит как «cannot find the file specified»
    про именованный канал — по ней невозможно догадаться, что надо просто
    открыть Docker Desktop.
    """
    if shutil.which("docker") is None:
        return False, "docker не установлен"

    try:
        # Аргументы фиксированные, ввода извне здесь нет — поэтому S603 тут
        # не срабатывает и подавлять его не нужно, в отличие от compose(),
        # куда аргументы приходят переменной.
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "docker не отвечает"

    if result.returncode != 0:
        if "permission denied" in result.stderr.lower():
            return False, "нет прав на docker: добавь себя в группу docker"
        return False, "демон docker не запущен"
    return True, result.stdout.strip()


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


# Порты, которые стек публикует на хосте, и переменные, которыми они задаются.
HOST_PORTS: Final[dict[str, tuple[int, str]]] = {
    "POSTGRES_HOST_PORT": (5432, "postgres"),
    "REDIS_HOST_PORT": (6379, "redis"),
    "API_HOST_PORT": (8000, "api"),
}


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def read_env(root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    path = root / ".env"
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


def set_env_value(root: Path, key: str, value: str) -> None:
    path = root / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def preflight_ports(root: Path) -> int:
    """Освобождает стеку порты, подбирая свободные вместо занятых.

    Типичная причина конфликта — второй докер. У человека может одновременно
    работать docker-ce внутри WSL и Docker Desktop на Windows, и оба пытаются
    занять 5432 или 6379. Сообщение самого докера при этом говорит про
    «Only one usage of each socket address», по которому непонятно ни кто
    держит порт, ни что с этим делать.

    Возвращает порт API — он нужен, чтобы открыть окно по верному адресу.
    """
    env = read_env(root)
    api_port = int(env.get("API_HOST_PORT", HOST_PORTS["API_HOST_PORT"][0]))

    for key, (default, service) in HOST_PORTS.items():
        current = int(env.get(key, default))
        if is_port_free(current):
            if key == "API_HOST_PORT":
                api_port = current
            continue

        # Ищем ближайший свободный, но не бесконечно: если занято двадцать
        # портов подряд, дело не в конфликте, а в чём-то посерьёзнее.
        chosen = next((p for p in range(current + 1, current + 21) if is_port_free(p)), None)
        if chosen is None:
            console.print(f"  [red]Порт {current} занят, свободного рядом нет ({service}).[/red]")
            raise typer.Exit(code=1)

        console.print(
            f"  [yellow]Порт {current} занят[/yellow] — {service} переезжает на {chosen}. "
            "Чаще всего это второй докер: проверь, не поднят ли стек в WSL."
        )
        set_env_value(root, key, str(chosen))
        if key == "API_HOST_PORT":
            api_port = chosen

    return api_port


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
    preflight_ports(root)

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

    Если докера нет, приложение всё равно запускается — без него недоступна
    только история прогресса. Останавливать человека на пороге из-за того,
    что он ещё не поставил Docker Desktop, было бы худшим решением: теория и
    квизы не требуют ни одного контейнера.
    """
    root = repo.resolve() if repo else find_repo_root()

    ready, reason = docker_available()
    if not ready:
        console.print(f"\n  [yellow]Docker недоступен: {reason}.[/yellow]")
        console.print("  Запускаю в ограниченном режиме — теория и квизы работают,")
        console.print("  прогресс не сохраняется. Что делать, написано в самом приложении.\n")
        serve(repo=root, port=port, open_window=open_window)
        return

    ensure_env(root)
    # Порт мог переехать из-за конфликта, поэтому окно открываем по тому
    # адресу, который стек действительно занял, а не по значению по умолчанию.
    port = preflight_ports(root)
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


@app.command("serve")
def serve(
    repo: Annotated[Path | None, typer.Option()] = None,
    port: Annotated[int, typer.Option(help="Порт, на котором слушать.")] = 8000,
    open_window: Annotated[bool, typer.Option("--app/--no-app")] = True,
) -> None:
    """Запустить приложение прямо здесь, без контейнеров.

    Нужно в двух случаях: когда докера ещё нет и когда правишь код — тогда
    не приходится пересобирать образ на каждое изменение.
    """
    import threading

    import uvicorn

    root = repo.resolve() if repo else find_repo_root()
    os.environ.setdefault("CONTENT_DIR", str(root / "content"))

    console.print(f"  Приложение: [bold]http://127.0.0.1:{port}[/bold]")
    console.print("  [dim]остановить — Ctrl+C[/dim]\n")

    if open_window:
        # Окно открывается с задержкой: сервер должен успеть занять порт,
        # иначе браузер покажет ошибку соединения и человек решит, что сломано.
        def open_later() -> None:
            time.sleep(2.5)
            from dojo_cli.desktop import launch

            launch(f"http://127.0.0.1:{port}")

        threading.Thread(target=open_later, daemon=True).start()

    uvicorn.run("dojo.web.app:app", host="127.0.0.1", port=port, log_level="warning")


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
