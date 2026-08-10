#!/usr/bin/env bash
# Установка docker-ce напрямую в Ubuntu WSL2 (без Docker Desktop).
#
# Запускать БЕЗ sudo — скрипт сам вызовет sudo там, где нужно:
#     bash tools/install-docker.sh
#
# Почему не Docker Desktop: экономит ~700 МБ RAM (критично при 16 ГБ хоста)
# и даёт ровно тот же CLI, что стоит на проде — см. docs/adr/0004.

set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Не запускай через sudo — иначе docker-группа достанется root, а не тебе."
[[ -f /etc/os-release ]] || die "Не Ubuntu?"

# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "Скрипт рассчитан на Ubuntu, обнаружено: ${ID:-unknown}"
CODENAME="${VERSION_CODENAME:?не удалось определить кодовое имя релиза}"

log "Ubuntu ${VERSION_ID} (${CODENAME}), пользователь ${USER}"

if ! grep -qi microsoft /proc/version; then
  warn "Похоже, это не WSL. Скрипт всё равно отработает, но проверки WSL пропущены."
fi

# ── 1. Снимаем конфликтующие пакеты из репозиториев Ubuntu ────────────────────
log "Удаляю конфликтующие пакеты (docker.io, podman-docker и пр.), если стоят"
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  sudo apt-get remove -y "$pkg" >/dev/null 2>&1 || true
done

# ── 2. Официальный репозиторий Docker ────────────────────────────────────────
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

# ── 3. Установка ─────────────────────────────────────────────────────────────
log "Ставлю docker-ce + compose plugin (это займёт пару минут)"
sudo apt-get update -qq
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# ── 4. Ротация логов ─────────────────────────────────────────────────────────
# Без этого json-логи контейнеров растут безгранично и раздувают ext4.vhdx,
# который в WSL не сжимается автоматически без sparseVhd.
log "Настраиваю ротацию логов демона"
sudo install -d -m 0755 /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "default-address-pools": [
    { "base": "172.30.0.0/16", "size": 24 }
  ]
}
JSON

# ── 5. Автозапуск через systemd (в /etc/wsl.conf уже systemd=true) ───────────
log "Включаю автозапуск демона"
sudo systemctl enable --now docker.service containerd.service

# ── 6. Работа без sudo ───────────────────────────────────────────────────────
log "Добавляю ${USER} в группу docker"
sudo groupadd -f docker
sudo usermod -aG docker "$USER"

# ── 7. Проверка ──────────────────────────────────────────────────────────────
log "Проверяю установку"
sudo docker version --format '  server: {{.Server.Version}}' || die "демон не отвечает"
sudo docker compose version || die "compose plugin не установлен"

cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
ГОТОВО. Осталось два шага, оба обязательны:

1. Членство в группе docker подхватывается только при новом входе.
   Из PowerShell:   wsl --shutdown
   Затем снова открой WSL.

2. Проверь, что sudo больше не нужен:
   docker run --rm hello-world

Если после перезапуска docker не поднялся сам:
   sudo systemctl status docker
────────────────────────────────────────────────────────────────────────────
EOF
