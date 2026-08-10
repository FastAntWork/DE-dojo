-- Фаза и приоритет узла.
--
-- Порядок обучения перестал быть прозой в ТЗ и стал данными: основой программы
-- взят роадмап (docs/curriculum.md), а часть исходных целей понижена в
-- приоритете. Планировщику нужно уметь это различать, а не полагаться на то,
-- что человек помнит порядок.
--
-- priority хранится текстом с CHECK, а не отдельным ENUM-типом: значений два,
-- и добавление третьего в ENUM потребовало бы ALTER TYPE, который до
-- PostgreSQL 12 не работал в транзакции и до сих пор неудобен в миграциях.

ALTER TABLE skills
    ADD COLUMN phase smallint NOT NULL DEFAULT 1 CHECK (phase BETWEEN 1 AND 6),
    ADD COLUMN priority text NOT NULL DEFAULT 'core' CHECK (priority IN ('core', 'secondary'));

-- Планировщик всегда спрашивает живое ядро в порядке фаз.
CREATE INDEX skills_phase_priority_idx ON skills (phase, priority)
    WHERE deprecated_at IS NULL;

COMMENT ON COLUMN skills.phase IS
    'Учебная фаза по docs/curriculum.md. Крупный порядок; внутри фазы порядок задают prereq.';
COMMENT ON COLUMN skills.priority IS
    'core — ядро профессии по роадмапу; secondary — отложено до закрытия ядра.';
