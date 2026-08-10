"""Проба железа и подбор профиля локальной LLM.

Логика подбора вынесена в чистые функции: она проверяется тестами на любых
значениях, а не только на том железе, где случайно запустили `dojo doctor`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

GIB: Final = 1024**3


# Держим загруженной ОДНУ модель: роли (tutor, reviewer, judge, interviewer)
# переключаются сменой системного промпта. Выгрузка и загрузка весов на
# каждую смену роли съела бы больше времени, чем сама генерация.
@dataclass(frozen=True, slots=True)
class LlmProfile:
    model: str
    variant: str
    quant: str
    min_vram_gib: float
    note: str

    @property
    def tag(self) -> str:
        """Тег в формате Ollama: одно двоеточие, квантование через дефис."""
        return f"{self.model}:{self.variant}-{self.quant}"


PROFILE_32B = LlmProfile(
    model="qwen2.5-coder",
    variant="32b-instruct",
    quant="q4_K_M",
    min_vram_gib=24,
    note="полностью в VRAM, роль judge работает с запасом",
)
PROFILE_14B = LlmProfile(
    model="qwen2.5-coder",
    variant="14b-instruct",
    quant="q5_K_M",
    min_vram_gib=16,
    note="полностью в VRAM",
)
PROFILE_7B = LlmProfile(
    model="qwen2.5-coder",
    variant="7b-instruct",
    quant="q4_K_M",
    min_vram_gib=6,
    note="полностью в VRAM, ~35 tok/s на ноутбучной 4060",
)
PROFILE_CPU = LlmProfile(
    model="qwen2.5-coder",
    variant="7b-instruct",
    quant="q4_K_M",
    min_vram_gib=0,
    note="на CPU: judge и reviewer будут медленными, но рабочими",
)

_PROFILES: Final = (PROFILE_32B, PROFILE_14B, PROFILE_7B)


def pick_llm_profile(vram_gib: float | None) -> LlmProfile:
    """Выбирает профиль по объёму VRAM.

    Порог берётся с запасом: под контекст, KV-кеш и оверхед сервера нужно
    заметно больше, чем весит сам файл весов. 14B в q5 при 8 ГБ формально
    «почти влезает», но на деле часть слоёв уедет в RAM и скорость упадёт
    в разы — поэтому ступени расставлены консервативно.
    """
    if vram_gib is None or vram_gib <= 0:
        return PROFILE_CPU
    for profile in _PROFILES:
        if vram_gib >= profile.min_vram_gib:
            return profile
    return PROFILE_CPU


@dataclass(frozen=True, slots=True)
class HardwareInfo:
    cpu_logical: int
    cpu_physical: int | None
    ram_total_gib: float
    ram_available_gib: float
    swap_total_gib: float
    disk_free_gib: float
    gpu_name: str | None
    vram_gib: float | None
    is_wsl: bool


def detect_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def detect_gpu() -> tuple[str | None, float | None]:
    """Возвращает (название, VRAM в ГиБ) или (None, None), если GPU не видно.

    В WSL2 nvidia-smi живёт в /usr/lib/wsl/lib и виден не всегда, поэтому
    отсутствие бинаря — это «GPU нет», а не ошибка.
    """
    nvidia_smi = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    if not Path(nvidia_smi).exists():
        return None, None

    try:
        # Аргументы фиксированные, ввод пользователя сюда не попадает.
        completed = subprocess.run(  # noqa: S603
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None

    if completed.returncode != 0 or not completed.stdout.strip():
        return None, None

    first = completed.stdout.strip().splitlines()[0]
    match = re.match(r"^(?P<name>.+?),\s*(?P<mib>\d+)\s*$", first)
    if match is None:
        return None, None

    return match["name"].strip(), int(match["mib"]) / 1024


def collect(path: Path) -> HardwareInfo:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = shutil.disk_usage(path)
    gpu_name, vram = detect_gpu()

    return HardwareInfo(
        cpu_logical=psutil.cpu_count(logical=True) or 0,
        cpu_physical=psutil.cpu_count(logical=False),
        ram_total_gib=memory.total / GIB,
        ram_available_gib=memory.available / GIB,
        swap_total_gib=swap.total / GIB,
        disk_free_gib=disk.free / GIB,
        gpu_name=gpu_name,
        vram_gib=vram,
        is_wsl=detect_wsl(),
    )
