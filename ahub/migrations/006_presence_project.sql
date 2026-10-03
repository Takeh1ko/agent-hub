-- presence per project: presence_project is keyed by (who, project) — a live `ahub watch` in A stops the
-- launcher in A only.
-- Additive on purpose: the old `presence` (one row per who) stays exactly as it was, because a process on the
-- previous code writes it during a live reload and its upsert must keep working. The new code writes both
-- tables and reads presence_project, falling back to the old one while it has rows.
CREATE TABLE presence_project (
  who TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  last_seen INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT '',              -- wait | watch | launched
  PRIMARY KEY (who, project)
);
CREATE INDEX idx_presence_project_seen ON presence_project(last_seen);