"""Тесты подбора профиля LLM и пороговых проверок.

Смысл этих тестов в том, чтобы логика проверялась на любых конфигурациях
железа, а не только на той машине, где случайно запустили `dojo doctor`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dojo_cli.doctor import (
    Status,
    check_disk,
    check_ram,
    check_repo_location,
    is_port_free,
)
from dojo_cli.hardware import (
    PROFILE_7B,
    PROFILE_14B,
    PROFILE_32B,
    PROFILE_CPU,
    HardwareInfo,
    LlmProfile,
    pick_llm_profile,
)


def hw(**overrides: object) -> HardwareInfo:
    defaults: dict[str, object] = {
        "cpu_logical": 16,
        "cpu_physical": 12,
        "ram_total_gib": 10.0,
        "ram_available_gib": 9.0,
        "swap_total_gib": 8.0,
        "disk_free_gib": 900.0,
        "gpu_name": "NVIDIA GeForce RTX 4060 Laptop GPU",
        "vram_gib": 8.0,
        "is_wsl": True,
    }
    return HardwareInfo(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestPickLlmProfile:
    @pytest.mark.parametrize(
        ("vram", "expected"),
        [
            (None, PROFILE_CPU),
            (0.0, PROFILE_CPU),
            (4.0, PROFILE_CPU),
            (6.0, PROFILE_7B),
            (8.0, PROFILE_7B),
            (12.0, PROFILE_7B),
            (16.0, PROFILE_14B),
            (24.0, PROFILE_32B),
            (48.0, PROFILE_32B),
        ],
    )
    def test_thresholds(self, vram: float | None, expected: LlmProfile) -> None:
        assert pick_llm_profile(vram) is expected

    @pytest.mark.parametrize("profile", [PROFILE_32B, PROFILE_14B, PROFILE_7B, PROFILE_CPU])
    def test_tag_matches_ollama_format(self, profile: LlmProfile) -> None:
        # Ollama-тег — ровно одно двоеточие, квантование через дефис:
        # qwen2.5-coder:7b-instruct-q4_K_M. Два двоеточия он не примет.
        assert profile.tag.count(":") == 1
        assert profile.tag.endswith(f"-{profile.quant}")

    def test_rtx_4060_laptop_gets_7b_not_14b(self) -> None:
        # 8 ГиБ VRAM: 14B в q5 формально «почти влезает», но часть слоёв
        # уедет в RAM и judge станет неюзабельным. Ступени консервативны
        # намеренно — это регрессионный тест на попытку их занизить.
        assert pick_llm_profile(8.0) is PROFILE_7B


class TestRam:
    def test_fails_below_minimum(self) -> None:
        check = check_ram(hw(ram_total_gib=6.0))
        assert check.status is Status.FAIL

    def test_wsl_gets_actionable_hint(self) -> None:
        check = check_ram(hw(ram_total_gib=6.0, is_wsl=True))
        assert ".wslconfig" in check.detail

    def test_warns_when_tight(self) -> None:
        assert check_ram(hw(ram_total_gib=9.0)).status is Status.WARN

    def test_ok_when_enough(self) -> None:
        assert check_ram(hw(ram_total_gib=16.0)).status is Status.OK


class TestDisk:
    @pytest.mark.parametrize(
        ("free", "expected"),
        [(5.0, Status.FAIL), (40.0, Status.WARN), (900.0, Status.OK)],
    )
    def test_thresholds(self, free: float, expected: Status) -> None:
        assert check_disk(hw(disk_free_gib=free)).status is expected


class TestRepoLocation:
    def test_warns_when_repo_on_windows_drive(self) -> None:
        check = check_repo_location(Path("/mnt/c/Users/x/dojo"), hw(is_wsl=True))
        assert check.status is Status.WARN
        assert "IO" in check.detail

    def test_ok_inside_wsl_filesystem(self, tmp_path: Path) -> None:
        assert check_repo_location(tmp_path, hw(is_wsl=True)).status is Status.OK

    def test_no_warning_outside_wsl(self) -> None:
        check = check_repo_location(Path("/mnt/data/dojo"), hw(is_wsl=False))
        assert check.status is Status.OK


class TestPortProbe:
    def test_unbound_port_reports_free(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # сокет закрыт — порт снова свободен
        assert is_port_free(port) is True

    def test_bound_port_reports_busy(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert is_port_free(port) is False
