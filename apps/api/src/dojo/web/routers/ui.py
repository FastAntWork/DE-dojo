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
from dojo.runner.lab import Lab, LabError, LabResult, reset_stand, run_check
from dojo.runner.sql_check import SqlTaskError
from dojo.runner.sql_check import check as sql_check
from dojo.scheduler.attempts import record_attempt
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


# ── Лабораторные стенды ──────────────────────────────────────────────────────
#
# Лаба отличается от каты и SQL-практики тем, что у неё есть СОСТОЯНИЕ: стенд
# поднят или нет, вариант такой-то, подсказок открыто столько-то. Состояние
# живёт в памяти процесса, потому что стенд всё равно не переживает
# перезапуск: база пересоздаётся с нуля при каждом запуске варианта.


@dataclass(slots=True)
class Stand:
    variant: int
    dsn: str


def _stands(request: Request) -> dict[str, Stand]:
    stands: dict[str, Stand] = request.app.state.lab_stands
    return stands


def _hints_taken(request: Request) -> dict[str, int]:
    """Сколько подсказок открыто по каждой лабе.

    Хранится отдельно от стенда намеренно. Пока счётчик жил внутри Stand,
    перезапуск стенда обнулял его — то есть подсказку можно было прочитать, а
    потом снять штраф, подняв стенд заново. Подсказка обязана стоить баллов,
    иначе её берут не задумываясь, и лаба перестаёт учить.
    """
    taken: dict[str, int] = request.app.state.lab_hints
    return taken


def _lab_or_404(index: ContentIndex, skill_id: str) -> Lab:
    lab = index.labs.get(skill_id)
    if lab is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет лабы")
    return lab


def _lab_context(
    request: Request,
    index: ContentIndex,
    skill_id: str,
    result: LabResult | None,
    **extra: Any,
) -> Any:
    lab = _lab_or_404(index, skill_id)
    stand = _stands(request).get(skill_id)
    hints = lab.hints[: _hints_taken(request).get(skill_id, 0)]

    # Разбор уезжает в браузер ТОЛЬКО после зачёта. Не «скрыт стилями» и не
    # спрятан в свёрнутый блок, а физически отсутствует в ответе: иначе он
    # читается через «просмотр кода страницы», и лаба перестаёт быть задачей.
    solution_html = markdown.render(lab.solution) if result and result.passed else None

    return context(
        request,
        skill=index.skill(skill_id),
        lab=lab,
        brief_html=markdown.render(lab.brief),
        stand=stand,
        hints=hints,
        next_hint=lab.hints[len(hints)] if len(hints) < len(lab.hints) else None,
        result=result,
        solution_html=solution_html,
        **extra,
    )


@router.get("/skills/{skill_id}/lab", response_class=HTMLResponse)
async def lab_page(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "lab.html", _lab_context(request, index, skill_id, result=None)
    )


@router.post("/skills/{skill_id}/lab/start", response_class=HTMLResponse)
async def lab_start(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    """Поднимает стенд заново. Всё, что человек успел сделать, пропадает.

    Пересоздание, а не починка: со своей базой человек мог сделать что угодно,
    и гарантировать исходное состояние можно только сборкой с нуля.
    """
    lab = _lab_or_404(index, skill_id)

    form = await request.form()
    variant = _requested_variant(form.get("variant"), lab)

    try:
        dsn = await reset_stand(request.app.state.settings.database_url, lab, variant)
    except (LabError, OSError, asyncpg.PostgresError) as exc:
        logger.warning("lab.stand.failed", skill_id=skill_id, error=str(exc))
        return templates.TemplateResponse(
            request,
            "_lab_panel.html",
            _lab_context(request, index, skill_id, result=None, stand_error=_stand_error(exc)),
        )

    _stands(request)[skill_id] = Stand(variant=variant, dsn=dsn)
    return templates.TemplateResponse(
        request, "_lab_panel.html", _lab_context(request, index, skill_id, result=None)
    )


def _requested_variant(raw: Any, lab: Lab) -> int:
    """Вариант из формы. Случайный, если не выбран явно."""
    try:
        variant = int(str(raw))
    except (TypeError, ValueError):
        # S311: выбор варианта задачи, криптостойкость не требуется.
        return random.randint(1, lab.variants)  # noqa: S311
    return min(max(variant, 1), lab.variants)


def _stand_error(exc: Exception) -> str:
    if isinstance(exc, LabError):
        return str(exc)
    return (
        f"Не удалось поднять стенд: {exc}. Лабе нужен работающий PostgreSQL — "
        "проверь, что хранилища запущены."
    )


@router.post("/skills/{skill_id}/lab/hint", response_class=HTMLResponse)
async def lab_hint(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    """Открывает следующую подсказку.

    Работает независимо от того, поднят ли стенд: раньше кнопка при не поднятом
    стенде молча не делала ничего, и человек жал её, не понимая, что сломалось.
    """
    lab = _lab_or_404(index, skill_id)
    taken = _hints_taken(request)
    taken[skill_id] = min(taken.get(skill_id, 0) + 1, len(lab.hints))

    return templates.TemplateResponse(
        request, "_lab_panel.html", _lab_context(request, index, skill_id, result=None)
    )


@router.post("/skills/{skill_id}/lab/check", response_class=HTMLResponse)
async def lab_check(skill_id: str, request: Request, index: IndexDep) -> HTMLResponse:
    lab = _lab_or_404(index, skill_id)
    stand = _stands(request).get(skill_id)
    if stand is None:
        return templates.TemplateResponse(
            request,
            "_lab_panel.html",
            _lab_context(
                request, index, skill_id, result=None, stand_error="Стенд не поднят — запусти лабу."
            ),
        )

    # check.py — отдельный процесс с сетевыми обращениями к базе. В потоке,
    # чтобы длинная проверка не держала весь сервер.
    result = await asyncio.to_thread(run_check, lab, stand.dsn, stand.variant)
    await _save_lab_attempt(request, index, skill_id, result, stand.variant)

    return templates.TemplateResponse(
        request, "_lab_panel.html", _lab_context(request, index, skill_id, result=result)
    )


async def _save_lab_attempt(
    request: Request, index: ContentIndex, skill_id: str, result: LabResult, variant: int
) -> None:
    """Пишет попытку. Недоступная база не должна ронять уже сданную лабу."""
    task = ContentIndex.lab_task(index.skill(skill_id))
    database = request.app.state.db
    if task is None or not database.is_connected:
        return

    lab = index.labs[skill_id]
    taken = _hints_taken(request).get(skill_id, 0)
    # Штраф берётся по самой дорогой открытой подсказке, а не суммой: иначе
    # человеку, честно дошедшему до третьего уровня, выгоднее было бы сразу
    # открыть последнюю.
    penalty = max((h.penalty for h in lab.hints[:taken]), default=0.0)
    score = round(max(result.score * (1 - penalty), 0.0), 2)

    try:
        async with database.acquire() as conn, conn.transaction():
            await record_attempt(
                conn,
                task_id=task.id,
                skill_id=skill_id,
                score=score,
                hints_used=taken,
                checks={
                    "variant": variant,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail} for c in result.checks
                    ],
                },
            )
    except (asyncpg.PostgresError, OSError, TimeoutError):
        logger.warning("attempt.save.failed", skill_id=skill_id, exc_info=True)


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
