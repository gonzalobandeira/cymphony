#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8081}"
ASSIGNEE="${ASSIGNEE:-gonzalobandeira}"
PROJECT_SLUG="${PROJECT_SLUG:-cymphony-b2a8d0064141}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$ROOT_DIR/.tmp/dashboard-smoke-workspaces}"
WORKFLOW_PATH="${WORKFLOW_PATH:-$ROOT_DIR/.tmp/WORKFLOW.dashboard-smoke.md}"
FAKE_RUNNER="$ROOT_DIR/scripts/fake_claude_stream.py"

if [ -z "${LINEAR_API_KEY:-}" ]; then
  echo "LINEAR_API_KEY must be set before running the dashboard smoke test." >&2
  exit 1
fi

mkdir -p "$(dirname "$WORKFLOW_PATH")" "$WORKSPACE_ROOT"
chmod +x "$FAKE_RUNNER"

cat > "$WORKFLOW_PATH" <<EOF
---
tracker:
  kind: linear
  api_key: \$LINEAR_API_KEY
  project_slug: $PROJECT_SLUG
  assignee: $ASSIGNEE
  active_states:
    - In Progress
  terminal_states:
    - Done
    - Cancelled
    - Canceled
    - Duplicate
    - Closed

polling:
  interval_ms: 5000

workspace:
  root: $WORKSPACE_ROOT

agent:
  max_concurrent_agents: 1
  max_turns: 1
  max_retry_backoff_ms: 30000

codex:
  command: $FAKE_RUNNER
  turn_timeout_ms: 600000
  stall_timeout_ms: 600000
  dangerously_skip_permissions: true

hooks:
  timeout_ms: 1000

server:
  port: $PORT
---
You are running a safe dashboard smoke test.

Issue: {{ issue.identifier }} - {{ issue.title }}
Description:
{{ issue.description or "No description provided." }}
EOF

echo "Dashboard smoke test workflow written to $WORKFLOW_PATH"
echo "Using fake runner mode: ${CYMPHONY_FAKE_CLAUDE_MODE:-success}"
echo "Open http://localhost:$PORT after startup."
echo "Suggested issue: BAP-177"

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec python -m cymphony --workflow-path "$WORKFLOW_PATH" --port "$PORT" --log-level INFO
