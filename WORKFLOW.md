---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: cd68ac867d6f
  assignee: Gonzalo Bandeira
  active_states:
  - In Progress
  - Todo
  terminal_states:
  - Canceled
  - Duplicate
  - Done
polling:
  interval_ms: 30000
workspace:
  root: ~/cymphony-workspaces
agent:
  max_concurrent_agents: 3
  max_turns: 20
codex:
  command: claude
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  dangerously_skip_permissions: true
hooks:
  after_create: 'git clone git@github.com:gonzalobandeira/windguru-spots.git .

    '
  before_run: 'git fetch origin && git checkout main && git reset --hard origin/main

    git branch | grep -v ''^\* '' | xargs -r git branch -D 2>/dev/null || true

    '
  after_run: "BRANCH=$(git branch --show-current)\nif [ \"$BRANCH\" != \"main\" ];\
    \ then\n  git add -A && git commit -m \"chore: agent work [skip ci]\" || true\n\
    \  git push -u origin \"$BRANCH\" || true\n  TITLE=$(git log --format=\"%s\" origin/main..HEAD\
    \ | tail -1)\n  gh pr create --title \"$TITLE\" --body \"\" --head \"$BRANCH\"\
    \ || true\nfi\n"
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
