-- Вариант 3: индекс есть, но не тот.
--
-- Индекс построен по created_at. Диапазон в отчёте — целый месяц, это заметная
-- доля таблицы, поэтому такой индекс планировщику невыгоден, и он выбирает
-- полный проход. Селективен здесь tenant_id, и он в индексе отсутствует.
--
-- Урок варианта — порядок и состав колонок в составном индексе. Человек,
-- увидевший «индекс есть», но не посмотревший, ПО ЧЕМУ он, застрянет.

CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  integer NOT NULL,
    created_at timestamptz NOT NULL,
    kind       text NOT NULL,
    payload    text NOT NULL
);

SELECT setseed(0.17);

INSERT INTO events (tenant_id, created_at, kind, payload)
SELECT
    CASE WHEN random() < 0.02 THEN 42 ELSE 1 + (random() * 200)::integer END,
    TIMESTAMPTZ '2025-06-01' + random() * 300 * interval '1 day',
    (ARRAY['login', 'logout', 'purchase', 'view', 'error'])[1 + floor(random() * 5)::integer],
    repeat('x', 40)
FROM generate_series(1, 200000);

-- Индекс, который выглядит подходящим, но не помогает этому запросу.
CREATE INDEX events_created_idx ON events (created_at);

ANALYZE events;
