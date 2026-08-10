# DE Dojo — точки входа для повседневной работы.
#
# Профили: make up PROFILE=ai | analytics | storage | full
# Без PROFILE поднимается только core (postgres, redis, api).

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV      ?= uv
COMPOSE ?= docker compose

# full разворачивается во все профили сразу; пустое значение оставляет core.
ifeq ($(PROFILE),full)
PROFILE_ARGS := --profile ai --profile analytics --profile storage
else ifeq ($(PROFILE),)
PROFILE_ARGS :=
else
PROFILE_ARGS := --profile $(PROFILE)
endif

# Сколько памяти требует профиль сверх core, в мегабайтах.
ifeq ($(PROFILE),full)
NEED_MB := 11800
else ifeq ($(PROFILE),ai)
NEED_MB := 9400
else ifeq ($(PROFILE),analytics)
NEED_MB := 5900
else ifeq ($(PROFILE),storage)
NEED_MB := 3400
else
NEED_MB := 2300
endif

.PHONY: help
help: ## Показать эту справку
	@echo "DE Dojo"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Профили: make up PROFILE=ai | analytics | storage | full"

# ── Окружение ────────────────────────────────────────────────────────────────

.PHONY: doctor
doctor: ## Проверить готовность машины и подобрать профиль LLM
	@$(UV) run --package dojo-cli dojo doctor

.PHONY: bootstrap
bootstrap: ## Поставить uv, Python и git-хуки
	@bash tools/bootstrap-python.sh

.PHONY: env
env: .env ## Создать .env из шаблона, если его нет
.env:
	@cp .env.example .env && echo "создан .env из .env.example"

# ── Стек ─────────────────────────────────────────────────────────────────────

.PHONY: check-ram
check-ram:
	@avail=$$(awk '/MemAvailable/ {print int($$2/1024)}' /proc/meminfo); \
	if [ "$$avail" -lt "$(NEED_MB)" ]; then \
		echo ""; \
		echo "  ВНИМАНИЕ: свободно $${avail} МБ, профилю нужно ~$(NEED_MB) МБ."; \
		echo "  Стек может уйти в своп или получить OOM."; \
		echo "  Подними memory в %USERPROFILE%\\.wslconfig и сделай wsl --shutdown,"; \
		echo "  либо погаси лишние профили: make down PROFILE=..."; \
		echo ""; \
	fi

.PHONY: up
up: env check-ram ## Поднять стек (core, либо PROFILE=...)
	@# --build обязателен: без него правка кода не попадает в контейнер, и
	@# человек полчаса выясняет, почему его изменение «не работает». При
	@# нетронутом коде сборка укладывается в пару секунд за счёт кеша слоёв.
	@$(COMPOSE) $(PROFILE_ARGS) up -d --build --quiet-pull
	@$(MAKE) --no-print-directory ps

.PHONY: start
start: up ## Поднять стек, применить миграции и залить контент — всё одной командой
	@printf 'жду готовности API'
	@for i in $$(seq 1 60); do \
		if curl -fsS http://127.0.0.1:$${API_HOST_PORT:-8000}/readyz >/dev/null 2>&1; then \
			echo " — готов"; break; \
		fi; \
		printf '.'; sleep 2; \
		if [ $$i -eq 60 ]; then \
			echo ""; echo "API не поднялся за две минуты. Смотри: make logs S=api"; exit 1; \
		fi; \
	done
	@$(MAKE) --no-print-directory migrate
	@$(MAKE) --no-print-directory sync
	@echo ""
	@echo "  Готово. API: http://127.0.0.1:$${API_HOST_PORT:-8000}"
	@echo "  Документация: http://127.0.0.1:$${API_HOST_PORT:-8000}/docs"

.PHONY: down
down: ## Остановить и удалить контейнеры (тома остаются)
	@$(COMPOSE) --profile ai --profile analytics --profile storage down

.PHONY: stop
stop: ## Остановить контейнеры, не удаляя их
	@$(COMPOSE) --profile ai --profile analytics --profile storage stop

.PHONY: ps
ps: ## Что сейчас запущено
	@$(COMPOSE) --profile ai --profile analytics --profile storage ps \
		--format 'table {{.Name}}\t{{.State}}\t{{.Status}}'

.PHONY: logs
logs: ## Хвост логов (S=имя сервиса)
	@$(COMPOSE) $(PROFILE_ARGS) logs -f --tail=100 $(S)

# ── Данные ───────────────────────────────────────────────────────────────────

.PHONY: migrate
migrate: ## Применить миграции
	@$(UV) run --package dojo python -m dojo.core.migrate

.PHONY: sync
sync: ## Спроецировать content/ в Postgres
	@$(UV) run --package dojo-cli dojo content sync

.PHONY: backup
backup: ## Сложить состояние в один архив
	@bash tools/backup.sh

# ── Качество ─────────────────────────────────────────────────────────────────

.PHONY: lint
lint: ## Линтер и проверка форматирования
	@$(UV) run ruff check .
	@$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Отформатировать и починить, что чинится автоматически
	@$(UV) run ruff check . --fix
	@$(UV) run ruff format .

.PHONY: typecheck
typecheck: ## mypy strict
	@$(UV) run mypy

.PHONY: validate
validate: ## Проверить учебный контент
	@$(UV) run python tools/content_validate.py

.PHONY: test
test: ## Быстрые тесты без IO
	@$(UV) run pytest tests/unit -q

.PHONY: test-int
test-int: ## Интеграционные тесты на testcontainers
	@$(UV) run pytest tests/integration -q

.PHONY: ci
ci: lint typecheck validate test ## Всё, что гоняет CI, локально

# ── Уборка ───────────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Удалить кеши инструментов
	@rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage
	@find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true

.PHONY: clean-data
clean-data: ## СНЕСТИ все тома: базы, модели, артефакты. Необратимо
	@printf 'Это удалит ВСЕ данные, включая скачанные модели. Введите YES: '; \
	read ans; \
	if [ "$$ans" = "YES" ]; then \
		$(COMPOSE) --profile ai --profile analytics --profile storage down -v; \
		echo "тома удалены"; \
	else \
		echo "отменено"; \
	fi
