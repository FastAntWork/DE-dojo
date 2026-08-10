#!/usr/bin/env bash
# Полный бэкап состояния Dojo в один архив.
#
#     make backup      (или bash tools/backup.sh)
#
# Postgres — единственный источник истины по состоянию обучения (docs/adr/0002),
# поэтому pg_dump плюс репозиторий контента восстанавливают систему целиком.
# ClickHouse, Redis и метрики сознательно НЕ бэкапятся: они производны и
# пересчитываются из событий и из Postgres.
#
# Ничего никуда не отправляется: архив остаётся на диске.

set -euo pipefail

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

cd "$(dirname "$0")/.."
REPO_ROOT="$PWD"

STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
OUT_DIR="${DOJO_BACKUP_DIR:-$REPO_ROOT/backups}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$OUT_DIR"

# ── Postgres ─────────────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -qx dojo-postgres; then
  log "pg_dump"
  # Формат custom, а не plain SQL: он сжат и допускает выборочное
  # восстановление отдельных таблиц.
  docker exec dojo-postgres pg_dump -U "${POSTGRES_USER:-dojo}" -d "${POSTGRES_DB:-dojo}" -Fc \
    > "$WORK/postgres.dump"
  printf '  %s\n' "$(du -h "$WORK/postgres.dump" | cut -f1)"
else
  warn "контейнер dojo-postgres не запущен — состояние обучения в бэкап не попадёт"
fi

# ── Контент и код ────────────────────────────────────────────────────────────
# git bundle вместо копии каталога: сохраняется вся история контента, а
# восстановление это обычный git clone из файла.
log "git bundle"
git bundle create "$WORK/repo.bundle" --all 2>/dev/null
printf '  %s\n' "$(du -h "$WORK/repo.bundle" | cut -f1)"

if ! git diff --quiet || ! git diff --cached --quiet; then
  warn "в рабочем дереве есть незакоммиченные правки — в bundle они не войдут"
  git status --short > "$WORK/uncommitted.txt"
fi

# ── MinIO ────────────────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -qx dojo-minio; then
  log "артефакты MinIO"
  docker exec dojo-minio mc alias set local http://127.0.0.1:9000 \
    "${MINIO_USER:-dojo}" "${MINIO_PASSWORD:-dojo_local_dev}" >/dev/null 2>&1 || true
  docker exec dojo-minio sh -c 'rm -rf /tmp/backup && mc mirror --quiet local /tmp/backup' \
    >/dev/null 2>&1 || warn "mc mirror не отработал, артефакты пропущены"
  docker cp dojo-minio:/tmp/backup "$WORK/minio" >/dev/null 2>&1 || true
else
  warn "dojo-minio не запущен — транскрипты и решения в бэкап не попадут"
fi

# ── Метаданные ───────────────────────────────────────────────────────────────
{
  echo "created_at: $STAMP"
  echo "git_head:   $(git rev-parse HEAD)"
  echo "git_branch: $(git rev-parse --abbrev-ref HEAD)"
  echo "host:       $(uname -sr)"
} > "$WORK/manifest.txt"

ARCHIVE="$OUT_DIR/dojo-$STAMP.tar.gz"
log "упаковываю в $ARCHIVE"
tar -czf "$ARCHIVE" -C "$WORK" .

cat <<EOF

────────────────────────────────────────────────────────────────────────────
Готово: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))

Восстановление:
  tar -xzf <архив> -C /tmp/restore
  git clone /tmp/restore/repo.bundle ~/de-dojo
  make up && make migrate
  docker exec -i dojo-postgres pg_restore -U dojo -d dojo --clean \\
      < /tmp/restore/postgres.dump
────────────────────────────────────────────────────────────────────────────
EOF
