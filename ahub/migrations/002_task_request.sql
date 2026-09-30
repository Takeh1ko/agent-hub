-- Просьба владельцу задачи (процессу задачи): '' | stop. Клиенты не меняют состояние активной задачи сами.
ALTER TABLE task ADD COLUMN request TEXT NOT NULL DEFAULT '';
