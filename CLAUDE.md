# Cymphony — Developer Guide

Orchestration service that polls Linear issues and runs coding agents (Claude Code or Codex) on them autonomously. See `README.md` for user-facing docs.

## Architecture

```
WORKFLOW.md          ← user config (YAML frontmatter + Jinja2 prompt template)
src/cymphony/
  __main__.py        ← CLI entry point (cymphony --port <port>)
  config.py          ← parses WORKFLOW.md YAML into typed ServiceConfig
  workflow.py        ← loads/watches WORKFLOW.md, renders Jinja2 prompts
  orchestrator.py    ← poll loop, dispatch, reconciliation, retry scheduling
  agent.py           ← backward-compat re-exports from runners/
  runners/
    __init__.py      ← provider registry, create_agent_runner() factory
    base.py          ← BaseAgentRunner ABC (subprocess lifecycle, timeouts, streaming)
    claude.py        ← ClaudeAgentRunner + stream-json parser
    codex.py         ← CodexAgentRunner + JSONL event parser
  linear.py          ← async Linear GraphQL client
  workspace.py       ← per-issue directory lifecycle and hook execution
  server.py          ← optional aiohttp HTTP server (dashboard + API)
  models.py          ← all domain dataclasses and enums
  logging_.py        ← structured logging helpers
```

### Config model

Provider selection uses one authoritative field: `agent.provider` (`"claude"` or `"codex"`).
Subprocess runtime settings live in a provider-neutral `runner:` YAML block (parsed into `RunnerConfig`).
The legacy `codex:` YAML key is still accepted as a fallback for backward compatibility.

## Data flow

1. **Poll**: `Orchestrator._tick()` fetches candidate issues from Linear (active states, project, assignee filter)
2. **Dispatch**: eligible issues get a workspace created, hooks run, and a `_worker` asyncio task is spawned
3. **Worker**: renders prompt → runs `AgentRunner.run_turn()` in a loop (up to `max_turns`) → runs `after_run` hook
4. **Plan phase**: before the main agent loop, a separate planning turn runs (`render_plan_prompt`) to produce a structured plan, which is then appended to the main prompt
5. **Reconcile**: each tick checks running workers for stalls and refreshes issue states from Linear; terminal-state issues get their workers cancelled and workspaces removed
6. **Retry**: workers that exit abnormally are retried with exponential backoff; clean exits get a 1-second continuation retry to re-evaluate the issue

State transitions are configured declaratively through `WORKFLOW.md` frontmatter (`transitions.dispatch`, `success`, `failure`, `blocked`, `cancelled`, and `qa_review.*`).

## Running

```bash
export LINEAR_API_KEY=...
cymphony --port 8081
# or
cymphony --workflow-path /path/to/WORKFLOW.md --port 8081 --log-level DEBUG
```

## Development

```bash
pip install -e ".[dev]"   # or: pip install -e .
pytest
```

Dependencies: `pyyaml`, `jinja2`, `aiohttp`, `watchdog`. Python ≥ 3.11. Built with Hatchling.

## Key gotchas

- **`--verbose` required**: `claude --output-format stream-json --print` must include `--verbose` or it exits silently.
- **Unset `CLAUDECODE`**: the subprocess env strips `CLAUDECODE` to avoid the nested Claude session error.
- **Linear filter quirk**: use `issues(filter: { id: { in: $ids } })` not `nodes(ids: $ids)` at the root Query level.
- **`active_states` must include `"In Progress"`**: Linear auto-transitions issues when a branch is created, so agents picking up `Todo` issues will see the issue become `In Progress` mid-run.
- **Assignee filter**: two separate query strings (with/without assignee) because passing `null` to `eqIgnoreCase` breaks the filter.
- **`after_run` hook cancellation race**: the reconciler may call `task.cancel()` while the worker is in its `finally` block. The hook is launched as `asyncio.create_task` + awaited via `asyncio.shield` so it survives worker cancellation.
- **PR title**: use `git log --format="%s" origin/main..HEAD | tail -1` in the `after_run` hook to get the agent's original commit as the PR title (not the hook's own commit).
- **Configurable issue transitions**: dispatch and worker completion transitions are no longer hardcoded. Defaults remain `dispatch -> "In Progress"` and `success -> "In Review"`, with optional `failure`, `blocked`, and `cancelled` transitions.
- **QA review lane is opt-in**: when `transitions.qa_review.enabled` is true, implementation success transitions to `qa_review.dispatch` (typically `QA Review`) instead of directly to `transitions.success`.
- **Required Linear states for QA review**: create `QA Review` in Linear before enabling the feature. The end-to-end workflow is `Todo -> In Progress -> QA Review -> (Todo | In Review)`.

## HTTP API

When `server.port` is set:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | HTML dashboard |
| `GET` | `/api/v1/state` | Full orchestrator snapshot (JSON) |
| `POST` | `/api/v1/refresh` | Trigger immediate poll |
| `GET` | `/api/v1/<IDENTIFIER>` | Per-issue debug details |
