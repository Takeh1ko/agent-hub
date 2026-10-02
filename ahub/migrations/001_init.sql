-- agent-hub v2: initial schema. Time — ms UTC. JSON — TEXT.

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Task. id — number, for humans "T<id>".
CREATE TABLE task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- model.Kind
  title TEXT NOT NULL,                      -- goal, one phrase
  spec TEXT NOT NULL DEFAULT '',            -- description
  spec_hash TEXT NOT NULL DEFAULT '',       -- statement fingerprint (change → new session on resume)
  result_format TEXT NOT NULL DEFAULT '',   -- expected result shape
  executor TEXT NOT NULL DEFAULT '',        -- model (short name)
  review_json TEXT NOT NULL DEFAULT '{}',   -- {models: [..], rounds: N} or {} — no review
  limits_json TEXT NOT NULL DEFAULT '{}',   -- allowed files, acceptance, time, resources, review input…
  budget_go REAL NOT NULL DEFAULT 0,
  budget_usd REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued',     -- model.State
  phase TEXT NOT NULL DEFAULT '',           -- model.Phase
  state_reason TEXT NOT NULL DEFAULT '',
  round INTEGER NOT NULL DEFAULT 0,
  branch TEXT NOT NULL DEFAULT '',
  worktree TEXT NOT NULL DEFAULT '',
  base_sha TEXT NOT NULL DEFAULT '',
  accepted_sha TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',      -- orchestrator | human | draft
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  -- task ownership (V02b): an active task has one owner with a lease
  owner TEXT NOT NULL DEFAULT '',           -- owner token
  owner_pid INTEGER,
  lease_until INTEGER,
  version INTEGER NOT NULL DEFAULT 0        -- optimistic transition lock
);
CREATE INDEX idx_task_state ON task(state, project);
CREATE INDEX idx_task_project ON task(project, id);

-- "after X": task waits until X becomes accepted.
CREATE TABLE task_dep (
  task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  after_id INTEGER NOT NULL REFERENCES task(id),
  PRIMARY KEY (task_id, after_id)
);

-- Worker session at the provider.
CREATE TABLE session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES task(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,                   -- opencode | agy | …
  external_id TEXT NOT NULL DEFAULT '',     -- provider-side session id (may appear after start)
  role TEXT NOT NULL,                       -- model.Role
  round INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',           -- model short name
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | killed
  outcome TEXT NOT NULL DEFAULT '',         -- provider outcome classification
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  cost_go REAL NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  quota REAL NOT NULL DEFAULT 0,
  tokens_json TEXT NOT NULL DEFAULT '{}',
  log_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_session_task ON session(task_id, id);
CREATE UNIQUE INDEX idx_session_ext ON session(provider, external_id) WHERE external_id != '';

-- Event log. needs_reaction — wakes the orchestrator; delivered/acked — lossless delivery.
CREATE TABLE event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  task_id INTEGER,
  project TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,                       -- model.Ev
  payload_json TEXT NOT NULL DEFAULT '{}',
  needs_reaction INTEGER NOT NULL DEFAULT 0,
  critical INTEGER NOT NULL DEFAULT 0,
  delivered_at INTEGER,                     -- handed to stream/wait
  acked_at INTEGER,                         -- orchestrator confirmed
  tg_sent_at INTEGER                        -- sent to the human in TG (alarms)
);
CREATE INDEX idx_event_task ON event(task_id, id);
CREATE INDEX idx_event_unacked ON event(needs_reaction, acked_at, id);

-- Question to the human (from the orchestrator or the hub) with options.
CREATE TABLE question (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  task_id INTEGER,
  asked_by TEXT NOT NULL DEFAULT '',        -- orchestrator | hub
  text TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',      -- open | answered | cancelled
  answer TEXT NOT NULL DEFAULT '',
  answered_via TEXT NOT NULL DEFAULT '',    -- tg | cli | top
  answered_at INTEGER,
  tg_sent_at INTEGER
);

-- Human ↔ Claude messages (TG). direction: in — from human, out — from Claude.
CREATE TABLE message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  direction TEXT NOT NULL,
  text TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  chat_id INTEGER,
  delivered_at INTEGER                      -- in: handed to Claude; out: sent to TG
);

-- Task draft: human text → model completes → preview → start.
CREATE TABLE draft (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  project TEXT NOT NULL,
  text TEXT NOT NULL,
  task_json TEXT NOT NULL DEFAULT '{}',     -- proposed task fields
  errors TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'drafting',  -- drafting | ready | failed | started | cancelled
  source TEXT NOT NULL DEFAULT '',          -- top | cli | orchestrator
  task_id INTEGER
);

-- Hub models: short name → provider + model + variant.
CREATE TABLE model (
  alias TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT ''
);

-- Role menu: which models fit a role, which is default.
CREATE TABLE role_model (
  role TEXT NOT NULL,
  alias TEXT NOT NULL REFERENCES model(alias) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (role, alias)
);

-- Orchestrator presence: fresh wait/stream mark.
CREATE TABLE presence (
  who TEXT PRIMARY KEY,                     -- claude | <other orchestrator>
  project TEXT NOT NULL DEFAULT '',
  last_seen INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT ''              -- wait | stream | launched
);

-- Claude launches by the hub (from TG, when none is live).
CREATE TABLE claude_launch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  project TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | killed
  reason TEXT NOT NULL DEFAULT '',
  ended_at INTEGER
);

-- Observer reports.
CREATE TABLE observer_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,                       -- quick (5 min, code) | triage (model on suspicion) | deep (30 min)
  verdict TEXT NOT NULL,                    -- ok | false_alarm | alarm | critical
  summary TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  cost_go REAL NOT NULL DEFAULT 0
);

-- TG chats that wrote to the bot.
CREATE TABLE tg_chat (
  chat_id INTEGER PRIMARY KEY,
  first_ts INTEGER NOT NULL,
  last_ts INTEGER NOT NULL,
  dead INTEGER NOT NULL DEFAULT 0
);

-- Command idempotency: retry with the same key returns the previous result.
CREATE TABLE op (
  key TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}'
);
