Live samples: oh-my-pi 18.14.0, 2026-11-01.

- `tool_read_ok.jsonl`, `go_quota_429.jsonl`: supplied live samples (willow read tool and Go 429).
- `start.jsonl`, `resume.jsonl`: consecutive runs on willow/gpt-6.1-sol, thinking low,
  approval-mode yolo, the same cwd/session-dir and session id. Start asks to remember OTTER
  and answer PONG; resume asks for the remembered word and returns OTTER. Both exit 0.
- `models.json`: willow rows selected from `omp models --json`, only model metadata retained;
  the two verified Go models are covered by the provider's fallback catalog.

The two captured runs used an isolated PI_CODING_AGENT_DIR and --session-dir within the
worktree; the user's omp config and default session store were not modified. One earlier
start failed before a model request with SQLITE_BUSY while the isolated catalog initialized;
the subsequent start and resume ran sequentially. No more live model runs were made.
