-- Задание, исчезнувшее из content/, нельзя удалять: attempts ссылается на
-- tasks с ON DELETE CASCADE, и удаление снесло бы историю попыток вместе с
-- заданием. У skills такая пометка была с самого начала — здесь та же логика
-- для tasks, симметрично.
--
-- Правку в 001 внести нельзя: миграция уже применена, и раннер сверяет
-- контрольную сумму. Это ровно тот случай, ради которого сверка и сделана.

ALTER TABLE tasks ADD COLUMN deprecated_at timestamptz;

-- Планировщик всегда спрашивает только живые задания.
DROP INDEX tasks_skill_type_idx;
CREATE INDEX tasks_skill_type_idx ON tasks (skill_id, type) WHERE deprecated_at IS NULL;

COMMENT ON COLUMN tasks.deprecated_at IS
    'Задание пропало из content/. Строка остаётся ради истории в attempts.';
