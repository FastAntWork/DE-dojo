"""Тесты разбора контента.

Загрузчик отделён от записи в БД именно ради этих тестов: самая объёмная
логика проверяется без поднятой базы.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dojo.content.loader import (
    ContentError,
    load_skills,
    parse_skill,
    parse_task,
    sha256_text,
)


def write_skill(root: Path, data: dict[str, Any], track: str = "sql") -> Path:
    directory = root / "content" / "tracks" / track
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def base_skill(skill_id: str = "sql.joins") -> dict[str, Any]:
    return {
        "id": skill_id,
        "title": "Соединения",
        "track": "sql",
        "level": 2,
        "estimated_hours": 6,
        "prereq": ["sql.core.select"],
        "soft_prereq": ["pg.basics"],
        "objectives": ["Выбрать тип соединения по задаче и обосновать выбор"],
        "theory": {"read": "theory/sql/sql.joins.md", "rag_scope": ["postgres-docs"]},
        "tasks": [{"type": "quiz", "file": "quizzes/sql.joins.yaml", "difficulty": 0.4}],
    }


class TestTaskIdentity:
    def test_id_survives_reordering(self) -> None:
        # Если бы идентификатор строился на позиции в списке, перестановка
        # заданий переименовала бы их все и оторвала историю попыток.
        quiz = {"type": "quiz", "file": "quizzes/sql.joins.yaml"}

        first = parse_task("sql.joins", quiz, 0, "src.yaml")
        moved = parse_task("sql.joins", quiz, 5, "src.yaml")

        assert first.id == moved.id
        assert first.ordinal != moved.ordinal

    @pytest.mark.parametrize(
        ("task", "expected_suffix"),
        [
            ({"type": "quiz", "file": "quizzes/sql.joins.yaml"}, "quiz::sql.joins"),
            ({"type": "lab", "path": "labs/kafka-lag"}, "lab::kafka-lag"),
            ({"type": "kata", "path": "katas/two-sum"}, "kata::two-sum"),
            (
                {"type": "design", "prompt": "x" * 30, "rubric": "rubrics/kafka.yaml"},
                "design::kafka",
            ),
        ],
    )
    def test_natural_key_drives_id(self, task: dict[str, Any], expected_suffix: str) -> None:
        assert parse_task("s.one", task, 0, "src.yaml").id == f"s.one::{expected_suffix}"

    def test_capstone_falls_back_to_prompt_hash(self) -> None:
        task = {"type": "capstone", "repo_task": "Реализуй DLQ", "tests": ["t.py"]}

        task_id = parse_task("s.one", task, 0, "src.yaml").id

        assert task_id == f"s.one::capstone::{sha256_text('Реализуй DLQ')[:12]}"

    def test_unidentifiable_task_raises(self) -> None:
        with pytest.raises(ContentError, match="нечем идентифицировать"):
            parse_task("s.one", {"type": "capstone", "tests": []}, 0, "src.yaml")


class TestHashes:
    def test_task_hash_ignores_key_order(self) -> None:
        # Пересортировка ключей в YAML не должна выглядеть как правка контента,
        # иначе sync будет переписывать строки на ровном месте.
        first = parse_task("s", {"type": "quiz", "file": "q.yaml", "difficulty": 0.4}, 0, "s.yaml")
        second = parse_task("s", {"difficulty": 0.4, "file": "q.yaml", "type": "quiz"}, 0, "s.yaml")

        assert first.content_hash == second.content_hash

    def test_task_hash_reacts_to_content(self) -> None:
        first = parse_task("s", {"type": "quiz", "file": "q.yaml", "difficulty": 0.4}, 0, "s.yaml")
        second = parse_task("s", {"type": "quiz", "file": "q.yaml", "difficulty": 0.7}, 0, "s.yaml")

        assert first.content_hash != second.content_hash

    def test_skill_hash_changes_with_file(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, base_skill())
        before = parse_skill(path, tmp_path).content_hash

        data = base_skill()
        data["title"] = "Другое название"
        path = write_skill(tmp_path, data)

        assert parse_skill(path, tmp_path).content_hash != before


class TestSkillParsing:
    def test_soft_prereq_marked_as_not_hard(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, base_skill())

        skill = parse_skill(path, tmp_path)

        assert ("sql.core.select", True) in skill.prereq
        assert ("pg.basics", False) in skill.prereq

    def test_defaults_are_applied(self, tmp_path: Path) -> None:
        data = base_skill()
        data["tasks"] = [{"type": "quiz", "file": "q.yaml"}]
        data.pop("review_after_days", None)
        path = write_skill(tmp_path, data)

        skill = parse_skill(path, tmp_path)

        assert skill.review_after_days == [1, 4, 12, 30]
        assert skill.tasks[0].difficulty == 0.5
        assert skill.tasks[0].variants == 1

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            ({"type": "lab", "path": "labs/x", "timeout_min": 45}, 45 * 60),
            ({"type": "kata", "path": "katas/x", "timeout_sec": 120}, 120),
            ({"type": "interview", "rubric": "rubrics/x.yaml", "duration_min": 40}, 40 * 60),
            ({"type": "quiz", "file": "q.yaml"}, 900),
        ],
    )
    def test_timeout_normalised_to_seconds(self, task: dict[str, Any], expected: int) -> None:
        assert parse_task("s", task, 0, "s.yaml").timeout_sec == expected

    def test_spec_holds_type_specific_fields_only(self, tmp_path: Path) -> None:
        task = parse_task(
            "s", {"type": "lab", "path": "labs/x", "variants": 3, "difficulty": 0.8}, 0, "s.yaml"
        )

        # variants и difficulty лежат в своих колонках, дублировать их в spec незачем
        assert task.spec == {"path": "labs/x"}


class TestLoadSkills:
    def test_reads_all_tracks_sorted(self, tmp_path: Path) -> None:
        write_skill(tmp_path, base_skill("sql.joins"), track="sql")
        write_skill(tmp_path, {**base_skill("pg.basics"), "track": "postgres"}, track="postgres")

        skills = load_skills(tmp_path / "content", tmp_path)

        assert [skill.id for skill in skills] == ["pg.basics", "sql.joins"]

    def test_source_path_is_relative_to_repo(self, tmp_path: Path) -> None:
        write_skill(tmp_path, base_skill())

        skill = load_skills(tmp_path / "content", tmp_path)[0]

        assert skill.source_path == "content/tracks/sql/sql.joins.yaml"
