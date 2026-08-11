-- Генерация данных. В репозитории лежит этот скрипт, а не сотни мегабайт.
--
-- Зерно генератора фиксировано: два человека, загрузившие датасет, получат
-- одинаковые данные, а значит и одинаковые планы запросов. Без setseed
-- обсуждать «почему у тебя Index Scan, а у меня Seq Scan» было бы бессмысленно.
--
-- Объёмы подобраны так, чтобы планировщик реально менял стратегии, а загрузка
-- укладывалась в несколько секунд на ноутбуке.

SELECT setseed(0.42);

-- ── Справочники ──────────────────────────────────────────────────────────────

INSERT INTO products (id, title, category, price)
SELECT i,
       'Товар №' || i,
       (ARRAY['Периферия', 'Мониторы', 'Ноутбуки', 'Сети', 'Хранение'])[1 + (i % 5)],
       round((50 + random() * 40000)::numeric, 2)
FROM generate_series(1, 500) AS i;

-- Города распределены неравномерно: у половины клиентов Москва. Это важно —
-- на равномерном распределении селективность везде одинакова, и разговор
-- о том, почему планировщик выбрал разные планы для разных значений, не
-- получится.
INSERT INTO customers (id, name, city, registered, segment)
SELECT i,
       'Клиент ' || i,
       CASE
           WHEN random() < 0.50 THEN 'Москва'
           WHEN random() < 0.60 THEN 'Санкт-Петербург'
           WHEN random() < 0.70 THEN 'Казань'
           WHEN random() < 0.85 THEN 'Новосибирск'
           WHEN random() < 0.97 THEN 'Екатеринбург'
           ELSE NULL
       END,
       DATE '2020-01-01' + (random() * 2200)::integer,
       CASE WHEN random() < 0.05 THEN 'vip' ELSE 'regular' END
FROM generate_series(1, 50000) AS i;

-- ── Факты ────────────────────────────────────────────────────────────────────

INSERT INTO orders (id, customer_id, created_at, status)
SELECT i,
       1 + (random() * 49999)::integer,
       TIMESTAMPTZ '2023-01-01' + (random() * 1100 || ' days')::interval,
       CASE
           WHEN random() < 0.70 THEN 'paid'
           WHEN random() < 0.85 THEN 'shipped'
           WHEN random() < 0.95 THEN 'new'
           ELSE 'cancelled'
       END
FROM generate_series(1, 300000) AS i;

INSERT INTO order_items (order_id, product_id, quantity, price)
SELECT o.id,
       1 + (random() * 499)::integer,
       1 + (random() * 4)::integer,
       round((50 + random() * 40000)::numeric, 2)
FROM orders o
CROSS JOIN generate_series(1, 3) AS n
WHERE random() < 0.8;

-- Без ANALYZE планировщик работает по умолчанию заданной статистике и строит
-- заведомо неверные планы. Для узла про EXPLAIN это критично: обсуждать
-- расхождение оценки и факта имеет смысл только на собранной статистике.
ANALYZE customers;
ANALYZE products;
ANALYZE orders;
ANALYZE order_items;
