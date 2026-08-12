"""Прогон kata в песочнице.

Проверяется две вещи, и вторая важнее первой:

1. Эталон проходит свои тесты, а заведомо неверные решения — не проходят.
2. Песочница действительно изолирует. Здесь исполняется чужой код, и если
   ограничения не работают, об этом должен узнать тест, а не пользователь.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dojo.runner.kata import (
    KataResult,
    build_image,
    docker_run_args,
    image_exists,
    load_kata,
    parse_junit,
    run,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT = REPO_ROOT / "content"


@pytest.fixture(scope="module", autouse=True)
def sandbox_image() -> None:
    if not image_exists():
        build_image(REPO_ROOT)


@pytest.fixture(scope="module")
def kata() -> object:
    return load_kata(CONTENT, "chunked")


def solve(kata: object, source: str) -> KataResult:
    return run(kata, source, timeout_sec=30)  # type: ignore[arg-type]


class TestReferenceSolution:
    def test_reference_passes_all_tests(self, kata: object) -> None:
        solution = (CONTENT / "katas" / "chunked" / "solution.py").read_text(encoding="utf-8")

        result = solve(kata, solution)

        assert result.passed, [c.name for c in result.cases if not c.passed]
        assert result.total >= 10, "скрытых тестов должно быть заметно больше открытых"
        assert result.score == 1.0

    def test_starter_does_not_pass(self, kata: object) -> None:
        # Заготовка обязана падать: иначе задача решается ничегонеделанием.
        starter = (CONTENT / "katas" / "chunked" / "starter.py").read_text(encoding="utf-8")

        result = solve(kata, starter)

        assert result.passed is False


class TestCatchesWrongSolutions:
    def test_eager_solution_is_stopped_by_memory_limit(self, kata: object) -> None:
        """Решение, начинающееся с list(items), не должно пройти.

        На бесконечном источнике оно упирается в лимит памяти и убивается.
        Проверяется не только сам факт незачёта, но и внятность объяснения:
        сообщение обязано указывать на list(items), а не говорить «тесты не
        запустились».
        """
        eager = """
def chunked(items, size):
    if size <= 0:
        raise ValueError("size")
    data = list(items)
    return iter([data[i:i + size] for i in range(0, len(data), size)])
"""
        result = solve(kata, eager)

        assert result.passed is False
        assert result.infrastructure_error is not None
        assert "list(items)" in result.infrastructure_error
        assert "памят" in result.infrastructure_error

    def test_generator_only_solution_fails_eager_error_check(self, kata: object) -> None:
        """Функция-генератор целиком откладывает проверку аргумента."""
        deferred = """
def chunked(items, size):
    if size <= 0:
        raise ValueError("size")
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
"""
        result = solve(kata, deferred)

        assert result.passed is False
        failed = {c.name for c in result.cases if not c.passed}
        assert "test_error_raised_at_call_not_at_iteration" in failed

    def test_broken_import_reports_collection_failure(self, kata: object) -> None:
        result = solve(kata, "это не питон((")

        assert result.passed is False
        # pytest сообщает о несобравшемся модуле как об отдельном «тесте» —
        # человек увидит именно это, а не молчаливый ноль.
        assert all(not case.passed for case in result.cases)
        assert result.score == 0.0

    def test_empty_answer_rejected(self, kata: object) -> None:
        assert solve(kata, "   ").passed is False


class TestSandboxIsolation:
    """Ограничения песочницы. Каждое — отдельная строка в docker run."""

    def test_network_is_unavailable(self, kata: object) -> None:
        code = """
import socket


def chunked(items, size):
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    return iter([])
"""
        result = solve(kata, code)

        assert result.passed is False

    def test_filesystem_is_read_only(self, kata: object) -> None:
        code = """
from pathlib import Path


def chunked(items, size):
    Path("/work/hacked.txt").write_text("x")
    return iter([])
"""
        result = solve(kata, code)

        assert result.passed is False
        assert not (CONTENT / "katas" / "chunked" / "hacked.txt").exists()

    def test_infinite_loop_is_killed_by_timeout(self, kata: object) -> None:
        code = """
def chunked(items, size):
    while True:
        pass
"""
        result = run(kata, code, timeout_sec=5)  # type: ignore[arg-type]

        assert result.passed is False
        assert result.timed_out or result.failed > 0

    def test_docker_socket_is_never_mounted(self) -> None:
        # Проброс docker.sock отдал бы песочнице полный контроль над хостом.
        args = docker_run_args(Path("/tmp/w"), Path("/tmp/o"), 30)

        assert not any("docker.sock" in a for a in args)

    def test_run_args_contain_all_limits(self) -> None:
        args = " ".join(docker_run_args(Path("/tmp/w"), Path("/tmp/o"), 30))

        assert "--network none" in args
        assert "--memory=256m" in args
        assert "--pids-limit=64" in args
        assert "--cap-drop=ALL" in args
        assert "--read-only" in args
        assert "--user=10001:10001" in args
        assert "/work:ro" in args


def all_kata_names() -> list[str]:
    """Имена всех кат в репозитории.

    Список берётся с диска, а не перечисляется: новая ката должна попадать под
    проверку автоматически, иначе однажды в графе окажется задача, эталон
    которой не проходит собственные тесты.
    """
    return sorted(path.name for path in (CONTENT / "katas").iterdir() if path.is_dir())


class TestEveryKataIsSolvable:
    """Инвариант на все каты сразу, а не только на первую написанную."""

    @pytest.mark.parametrize("name", all_kata_names())
    def test_reference_solution_passes(self, name: str) -> None:
        kata = load_kata(CONTENT, name)
        solution = (CONTENT / "katas" / name / "solution.py").read_text(encoding="utf-8")

        result = run(kata, solution, timeout_sec=60)

        assert result.passed, f"{name}: " + str([c.name for c in result.cases if not c.passed])
        assert result.score == 1.0

    @pytest.mark.parametrize("name", all_kata_names())
    def test_starter_fails(self, name: str) -> None:
        """Заготовка обязана падать: иначе задача решается ничегонеделанием."""
        kata = load_kata(CONTENT, name)
        starter = (CONTENT / "katas" / name / "starter.py").read_text(encoding="utf-8")

        result = run(kata, starter, timeout_sec=60)

        assert result.passed is False, f"{name}: заготовка проходит тесты"

    @pytest.mark.parametrize("name", all_kata_names())
    def test_hidden_tests_outnumber_public(self, name: str) -> None:
        """Скрытых проверок должно быть больше: открытые — это примеры."""
        kata = load_kata(CONTENT, name)
        solution = (CONTENT / "katas" / name / "solution.py").read_text(encoding="utf-8")

        result = run(kata, solution, timeout_sec=60)

        hidden = sum(1 for case in result.cases if case.hidden)
        public = sum(1 for case in result.cases if not case.hidden)
        assert hidden > public, f"{name}: скрытых {hidden}, открытых {public}"


class TestReportParsing:
    def test_marks_hidden_tests(self) -> None:
        xml = """<testsuites><testsuite>
            <testcase classname="tests_public" name="test_a"/>
            <testcase classname="tests_hidden" name="test_b"><failure message="boom"/></testcase>
        </testsuite></testsuites>"""

        cases = parse_junit(xml)

        assert cases[0].hidden is False
        assert cases[1].hidden is True
        assert cases[1].passed is False
        assert cases[1].message == "boom"
