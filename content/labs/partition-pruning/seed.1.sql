-- Вариант 1: секционирования нет вовсе.
--
-- Обычная таблица, выросшая до неудобного размера. Задача целиком:
-- спроектировать секционирование и перенести данные, ничего не потеряв.

CREATE TABLE events (
    event_id   bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id  integer NOT NULL,
    kind       text NOT NULL,
    created_at timestamptz NOT NULL,
    payload    text NOT NULL
);

SELECT setseed(0.73);

INSERT INTO events (tenant_id, kind, created_at, payload)
SELECT
    1 + (random() * 200)::integer,
    (ARRAY['login', 'logout', 'purchase', 'view', 'error'])[1 + floor(random() * 5)::integer],
    TIMESTAMPTZ '2026-01-01' + random() * 89 * interval '1 day',
    repeat('x', 40)
FROM generate_series(1, 400000);

CREATE INDEX events_created_idx ON events (created_at);

ANALYZE events;
