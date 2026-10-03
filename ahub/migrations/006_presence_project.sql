-- presence per project: the key is (who, project) — a live `ahub watch` in A stops the launcher in A only.
-- The old rows keep their project as they are; a row with project='' is the owner's presence.
CREATE TABLE presence_new (
  who TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  last_seen INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (who, project)
);
INSERT INTO presence_new(who, project, last_seen, session_id, via)
  SELECT who, project, last_seen, session_id, via FROM presence;
DROP TABLE presence;
ALTER TABLE presence_new RENAME TO presence;
CREATE INDEX idx_presence_seen ON presence(last_seen);