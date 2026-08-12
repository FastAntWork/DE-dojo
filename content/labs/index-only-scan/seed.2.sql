-- Вариант 2: индекс правильный, а плана всё равно нет.
--
-- Индекс построен верно и включает все нужные колонки. Но после массовой
-- вставки карта видимости пуста, автоочистка на таблице выключена — и
-- планировщик ОТКАЗЫВАЕТСЯ от Index Only Scan вовсе: он оценивает, какую долю
-- страниц пришлось бы перепроверять в таблице, и при пустой карте такой план
-- выходит дороже обычного. В плане будет Bitmap Heap Scan.
--
-- Урок варианта — вторая половина Index Only Scan, о которой забывают: индекс
-- необходим, но не достаточен. Карту видимости заполняет VACUUM, и без него
-- покрывающий индекс просто не используется по назначению.

CREATE TABLE orders (
    order_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  integer NOT NULL,
    status     text NOT NULL,
    amount     numeric(12, 2) NOT NULL,
    created_at timestamptz NOT NULL
) WITH (autovacuum_enabled = false);

SELECT setseed(0.61);

INSERT INTO orders (tenant_id, status, amount, created_at)
SELECT
    CASE WHEN random() < 0.05 THEN 42 ELSE 1 + (random() * 300)::integer END,
    (ARRAY['new', 'paid', 'shipped', 'cancelled'])[1 + floor(random() * 4)::integer],
    round((random() * 9900 + 100)::numeric, 2),
    TIMESTAMPTZ '2026-01-01' + random() * 90 * interval '1 day'
FROM generate_series(1, 300000);

CREATE INDEX orders_report_idx ON orders (tenant_id, created_at) INCLUDE (status);

-- Статистику собираем, а карту видимости — нет. Планы будут верными, но
-- каждая строка потребует похода в таблицу.
ANALYZE orders;
