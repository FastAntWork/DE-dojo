"""API учебного контента.

Главное правило этого модуля: **признак правильности никогда не покидает
сервер**. Клиент получает варианты ответа с идентификаторами, но не знает,
какой из них верный, и проверку делает сервер. Иначе квиз проходится
просмотром сетевой вкладки браузера, и вся детерминированная проверка
превращается в декорацию.
"""

from __future__ import annotations

import random
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from dojo.content.quiz import Quiz
from dojo.content.registry import ContentIndex, SkillNotFoundError
from dojo.core.logging import get_logger
from dojo.scheduler.attempts import PASS_THRESHOLD, record_attempt

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["content"])


def get_index(request: Request) -> ContentIndex:
    index: ContentIndex = request.app.state.content
    return index


IndexDep = Annotated[ContentIndex, Depends(get_index)]


# ── Схемы ответов ────────────────────────────────────────────────────────────


class SkillSummary(BaseModel):
    id: str
    title: str
    track: str
    phase: int
    priority: str
    level: int
    estimated_hours: float
    prereq: list[str]
    has_quiz: bool
    has_sql: bool = Field(description="Есть практика на настоящей базе")
    theory_ready: bool = Field(description="Теория написана, а не заглушка")


class SkillDetail(SkillSummary):
    objectives: list[str]
    job_tags: list[str]
    soft_prereq: list[str]
    theory_markdown: str | None


class QuizOption(BaseModel):
    id: str
    text: str


class QuizQuestion(BaseModel):
    id: str
    kind: str
    prompt: str
    options: list[QuizOption]
    # explanation и признак правильности сознательно отсутствуют:
    # они появляются только в ответе на отправку.


class QuizPayload(BaseModel):
    skill_id: str
    title: str
    questions: list[QuizQuestion]


class SubmittedAnswer(BaseModel):
    question_id: str
    option_ids: list[str] = Field(default_factory=list)


class QuizSubmission(BaseModel):
    answers: list[SubmittedAnswer]


class QuestionVerdict(BaseModel):
    question_id: str
    correct: bool
    correct_option_ids: list[str]
    explanation: str
    source: str | None


class QuizResult(BaseModel):
    skill_id: str
    right: int
    total: int
    score: float
    passed: bool
    verdicts: list[QuestionVerdict]
    attempt_id: int | None = Field(description="None, если запись в базу не удалась")


# ── Эндпоинты ────────────────────────────────────────────────────────────────


def _summary(index: ContentIndex, skill_id: str) -> SkillSummary:
    skill = index.skill(skill_id)
    return SkillSummary(
        id=skill.id,
        title=skill.title,
        track=skill.track,
        phase=skill.phase,
        priority=skill.priority,
        level=skill.level,
        estimated_hours=skill.estimated_hours,
        prereq=[pid for pid, hard in skill.prereq if hard],
        has_quiz=skill.id in index.quizzes,
        has_sql=skill.id in index.sql_tasks,
        theory_ready=not index.is_stub_theory(skill.id),
    )


@router.get("/skills", response_model=list[SkillSummary])
async def list_skills(index: IndexDep) -> list[SkillSummary]:
    """Все узлы графа, отсортированные по фазе и идентификатору."""
    summaries = [_summary(index, skill_id) for skill_id in index.skills]
    summaries.sort(key=lambda s: (s.phase, s.id))
    return summaries


@router.get("/skills/{skill_id}", response_model=SkillDetail)
async def get_skill(skill_id: str, index: IndexDep) -> SkillDetail:
    try:
        skill = index.skill(skill_id)
    except SkillNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    base = _summary(index, skill_id)
    return SkillDetail(
        **base.model_dump(),
        objectives=skill.objectives,
        job_tags=skill.job_tags,
        soft_prereq=[pid for pid, hard in skill.prereq if not hard],
        theory_markdown=index.theory(skill_id),
    )


def _public_quiz(quiz: Quiz, title: str) -> QuizPayload:
    return QuizPayload(
        skill_id=quiz.skill_id,
        title=title,
        questions=[
            QuizQuestion(
                id=question.id,
                kind=question.kind,
                prompt=question.prompt,
                options=[QuizOption(id=o.id, text=o.text) for o in question.options],
            )
            for question in quiz.questions
        ],
    )


@router.get("/skills/{skill_id}/quiz", response_model=QuizPayload)
async def get_quiz(skill_id: str, index: IndexDep) -> QuizPayload:
    """Вопросы без указания правильных вариантов."""
    try:
        skill = index.skill(skill_id)
    except SkillNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    quiz = index.quizzes.get(skill_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет квиза")

    # Перемешиваем на каждый запрос: иначе повторное прохождение проверяет
    # память на позицию варианта, а не на материал.
    # S311: тасуем варианты ответов, криптостойкость тут не нужна.
    return _public_quiz(quiz.shuffled(random.Random()), skill.title)  # noqa: S311


@router.post("/skills/{skill_id}/quiz", response_model=QuizResult)
async def submit_quiz(
    skill_id: str, submission: QuizSubmission, request: Request, index: IndexDep
) -> QuizResult:
    """Проверяет ответы на сервере и записывает попытку."""
    quiz = index.quizzes.get(skill_id)
    if quiz is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"у узла {skill_id} нет квиза")

    chosen = {answer.question_id: set(answer.option_ids) for answer in submission.answers}

    verdicts: list[QuestionVerdict] = []
    for question in quiz.questions:
        correct = question.grade_ids(chosen.get(question.id, set()))
        verdicts.append(
            QuestionVerdict(
                question_id=question.id,
                correct=correct,
                correct_option_ids=sorted(question.correct_option_ids),
                explanation=question.explanation,
                source=question.source,
            )
        )

    right = sum(1 for v in verdicts if v.correct)
    total = len(verdicts)
    score = round(right / total, 2) if total else 0.0

    attempt_id = await _save_attempt(request, index, skill_id, score)

    return QuizResult(
        skill_id=skill_id,
        right=right,
        total=total,
        score=score,
        passed=score >= PASS_THRESHOLD,
        verdicts=verdicts,
        attempt_id=attempt_id,
    )


async def _save_attempt(
    request: Request, index: ContentIndex, skill_id: str, score: float
) -> int | None:
    """Пишет попытку. Недоступная база не должна ронять уже пройденный квиз."""
    task = ContentIndex.quiz_task(index.skill(skill_id))
    if task is None:
        return None

    database = request.app.state.db
    if not database.is_connected:
        return None

    try:
        async with database.acquire() as conn, conn.transaction():
            return await record_attempt(conn, task_id=task.id, skill_id=skill_id, score=score)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        logger.warning("attempt.save.failed", skill_id=skill_id, exc_info=True)
        return None
