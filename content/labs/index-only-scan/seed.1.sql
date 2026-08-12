-- Вариант 1: подходящего индекса нет вовсе.
--
-- Самый прямой случай: нужно понять, какие колонки требуются запросу ЦЕЛИКОМ,
-- и построить покрывающий индекс. Плюс не забыть, что Index Only Scan
-- требует заполненной карты видимости.

CREATE TABLE orders (
    order_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  integer NOT NULL,
    status     text NOT NULL,
    amount     numeric(12, 2) NOT NULL,
    created_at timestamptz NOT NULL
);

SELECT setseed(0.61);

INSERT INTO orders (tenant_id, status, amount, created_at)
SELECT
    CASE WHEN random() < 0.05 THEN 42 ELSE 1 + (random() * 300)::integer END,
    (ARRAY['new', 'paid', 'shipped', 'cancelled'])[1 + floor(random() * 4)::integer],
    round((random() * 9900 + 100)::numeric, 2),
    TIMESTAMPTZ '2026-01-01' + random() * 90 * interval '1 day'
FROM generate_series(1, 300000);

-- dojo:no-transaction
VACUUM ANALYZE orders;
