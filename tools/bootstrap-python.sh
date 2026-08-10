#!/usr/bin/env bash
# Поднимает Python-окружение монорепо. Ничего не требует от root.
#     bash tools/bootstrap-python.sh

set -euo pipefail

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  log "Ставлю uv (менеджер пакетов и версий Python)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # установщик кладёт бинарь сюда и правит профиль, но текущий шелл об этом не знает
  export PATH="$HOME/.local/bin:$PATH"
fi

log "uv $(uv --version | awk '{print $2}')"

log "Ставлю Python 3.12 и синхронизирую workspace"
uv python install 3.12
uv sync --all-packages

log "Ставлю git-хуки"
uv run pre-commit install --install-hooks -t pre-commit -t commit-msg

cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
Окружение готово.

  uv run ruff check .        линт
  uv run mypy               типы (strict)
  uv run pytest tests/unit  быстрые тесты

Если команда `uv` не находится в новом терминале — добавь в ~/.bashrc:
  export PATH="$HOME/.local/bin:$PATH"
────────────────────────────────────────────────────────────────────────────
EOF
