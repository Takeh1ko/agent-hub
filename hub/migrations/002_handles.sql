-- Ручки Claude: индекс событий, outbox для hub say, meta для пульса wait.
CREATE INDEX IF NOT EXISTS idx_event_task_id ON event(task_id, id);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  sent_ts INTEGER NULL
);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
