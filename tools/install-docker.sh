#!/usr/bin/env bash
# Установка и донастройка docker-ce в Ubuntu/WSL2 (без Docker Desktop).
# Идемпотентен: если docker-ce уже стоит, установка пропускается и скрипт
# доводит до ума только то, чего не хватает (группа, ротация логов, автозапуск).
#
# Запускать БЕЗ sudo — скрипт сам вызовет sudo там, где нужно:
#     bash tools/install-docker.sh
#
# Почему не Docker Desktop: экономит ~700 МБ RAM (существенно при 16 ГБ хоста)
# и даёт ровно тот же CLI, что стоит на проде — см. docs/adr/0004.

set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
skip() { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Не запускай через sudo — иначе docker-группа достанется root, а не тебе."
[[ -f /etc/os-release ]] || die "Не Ubuntu?"

# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "Скрипт рассчитан на Ubuntu, обнаружено: ${ID:-unknown}"
CODENAME="${VERSION_CODENAME:?не удалось определить кодовое имя релиза}"

log "Ubuntu ${VERSION_ID} (${CODENAME}), пользователь ${USER}"
grep -qi microsoft /proc/version || warn "Похоже, это не WSL — проверки WSL пропущены."

NEED_RELOGIN=0

# ── 1. Установка docker-ce, если его ещё нет ─────────────────────────────────
if dpkg -s docker-ce >/dev/null 2>&1; then
  skip "docker-ce уже установлен ($(docker --version | awk '{print $3}' | tr -d ,))"
else
  log "Удаляю конфликтующие пакеты из репозиториев Ubuntu"
  for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    sudo apt-get remove -y "$pkg" >/dev/null 2>&1 || true
  done

  log "Подключаю официальный APT-репозиторий Docker"
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl gnupg

  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
       -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

  log "Ставлю docker-ce + compose plugin (пара минут)"
  sudo apt-get update -qq
  sudo apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# ── 2. Ротация логов ─────────────────────────────────────────────────────────
# Без неё json-логи контейнеров растут безгранично и раздувают ext4.vhdx,
# который в WSL не сжимается сам без sparseVhd в .wslconfig.
DAEMON_JSON=/etc/docker/daemon.json
if [[ -f $DAEMON_JSON ]] && grep -q '"max-size"' "$DAEMON_JSON"; then
  skip "ротация логов уже настроена"
else
  if [[ -f $DAEMON_JSON ]]; then
    warn "$DAEMON_JSON существует без ротации логов — сохраняю копию в ${DAEMON_JSON}.bak"
    sudo cp -n "$DAEMON_JSON" "${DAEMON_JSON}.bak"
    warn "объедини настройки вручную, перезаписывать чужой конфиг не буду"
  else
    log "Настраиваю ротацию логов демона"
    sudo install -d -m 0755 /etc/docker
    sudo tee "$DAEMON_JSON" >/dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "default-address-pools": [
    { "base": "172.30.0.0/16", "size": 24 }
  ]
}
JSON
    sudo systemctl restart docker
  fi
fi

# ── 3. Автозапуск ────────────────────────────────────────────────────────────
if [[ "$(systemctl is-enabled docker 2>/dev/null)" == "enabled" ]]; then
  skip "автозапуск демона включён"
else
  log "Включаю автозапуск демона"
  sudo systemctl enable --now docker.service containerd.service
fi

# ── 4. Работа без sudo ───────────────────────────────────────────────────────
if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  skip "$USER уже в группе docker"
else
  log "Добавляю ${USER} в группу docker"
  sudo groupadd -f docker
  sudo usermod -aG docker "$USER"
  NEED_RELOGIN=1
fi

# ── 5. Проверка ──────────────────────────────────────────────────────────────
log "Проверяю доступ к демону"
if docker version --format '  server: {{.Server.Version}}' 2>/dev/null; then
  docker compose version
  echo
  skip "docker готов к работе, перезапуск не нужен"
  exit 0
fi

if [[ $NEED_RELOGIN -eq 1 ]]; then
  cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
Docker установлен, но членство в группе подхватывается только при новом входе.

  1. В PowerShell:   wsl --shutdown
  2. Снова открой WSL и проверь:   docker run --rm hello-world
────────────────────────────────────────────────────────────────────────────
EOF
  exit 0
fi

die "демон не отвечает, хотя группа на месте. Смотри: systemctl status docker"
