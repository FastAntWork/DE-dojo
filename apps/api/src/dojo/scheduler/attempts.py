"""Запись попыток.

Живёт в scheduler, потому что именно он будет владеть mastery и расписанием
повторений (M3). Пока модуль умеет только записывать факт попытки — и этого
достаточно: attempts устроена как append-only и хранит всё нужное, чтобы
кривую обучения можно было пересчитать задним числом (docs/adr/0002).

Поэтому mastery_before и mastery_after сейчас остаются пустыми, а не
заполняются нулями: пустое значение честно означает «на момент попытки
mastery не считался», а ноль означал бы «считался и был равен нулю».
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import asyncpg

# Квиз считается сданным с 80% верных ответов. Порог тот же, что и для
# закрытия узла по mastery, — чтобы у пользователя не возникало двух разных
# представлений о том, что такое «сдал».
PASS_THRESHOLD: Final = 0.8

INSERT_ATTEMPT: Final = """
INSERT INTO attempts (
    task_id, skill_id, attempt_no, status, score, result,
    hints_used, checks, duration_ms, finished_at
) VALUES ($1, $2, $3, $4::attempt_status, $5, $6, $7, $8::jsonb, $9, now())
RETURNING id
"""


async def next_attempt_no(conn: asyncpg.Connection[asyncpg.Record], task_id: str) -> int:
    value = await conn.fetchval(
        "SELECT coalesce(max(attempt_no), 0) + 1 FROM attempts WHERE task_id = $1",
        task_id,
    )
    return int(value)


async def record_attempt(
    conn: asyncpg.Connection[asyncpg.Record],
    *,
    task_id: str,
    skill_id: str,
    score: float,
    hints_used: int = 0,
    checks: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> int:
    """Записывает завершённую попытку и возвращает её идентификатор."""
    attempt_no = await next_attempt_no(conn, task_id)
    status = "passed" if score >= PASS_THRESHOLD else "failed"

    # Штрафа за подсказки в квизах нет — их там не бывает. Поле оставлено,
    # чтобы у лаб и кат была та же форма записи.
    result = score

    value = await conn.fetchval(
        INSERT_ATTEMPT,
        task_id,
        skill_id,
        attempt_no,
        status,
        score,
        result,
        hints_used,
        json.dumps(checks or {}, ensure_ascii=False),
        duration_ms,
    )
    return int(value)


async def attempt_history(
    conn: asyncpg.Connection[asyncpg.Record], skill_id: str, limit: int = 10
) -> list[asyncpg.Record]:
    """Последние попытки по узлу — для показа динамики пользователю."""
    return list(
        await conn.fetch(
            """
            SELECT attempt_no, status, score, finished_at
            FROM attempts
            WHERE skill_id = $1 AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT $2
            """,
            skill_id,
            limit,
        )
    )
