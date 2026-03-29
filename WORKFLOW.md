---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: cymphony-b2a8d0064141
  active_states:
  - Todo
  - In Progress
  terminal_states:
  - Done
  - Cancelled
  - Canceled
  - Duplicate
  - Closed
  assignee: gonzalobandeira
polling:
  interval_ms: 15000
workspace:
  root: ~/cymphony-workspaces
agent:
  max_concurrent_agents: 2
  max_turns: 15
  max_retry_backoff_ms: 300000
codex:
  command: claude
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  dangerously_skip_permissions: true
hooks:
  timeout_ms: 120000
  after_create: git clone git@github.com:gonzalobandeira/cymphony.git .
  before_run: "git fetch origin && git checkout main && git reset --hard origin/main\r\
    \ngit branch | grep -v '^\\* ' | xargs -r git branch -D 2>/dev/null || true"
  after_run: "BRANCH=$(git branch --show-current)\r\nif [ \"$BRANCH\" != \"main\"\
    \ ]; then\r\n  git add -A && git commit -m \"chore: agent work [skip ci]\" ||\
    \ true\r\n  git push -u origin \"$BRANCH\" || true\r\n  TITLE=$(git log --format=\"\
    %s\" origin/main..HEAD | tail -1)\r\n  gh pr create --title \"$TITLE\" --body\
    \ \"\" --head \"$BRANCH\" || true\r\nfi"
server:
  port: 8080
# Workflow transitions — map lifecycle events to Linear state names.
# Set a value to false or omit it to skip the transition for that event.
# Defaults: dispatch → "In Progress", success → "In Review", others → no transition.
transitions:
  dispatch: In Progress
  success: In Review
  # failure: null
  # blocked: null
  # cancelled: null
---
You are a senior software engineer working on the **Cymphony** project.

## Issue

**Title:** {{ issue.title }}
**Identifier:** {{ issue.identifier }}
**Priority:** {{ issue.priority }}
**State:** {{ issue.state }}
{% if issue.description %}

**Description:**
{{ issue.description }}
{% endif %}
{% if issue.labels %}
**Labels:** {{ issue.labels | join(', ') }}
{% endif %}
{% if issue.blocked_by %}

**Blocked by:**
{% for b in issue.blocked_by %}
- {{ b.identifier }} ({{ b.state }})
{% endfor %}
{% endif %}
{% if issue.comments %}

**Comments:**
{% for c in issue.comments %}
- **{{ c.author }}** ({{ c.created_at }}): {{ c.body }}
{% endfor %}
{% endif %}
{% if attempt and attempt > 1 %}

---
**Note:** This is attempt {{ attempt }}. Review any previous work in this workspace and continue from where things left off.
{% endif %}

## Instructions

1. Carefully read the issue title, description, and any comments above. Comments may contain feedback from previous attempts or reviewer instructions — treat them as high-priority guidance.
2. Create and checkout a branch named `agent/{{ issue.identifier | lower }}` before making any changes.
3. Explore the repository structure in your working directory to understand the codebase.
4. Implement the changes described in the issue.
5. Write or update tests as appropriate.
6. Ensure all existing tests continue to pass.
7. Commit your changes with a descriptive commit message referencing the issue identifier. Push to remote and create PR. Update linear task.

Focus on correctness, simplicity, and consistency with the existing codebase style.
