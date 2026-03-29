---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: windguruspots-cd68ac867d6f
  assignee: gonzalobandeira
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Cancelled
    - Canceled
    - Duplicate
    - Closed

polling:
  interval_ms: 15000 # seconds

workspace:
  root: ~/windguruspots-workspaces

agent:
  provider: claude                  # claude | codex
  max_concurrent_agents: 2
  max_turns: 15
  max_retry_backoff_ms: 300000    # 5 minutes

codex:
  command: claude
  turn_timeout_ms: 3600000        # 1 hour per turn
  stall_timeout_ms: 300000        # 5 minutes stall detection
  dangerously_skip_permissions: true

hooks:
  after_create: |
    git clone git@github.com:gonzalobandeira/windguru-spots.git .
  before_run: |
    git fetch origin && git checkout main && git reset --hard origin/main
    git branch | grep -v '^\* ' | xargs -r git branch -D 2>/dev/null || true
  after_run: |
    BRANCH=$(git branch --show-current)
    if [ "$BRANCH" != "main" ]; then
      git add -A && git commit -m "chore: agent work [skip ci]" || true
      git push -u origin "$BRANCH" || true
      TITLE=$(git log --format="%s" origin/main..HEAD | tail -1)
      gh pr create --title "$TITLE" --body "" --head "$BRANCH" || true
    fi
  timeout_ms: 120000

server:
  port: 8080
---
You are a senior software engineer working on the **WindguruSpots** project.

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
7. Commit your changes with a descriptive commit message referencing the issue identifier.

Focus on correctness, simplicity, and consistency with the existing codebase style.
