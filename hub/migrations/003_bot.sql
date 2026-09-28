-- TG-пульт H05: чаты владельцев для рассылки.
CREATE TABLE IF NOT EXISTS tg_chat(
  chat_id INTEGER PRIMARY KEY,
  first_ts INTEGER NOT NULL DEFAULT 0
);
