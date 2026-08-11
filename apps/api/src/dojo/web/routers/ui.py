"""Страницы интерфейса.

Сервер отдаёт готовый HTML, htmx подменяет только результат квиза. Ни сборки,
ни клиентского роутера, ни состояния в браузере — см. docs/adr/0005.

Проверка ответов здесь не дублируется: страница вызывает ту же функцию, что и
JSON-API. Два независимых судьи рано или поздно разошлись бы.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from dojo.content.registry import ContentIndex, SkillNotFoundError
from dojo.core.logging import get_logger
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
        {
            "phases": phases,
            "total": len(summaries),
            "ready": sum(1 for s in summaries if s.theory_ready),
        },
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
        {
            "skill": {
                **detail.model_dump(),
                "objectives": skill.objectives,
                "job_tags": skill.job_tags,
            },
            "theory_html": markdown.render(raw) if raw else None,
        },
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
    return templates.TemplateResponse(request, "quiz.html", {"quiz": payload})


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
        request, "_quiz_result.html", {"result": result, "items": items}
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
    return templates.TemplateResponse(request, "progress.html", {"rows": rows, "titles": titles})
