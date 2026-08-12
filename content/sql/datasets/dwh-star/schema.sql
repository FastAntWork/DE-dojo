-- Датасет «звезда» — для узлов моделирования хранилищ.
--
-- Спроектирован под ошибки, которые разбираются в теории, а не «для объёма»:
--
--   * измерение клиентов ведётся как SCD типа 2 — соединение по естественному
--     ключу без выбора версии задваивает факты;
--   * факт продаж и факт остатков имеют РАЗНОЕ зерно и разную аддитивность:
--     остаток нельзя складывать по времени;
--   * есть мостовая таблица «многие ко многим» между продажами и акциями —
--     на ней ловится задвоение выручки при группировке по акциям;
--   * есть факт без мер (посещения), где считать можно только строки;
--   * измерение дат содержит нерасчётные атрибуты: выходные и праздники.

-- ── Измерения ───────────────────────────────────────────────────────────────

CREATE TABLE dim_date (
    date_key      date PRIMARY KEY,
    day_of_week   integer NOT NULL,      -- 1 = понедельник
    is_weekend    boolean NOT NULL,
    is_holiday    boolean NOT NULL,      -- нерасчётный признак: из календаря
    month_start   date NOT NULL
);

-- SCD типа 2: у клиента несколько версий с интервалами действия.
-- Интервал полуоткрытый: [valid_from, valid_to), valid_to IS NULL — открыт.
CREATE TABLE dim_customer (
    customer_key integer PRIMARY KEY,    -- суррогатный ключ ВЕРСИИ
    customer_id  integer NOT NULL,       -- естественный ключ клиента
    name         text NOT NULL,
    city         text NOT NULL,
    segment      text NOT NULL,          -- small | medium | key
    valid_from   date NOT NULL,
    valid_to     date,
    is_current   boolean NOT NULL
);

CREATE TABLE dim_product (
    product_key integer PRIMARY KEY,
    title       text NOT NULL,
    category    text NOT NULL
);

CREATE TABLE dim_promo (
    promo_key integer PRIMARY KEY,
    title     text NOT NULL,
    channel   text NOT NULL
);

-- ── Факты ───────────────────────────────────────────────────────────────────

-- Зерно: одна строка на позицию продажи. Меры полностью аддитивны.
CREATE TABLE fact_sales (
    sale_id      integer PRIMARY KEY,
    date_key     date NOT NULL REFERENCES dim_date(date_key),
    customer_key integer NOT NULL REFERENCES dim_customer(customer_key),
    product_key  integer NOT NULL REFERENCES dim_product(product_key),
    quantity     integer NOT NULL,
    revenue      numeric(12,2) NOT NULL,
    cost         numeric(12,2) NOT NULL
);

-- Зерно: остаток товара на конец дня. Мера ПОЛУаддитивна: складывается по
-- товарам, но не по датам.
CREATE TABLE fact_stock_snapshot (
    date_key    date NOT NULL REFERENCES dim_date(date_key),
    product_key integer NOT NULL REFERENCES dim_product(product_key),
    on_hand     integer NOT NULL,
    PRIMARY KEY (date_key, product_key)
);

-- Факт без мер: сам факт посещения. Считается количеством строк.
CREATE TABLE fact_visits (
    date_key     date NOT NULL REFERENCES dim_date(date_key),
    customer_key integer NOT NULL REFERENCES dim_customer(customer_key),
    channel      text NOT NULL,
    PRIMARY KEY (date_key, customer_key, channel)
);

-- Мост «многие ко многим»: одна продажа может относиться к нескольким акциям.
CREATE TABLE bridge_sale_promo (
    sale_id   integer NOT NULL REFERENCES fact_sales(sale_id),
    promo_key integer NOT NULL REFERENCES dim_promo(promo_key),
    PRIMARY KEY (sale_id, promo_key)
);
