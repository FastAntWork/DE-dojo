@echo off
rem Запуск DE Dojo двойным кликом на Windows.
rem
rem Делает то же, что `uv run dojo start`: поднимает контейнеры, ждёт
rem готовности, применяет миграции, заливает контент и открывает окно.
rem
rem Требуется: Docker Desktop и uv. Всё остальное ставится само.

setlocal
cd /d "%~dp0"
chcp 65001 >nul

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Не найден uv — менеджер зависимостей Python.
    echo   Установи одной командой в PowerShell:
    echo.
    echo     powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 1
)

where docker >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Не найден docker. Установи Docker Desktop и запусти его:
    echo   https://www.docker.com/products/docker-desktop/
    echo.
    pause
    exit /b 1
)

echo   Готовлю окружение, первый запуск займёт несколько минут...
call uv sync --all-packages --quiet
if errorlevel 1 (
    echo   Не удалось установить зависимости.
    pause
    exit /b 1
)

call uv run dojo start
if errorlevel 1 (
    echo.
    echo   Что-то пошло не так. Подробности:  uv run dojo stack logs api
    pause
    exit /b 1
)

endlocal
