#!/usr/bin/env bash
# Запуск DE Dojo на macOS и Linux.
#
# Аналог Dojo.cmd для Windows: поднимает стек, ждёт готовности, применяет
# миграции, заливает контент и открывает окно приложения.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  cat <<'EOF'

  Не найден uv — менеджер зависимостей Python. Установи:

    curl -LsSf https://astral.sh/uv/install.sh | sh

EOF
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  cat <<'EOF'

  Не найден docker. Поставь Docker Desktop (macOS) или docker-ce (Linux):
  на Linux достаточно  bash tools/install-docker.sh

EOF
  exit 1
fi

echo "  Готовлю окружение, первый запуск займёт несколько минут..."
uv sync --all-packages --quiet

exec uv run dojo start "$@"
