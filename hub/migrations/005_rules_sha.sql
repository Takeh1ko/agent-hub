-- H02: хеш правил для preflight (арбитр круг 3, п.6).
ALTER TABLE task ADD COLUMN rules_sha TEXT NOT NULL DEFAULT '';
