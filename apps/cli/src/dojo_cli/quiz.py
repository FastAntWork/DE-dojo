"""Команда `dojo quiz` — пройти квиз по узлу.

Разбор показывается после каждого вопроса независимо от того, верен ответ или
нет: неверный ответ без объяснения ничему не учит, а верный по случайному
совпадению — тем более.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Annotated

import asyncpg
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from dojo.content.loader import SkillSpec, TaskSpec, load_skills
from dojo.content.quiz import Answer, Question, Quiz, QuizError, Result, grade, parse_quiz
from dojo.core.config import get_settings
from dojo.scheduler.attempts import PASS_THRESHOLD, record_attempt

app = typer.Typer(help="Квизы по узлам графа.", no_args_is_help=True)
console = Console()


def _find_skill(skills: list[SkillSpec], skill_id: str) -> SkillSpec:
    for skill in skills:
        if skill.id == skill_id:
            return skill
    known = ", ".join(sorted(s.id for s in skills))
    msg = f"узел {skill_id!r} не найден. Есть: {known}"
    raise QuizError(msg)


def _find_quiz_task(skill: SkillSpec) -> TaskSpec:
    for task in skill.tasks:
        if task.type == "quiz":
            return task
    msg = f"у узла {skill.id} нет задания типа quiz"
    raise QuizError(msg)


def _ask(question: Question, number: int, total: int) -> frozenset[int]:
    console.print()
    console.print(Panel(question.prompt, title=f"Вопрос {number} из {total}", border_style="cyan"))

    for index, option in enumerate(question.options, start=1):
        console.print(f"  [bold]{index}[/bold]. {option.text}")

    hint = "номера через запятую" if question.multiple else "номер"
    valid = {str(i) for i in range(1, len(question.options) + 1)}

    while True:
        raw = Prompt.ask(f"\n  Твой ответ ({hint})").strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts and all(p in valid for p in parts):
            if not question.multiple and len(parts) > 1:
                console.print("  [yellow]Здесь только один верный вариант.[/yellow]")
                continue
            return frozenset(int(p) - 1 for p in parts)
        console.print(f"  [yellow]Нужно ввести {hint} из списка выше.[/yellow]")


def _show_verdict(answer: Answer) -> None:
    question = answer.question
    if answer.correct:
        console.print("\n  [green]Верно.[/green]")
    else:
        right = ", ".join(question.options[i].text for i in sorted(question.correct_indices))
        console.print(f"\n  [red]Неверно.[/red] Правильный ответ: {right}")

    console.print(Panel(question.explanation, border_style="dim", title="Разбор"))
    if question.source:
        console.print(f"  [dim]источник: {question.source}[/dim]")


def _show_summary(result: Result, quiz: Quiz) -> None:
    table = Table(title=f"Итог: {quiz.skill_id}", title_justify="left")
    table.add_column("вопрос")
    table.add_column("результат")
    for answer in result.answers:
        mark = "[green]верно[/green]" if answer.correct else "[red]неверно[/red]"
        table.add_row(answer.question.id, mark)
    console.print()
    console.print(table)

    percent = int(result.score * 100)
    verdict = (
        "[green]сдано[/green]" if result.score >= PASS_THRESHOLD else "[yellow]не сдано[/yellow]"
    )
    console.print(f"  {result.right} из {result.total} ({percent}%) — {verdict}")

    if result.wrong:
        console.print("\n  [bold]Вернуться к этим темам:[/bold]")
        for answer in result.wrong:
            console.print(f"    • {answer.question.id}")


async def _save(dsn: str, task: TaskSpec, score: float, duration_ms: int) -> int | None:
    """Пишет попытку. Недоступная БД не должна обесценивать пройденный квиз."""
    try:
        conn: asyncpg.Connection[asyncpg.Record] = await asyncpg.connect(dsn, timeout=5)
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return None
    try:
        async with conn.transaction():
            return await record_attempt(
                conn,
                task_id=task.id,
                skill_id=task.skill_id,
                score=score,
                duration_ms=duration_ms,
            )
    except asyncpg.PostgresError:
        return None
    finally:
        await conn.close()


@app.command("run")
def run(
    skill_id: Annotated[str, typer.Argument(help="Идентификатор узла, например sql.joins")],
    repo: Annotated[Path | None, typer.Option(help="Корень репозитория.")] = None,
    no_save: Annotated[
        bool, typer.Option("--no-save", help="Не записывать попытку в базу.")
    ] = False,
) -> None:
    """Пройти квиз по узлу."""
    from dojo_cli.__main__ import find_repo_root

    root = repo.resolve() if repo is not None else find_repo_root()

    try:
        skills = load_skills(root / "content", root)
        skill = _find_skill(skills, skill_id)
        task = _find_quiz_task(skill)
        quiz = parse_quiz(skill.id, root / "content" / task.spec["file"])
    except QuizError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"\n[bold]{skill.title}[/bold]")
    console.print(f"[dim]{len(quiz.questions)} вопросов. Порядок вариантов перемешан.[/dim]")

    # S311: тасуем варианты ответов, а не генерируем ключи. Криптостойкость
    # тут не нужна, а secrets.SystemRandom не умеет shuffle без плясок.
    shuffled = quiz.shuffled(random.Random())  # noqa: S311
    started = time.monotonic()

    answers: list[Answer] = []
    for number, question in enumerate(shuffled.questions, start=1):
        chosen = _ask(question, number, len(shuffled.questions))
        answer = Answer(question=question, chosen=chosen)
        _show_verdict(answer)
        answers.append(answer)

    result = grade(answers)
    duration_ms = int((time.monotonic() - started) * 1000)
    _show_summary(result, quiz)

    if no_save:
        return

    attempt_id = asyncio.run(_save(get_settings().database_url, task, result.score, duration_ms))
    if attempt_id is None:
        console.print(
            "\n  [yellow]Попытка не записана: база недоступна.[/yellow] "
            "Подними стек через make start, если хочешь вести историю."
        )
    else:
        console.print(f"\n  [dim]попытка #{attempt_id} записана[/dim]")


@app.command("list")
def list_quizzes(
    repo: Annotated[Path | None, typer.Option(help="Корень репозитория.")] = None,
) -> None:
    """Показать узлы, по которым есть квизы."""
    from dojo_cli.__main__ import find_repo_root

    root = repo.resolve() if repo is not None else find_repo_root()
    skills = load_skills(root / "content", root)

    table = Table(title="Доступные квизы", title_justify="left")
    table.add_column("узел")
    table.add_column("название")
    table.add_column("вопросов", justify="right")

    for skill in skills:
        try:
            task = _find_quiz_task(skill)
            quiz = parse_quiz(skill.id, root / "content" / task.spec["file"])
        except QuizError:
            continue
        table.add_row(skill.id, skill.title, str(len(quiz.questions)))

    console.print(table)
    console.print("\n  Запуск: [bold]dojo quiz run <узел>[/bold]")
