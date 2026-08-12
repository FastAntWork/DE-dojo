-- Вариант 3: всё сразу, включая закрытую текущую версию.
--
-- Самый жизненный случай: измерение чинили несколько раз разными способами,
-- и теперь в нём есть и пересечения, и разрывы, и версии с проставленным
-- valid_to при поднятом флаге актуальности.
--
-- Отдельная ловушка: у части клиентов последняя версия закрыта конкретной
-- датой. Соединение по «дате события между valid_from и valid_to» перестанет
-- находить их после этой даты — клиент исчезнет из отчётов, оставшись в базе.

CREATE TABLE dim_customer (
    version_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id integer NOT NULL,
    city        text NOT NULL,
    plan        text NOT NULL,
    valid_from  date NOT NULL,
    valid_to    date,
    is_current  boolean NOT NULL
);

CREATE TABLE _seed_versions (
    customer_id integer NOT NULL,
    valid_from  date NOT NULL,
    city        text NOT NULL,
    plan        text NOT NULL
);

SELECT setseed(0.53);

CREATE TEMP TABLE gen AS
SELECT
    c.customer_id,
    DATE '2024-01-01' + (v.n * 90 + floor(random() * 60)::integer) AS valid_from,
    (ARRAY['Москва', 'Казань', 'Пермь', 'Омск', 'Тверь'])[
        1 + floor(random() * 5)::integer
    ] AS city,
    (ARRAY['basic', 'pro', 'enterprise'])[1 + floor(random() * 3)::integer] AS plan,
    v.n AS version_no,
    max(v.n) OVER (PARTITION BY c.customer_id) AS last_version
FROM generate_series(1, 2000) AS c(customer_id)
CROSS JOIN LATERAL (
    SELECT generate_series(0, floor(random() * 3)::integer) AS n
) AS v;

INSERT INTO _seed_versions (customer_id, valid_from, city, plan)
SELECT customer_id, valid_from, city, plan FROM gen;

INSERT INTO dim_customer (customer_id, city, plan, valid_from, valid_to, is_current)
SELECT
    customer_id,
    city,
    plan,
    valid_from,
    CASE
        -- Часть последних версий закрыта датой вместо открытого интервала.
        WHEN version_no = last_version AND customer_id % 3 = 0 THEN DATE '2025-12-31'
        WHEN version_no = last_version THEN NULL
        -- У остальных вперемешку пересечения и разрывы.
        WHEN customer_id % 2 = 0 THEN CURRENT_DATE
        ELSE lead(valid_from) OVER (PARTITION BY customer_id ORDER BY valid_from) - 5
    END,
    version_no = last_version
FROM gen;

CREATE INDEX dim_customer_key_idx ON dim_customer (customer_id, valid_from);

ANALYZE dim_customer;
