-- H15: черновики карточек от владельца (текст → карточка модели → запуск по кнопке).
CREATE TABLE IF NOT EXISTS draft(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL DEFAULT 0,
  project TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  card_path TEXT NOT NULL DEFAULT '',
  card_text TEXT NOT NULL DEFAULT '',
  lint_errors TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'drafting',
  task_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  chat_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_draft_status ON draft(status, id);
