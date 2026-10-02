-- Owner request to the task process: '' | stop. Clients never change an active task's state themselves.
ALTER TABLE task ADD COLUMN request TEXT NOT NULL DEFAULT '';
