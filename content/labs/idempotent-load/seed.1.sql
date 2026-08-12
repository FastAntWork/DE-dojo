-- Вариант 1: простая вставка без удаления.
--
-- Самая частая причина задвоения. Процедура написана так, будто её вызывают
-- ровно один раз за день, — а в жизни она вызывается ещё и после каждого
-- сбоя. Ошибка видна сразу, если посмотреть на текст.

CREATE TABLE raw_orders (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  date NOT NULL,
    customer_id integer NOT NULL,
    amount      numeric(12, 2) NOT NULL
);

CREATE TABLE orders_daily (
    day     date NOT NULL,
    orders  bigint NOT NULL,
    revenue numeric(14, 2) NOT NULL
);

SELECT setseed(0.31);

INSERT INTO raw_orders (created_at, customer_id, amount)
SELECT
    DATE '2026-03-01' + (random() * 29)::integer,
    1 + (random() * 50000)::integer,
    round((random() * 9900 + 100)::numeric, 2)
FROM generate_series(1, 200000);

CREATE INDEX raw_orders_created_at_idx ON raw_orders (created_at);

CREATE PROCEDURE load_day(p_day date)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO orders_daily (day, orders, revenue)
    SELECT p_day, count(*), coalesce(sum(amount), 0)
    FROM raw_orders
    WHERE created_at = p_day;
END;
$$;

ANALYZE raw_orders;
