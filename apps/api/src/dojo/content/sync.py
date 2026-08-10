"""Проекция контента из файлов в Postgres.

Направление одностороннее: файлы — источник истины, таблицы — производное.
Обратной записи нет и не будет, иначе появятся два источника правды.

Ключевые свойства:

* **Идемпотентность.** Повторный запуск без правок в content/ не меняет ни
  одной строки — сверка идёт по content_hash.
* **Ничего не удаляется.** Узел или задание, пропавшие из файлов, получают
  deprecated_at. На них ссылается история попыток, и терять её нельзя.
* **Возврат.** Если узел вернулся в content/, пометка снимается, а вся
  накопленная по нему статистика остаётся на месте.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from dojo.content.loader import SkillSpec
from dojo.core.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)


class UnknownPrereqError(RuntimeError):
    """prereq ссылается на узел, которого нет в контенте."""

    def __init__(self, skill_id: str, prereq_id: str) -> None:
        super().__init__(
            f"{skill_id}: prereq {prereq_id!r} отсутствует в content/. "
            f"Запусти tools/content_validate.py — он ловит это до записи в БД."
        )


@dataclass(frozen=True, slots=True)
class SyncReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    restored: int = 0
    deprecated: int = 0
    tasks_written: int = 0
    tasks_deprecated: int = 0
    edges: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.inserted or self.updated or self.restored or self.deprecated or self.tasks_written
        )

    def as_payload(self) -> dict[str, int]:
        """Словарь для события в outbox.

        Именно asdict, а не __dict__: датакласс объявлен со slots=True и
        атрибута __dict__ у него попросту нет.
        """
        return asdict(self)

    def as_text(self) -> str:
        return (
            f"узлы: +{self.inserted} ~{self.updated} ={self.unchanged} "
            f"↑{self.restored} ✗{self.deprecated}; "
            f"задания: {self.tasks_written} записано, {self.tasks_deprecated} снято; "
            f"рёбер: {self.edges}"
        )


UPSERT_SKILL = """
INSERT INTO skills (
    id, title, track, level, estimated_hours, objectives, job_tags,
    theory_path, rag_scope, review_after_days, source_path, content_hash,
    phase, priority
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14)
ON CONFLICT (id) DO UPDATE SET
    title             = EXCLUDED.title,
    track             = EXCLUDED.track,
    level             = EXCLUDED.level,
    phase             = EXCLUDED.phase,
    priority          = EXCLUDED.priority,
    estimated_hours   = EXCLUDED.estimated_hours,
    objectives        = EXCLUDED.objectives,
    job_tags          = EXCLUDED.job_tags,
    theory_path       = EXCLUDED.theory_path,
    rag_scope         = EXCLUDED.rag_scope,
    review_after_days = EXCLUDED.review_after_days,
    source_path       = EXCLUDED.source_path,
    content_hash      = EXCLUDED.content_hash,
    -- job_weight сознательно не трогаем: его владелец — пайплайн вакансий,
    -- а не файл узла.
    deprecated_at     = NULL,
    updated_at        = now()
"""

UPSERT_TASK = """
INSERT INTO tasks (
    id, skill_id, type, ordinal, difficulty, variants, timeout_sec,
    spec, hints, source_path, content_hash
) VALUES ($1, $2, $3::task_type, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11)
ON CONFLICT (id) DO UPDATE SET
    ordinal       = EXCLUDED.ordinal,
    difficulty    = EXCLUDED.difficulty,
    variants      = EXCLUDED.variants,
    timeout_sec   = EXCLUDED.timeout_sec,
    spec          = EXCLUDED.spec,
    hints         = EXCLUDED.hints,
    source_path   = EXCLUDED.source_path,
    content_hash  = EXCLUDED.content_hash,
    deprecated_at = NULL,
    updated_at    = now()
"""


async def sync_skills(
    conn: asyncpg.Connection[asyncpg.Record],
    skills: list[SkillSpec],
) -> SyncReport:
    """Записывает контент в БД. Вызывать внутри транзакции."""
    known = {skill.id for skill in skills}
    for skill in skills:
        for prereq_id, _hard in skill.prereq:
            if prereq_id not in known:
                raise UnknownPrereqError(skill.id, prereq_id)

    existing: dict[str, tuple[str, bool]] = {
        row["id"]: (row["content_hash"], row["deprecated_at"] is not None)
        for row in await conn.fetch("SELECT id, content_hash, deprecated_at FROM skills")
    }

    inserted = updated = unchanged = restored = 0

    for skill in skills:
        previous = existing.get(skill.id)
        if previous is None:
            inserted += 1
        elif previous[0] != skill.content_hash:
            updated += 1
        elif previous[1]:
            restored += 1
        else:
            unchanged += 1
            continue  # ничего не изменилось — не трогаем строку вовсе

        await conn.execute(
            UPSERT_SKILL,
            skill.id,
            skill.title,
            skill.track,
            skill.level,
            skill.estimated_hours,
            json.dumps(skill.objectives, ensure_ascii=False),
            skill.job_tags,
            skill.theory_path,
            skill.rag_scope,
            skill.review_after_days,
            skill.source_path,
            skill.content_hash,
            skill.phase,
            skill.priority,
        )

    # Узлы, пропавшие из content/. DELETE здесь был бы потерей истории.
    deprecated = 0
    if known:
        deprecated = len(
            await conn.fetch(
                """
                UPDATE skills SET deprecated_at = now(), updated_at = now()
                WHERE deprecated_at IS NULL AND id <> ALL($1::text[])
                RETURNING id
                """,
                list(known),
            )
        )

    # Рёбра истории не несут, поэтому их можно смело пересобирать.
    await conn.execute("DELETE FROM skill_edges WHERE child_id = ANY($1::text[])", list(known))
    edge_rows = [
        (prereq_id, skill.id, hard) for skill in skills for prereq_id, hard in skill.prereq
    ]
    if edge_rows:
        await conn.executemany(
            "INSERT INTO skill_edges (parent_id, child_id, hard) VALUES ($1, $2, $3)",
            edge_rows,
        )

    tasks_written = await _sync_tasks(conn, skills)
    tasks_deprecated = await _deprecate_missing_tasks(conn, skills)
    await _ensure_skill_states(conn, known)

    report = SyncReport(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        restored=restored,
        deprecated=deprecated,
        tasks_written=tasks_written,
        tasks_deprecated=tasks_deprecated,
        edges=len(edge_rows),
    )

    if report.changed:
        # Событие пишется в той же транзакции, что и само изменение —
        # ради этого outbox и заведён до появления Kafka.
        await conn.execute(
            """
            INSERT INTO event_outbox (topic, key, payload)
            VALUES ('dojo.content.synced', 'content', $1::jsonb)
            """,
            json.dumps(report.as_payload(), ensure_ascii=False),
        )

    return report


async def _sync_tasks(conn: asyncpg.Connection[asyncpg.Record], skills: list[SkillSpec]) -> int:
    existing = {
        row["id"]: (row["content_hash"], row["deprecated_at"] is not None)
        for row in await conn.fetch("SELECT id, content_hash, deprecated_at FROM tasks")
    }

    written = 0
    for skill in skills:
        for task in skill.tasks:
            previous = existing.get(task.id)
            if previous is not None and previous[0] == task.content_hash and not previous[1]:
                continue
            await conn.execute(
                UPSERT_TASK,
                task.id,
                task.skill_id,
                task.type,
                task.ordinal,
                task.difficulty,
                task.variants,
                task.timeout_sec,
                json.dumps(task.spec, ensure_ascii=False),
                json.dumps(task.hints, ensure_ascii=False),
                task.source_path,
                task.content_hash,
            )
            written += 1
    return written


async def _deprecate_missing_tasks(
    conn: asyncpg.Connection[asyncpg.Record], skills: list[SkillSpec]
) -> int:
    alive = [task.id for skill in skills for task in skill.tasks]
    rows = await conn.fetch(
        """
        UPDATE tasks SET deprecated_at = now(), updated_at = now()
        WHERE deprecated_at IS NULL AND id <> ALL($1::text[])
        RETURNING id
        """,
        alive,
    )
    return len(rows)


async def _ensure_skill_states(
    conn: asyncpg.Connection[asyncpg.Record], skill_ids: set[str]
) -> None:
    """Заводит строку состояния под каждый живой узел.

    Так планировщику не приходится различать «узел не начат» и «строки нет»:
    любой запрос к состоянию всегда что-то возвращает.
    """
    if not skill_ids:
        return
    await conn.execute(
        """
        INSERT INTO skill_states (skill_id)
        SELECT unnest($1::text[])
        ON CONFLICT (skill_id) DO NOTHING
        """,
        list(skill_ids),
    )
