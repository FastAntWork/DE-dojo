-- Вариант 1: индекса нет вовсе.
--
-- Самая простая причина и самая частая в жизни: таблица выросла, а индекс под
-- фильтр никто не создал. Планировщик честно выбирает полный проход, потому
-- что другого пути у него нет.

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
    -- Арендатор 42 — примерно два процента строк. Селективность высокая,
    -- поэтому индекс здесь однозначно выгоден.
    CASE WHEN random() < 0.02 THEN 42 ELSE 1 + (random() * 200)::integer END,
    TIMESTAMPTZ '2025-06-01' + random() * 300 * interval '1 day',
    (ARRAY['login', 'logout', 'purchase', 'view', 'error'])[1 + floor(random() * 5)::integer],
    repeat('x', 40)
FROM generate_series(1, 200000);

ANALYZE events;
