---
# Cymphony Workflow Configuration — Example Template
#
# Copy this file to .cymphony/workflow.md and fill in your values:
#
#   mkdir -p .cymphony && cp WORKFLOW.example.md .cymphony/workflow.md
#
# Or start Cymphony with --port to use the setup screen:
#
#   cymphony --port 8080
#
# See README.md and AGENTS.md for full documentation.

tracker:
  kind: linear
  api_key: $LINEAR_API_KEY            # Reads from env var; set in .env or shell
  project_slug: ""                     # Required: your Linear project slug
  active_states:
  - Todo
  - In Progress
  terminal_states:
  - Done
  - Cancelled
  - Canceled
  - Duplicate
  - Closed
  assignee: ""                         # Optional: filter issues by assignee username

polling:
  interval_ms: 30000

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
  after_create: ""                     # e.g. git clone <repo-url> .
  before_run: ""                       # e.g. git fetch origin && git checkout main ...
  after_run: ""                        # e.g. git push && gh pr create ...

server:
  port: 8080

# Workflow transitions — map lifecycle events to Linear state names.
# Set a value to false or omit it to skip the transition for that event.
transitions:
  dispatch: In Progress
  success: In Review
  # failure: null
  # blocked: null
  # cancelled: null
  # qa_review:
  #   enabled: true
  #   dispatch: QA Review
  #   success: In Review
  #   failure: Todo
---
You are a senior software engineer working on the project.

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

1. Carefully read the issue title, description, and any comments above.
2. Create and checkout a branch named `agent/{{ issue.identifier | lower }}` before making any changes.
3. Explore the repository structure in your working directory to understand the codebase.
4. Implement the changes described in the issue.
5. Write or update tests as appropriate.
6. Ensure all existing tests continue to pass.
7. Commit your changes with a descriptive commit message referencing the issue identifier. Push to remote and create PR.

Focus on correctness, simplicity, and consistency with the existing codebase style.
