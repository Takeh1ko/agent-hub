-- agent-hub v2: начальная схема. Время — мс UTC. JSON — TEXT.

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Задача. id — число, для людей «T<id>».
CREATE TABLE task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- model.Kind
  title TEXT NOT NULL,                      -- цель, одна фраза
  spec TEXT NOT NULL DEFAULT '',            -- описание
  spec_hash TEXT NOT NULL DEFAULT '',       -- отпечаток постановки (смена → новая сессия при продолжении)
  result_format TEXT NOT NULL DEFAULT '',   -- ожидаемая форма результата
  executor TEXT NOT NULL DEFAULT '',        -- модель (короткое имя)
  review_json TEXT NOT NULL DEFAULT '{}',   -- {models: [..], rounds: N} или {} — без ревью
  limits_json TEXT NOT NULL DEFAULT '{}',   -- разрешённые файлы, приёмка, время, ресурсы, вход ревью…
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
  -- владение задачей (V02b): у активной задачи один владелец с арендой
  owner TEXT NOT NULL DEFAULT '',           -- токен владельца
  owner_pid INTEGER,
  lease_until INTEGER,
  version INTEGER NOT NULL DEFAULT 0        -- оптимистичная блокировка переходов
);
CREATE INDEX idx_task_state ON task(state, project);
CREATE INDEX idx_task_project ON task(project, id);

-- «после X»: задача ждёт, пока X не станет accepted.
CREATE TABLE task_dep (
  task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  after_id INTEGER NOT NULL REFERENCES task(id),
  PRIMARY KEY (task_id, after_id)
);

-- Сессия работника у поставщика.
CREATE TABLE session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES task(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,                   -- opencode | agy | …
  external_id TEXT NOT NULL DEFAULT '',     -- id сессии у поставщика (может появиться позже старта)
  role TEXT NOT NULL,                       -- model.Role
  round INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',           -- короткое имя модели
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed | killed
  outcome TEXT NOT NULL DEFAULT '',         -- классификация итога поставщиком
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

-- Журнал событий. needs_reaction — будит оркестратора; delivered/acked — доставка без потерь.
CREATE TABLE event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  task_id INTEGER,
  project TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,                       -- model.Ev
  payload_json TEXT NOT NULL DEFAULT '{}',
  needs_reaction INTEGER NOT NULL DEFAULT 0,
  critical INTEGER NOT NULL DEFAULT 0,
  delivered_at INTEGER,                     -- отдано в поток/wait
  acked_at INTEGER,                         -- оркестратор подтвердил
  tg_sent_at INTEGER                        -- отправлено человеку в TG (тревоги)
);
CREATE INDEX idx_event_task ON event(task_id, id);
CREATE INDEX idx_event_unacked ON event(needs_reaction, acked_at, id);

-- Вопрос человеку (от оркестратора или хаба) с вариантами.
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

-- Сообщения человек ↔ Claude (TG). direction: in — от человека, out — от Claude.
CREATE TABLE message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  direction TEXT NOT NULL,
  text TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  chat_id INTEGER,
  delivered_at INTEGER                      -- in: отдано Claude; out: отправлено в TG
);

-- Черновик задачи: текст человека → модель дописывает → предпросмотр → запуск.
CREATE TABLE draft (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  project TEXT NOT NULL,
  text TEXT NOT NULL,
  task_json TEXT NOT NULL DEFAULT '{}',     -- предложенные поля задачи
  errors TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'drafting',  -- drafting | ready | failed | started | cancelled
  source TEXT NOT NULL DEFAULT '',          -- top | cli | orchestrator
  task_id INTEGER
);

-- Модели хаба: короткое имя → поставщик + модель + вариант.
CREATE TABLE model (
  alias TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  variant TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT ''
);

-- Меню ролей: какие модели допустимы в роли, какая по умолчанию.
CREATE TABLE role_model (
  role TEXT NOT NULL,
  alias TEXT NOT NULL REFERENCES model(alias) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (role, alias)
);

-- Присутствие оркестратора: свежая отметка ожидания/потока.
CREATE TABLE presence (
  who TEXT PRIMARY KEY,                     -- claude | <другой оркестратор>
  project TEXT NOT NULL DEFAULT '',
  last_seen INTEGER NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT ''              -- wait | stream | launched
);

-- Запуски Claude хабом (из TG, когда живого нет).
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

-- Отчёты наблюдателя.
CREATE TABLE observer_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,                       -- quick (5 мин, код) | triage (модель по подозрению) | deep (30 мин)
  verdict TEXT NOT NULL,                    -- ok | false_alarm | alarm | critical
  summary TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  cost_go REAL NOT NULL DEFAULT 0
);

-- Чаты TG, писавшие боту.
CREATE TABLE tg_chat (
  chat_id INTEGER PRIMARY KEY,
  first_ts INTEGER NOT NULL,
  last_ts INTEGER NOT NULL,
  dead INTEGER NOT NULL DEFAULT 0
);

-- Идемпотентность команд: повтор с тем же ключом возвращает прежний результат.
CREATE TABLE op (
  key TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}'
);
