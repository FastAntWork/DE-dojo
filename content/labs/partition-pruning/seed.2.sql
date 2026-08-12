-- Вариант 2: секционирование есть, но не по тому ключу.
--
-- Таблица разбита по арендатору. Выглядит осмысленно: запросы часто фильтруют
-- по tenant_id. Но чистка истории идёт по времени, и по времени же идёт
-- месячный отчёт — а по нему отсечение не работает: события каждого месяца
-- размазаны по всем секциям.
--
-- Урок варианта: ключ секционирования выбирается под ту задачу, ради которой
-- секционирование затевалось. Если это удаление старых данных — ключ обязан
-- быть временем.

CREATE TABLE events (
    event_id   bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id  integer NOT NULL,
    kind       text NOT NULL,
    created_at timestamptz NOT NULL,
    payload    text NOT NULL
) PARTITION BY HASH (tenant_id);

CREATE TABLE events_h0 PARTITION OF events FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE events_h1 PARTITION OF events FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE events_h2 PARTITION OF events FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE events_h3 PARTITION OF events FOR VALUES WITH (MODULUS 4, REMAINDER 3);

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
