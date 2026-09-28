-- H06: очередь конвейера (rounds/after/blind). Читаются напрямую SQL,
-- upsert_task их не трогает (колонки H01), запись — UPDATE после upsert.
ALTER TABLE task ADD COLUMN rounds INTEGER NOT NULL DEFAULT 2;
ALTER TABLE task ADD COLUMN after_id TEXT NOT NULL DEFAULT '';
ALTER TABLE task ADD COLUMN blind INTEGER NOT NULL DEFAULT 0;
