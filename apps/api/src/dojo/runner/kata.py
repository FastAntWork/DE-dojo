"""Прогон kata-заданий в песочнице.

Единственное место в проекте, где выполняется произвольный код, написанный
человеком. Поэтому ограничения перечислены явно и каждое закрыто тестом:

* **Нет сети.** `--network none`. Решение не может ни скачать ответ, ни
  отправить что-либо наружу.
* **Память и процессы ограничены.** Бесконечная рекурсия или fork-бомба
  убивают контейнер, а не машину.
* **Файловая система только для чтения**, кроме каталога отчёта и /tmp.
* **Не root.** Внутри контейнера пользователь без прав.
* **Таймаут.** Зависший прогон снимается принудительно.
* **docker.sock внутрь не пробрасывается никогда** — иначе песочница
  получила бы полный контроль над хостом.

Скрытые тесты подкладываются рядом с решением на время прогона и в задании
не лежат: иначе их можно прочитать и подогнать ответ.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dojo.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_IMAGE: Final = "dojo-sandbox:latest"

# Лимиты песочницы. Учебная ката укладывается в них с многократным запасом;
# всё, что не укладывается, — это ошибка в решении, а не тесность лимитов.
MEMORY_LIMIT: Final = "256m"
PIDS_LIMIT: Final = 64
CPU_LIMIT: Final = "1.0"
DEFAULT_TIMEOUT_SEC: Final = 30
# Отдельный тест учебной каты не считает дольше секунды. Пять — с запасом на
# медленный контейнер и на property-based тесты hypothesis.
PER_TEST_TIMEOUT_SEC: Final = 5

KATA_FILES: Final = ("task.md", "starter.py", "tests_public.py", "tests_hidden.py", "solution.py")


class KataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Kata:
    name: str
    path: Path
    task_md: str
    starter: str
    tests_public: str

    @property
    def module_name(self) -> str:
        return "solution"


@dataclass(frozen=True, slots=True)
class TestCase:
    name: str
    passed: bool
    message: str = ""
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class KataResult:
    passed: bool
    total: int
    failed: int
    cases: tuple[TestCase, ...] = ()
    stderr: str = ""
    timed_out: bool = False
    infrastructure_error: str | None = None

    @property
    def score(self) -> float:
        if not self.total:
            return 0.0
        return round((self.total - self.failed) / self.total, 2)


def load_kata(content_root: Path, name: str) -> Kata:
    directory = content_root / "katas" / name
    missing = [f for f in KATA_FILES if not (directory / f).is_file()]
    if missing:
        msg = f"ката {name}: не хватает файлов: {', '.join(missing)}"
        raise KataError(msg)

    return Kata(
        name=name,
        path=directory,
        task_md=(directory / "task.md").read_text(encoding="utf-8"),
        starter=(directory / "starter.py").read_text(encoding="utf-8"),
        tests_public=(directory / "tests_public.py").read_text(encoding="utf-8"),
    )


def image_exists() -> bool:
    result = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", SANDBOX_IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def build_image(repo_root: Path) -> None:
    """Собирает образ песочницы. Идемпотентно и быстро при готовых слоях."""
    context = repo_root / "apps" / "runner" / "sandbox"
    logger.info("sandbox.image.building", image=SANDBOX_IMAGE)
    result = subprocess.run(  # noqa: S603
        ["docker", "build", "-t", SANDBOX_IMAGE, str(context)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"не удалось собрать образ песочницы: {result.stderr[-500:]}"
        raise KataError(msg)
    logger.info("sandbox.image.built", image=SANDBOX_IMAGE)


def docker_run_args(work: Path, out: Path, timeout_sec: int) -> list[str]:
    """Аргументы запуска. Вынесены отдельно, чтобы их можно было проверить тестом."""
    return [
        "docker",
        "run",
        "--rm",
        # Ни байта наружу и ни байта снаружи.
        "--network",
        "none",
        f"--memory={MEMORY_LIMIT}",
        # Без ограничения swap лимит памяти обходится вытеснением.
        f"--memory-swap={MEMORY_LIMIT}",
        f"--pids-limit={PIDS_LIMIT}",
        f"--cpus={CPU_LIMIT}",
        # Отнимаем всё, что не нужно для запуска интерпретатора.
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=32m",
        f"--stop-timeout={timeout_sec}",
        "--user=10001:10001",
        "-v",
        f"{work}:/work:ro",
        "-v",
        f"{out}:/out:rw",
        "-w",
        "/work",
        SANDBOX_IMAGE,
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--junit-xml=/out/report.xml",
        # Таймаут на КАЖДЫЙ тест, а не на прогон целиком: иначе решение,
        # зависшее на одном тесте, лишает человека результатов по остальным.
        f"--timeout={PER_TEST_TIMEOUT_SEC}",
        "-q",
        "tests_public.py",
        "tests_hidden.py",
    ]


def parse_junit(xml_text: str, hidden_prefix: str = "tests_hidden") -> tuple[TestCase, ...]:
    """Разбирает отчёт pytest в формате JUnit.

    Формат встроен в pytest, поэтому не нужен плагин, а парсинг не зависит от
    того, как выглядит текстовый вывод в конкретной версии.
    """
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 — отчёт создан нами же в песочнице
    except ET.ParseError as exc:
        msg = f"отчёт тестов не разобран: {exc}"
        raise KataError(msg) from exc

    cases: list[TestCase] = []
    for element in root.iter("testcase"):
        failure = element.find("failure")
        error = element.find("error")
        problem = failure if failure is not None else error
        classname = element.get("classname", "")
        cases.append(
            TestCase(
                name=element.get("name", "?"),
                passed=problem is None,
                message=(problem.get("message", "") if problem is not None else ""),
                hidden=hidden_prefix in classname,
            )
        )
    return tuple(cases)


def _explain_missing_report(returncode: int) -> str:
    """Объясняет, почему отчёта нет, по коду возврата контейнера.

    Отчёт пишется в конце прогона, поэтому убитый контейнер не оставляет
    ничего. Без разбора кода возврата человек получил бы «тесты не
    запустились» и на решение, которое не импортируется, и на решение,
    сожравшее всю память, — то есть подсказку, ведущую не туда.
    """
    if returncode == 137:
        return (
            f"Решение исчерпало доступную память ({MEMORY_LIMIT}) и было остановлено. "
            "Чаще всего это попытка сложить в список бесконечный или очень "
            "большой источник — посмотри, не начинается ли решение с list(items)."
        )
    if returncode == 124:
        return "Прогон не уложился в отведённое время и был остановлен."
    return (
        "Тесты не запустились. Скорее всего, решение не импортируется: "
        "синтаксическая ошибка или отсутствует функция chunked."
    )


def run(kata: Kata, answer: str, *, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> KataResult:
    """Выполняет решение в песочнице и возвращает результат."""
    if not answer.strip():
        return KataResult(passed=False, total=0, failed=0, infrastructure_error="Пустое решение.")

    root = Path(tempfile.mkdtemp(prefix=f"dojo-kata-{uuid.uuid4().hex[:8]}-"))
    work = root / "work"
    out = root / "out"
    work.mkdir()
    out.mkdir()
    # Каталог отчёта должен быть доступен пользователю контейнера, который
    # заведомо не совпадает с владельцем временного каталога на хосте.
    out.chmod(0o777)

    try:
        (work / "solution.py").write_text(answer, encoding="utf-8")
        shutil.copy(kata.path / "tests_public.py", work / "tests_public.py")
        shutil.copy(kata.path / "tests_hidden.py", work / "tests_hidden.py")

        try:
            completed = subprocess.run(  # noqa: S603
                docker_run_args(work, out, timeout_sec),
                capture_output=True,
                text=True,
                timeout=timeout_sec + 10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return KataResult(
                passed=False,
                total=0,
                failed=0,
                timed_out=True,
                infrastructure_error=(
                    f"Решение не уложилось в {timeout_sec} с. Чаще всего это бесконечный "
                    "цикл или попытка материализовать бесконечный генератор."
                ),
            )

        report = out / "report.xml"
        if not report.is_file():
            return KataResult(
                passed=False,
                total=0,
                failed=0,
                stderr=(completed.stdout[-800:] + completed.stderr[-700:]),
                infrastructure_error=_explain_missing_report(completed.returncode),
            )

        cases = parse_junit(report.read_text(encoding="utf-8"))
        failed = sum(1 for case in cases if not case.passed)
        return KataResult(
            passed=failed == 0 and bool(cases),
            total=len(cases),
            failed=failed,
            cases=cases,
            stderr=completed.stderr[-1500:] if failed else "",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
