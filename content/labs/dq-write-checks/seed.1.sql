-- Стенд: чистые данные и пустой реестр проверок.
--
-- В отличие от остальных лаб здесь ничего не сломано. Задача обратная:
-- написать проверки, которые заметят порчу, когда она появится. Порчу
-- устроит чекер — шестью разными способами, каждый в откатываемой
-- транзакции.

CREATE TABLE customers (
    customer_id bigint PRIMARY KEY,
    name        text NOT NULL,
    city        text NOT NULL
);

CREATE TABLE orders (
    order_id    bigint NOT NULL,
    customer_id bigint,
    status      text NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    created_at  timestamptz NOT NULL
);

-- Реестр проверок. Каждая — SELECT, возвращающий ПЛОХИЕ строки:
-- пусто значит проверка прошла. Та же идея, что у тестов dbt.
CREATE TABLE dq_checks (
    name  text PRIMARY KEY,
    query text NOT NULL
);

SELECT setseed(0.47);

INSERT INTO customers (customer_id, name, city)
SELECT
    i,
    'Клиент ' || i,
    (ARRAY['Москва', 'Санкт-Петербург', 'Казань', 'Новосибирск', 'Екатеринбург'])[
        1 + floor(random() * 5)::integer
    ]
FROM generate_series(1, 5000) AS i;

INSERT INTO orders (order_id, customer_id, status, amount, created_at)
SELECT
    i,
    1 + floor(random() * 5000)::integer,
    (ARRAY['new', 'paid', 'shipped', 'cancelled'])[1 + floor(random() * 4)::integer],
    round((random() * 9900 + 100)::numeric, 2),
    now() - random() * 30 * interval '1 day'
FROM generate_series(1, 50000) AS i;

CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_created_idx ON orders (created_at);

-- Пример, задающий формат. Он проверяет полноту одного поля и уже работает:
-- на чистых данных возвращает ноль строк.
INSERT INTO dq_checks (name, query) VALUES (
    'example_status_not_null',
    'SELECT order_id FROM orders WHERE status IS NULL'
);

ANALYZE customers;
ANALYZE orders;
