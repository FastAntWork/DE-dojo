-- Вариант 3: индекс покрывает фильтр, но не выдачу.
--
-- Индекс по (tenant_id, created_at) построен верно и данные подготовлены —
-- Index Only Scan невозможен только потому, что запросу нужна ещё колонка
-- status, которой в индексе нет. План берёт обычный Index Scan и ходит в
-- таблицу за каждой строкой.
--
-- Урок варианта: покрывающим индекс делают колонки ВЫДАЧИ, а не только
-- фильтра. Добавлять их в ключ незачем — для этого есть INCLUDE.

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

CREATE INDEX orders_tenant_created_idx ON orders (tenant_id, created_at);

-- dojo:no-transaction
VACUUM ANALYZE orders;
