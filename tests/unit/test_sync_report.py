"""Тесты отчёта синхронизации.

Отдельный файл, потому что сама синхронизация требует БД и живёт в
tests/integration, а отчёт — чистая структура и проверяется мгновенно.
"""

from __future__ import annotations

import json

from dojo.content.sync import SyncReport


class TestPayload:
    def test_payload_is_json_serialisable(self) -> None:
        # Регрессия: раньше здесь был report.__dict__, которого у датакласса
        # со slots=True не существует, и sync падал на записи события.
        report = SyncReport(inserted=10, edges=8, tasks_written=10)

        restored = json.loads(json.dumps(report.as_payload()))

        assert restored["inserted"] == 10
        assert restored["edges"] == 8

    def test_payload_covers_all_fields(self) -> None:
        payload = SyncReport().as_payload()

        assert set(payload) == {
            "inserted",
            "updated",
            "unchanged",
            "restored",
            "deprecated",
            "tasks_written",
            "tasks_deprecated",
            "edges",
        }


class TestChangedFlag:
    def test_untouched_run_is_not_changed(self) -> None:
        assert SyncReport(unchanged=10).changed is False

    def test_deprecation_alone_counts_as_change(self) -> None:
        # Узел, пропавший из content/, меняет состояние базы, даже если
        # ничего не вставлялось и не обновлялось.
        assert SyncReport(unchanged=9, deprecated=1).changed is True

    def test_restore_counts_as_change(self) -> None:
        assert SyncReport(restored=1).changed is True
