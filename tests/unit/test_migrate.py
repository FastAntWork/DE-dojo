"""Тесты инвариантов раннера миграций.

Все функции здесь чистые, поэтому тесты быстрые и без IO: поднятая БД нужна
только интеграционному тесту в tests/integration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dojo.core.migrate import (
    ChecksumMismatchError,
    MigrationError,
    OutOfOrderError,
    _normalize,
    load_migrations,
    select_pending,
)


def write(directory: Path, name: str, sql: str = "SELECT 1;\n") -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


class TestLoadMigrations:
    def test_sorts_numerically_not_lexicographically(self, tmp_path: Path) -> None:
        # Лексикографически "010" < "9", поэтому наивная сортировка строк
        # применила бы миграции в неверном порядке.
        write(tmp_path, "009_nine.sql")
        write(tmp_path, "010_ten.sql")
        write(tmp_path, "001_init.sql")

        versions = [m.version for m in load_migrations(tmp_path)]

        assert versions == ["001", "009", "010"]

    def test_rejects_bad_filename(self, tmp_path: Path) -> None:
        write(tmp_path, "init.sql")

        with pytest.raises(MigrationError, match="имя не по формату"):
            load_migrations(tmp_path)

    def test_rejects_duplicate_version(self, tmp_path: Path) -> None:
        write(tmp_path, "001_init.sql")
        write(tmp_path, "001_other.sql")

        with pytest.raises(MigrationError, match="дублирующийся номер"):
            load_migrations(tmp_path)

    def test_detects_no_transaction_marker(self, tmp_path: Path) -> None:
        write(tmp_path, "001_plain.sql", "SELECT 1;\n")
        write(
            tmp_path,
            "002_concurrent.sql",
            "-- dojo:no-transaction\nCREATE INDEX CONCURRENTLY x ON t (c);\n",
        )

        plain, concurrent = load_migrations(tmp_path)

        assert plain.transactional is True
        assert concurrent.transactional is False

    def test_marker_below_head_is_ignored(self, tmp_path: Path) -> None:
        # Маркер ищется только в первых строках: упоминание в комментарии
        # посреди файла не должно молча отключать транзакцию.
        body = "\n".join(["-- строка"] * 10) + "\n-- dojo:no-transaction\nSELECT 1;\n"
        write(tmp_path, "001_init.sql", body)

        assert load_migrations(tmp_path)[0].transactional is True

    def test_empty_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_migrations(tmp_path) == []


class TestNormalize:
    def test_ignores_trailing_whitespace_and_final_newlines(self) -> None:
        # Иначе хук trailing-whitespace, прошедший по репозиторию, сломал бы
        # сверку контрольных сумм для давно применённых миграций.
        assert _normalize("SELECT 1;   \n\n\n") == _normalize("SELECT 1;\n")

    def test_keeps_meaningful_difference(self) -> None:
        assert _normalize("SELECT 1;\n") != _normalize("SELECT 2;\n")


class TestSelectPending:
    def test_returns_only_unapplied(self, tmp_path: Path) -> None:
        write(tmp_path, "001_init.sql")
        write(tmp_path, "002_more.sql")
        migrations = load_migrations(tmp_path)
        applied = {"001": migrations[0].checksum}

        pending = select_pending(migrations, applied)

        assert [m.version for m in pending] == ["002"]

    def test_raises_when_applied_file_changed(self, tmp_path: Path) -> None:
        write(tmp_path, "001_init.sql")
        migrations = load_migrations(tmp_path)

        with pytest.raises(ChecksumMismatchError, match="изменился после применения"):
            select_pending(migrations, {"001": "0" * 64})

    def test_raises_on_migration_below_applied_high_water_mark(self, tmp_path: Path) -> None:
        # Классика: ветка отстала от main, в ней миграция 002, а в БД уже 003.
        write(tmp_path, "002_straggler.sql")
        write(tmp_path, "003_applied.sql")
        migrations = load_migrations(tmp_path)
        applied = {"003": migrations[1].checksum}

        with pytest.raises(OutOfOrderError, match=re.escape("002_straggler.sql")):
            select_pending(migrations, applied)

    def test_first_run_applies_everything(self, tmp_path: Path) -> None:
        write(tmp_path, "001_init.sql")
        write(tmp_path, "002_more.sql")
        migrations = load_migrations(tmp_path)

        assert select_pending(migrations, {}) == migrations
