"""`dojo doctor` — проверка, что машина готова к работе.

Задача команды — назвать конкретную причину и конкретное действие. Проверка,
которая говорит «что-то не так», бесполезна: она перекладывает диагностику
обратно на человека.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dojo_cli.hardware import HardwareInfo, LlmProfile, collect, pick_llm_profile

# core поднимается всегда; остальные порты нужны только под своими профилями.
CORE_PORTS: Final[dict[int, str]] = {
    5432: "postgres",
    6379: "redis",
    8000: "api",
}
OPTIONAL_PORTS: Final[dict[int, str]] = {
    11434: "ollama (профиль ai)",
    9092: "kafka (профиль analytics)",
    8123: "clickhouse http (профиль analytics)",
    9000: "clickhouse native (профиль analytics)",
    3000: "grafana (профиль analytics)",
    9090: "prometheus (профиль analytics)",
    9010: "minio api (профиль storage)",
    9001: "minio console (профиль storage)",
    27017: "mongodb (профиль storage)",
}

MIN_RAM_GIB: Final = 8.0
COMFORTABLE_RAM_GIB: Final = 10.0
MIN_DISK_GIB: Final = 20.0
COMFORTABLE_DISK_GIB: Final = 60.0
MIN_PYTHON: Final = (3, 12)


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


def _run(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """Запускает внешнюю команду. Аргументы фиксированные, ввода извне нет."""
    try:
        completed = subprocess.run(  # noqa: S603
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return completed.returncode, completed.stdout.strip()


def check_python() -> Check:
    version = sys.version_info
    actual = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) < MIN_PYTHON:
        need = ".".join(str(p) for p in MIN_PYTHON)
        return Check("python", Status.FAIL, f"{actual}, нужен ≥ {need}")
    return Check("python", Status.OK, actual)


def check_cpu(hw: HardwareInfo) -> Check:
    physical = hw.cpu_physical or hw.cpu_logical
    detail = f"{hw.cpu_logical} логических / {physical} физических"
    if hw.cpu_logical < 4:
        return Check("cpu", Status.WARN, f"{detail} — стенды лаб будут медленными")
    return Check("cpu", Status.OK, detail)


def check_ram(hw: HardwareInfo) -> Check:
    detail = f"{hw.ram_total_gib:.1f} ГиБ всего, {hw.ram_available_gib:.1f} свободно"
    if hw.ram_total_gib < MIN_RAM_GIB:
        hint = ""
        if hw.is_wsl:
            hint = ". Подними memory в %USERPROFILE%\\.wslconfig и сделай wsl --shutdown"
        return Check("ram", Status.FAIL, f"{detail} — нужно ≥ {MIN_RAM_GIB:.0f} ГиБ{hint}")
    if hw.ram_total_gib < COMFORTABLE_RAM_GIB:
        return Check("ram", Status.WARN, f"{detail} — профиль full не поднимется")
    return Check("ram", Status.OK, detail)


def check_swap(hw: HardwareInfo) -> Check:
    detail = f"{hw.swap_total_gib:.1f} ГиБ"
    if hw.swap_total_gib < 1:
        return Check(
            "swap",
            Status.WARN,
            f"{detail} — без свопа пик сборки образов может уронить контейнер по OOM",
        )
    return Check("swap", Status.OK, detail)


def check_disk(hw: HardwareInfo) -> Check:
    detail = f"{hw.disk_free_gib:.0f} ГиБ свободно"
    if hw.disk_free_gib < MIN_DISK_GIB:
        return Check("disk", Status.FAIL, f"{detail} — образов и моделей не хватит")
    if hw.disk_free_gib < COMFORTABLE_DISK_GIB:
        return Check("disk", Status.WARN, f"{detail} — хватит на core, но не на модели и лабы")
    return Check("disk", Status.OK, detail)


def check_docker() -> list[Check]:
    if shutil.which("docker") is None:
        return [
            Check("docker", Status.FAIL, "не установлен — запусти bash tools/install-docker.sh")
        ]

    code, version = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        return [
            Check(
                "docker",
                Status.FAIL,
                "демон не отвечает. Если это «permission denied» — ты не в группе "
                "docker: bash tools/install-docker.sh, затем wsl --shutdown",
            )
        ]

    checks = [Check("docker", Status.OK, f"демон {version}")]

    code, compose_version = _run(["docker", "compose", "version", "--short"])
    if code != 0:
        checks.append(Check("docker compose", Status.FAIL, "плагин compose не установлен"))
    else:
        checks.append(Check("docker compose", Status.OK, compose_version))

    return checks


def port_owner(port: int) -> str | None:
    """Имя контейнера, публикующего порт, если это наш контейнер."""
    code, out = _run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"])
    if code != 0 or not out:
        return None
    return out.splitlines()[0]


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def check_ports(ports: dict[int, str], *, required: bool) -> list[Check]:
    checks: list[Check] = []
    for port, service in sorted(ports.items()):
        name = f"порт {port}"
        if is_port_free(port):
            checks.append(Check(name, Status.OK, f"свободен ({service})"))
            continue

        owner = port_owner(port)
        if owner is not None and owner.startswith("dojo-"):
            checks.append(Check(name, Status.OK, f"занят своим же контейнером {owner}"))
        elif required:
            checks.append(Check(name, Status.FAIL, f"занят чужим процессом, нужен для {service}"))
        else:
            checks.append(Check(name, Status.WARN, f"занят, помешает профилю: {service}"))
    return checks


def check_repo_location(repo: Path, hw: HardwareInfo) -> Check:
    resolved = str(repo.resolve())
    if hw.is_wsl and resolved.startswith("/mnt/"):
        return Check(
            "расположение репо",
            Status.WARN,
            f"{resolved} — это диск Windows через drvfs. Bind-mount оттуда даёт "
            "многократную просадку IO: перенеси репозиторий в ~/ внутри WSL",
        )
    return Check("расположение репо", Status.OK, resolved)


def check_gpu_runtime(hw: HardwareInfo) -> Check:
    """Видит ли docker видеокарту.

    Отдельно от check_llm: наличие GPU в системе и доступность её внутри
    контейнера — разные вещи. В WSL nvidia-smi работает почти всегда, а вот
    проброс в контейнер требует nvidia-container-toolkit, и без него ollama
    молча уходит на CPU, где 7B выдаёт единицы токенов в секунду.
    """
    name = "gpu в докере"
    if hw.gpu_name is None:
        return Check(name, Status.WARN, "видеокарта не обнаружена, проверять нечего")

    code, runtimes = _run(["docker", "info", "--format", "{{json .Runtimes}}"])
    if code != 0:
        return Check(name, Status.WARN, "демон не ответил")
    if "nvidia" in runtimes:
        return Check(name, Status.OK, "рантайм nvidia доступен")
    return Check(
        name,
        Status.WARN,
        "nvidia-container-toolkit не установлен: ollama в контейнере пойдёт на CPU. "
        "Инструкция по установке — в шапке compose/ai.gpu.yml",
    )


def check_llm(hw: HardwareInfo) -> tuple[Check, LlmProfile]:
    profile = pick_llm_profile(hw.vram_gib)
    if hw.gpu_name is None:
        return (
            Check(
                "gpu",
                Status.WARN,
                "не обнаружена — LLM-роли пойдут на CPU и будут медленными",
            ),
            profile,
        )
    return (
        Check("gpu", Status.OK, f"{hw.gpu_name}, {hw.vram_gib:.1f} ГиБ VRAM"),
        profile,
    )


def run_all(repo: Path) -> tuple[list[Check], HardwareInfo, LlmProfile]:
    hw = collect(repo)
    gpu_check, profile = check_llm(hw)

    checks: list[Check] = [
        check_python(),
        check_cpu(hw),
        check_ram(hw),
        check_swap(hw),
        check_disk(hw),
        gpu_check,
        check_gpu_runtime(hw),
        check_repo_location(repo, hw),
        *check_docker(),
        *check_ports(CORE_PORTS, required=True),
        *check_ports(OPTIONAL_PORTS, required=False),
    ]
    return checks, hw, profile
