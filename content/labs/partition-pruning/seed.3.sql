-- Вариант 3: ключ верный, но все данные в секции по умолчанию.
--
-- Таблица секционирована по времени, и это правильно. Только диапазонных
-- секций никто не завёл: есть одна секция DEFAULT, и в неё сложилось всё.
-- Отсечение при этом бесполезно — секция одна, и читается она целиком.
--
-- Второй урок варианта — ловушка присоединения: пока в DEFAULT лежат строки,
-- подходящие под новый диапазон, присоединить новую секцию нельзя. Сначала
-- эти строки надо оттуда убрать.

CREATE TABLE events (
    event_id   bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id  integer NOT NULL,
    kind       text NOT NULL,
    created_at timestamptz NOT NULL,
    payload    text NOT NULL
) PARTITION BY RANGE (created_at);

CREATE TABLE events_default PARTITION OF events DEFAULT;

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
