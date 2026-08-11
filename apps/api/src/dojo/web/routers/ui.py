"""Страницы интерфейса.

Сервер отдаёт готовый HTML, htmx подменяет только результат квиза. Ни сборки,
ни клиентского роутера, ни состояния в браузере — см. docs/adr/0005.

Проверка ответов здесь не дублируется: страница вызывает ту же функцию, что и
JSON-API. Два независимых судьи рано или поздно разошлись бы.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from dojo.content.registry import ContentIndex, SkillNotFoundError
from dojo.core.logging import get_logger
from dojo.runner.datasets import DatasetError, ensure_dataset, load_dataset
from dojo.runner.kata import Kata, KataError, KataResult, build_image, image_exists
from dojo.runner.kata import run as run_kata
from dojo.runner.sql_check import SqlTaskError
from dojo.runner.sql_check import check as sql_check
from dojo.web.routers.content import (
    QuizSubmission,
    SubmittedAnswer,
    _public_quiz,
    _summary,
    get_index,
    submit_quiz,
)

logger = get_logger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

TEMPLATES_DIR: Final = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Таблицы нужны: в конспектах их много, а без плагина markdown-it отдаёт их
# как обычный текст с палками.
markdown = MarkdownIt("commonmark", {"typographer": True}).enable(["table", "strikethrough"])

PHASE_TITLES: Final[dict[int, str]] = {
    1: "Основы",
    2: "Реляционные базы и оптимизация",
    3: "Хранилища и моделирование",
    4: "Обработка и оркестрация",
    5: "Инженерная гигиена",
    6: "Собеседования",
}

IndexDep = Annotated[ContentIndex, Depends(get_index)]


@dataclass(frozen=True, slots=True)
class Phase:
    number: int
    title: str
    skills: list[Any]


def context(request: Request, **extra: Any) -> dict[str, Any]:
    """Общий контекст шаблонов.

    storage_ready попадает в каждую страницу, потому что плашка «прогресс не
    сохраняется» живёт в базовом шаблоне: узнать об этом человек должен на
    любом экране, а не только на том, где полез смотреть историю.
    """
    return {"storage_ready": request.app.state.db.is_connected, **extra}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, index: IndexDep) -> HTMLResponse:
    summaries = [_summary(index, skill_id) for skill_id in index.skills]
    summaries.sort(key=lambda s: (s.phase, s.id))

    phases = [
        Phase(number=number, title=PHASE_TITLES.get(number, ""), skills=items)
        for number, items in _group_by_phase(summaries)
    ]

    return templates.TemplateResponse(
        request,
        "index.html",
        context(
            request,
            phases=phases,
            total=len(summaries),
            ready=sum(1 for s in summaries if s.theory_ready),
        ),
    )


def _group_by_phase(summaries: list[Any]) -> list[tuple[int, list[Any]]]:
    grouped: dict[int, list[Any]] = {}
    for item in summaries:
        grouped.setdefault(item.phase, []).append(item)
    return sorted(grouped.items())


@router.get("/skills/{skill_id}", response_class=HTMLResponse)
async def skill_page(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    try:
        skill = index.skill(skill_id)
    except SkillNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    raw = index.theory(skill_id)
    detail = _summary(index, skill_id)

    return templates.TemplateResponse(
        request,
        "skill.html",
        context(
            request,
            skill={
                **detail.model_dump(),
                "objectives": skill.objectives,
                "job_tags": skill.job_tags,
            },
            theory_html=markdown.render(raw) if raw else None,
        ),
    )


@router.get("/skills/{skill_id}/quiz", response_class=HTMLResponse)
async def quiz_page(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    try:
        skill = index.skill(skill_id)
    except SkillNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    quiz = index.quizzes.get(skill_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет квиза")

    # S311: перемешивание вариантов, криптостойкость не требуется.
    payload = _public_quiz(quiz.shuffled(random.Random()), skill.title)  # noqa: S311
    return templates.TemplateResponse(request, "quiz.html", context(request, quiz=payload))


@router.post("/skills/{skill_id}/quiz", response_class=HTMLResponse)
async def quiz_submit(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    quiz = index.quizzes.get(skill_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет квиза")

    form = await request.form()
    chosen: dict[str, list[str]] = {}
    for key in form:
        if not key.startswith("q:"):
            continue
        values = [str(v) for v in form.getlist(key)]
        chosen[key.removeprefix("q:")] = values

    submission = QuizSubmission(
        answers=[SubmittedAnswer(question_id=qid, option_ids=ids) for qid, ids in chosen.items()]
    )
    result = await submit_quiz(skill_id, submission, request, index)

    by_id = {question.id: question for question in quiz.questions}
    items = [
        {
            "question": by_id[verdict.question_id],
            "verdict": verdict,
            "chosen": set(chosen.get(verdict.question_id, [])),
        }
        for verdict in result.verdicts
    ]

    return templates.TemplateResponse(
        request, "_quiz_result.html", context(request, result=result, items=items)
    )


async def dataset_connection(request: Request, name: str) -> asyncpg.Connection[asyncpg.Record]:
    """Соединение с базой датасета, при необходимости подготовив её.

    Подготовка ленивая: датасет нужен только тому, кто открыл практику, и
    грузить его при старте приложения незачем. DSN кешируется — проверка
    контрольной суммы стоит одного запроса, но и он лишний на каждый ответ.
    """
    cache: dict[str, str] = request.app.state.dataset_dsns
    if name not in cache:
        index: ContentIndex = request.app.state.content
        dataset = load_dataset(index.content_root, name)
        cache[name] = await ensure_dataset(request.app.state.settings.database_url, dataset)
    return await asyncpg.connect(cache[name])


@router.get("/skills/{skill_id}/sql", response_class=HTMLResponse)
async def sql_page(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    try:
        skill = index.skill(skill_id)
    except SkillNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    task_file = index.sql_tasks.get(skill_id)
    if task_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет SQL-практики")

    return templates.TemplateResponse(
        request,
        "sql.html",
        context(request, skill=skill, tasks=task_file.tasks, dataset=task_file.dataset),
    )


@router.post("/skills/{skill_id}/sql/{task_id}", response_class=HTMLResponse)
async def sql_submit(
    skill_id: str, task_id: str, request: Request, index: IndexDep
) -> HTMLResponse:
    task_file = index.sql_tasks.get(skill_id)
    if task_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет SQL-практики")

    try:
        task = task_file.task(task_id)
    except SqlTaskError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    form = await request.form()
    answer = str(form.get("answer", ""))

    try:
        conn = await dataset_connection(request, task_file.dataset)
    except (OSError, asyncpg.PostgresError, DatasetError) as exc:
        logger.warning("sql.dataset.unavailable", dataset=task_file.dataset, error=str(exc))
        return templates.TemplateResponse(
            request,
            "_sql_result.html",
            context(request, task=task, verdict=None, answer=answer),
        )

    try:
        verdict = await sql_check(conn, task, answer)
    finally:
        await conn.close()

    return templates.TemplateResponse(
        request,
        "_sql_result.html",
        context(request, task=task, verdict=verdict, answer=answer),
    )


@router.get("/skills/{skill_id}/kata", response_class=HTMLResponse)
async def kata_page(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    kata = index.katas.get(skill_id)
    if kata is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет каты")

    return templates.TemplateResponse(
        request,
        "kata.html",
        context(
            request,
            skill=index.skill(skill_id),
            kata=kata,
            task_html=markdown.render(kata.task_md),
        ),
    )


@router.post("/skills/{skill_id}/kata", response_class=HTMLResponse)
async def kata_submit(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    kata = index.katas.get(skill_id)
    if kata is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет каты")

    form = await request.form()
    answer = str(form.get("answer", ""))

    # Прогон в песочнице — блокирующий вызов docker, поэтому уводим его в
    # поток: иначе одно решение подвешивало бы весь сервер на десятки секунд.
    result = await asyncio.to_thread(_run_kata_safely, kata, answer)

    return templates.TemplateResponse(
        request, "_kata_result.html", context(request, result=result, answer=answer)
    )


def _run_kata_safely(kata: Kata, answer: str) -> KataResult:
    """Собирает образ песочницы при первом обращении и запускает прогон."""
    try:
        if not image_exists():
            build_image(Path(__file__).resolve().parents[5])
        return run_kata(kata, answer)
    except KataError as exc:
        return KataResult(passed=False, total=0, failed=0, infrastructure_error=str(exc))
    except OSError as exc:
        return KataResult(
            passed=False,
            total=0,
            failed=0,
            infrastructure_error=(
                f"Не удалось запустить песочницу: {exc}. Она работает в Docker — "
                "проверь, что он запущен."
            ),
        )


@router.get("/progress", response_class=HTMLResponse)
async def progress(request: Request, index: IndexDep) -> HTMLResponse:
    """История попыток. Пока таблица, графики появятся вместе с аналитикой."""
    database = request.app.state.db
    rows: list[Any] = []
    if database.is_connected:
        async with database.acquire() as conn:
            rows = list(
                await conn.fetch(
                    """
                    SELECT skill_id, attempt_no, status, score, finished_at
                    FROM attempts
                    WHERE finished_at IS NOT NULL
                    ORDER BY finished_at DESC
                    LIMIT 100
                    """
                )
            )

    titles = {skill_id: index.skills[skill_id].title for skill_id in index.skills}
    return templates.TemplateResponse(
        request, "progress.html", context(request, rows=rows, titles=titles)
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup(request: Request) -> HTMLResponse:
    """Что не так с окружением и как это починить.

    Отдельная страница, а не текст в консоли: человек, запустивший приложение
    двойным кликом, консоль не читает.
    """
    return templates.TemplateResponse(request, "setup.html", context(request))
