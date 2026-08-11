-- Датасет «магазин» — общий для всех SQL-узлов.
--
-- Спроектирован так, чтобы на нём были заметны ровно те ошибки, которые
-- разбираются в теории: клиенты без заказов, заказы без позиций, NULL в
-- ссылочном поле, дубли по времени, связь многие-ко-многим через две ветки.
-- Данные без этих особенностей делают задачи бессмысленными: любой запрос
-- на них выглядит правильным.

CREATE TABLE customers (
    id          integer PRIMARY KEY,
    name        text NOT NULL,
    city        text,               -- NULL встречается: клиент без города
    registered  date NOT NULL,
    manager_id  integer             -- NULL: у клиента может не быть менеджера
);

CREATE TABLE products (
    id       integer PRIMARY KEY,
    title    text NOT NULL,
    category text NOT NULL,
    price    numeric(10,2) NOT NULL
);

CREATE TABLE orders (
    id          integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(id),
    created_at  timestamptz NOT NULL,
    status      text NOT NULL        -- new | paid | shipped | cancelled
);

CREATE TABLE order_items (
    order_id   integer NOT NULL REFERENCES orders(id),
    product_id integer NOT NULL REFERENCES products(id),
    quantity   integer NOT NULL,
    price      numeric(10,2) NOT NULL,   -- цена на момент покупки
    PRIMARY KEY (order_id, product_id)
);

-- Сотрудники со ссылкой на самих себя: без иерархии не поставить ни задачу
-- на рекурсивный CTE, ни на self-join.
CREATE TABLE employees (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    manager_id integer REFERENCES employees(id),
    hired      date NOT NULL,
    salary     numeric(10,2) NOT NULL
);

CREATE TABLE tickets (
    id          integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(id),
    created_at  timestamptz NOT NULL,
    subject     text NOT NULL
);

CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX order_items_product_idx ON order_items (product_id);
