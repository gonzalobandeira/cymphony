# Cymphony

Orchestration service that polls Linear issues and runs Claude Code agents on them autonomously.

## Architecture

```
WORKFLOW.md          ← user config (YAML frontmatter + Jinja2 prompt template)
src/cymphony/
  __main__.py        ← CLI entry point (cymphony --port <port>)
  config.py          ← parses WORKFLOW.md YAML into typed ServiceConfig
  workflow.py        ← loads/watches WORKFLOW.md, renders Jinja2 prompts
  orchestrator.py    ← poll loop, dispatch, reconciliation, retry scheduling
  agent.py           ← runs `claude` CLI as subprocess, streams stream-json events
  linear.py          ← async Linear GraphQL client
  workspace.py       ← per-issue directory lifecycle and hook execution
  server.py          ← optional aiohttp HTTP server (dashboard + API)
  models.py          ← all domain dataclasses and enums
  logging_.py        ← structured logging helpers
```

### Data flow

1. **Poll**: `Orchestrator._tick()` fetches candidate issues from Linear (active states, project, assignee filter)
2. **Dispatch**: eligible issues get a workspace created, hooks run, and a `_worker` asyncio task is spawned
3. **Worker**: renders prompt → runs `AgentRunner.run_turn()` in a loop (up to `max_turns`) → runs `after_run` hook
4. **Reconcile**: each tick checks running workers for stalls and refreshes issue states from Linear; terminal-state issues get their workers cancelled and workspaces removed
5. **Retry**: workers that exit abnormally are retried with exponential backoff; clean exits get a 1-second continuation retry to re-evaluate the issue

## Running

```bash
export LINEAR_API_KEY=...
cymphony --port 8081
# or
cymphony --workflow-path /path/to/WORKFLOW.md --port 8081 --log-level DEBUG
```

## WORKFLOW.md format

```yaml
---
tracker:
  kind: linear               # only "linear" supported
  api_key: $LINEAR_API_KEY   # env var reference
  project_slug: <slug>       # Linear project slugId
  assignee: <username>       # optional; omit to pick up all issues
  active_states: [Todo, In Progress]
  terminal_states: [Done, Cancelled, Canceled, Duplicate, Closed]

polling:
  interval_ms: 30000

workspace:
  root: ~/my-workspaces      # ~ and $VAR are expanded

agent:
  max_concurrent_agents: 10
  max_turns: 20
  max_retry_backoff_ms: 300000
  max_concurrent_agents_by_state:   # optional per-state cap
    todo: 3

codex:
  command: claude
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  dangerously_skip_permissions: true

hooks:
  after_create: |            # runs once when workspace directory is first created
    git clone git@github.com:org/repo.git .
  before_run: |              # runs before each agent turn batch
    git fetch origin && git checkout main && git reset --hard origin/main
  after_run: |               # runs after each agent turn batch (failure is non-fatal)
    ...
  before_remove: |           # runs before workspace is deleted
    ...
  timeout_ms: 120000

server:
  port: 8080
---
Jinja2 prompt template goes here.
Available variables: issue (dict), attempt (int|None)
```

## Key gotchas

- **`--verbose` required**: `claude --output-format stream-json --print` must include `--verbose` or it exits silently.
- **Unset `CLAUDECODE`**: the subprocess env strips `CLAUDECODE` to avoid the nested Claude session error.
- **Linear filter quirk**: use `issues(filter: { id: { in: $ids } })` not `nodes(ids: $ids)` at the root Query level.
- **`active_states` must include `"In Progress"`**: Linear auto-transitions issues when a branch is created, so agents picking up `Todo` issues will see the issue become `In Progress` mid-run.
- **Assignee filter**: two separate query strings (with/without assignee) because passing `null` to `eqIgnoreCase` breaks the filter.
- **`after_run` hook cancellation race**: the reconciler may call `task.cancel()` while the worker is in its `finally` block. The hook is launched as `asyncio.create_task` + awaited via `asyncio.shield` so it survives worker cancellation.
- **PR title**: use `git log --format="%s" origin/main..HEAD | tail -1` in the `after_run` hook to get the agent's original commit as the PR title (not the hook's own commit).

## HTTP API

When `server.port` is set:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | HTML dashboard |
| `GET` | `/api/v1/state` | Full orchestrator snapshot (JSON) |
| `POST` | `/api/v1/refresh` | Trigger immediate poll |
| `GET` | `/api/v1/<IDENTIFIER>` | Per-issue debug details |

## Development

```bash
pip install -e ".[dev]"   # or: pip install -e .
pytest
```

Dependencies: `pyyaml`, `jinja2`, `aiohttp`, `watchdog`. Python ≥ 3.11.

The package is built with Hatchling. `pyproject.toml` defines the `cymphony` script entry point.
