-- Большой датасет для узлов про производительность.
--
-- Зачем отдельный от shop: на шести строках планировщик всегда выбирает
-- Seq Scan, и разговор об индексах остаётся теоретическим. Здесь сотни тысяч
-- строк — достаточно, чтобы Index Scan действительно выигрывал, а EXPLAIN
-- показывал разные стратегии соединения.
--
-- Данные не лежат в репозитории, а генерируются: скрипт весит килобайты
-- вместо сотен мегабайт, объём настраивается одним числом, а воспроизводимость
-- обеспечивается фиксированным зерном генератора.

CREATE TABLE customers (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    city       text,
    registered date NOT NULL,
    segment    text NOT NULL
);

CREATE TABLE products (
    id       integer PRIMARY KEY,
    title    text NOT NULL,
    category text NOT NULL,
    price    numeric(10,2) NOT NULL
);

CREATE TABLE orders (
    id          integer PRIMARY KEY,
    customer_id integer NOT NULL,
    created_at  timestamptz NOT NULL,
    status      text NOT NULL
);

CREATE TABLE order_items (
    order_id   integer NOT NULL,
    product_id integer NOT NULL,
    quantity   integer NOT NULL,
    price      numeric(10,2) NOT NULL
);

-- Индексы намеренно НЕ на всех колонках: часть задач состоит в том, чтобы
-- увидеть по плану, какого индекса не хватает, и объяснить это.
CREATE INDEX orders_customer_idx   ON orders (customer_id);
CREATE INDEX orders_created_idx    ON orders (created_at);
CREATE INDEX order_items_order_idx ON order_items (order_id);
CREATE INDEX customers_city_idx    ON customers (city);
