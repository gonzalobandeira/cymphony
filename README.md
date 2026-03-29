# Cymphony

An autonomous orchestration service that polls [Linear](https://linear.app) issues and runs [Claude Code](https://claude.ai/code) agents on them — automatically picking up tasks, writing code, and opening pull requests.

## How it works

1. Cymphony polls your Linear project for issues in configured active states (e.g. `Todo`, `In Progress`)
2. For each eligible issue, it creates an isolated workspace directory and runs a `before_run` hook (e.g. clone the repo, reset to `main`)
3. A Claude Code agent is launched with a rendered prompt containing the issue title, description, comments, labels, and blocking issues
4. After the agent completes its turn, an `after_run` hook runs (e.g. push branch, open PR)
5. On the next poll, Cymphony checks if the issue reached a terminal state (e.g. `Done`) and cleans up, or retries if needed

```
Linear Issue (Todo)
      │
      ▼
  Orchestrator polls Linear
      │
      ▼
  Workspace created → after_create hook (git clone)
      │
      ▼
  before_run hook (git fetch, reset to main)
      │
      ▼
  Claude Code agent runs (up to max_turns)
      │
      ▼
  after_run hook (git push, gh pr create)
      │
      ▼
  Reconcile: issue Done → clean up workspace
```

## Prerequisites

- Python ≥ 3.11
- [Claude Code CLI](https://claude.ai/code) installed (`claude`)
- A Linear account with an API key
- An Anthropic API key (recommended over OAuth for background services)
- `gh` CLI (optional, for auto-creating PRs in hooks)

## Installation

```bash
pip install -e .
# or with dev dependencies
pip install -e ".[dev]"
```

## Quick start

**1. Create a `.env` file** next to your `WORKFLOW.md`:

```bash
LINEAR_API_KEY=lin_api_...
ANTHROPIC_API_KEY=sk-ant-...
```

Cymphony loads this file automatically on startup. You can also export these as shell environment variables instead.

> **Why `ANTHROPIC_API_KEY`?** The Claude Code CLI supports both OAuth (interactive login) and API key authentication. OAuth tokens expire, which silently breaks background agents. An API key never expires and is the recommended approach for automated workflows. Cymphony passes Claude auth environment variables such as `ANTHROPIC_API_KEY` through to the Claude subprocess unchanged; it only removes the internal `CLAUDECODE` sentinel so nested CLI runs behave correctly.

**2. Create a `WORKFLOW.md`** in your project directory (see [Configuration](#configuration) below).

**3. Run:**

```bash
cymphony --port 8081
# or with a custom workflow path
cymphony --workflow-path /path/to/WORKFLOW.md --port 8081 --log-level DEBUG
```

Open `http://localhost:8081` for the live dashboard.

## Configuration

`WORKFLOW.md` has two sections separated by YAML frontmatter:

1. **YAML config block** (between `---` delimiters): service settings
2. **Jinja2 prompt template** (after the closing `---`): the prompt sent to the Claude Code agent

### Example `WORKFLOW.md`

```yaml
---
tracker:
  kind: linear                 # only "linear" supported
  api_key: $LINEAR_API_KEY     # env var reference (expanded at runtime)
  project_slug: my-project-abc # Linear project slugId (found in project URL)
  assignee: myusername         # optional; omit to pick up all issues in project
  active_states: [Todo, In Progress]
  terminal_states: [Done, Cancelled, Canceled, Duplicate, Closed]

polling:
  interval_ms: 30000           # how often to poll Linear (ms)

workspace:
  root: ~/my-workspaces        # ~ and $VAR are expanded

agent:
  provider: claude               # agent provider: "claude" or "codex"
  max_concurrent_agents: 5     # max parallel agents across all issues
  max_turns: 20                # max agent turns per issue before giving up
  max_retry_backoff_ms: 300000 # 5 min cap for exponential backoff on failures
  max_concurrent_agents_by_state:  # optional per-state concurrency caps
    todo: 3

codex:
  command: claude              # the Claude Code CLI binary
  turn_timeout_ms: 3600000    # 1 hour per turn
  stall_timeout_ms: 300000    # 5 min with no output = stall
  dangerously_skip_permissions: true

hooks:
  after_create: |              # runs once when workspace directory is created
    git clone git@github.com:org/repo.git .
  before_run: |                # runs before each agent turn batch
    git fetch origin && git checkout main && git reset --hard origin/main
  after_run: |                 # runs after each agent turn batch
    BRANCH=$(git branch --show-current)
    if [ "$BRANCH" != "main" ]; then
      git add -A && git commit -m "chore: agent work [skip ci]" || true
      git push -u origin "$BRANCH" || true
      TITLE=$(git log --format="%s" origin/main..HEAD | tail -1)
      gh pr create --title "$TITLE" --body "" --head "$BRANCH" || true
    fi
  before_remove: |             # runs before workspace is deleted
    ...
  timeout_ms: 120000           # hook timeout

preflight:
  enabled: true                # set false to skip all preflight checks
  required_clis: [git]         # CLIs that must be on PATH before dispatch
  required_env_vars: []        # env vars that must be set (e.g. ANTHROPIC_API_KEY)
  expect_clean_worktree: false # if true, fail when workspace has uncommitted changes
  base_branch: main            # expected base branch in each workspace

server:
  port: 8080                   # omit to disable the HTTP server
---
You are a senior software engineer working on the **MyProject** project.

## Issue

**Title:** {{ issue.title }}
**Identifier:** {{ issue.identifier }}
**Priority:** {{ issue.priority }}
**State:** {{ issue.state }}
{% if issue.description %}
**Description:**
{{ issue.description }}
{% endif %}
{% if issue.comments %}
**Comments:**
{% for c in issue.comments %}
- **{{ c.author }}** ({{ c.created_at }}): {{ c.body }}
{% endfor %}
{% endif %}

## Instructions

1. Read the issue carefully.
2. Create and checkout a branch named `agent/{{ issue.identifier | lower }}`.
3. Implement the changes described in the issue.
4. Write or update tests as appropriate.
5. Commit with a descriptive message referencing the issue.
```

### Prompt template variables

| Variable | Type | Description |
|---|---|---|
| `issue.title` | `str` | Issue title |
| `issue.identifier` | `str` | Issue identifier (e.g. `PROJ-42`) |
| `issue.description` | `str \| None` | Issue description (Markdown) |
| `issue.state` | `str` | Current state name |
| `issue.priority` | `str` | Priority label |
| `issue.labels` | `list[str]` | Label names |
| `issue.comments` | `list` | Comments with `.author`, `.body`, `.created_at` |
| `issue.blocked_by` | `list` | Blocking issues with `.identifier`, `.state` |
| `attempt` | `int \| None` | Retry attempt number (>1 means re-attempt) |

## HTTP API

When `server.port` is configured:

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Live HTML dashboard |
| `GET` | `/api/v1/state` | Full orchestrator snapshot (JSON) |
| `POST` | `/api/v1/refresh` | Trigger an immediate poll |
| `POST` | `/api/v1/app/kill` | Stop the orchestrator and cancel active workers |
| `GET` | `/api/v1/<IDENTIFIER>` | Per-issue debug details |

## Preflight checks

Cymphony runs preflight checks before dispatching work to agents, catching common setup problems before expensive agent work begins. Checks run at two levels:

**Global checks** (every poll tick):
- Required CLIs are on PATH (default: `git`)
- Required environment variables are set

**Workspace checks** (per-issue, after workspace creation and `before_run` hook):
- Workspace is a git repository (`.git` exists)
- At least one git remote is configured
- The configured `base_branch` exists locally or on origin
- Worktree is clean (when `expect_clean_worktree: true`)

Preflight failures are surfaced as:
- Structured log messages (`action=preflight_check_failed`)
- Problems in the dashboard and `/api/v1/state` JSON
- `preflight_errors` array in the state snapshot

To disable all checks: set `preflight.enabled: false` in your WORKFLOW.md.

## Recommended safe hook patterns

When running Cymphony against real project repos, use these hook patterns for reliable workspace lifecycle management.

### Clone (after_create)

```bash
# Clone once when workspace is first created
git clone git@github.com:org/repo.git .
```

### Sync and reset (before_run)

```bash
# Ensure clean state on the base branch before each agent run
git fetch origin
git checkout main
git reset --hard origin/main
git clean -fd
# Delete stale agent branches to avoid conflicts
git branch --list 'agent/*' | xargs -r git branch -D 2>/dev/null || true
```

### Branch, commit, and publish (after_run)

```bash
# Only push and create PR if the agent created a branch
BRANCH=$(git branch --show-current)
if [ "$BRANCH" != "main" ]; then
  git add -A
  git commit -m "agent: $(git branch --show-current)" || true
  git push -u origin "$BRANCH" --force-with-lease || true
  # Use first commit subject as PR title
  TITLE=$(git log --format="%s" origin/main..HEAD | tail -1)
  gh pr create --title "$TITLE" --body "Automated PR by Cymphony agent" --head "$BRANCH" 2>/dev/null || true
fi
```

### Cleanup (before_remove)

```bash
# Delete the remote branch when workspace is removed
BRANCH=$(git branch --show-current 2>/dev/null)
if [ -n "$BRANCH" ] && [ "$BRANCH" != "main" ]; then
  git push origin --delete "$BRANCH" 2>/dev/null || true
fi
```

### How preflight interacts with hooks

Preflight checks run **after** workspace creation and the `before_run` hook. This means the `before_run` hook is responsible for getting the workspace into the expected state (e.g., fetching, resetting to the base branch). Preflight then **verifies** the result before the agent starts. If preflight fails, no agent turns are consumed.

## Architecture

```
src/cymphony/
  __main__.py     CLI entry point
  config.py       Parses WORKFLOW.md YAML into typed ServiceConfig
  workflow.py     Loads/watches WORKFLOW.md, renders Jinja2 prompts
  orchestrator.py Poll loop, dispatch, reconciliation, retry scheduling
  agent.py        Runs `claude` CLI as subprocess, streams stream-json events
  linear.py       Async Linear GraphQL client
  workspace.py    Per-issue directory lifecycle and hook execution
  server.py       Optional aiohttp HTTP server (dashboard + API)
  models.py       Domain dataclasses and enums
  logging_.py     Structured logging helpers
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

Dependencies: `pyyaml`, `jinja2`, `aiohttp`, `watchdog`. Python ≥ 3.11.

## Dashboard Smoke Test

For a live operator pass against a real orchestrator process without invoking the real Claude CLI, use:

```bash
CYMPHONY_FAKE_CLAUDE_MODE=success ./scripts/run_dashboard_smoke_test.sh
```

The detailed checklist lives in [`docs/dashboard-smoke-test.md`](docs/dashboard-smoke-test.md).
