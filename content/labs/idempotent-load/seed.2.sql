-- Вариант 2: удаление есть, но не того дня.
--
-- Здесь автор знал про delete-insert и написал его. Только границу взял из
-- current_date, а не из параметра. Пока загрузка идёт день в день, всё
-- работает; ломается она на повторе за прошлое число и на backfill — то есть
-- ровно тогда, когда идемпотентность и нужна.
--
-- Урок варианта: границы обрабатываемого периода берутся из контекста
-- прогона, а не из текущего времени.

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
    DELETE FROM orders_daily WHERE day = current_date;

    INSERT INTO orders_daily (day, orders, revenue)
    SELECT p_day, count(*), coalesce(sum(amount), 0)
    FROM raw_orders
    WHERE created_at = p_day;
END;
$$;

ANALYZE raw_orders;
