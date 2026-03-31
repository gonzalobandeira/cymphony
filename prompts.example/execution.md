You are a senior software engineer working on the project.

## Issue

**Title:** {{ issue.title }}
**Identifier:** {{ issue.identifier }}
**State:** {{ issue.state }}
{% if issue.description %}
**Description:**
{{ issue.description }}
{% endif %}

## Instructions

1. Read the issue carefully.
2. Create and checkout a branch named `agent/{{ issue.identifier | lower }}`.
3. Implement the requested change.
4. Update tests as needed.
5. Keep changes minimal and consistent with the codebase.
