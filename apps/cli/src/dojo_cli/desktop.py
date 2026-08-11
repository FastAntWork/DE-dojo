"""Запуск Dojo отдельным окном, а не вкладкой браузера.

Используется режим `--app=URL`, который есть у всех браузеров на движке
Chromium: окно без адресной строки, без вкладок и со своей записью в панели
задач. Отдельного рантайма это не требует — ни Electron, ни Tauri, ни Node.

Второй путь к тому же результату — установить приложение из самого браузера
(манифест лежит в static/manifest.webmanifest). Тогда оно появится в меню
«Пуск» и будет запускаться без этой команды вовсе.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Final

from rich.console import Console

console = Console()

# Порядок важен: сначала то, что почти наверняка стоит в системе.
CHROMIUM_BROWSERS: Final[dict[str, tuple[str, ...]]] = {
    "Windows": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
    "Darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ),
    "Linux": (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
    ),
}


def find_chromium() -> str | None:
    """Путь к браузеру на движке Chromium или None."""
    for candidate in CHROMIUM_BROWSERS.get(platform.system(), ()):
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def find_windows_browser_from_wsl() -> str | None:
    """Браузер Windows, видимый из WSL через /mnt/c.

    Из WSL открывать надо именно виндовый браузер: линуксового в дистрибутиве
    обычно нет, а окно всё равно должно появиться на рабочем столе Windows.
    """
    for candidate in CHROMIUM_BROWSERS["Windows"]:
        path = "/mnt/" + candidate[0].lower() + candidate[2:].replace("\\", "/")
        if Path(path).is_file():
            return path
    return None


def launch(url: str) -> bool:
    """Открывает URL отдельным окном. True, если получилось именно окно."""
    browser = find_windows_browser_from_wsl() if is_wsl() else find_chromium()

    if browser is None:
        console.print(
            "  [yellow]Браузер на движке Chromium не найден.[/yellow]\n"
            "  Открываю в браузере по умолчанию — это будет обычная вкладка."
        )
        webbrowser.open(url)
        return False

    # Профиль отдельный: иначе окно наследует расширения и вкладки основного
    # браузера, и «приложение» ведёт себя как ещё одно его окно.
    profile = Path("~").expanduser() / ".dojo" / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)

    # Аргументы формируются кодом, пользовательский ввод сюда не попадает.
    subprocess.Popen(  # noqa: S603
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True
