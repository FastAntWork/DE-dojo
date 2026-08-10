# Сборка API. Два слоя зависимостей: сначала lock-файл, потом исходники —
# правка кода не инвалидирует слой с пакетами.
#
# Директива `# syntax=docker/dockerfile:N` намеренно НЕ используется: она
# заставляет BuildKit тянуть фронтенд с Docker Hub на каждой сборке, из-за
# чего сборка перестаёт работать офлайн. Всё, что здесь нужно (RUN --mount),
# встроенный фронтенд Docker 23+ умеет сам.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# uv ставится из PyPI, а не копируется из ghcr.io/astral-sh/uv: PyPI нужен
# для зависимостей в любом случае, а лишний реестр — лишняя точка отказа.
# На этой машине ghcr.io уже отваливался по таймауту в середине сборки.
RUN pip install --no-cache-dir uv==0.12.3

WORKDIR /app

# ── Слой зависимостей ────────────────────────────────────────────────────────
# Копируем только манифесты воркспейса: uv sync читает их и uv.lock,
# исходники ему на этом шаге не нужны.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml   apps/api/
COPY apps/cli/pyproject.toml   apps/cli/
COPY apps/runner/pyproject.toml apps/runner/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package dojo --frozen --no-install-project --no-dev

# ── Слой приложения ──────────────────────────────────────────────────────────
COPY apps/api/src apps/api/src
COPY migrations migrations

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package dojo --frozen --no-dev

# Непривилегированный пользователь: контейнер API не имеет никаких причин
# работать от root, а Runner тем более (там исполняется чужой код).
RUN useradd --create-home --uid 10001 dojo && chown -R dojo:dojo /app
USER dojo

EXPOSE 8000

# Без --reload: перезагрузчик держит вотчеры на всё дерево, а в WSL с
# bind-mount это заметная нагрузка. Для разработки есть отдельный override.
CMD ["uvicorn", "dojo.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
