-- Начальная схема DE Dojo.
--
-- Принцип: Postgres — единственный источник истины по состоянию обучения.
-- Всё остальное (ClickHouse, Redis, MinIO) производно и восстановимо, поэтому
-- один pg_dump является полным бэкапом. См. docs/adr/0002.
--
-- Таблицы skills/tasks — ПРОЕКЦИЯ YAML-файлов из content/, а не источник
-- истины: их пересобирает `dojo content sync`. Отсюда поля source_path и
-- content_hash в обеих.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE task_type AS ENUM (
    'flashcard', 'quiz', 'sql', 'kata', 'lab',
    'review', 'design', 'interview', 'capstone'
);

CREATE TYPE attempt_status AS ENUM (
    'running', 'passed', 'failed', 'timeout', 'error', 'abandoned'
);

CREATE TYPE fsrs_state AS ENUM ('new', 'learning', 'review', 'relearning');


-- ════════════════════════════════════════════════════════════════════════════
-- ГРАФ НАВЫКОВ
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE skills (
    id                text         PRIMARY KEY,          -- 'kafka.consumer-groups'
    title             text         NOT NULL,
    track             text         NOT NULL,
    level             smallint     NOT NULL CHECK (level BETWEEN 1 AND 5),
    estimated_hours   numeric(4,1) NOT NULL CHECK (estimated_hours > 0),
    objectives        jsonb        NOT NULL DEFAULT '[]',
    job_tags          text[]       NOT NULL DEFAULT '{}',
    theory_path       text,
    rag_scope         text[]       NOT NULL DEFAULT '{}',
    review_after_days integer[]    NOT NULL DEFAULT '{1,4,12,30}',

    -- Вес узла на рынке труда. Перезаписывается пайплайном вакансий (M7),
    -- поэтому не приходит из YAML и не участвует в content_hash.
    job_weight        numeric(5,4) NOT NULL DEFAULT 0 CHECK (job_weight BETWEEN 0 AND 1),

    source_path       text         NOT NULL,   -- откуда спроецировано
    content_hash      text         NOT NULL,   -- sha256 файла: инкрементальный ресинк

    -- Узел, исчезнувший из content/, помечается, а не удаляется:
    -- на него ссылаются attempts, а историю попыток терять нельзя.
    deprecated_at     timestamptz,

    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX skills_track_idx ON skills (track) WHERE deprecated_at IS NULL;
CREATE INDEX skills_job_tags_idx ON skills USING gin (job_tags);

COMMENT ON TABLE skills IS
    'Проекция content/tracks/**.yaml. Источник истины — файлы, не эта таблица.';


-- parent — предпосылка для child.
CREATE TABLE skill_edges (
    parent_id text    NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    child_id  text    NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    -- hard=false: «желательно, но не блокирует выдачу узла планировщиком».
    hard      boolean NOT NULL DEFAULT true,
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);

CREATE INDEX skill_edges_child_idx ON skill_edges (child_id);

-- Ацикличность проверяет tools/content_validate.py в CI, а не триггер в БД:
-- цикл должен ломать сборку на этапе валидации контента, а не всплывать
-- при попытке записи в конце пайплайна.


-- ════════════════════════════════════════════════════════════════════════════
-- ЗАДАНИЯ
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE tasks (
    id           text         PRIMARY KEY,      -- 'sql.joins::sql::003'
    skill_id     text         NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    type         task_type    NOT NULL,
    ordinal      smallint     NOT NULL DEFAULT 0,

    -- Сложность в той же шкале, что и mastery: expected = сигмоида(mastery - difficulty).
    difficulty   numeric(3,2) NOT NULL DEFAULT 0.50 CHECK (difficulty BETWEEN 0 AND 1),

    -- Число вариантов chaos-режима: seed.sh --variant N ломает стенд по-разному,
    -- чтобы решение нельзя было выучить наизусть.
    variants     smallint     NOT NULL DEFAULT 1 CHECK (variants >= 1),
    timeout_sec  integer      NOT NULL DEFAULT 900 CHECK (timeout_sec > 0),

    -- Тип-специфичная часть: path / file / rubric / tests[] / repo_task.
    -- В отдельные колонки не разложено сознательно: у девяти типов заданий
    -- почти не пересекающиеся наборы полей, таблица стала бы решетом из NULL.
    spec         jsonb        NOT NULL,
    hints        jsonb        NOT NULL DEFAULT '[]',   -- [{level, text, penalty}]

    source_path  text         NOT NULL,
    content_hash text         NOT NULL,
    created_at   timestamptz  NOT NULL DEFAULT now(),
    updated_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX tasks_skill_type_idx ON tasks (skill_id, type);


-- ════════════════════════════════════════════════════════════════════════════
-- ПОПЫТКИ — append-only, полная история обучения
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE attempts (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id        text           NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    skill_id       text           NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    variant        smallint       NOT NULL DEFAULT 0,
    attempt_no     smallint       NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    status         attempt_status NOT NULL DEFAULT 'running',

    -- score — сырой вердикт чекера; result — он же после штрафов за подсказки
    -- и повторные попытки. В формулу mastery идёт именно result.
    score          numeric(3,2) CHECK (score  BETWEEN 0 AND 1),
    result         numeric(3,2) CHECK (result BETWEEN 0 AND 1),
    hints_used     smallint       NOT NULL DEFAULT 0 CHECK (hints_used >= 0),

    -- Снимок mastery до и после. Благодаря им всю кривую обучения можно
    -- пересчитать с нуля, а skill_states остаётся всего лишь кешем.
    mastery_before numeric(4,3),
    mastery_after  numeric(4,3),

    checks         jsonb,   -- сырой stdout check.py или отчёт pytest
    judge_rubric   jsonb,   -- для design/interview: пункт → балл + цитата-подтверждение
    artifact_uri   text,    -- s3://dojo-artifacts/...

    duration_ms    integer CHECK (duration_ms >= 0),
    started_at     timestamptz    NOT NULL DEFAULT now(),
    finished_at    timestamptz,

    CHECK (status = 'running' OR finished_at IS NOT NULL),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX attempts_skill_finished_idx ON attempts (skill_id, finished_at DESC);
CREATE INDEX attempts_task_started_idx   ON attempts (task_id, started_at DESC);

COMMENT ON COLUMN attempts.judge_rubric IS
    'Балл по каждому пункту рубрики плюс цитата из ответа. Нет цитаты — пункт не засчитан.';


-- Материализованное состояние узла. Производно от attempts.
CREATE TABLE skill_states (
    skill_id        text         PRIMARY KEY REFERENCES skills(id) ON DELETE CASCADE,
    mastery         numeric(4,3) NOT NULL DEFAULT 0 CHECK (mastery BETWEEN 0 AND 1),
    attempts_count  integer      NOT NULL DEFAULT 0 CHECK (attempts_count >= 0),

    reached_08_at   timestamptz,   -- когда mastery впервые взяла 0.8
    -- Узел закрыт при mastery >= 0.8 И успешном повторе через >= 7 дней:
    -- одного удачного дня недостаточно, нас интересует удержание.
    closed_at       timestamptz,

    last_attempt_at timestamptz,
    updated_at      timestamptz  NOT NULL DEFAULT now(),

    CHECK (closed_at IS NULL OR reached_08_at IS NOT NULL),
    CHECK (closed_at IS NULL OR closed_at >= reached_08_at + interval '7 days')
);


-- ════════════════════════════════════════════════════════════════════════════
-- ИНТЕРВАЛЬНЫЕ ПОВТОРЕНИЯ (FSRS)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE reviews_schedule (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_kind      text       NOT NULL CHECK (item_kind IN ('skill', 'card', 'task')),
    item_id        text       NOT NULL,
    skill_id       text       NOT NULL REFERENCES skills(id) ON DELETE CASCADE,

    state          fsrs_state NOT NULL DEFAULT 'new',
    stability      real       NOT NULL DEFAULT 0 CHECK (stability >= 0),
    difficulty     real       NOT NULL DEFAULT 0,
    due            timestamptz NOT NULL DEFAULT now(),
    last_review    timestamptz,
    reps           integer    NOT NULL DEFAULT 0 CHECK (reps >= 0),
    lapses         integer    NOT NULL DEFAULT 0 CHECK (lapses >= 0),
    scheduled_days integer    NOT NULL DEFAULT 0,
    suspended      boolean    NOT NULL DEFAULT false,

    UNIQUE (item_kind, item_id)
);

-- Частичный индекс: планировщик всегда спрашивает «что просрочено и не снято».
CREATE INDEX reviews_schedule_due_idx ON reviews_schedule (due) WHERE NOT suspended;

-- Сырьё для переоптимизации параметров FSRS под конкретного человека.
CREATE TABLE review_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schedule_id  bigint     NOT NULL REFERENCES reviews_schedule(id) ON DELETE CASCADE,
    rating       smallint   NOT NULL CHECK (rating BETWEEN 1 AND 4),
    state_before fsrs_state NOT NULL,
    elapsed_days integer    NOT NULL CHECK (elapsed_days >= 0),
    reviewed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX review_log_schedule_idx ON review_log (schedule_id, reviewed_at DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- VACANCY-DRIVEN ВЕСА
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE job_postings (
    id             text        PRIMARY KEY,       -- 'hh:136073653'
    source         text        NOT NULL,
    external_id    text        NOT NULL,
    url            text        NOT NULL,
    title          text        NOT NULL,
    company        text,
    area           text,
    salary_from    integer,
    salary_to      integer,
    salary_currency text,
    published_at   timestamptz,

    raw_ref        text,       -- ObjectId сырья в MongoDB: там версии, тут витрина
    body_hash      text        NOT NULL,
    parser_version smallint    NOT NULL DEFAULT 1,
    parsed_at      timestamptz,
    embedding      vector(1024),   -- bge-m3

    is_target      boolean     NOT NULL DEFAULT false,  -- вакансии из блока вводных
    created_at     timestamptz NOT NULL DEFAULT now(),

    UNIQUE (source, external_id),
    CHECK (salary_to IS NULL OR salary_from IS NULL OR salary_to >= salary_from)
);

CREATE INDEX job_postings_embedding_idx ON job_postings
    USING hnsw (embedding vector_cosine_ops);


-- Извлечённые требования. evidence обязателен: без дословной цитаты
-- утверждение «навык встречается в 73% вакансий» непроверяемо, и ошибку
-- парсера не отличить от реального сигнала рынка.
CREATE TABLE job_posting_skills (
    posting_id text         NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
    skill_id   text         NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    confidence numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence   text         NOT NULL CHECK (length(trim(evidence)) > 0),
    matched_by text         NOT NULL CHECK (matched_by IN ('dict', 'llm', 'embedding')),
    PRIMARY KEY (posting_id, skill_id)
);

CREATE INDEX job_posting_skills_skill_idx ON job_posting_skills (skill_id);


-- Недельные снапшоты: история весов сохраняется, сдвиг рынка видно.
CREATE TABLE job_skill_stats (
    snapshot_date       date         NOT NULL,
    skill_id            text         NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    postings_total      integer      NOT NULL CHECK (postings_total >= 0),
    postings_with_skill integer      NOT NULL CHECK (postings_with_skill >= 0),
    frequency           numeric(5,4) GENERATED ALWAYS AS (
        postings_with_skill::numeric / NULLIF(postings_total, 0)
    ) STORED,
    job_weight          numeric(5,4) NOT NULL CHECK (job_weight BETWEEN 0 AND 1),

    PRIMARY KEY (snapshot_date, skill_id),
    CHECK (postings_with_skill <= postings_total)
);


-- ════════════════════════════════════════════════════════════════════════════
-- СЕССИИ И СОБЫТИЯ
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE sessions (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    -- Что выдал планировщик и из какой корзины: повторение / новое /
    -- закрепление / интерливинг. Нужно, чтобы потом оценить сами пропорции.
    plan       jsonb       NOT NULL DEFAULT '{}',

    CHECK (ended_at IS NULL OR ended_at >= started_at)
);


-- Транзакционный outbox. Заводится сразу, хотя Kafka появится только в M6:
-- иначе к M6 публикацию событий пришлось бы ретроспективно вшивать во все
-- обработчики, а так они с самого начала пишут события в той же транзакции,
-- что и изменение состояния.
CREATE TABLE event_outbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic        text        NOT NULL,
    key          text        NOT NULL,
    payload      jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);

CREATE INDEX event_outbox_unpublished_idx ON event_outbox (id) WHERE published_at IS NULL;
