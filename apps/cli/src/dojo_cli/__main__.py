"""CLI `dojo`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dojo_cli import content
from dojo_cli.doctor import Status, run_all

app = typer.Typer(
    name="dojo",
    help="DE Dojo — локальный тренажёр хард-скиллов Data Engineer.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(content.app, name="content")

console = Console()

_STATUS_STYLE = {
    Status.OK: ("[green]ok[/green]", ""),
    Status.WARN: ("[yellow]warn[/yellow]", "yellow"),
    Status.FAIL: ("[red]fail[/red]", "red"),
}


def find_repo_root(start: Path | None = None) -> Path:
    """Ищет корень репозитория вверх по дереву от текущего каталога."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists() or (candidate / "docker-compose.yml").exists():
            return candidate
    return current


@app.callback()
def main() -> None:
    """Точка входа группы команд.

    Нужна даже будучи пустой: Typer с единственной зарегистрированной командой
    схлопывает её в корневую, и `dojo doctor` начинает считаться лишним
    аргументом. Появление второй команды этого не изменит — callback задаёт
    режим группы явно.
    """


@app.command()
def doctor(
    repo: Annotated[
        Path | None,
        typer.Option(help="Корень репозитория. По умолчанию определяется автоматически."),
    ] = None,
) -> None:
    """Проверяет, готова ли машина к работе, и подбирает профиль локальной LLM."""
    root = repo.resolve() if repo is not None else find_repo_root()
    checks, hw, profile = run_all(root)

    table = Table(title="dojo doctor", title_justify="left", header_style="bold")
    table.add_column("проверка", no_wrap=True)
    table.add_column("", no_wrap=True)
    table.add_column("подробности")

    for check in checks:
        label, style = _STATUS_STYLE[check.status]
        table.add_row(check.name, label, check.detail, style=style)

    console.print(table)

    console.print()
    console.print("[bold]Профиль локальной LLM[/bold]")
    console.print(f"  модель:  {profile.tag}")
    console.print(f"  причина: {profile.note}")
    if hw.is_wsl:
        console.print(
            "  [dim]лимиты WSL задаются в %USERPROFILE%\\.wslconfig "
            "и применяются после wsl --shutdown[/dim]"
        )

    failed = [c for c in checks if c.status is Status.FAIL]
    warned = [c for c in checks if c.status is Status.WARN]

    console.print()
    if failed:
        console.print(f"[red]Проблем: {len(failed)}.[/red] Их надо починить до make up.")
        raise typer.Exit(code=1)
    if warned:
        console.print(f"[yellow]Предупреждений: {len(warned)}.[/yellow] Запускаться можно.")
    else:
        console.print("[green]Всё в порядке.[/green]")


if __name__ == "__main__":
    app()
