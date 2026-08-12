-- Вариант 2: разрывы в истории и флаг на нескольких версиях.
--
-- Загрузка закрывала предыдущую версию на день раньше, чем начиналась
-- следующая, — «чтобы интервалы не пересекались». В результате в истории
-- появились дыры: события, попавшие в разрыв, не найдут версию измерения
-- вовсе и тихо выпадут из отчёта.
--
-- Заодно флаг актуальности проставлен не только последней версии.

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

-- Порча: valid_to = дата начала следующей версии МИНУС три дня, плюс флаг
-- актуальности у каждой второй версии.
INSERT INTO dim_customer (customer_id, city, plan, valid_from, valid_to, is_current)
SELECT
    customer_id,
    city,
    plan,
    valid_from,
    CASE
        WHEN version_no = last_version THEN NULL
        ELSE lead(valid_from) OVER (PARTITION BY customer_id ORDER BY valid_from) - 3
    END,
    version_no = last_version OR version_no % 2 = 0
FROM gen;

CREATE INDEX dim_customer_key_idx ON dim_customer (customer_id, valid_from);

ANALYZE dim_customer;
