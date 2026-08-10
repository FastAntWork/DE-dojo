"""Команды `dojo content`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import asyncpg
import typer
from rich.console import Console

from dojo.content.loader import load_skills
from dojo.content.sync import SyncReport, sync_skills
from dojo.core.config import get_settings

app = typer.Typer(help="Работа с учебным контентом.", no_args_is_help=True)
console = Console()


async def _run_sync(database_url: str, repo_root: Path, *, dry_run: bool) -> SyncReport:
    skills = load_skills(repo_root / "content", repo_root)
    conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(database_url)
    try:
        transaction = conn.transaction()
        await transaction.start()
        try:
            report = await sync_skills(conn, skills)
        except BaseException:
            await transaction.rollback()
            raise
        if dry_run:
            # Откат вместо отдельной ветки «посчитать, но не писать»: так
            # dry-run выполняет ровно тот же код, что и настоящий запуск,
            # и не может разойтись с ним по поведению.
            await transaction.rollback()
        else:
            await transaction.commit()
        return report
    finally:
        await conn.close()


@app.command("sync")
def sync(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Показать изменения и откатить транзакцию.")
    ] = False,
    database_url: Annotated[
        str | None, typer.Option(help="DSN Postgres. По умолчанию из DATABASE_URL.")
    ] = None,
    repo: Annotated[Path | None, typer.Option(help="Корень репозитория.")] = None,
) -> None:
    """Проецирует content/ в таблицы skills, skill_edges и tasks.

    Направление одностороннее: файлы — источник истины. Повторный запуск без
    правок не меняет ни одной строки.
    """
    from dojo_cli.__main__ import find_repo_root

    root = repo.resolve() if repo is not None else find_repo_root()
    dsn = database_url or get_settings().database_url

    try:
        report = asyncio.run(_run_sync(dsn, root, dry_run=dry_run))
    except OSError as exc:
        console.print(f"[red]Не удалось подключиться к Postgres:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except (asyncpg.PostgresError, RuntimeError) as exc:
        console.print(f"[red]Синхронизация не выполнена:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    prefix = "[yellow][dry-run][/yellow] " if dry_run else ""
    console.print(f"{prefix}{report.as_text()}")
    if not report.changed:
        console.print("[green]Изменений нет — контент и база совпадают.[/green]")
