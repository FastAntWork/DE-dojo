-- Вариант 2: индекс есть, но статистика врёт.
--
-- Данные массово поменялись, ANALYZE не выполнялся, autovacuum на таблице
-- выключен. Планировщик считает, что арендатор 42 занимает почти всю таблицу,
-- и отказывается от индекса в пользу полного прохода.
--
-- Разница с вариантом 1 принципиальная: индекс на месте, и человек, который
-- решает такие задачи созданием индексов, здесь застрянет.

CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  integer NOT NULL,
    created_at timestamptz NOT NULL,
    kind       text NOT NULL,
    payload    text NOT NULL
) WITH (autovacuum_enabled = false);

SELECT setseed(0.17);

-- Сначала наполняем так, будто арендатор 42 занимает почти всё.
INSERT INTO events (tenant_id, created_at, kind, payload)
SELECT
    CASE WHEN random() < 0.95 THEN 42 ELSE 1 + (random() * 200)::integer END,
    TIMESTAMPTZ '2025-06-01' + random() * 300 * interval '1 day',
    (ARRAY['login', 'logout', 'purchase', 'view', 'error'])[1 + floor(random() * 5)::integer],
    repeat('x', 40)
FROM generate_series(1, 200000);

CREATE INDEX events_tenant_created_idx ON events (tenant_id, created_at);

-- Собираем статистику на этих данных — она запомнит, что 42 везде.
ANALYZE events;

-- А теперь массово переписываем: почти все строки уезжают к другим арендаторам.
-- Статистика при этом остаётся прежней, и планировщик продолжает считать, что
-- по 42 вернётся почти вся таблица.
UPDATE events
SET tenant_id = 1 + (random() * 200)::integer
WHERE tenant_id = 42 AND random() < 0.98;
