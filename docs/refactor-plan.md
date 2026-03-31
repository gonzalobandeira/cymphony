# Cymphony Refactor Plan

This document maps the current codebase to the target architecture described in
[`docs/spec-agent-workflows.md`](./spec-agent-workflows.md).

The intent is not a full rewrite. The intent is to keep proven integrations,
replace the current orchestration center, and migrate behavior into explicit
workflow handlers.

## Recommendation

Refactor in place.

Do not start from scratch. The current codebase already contains useful and
tested pieces for:

- Linear API integration
- Claude and Codex subprocess runner integration
- workflow file parsing and prompt rendering
- existing runtime and parser tests

Do not keep the current runtime architecture as-is. The current
[`src/cymphony/orchestrator.py`](../src/cymphony/orchestrator.py) is too large
and owns too many independent policies.

## Target Architecture

The refactor target is:

- `IssueStateMachine`
- `ExecutionWorkflow`
- `QAReviewWorkflow`
- `LinearService`
- `WorkspaceService`
- `PRService`
- thin poller / dispatcher orchestration

Rule of ownership:

- the orchestrator decides which workflow to run
- the selected workflow owns its own behavior

## Module Mapping

### Keep

These areas should be retained and adapted rather than rewritten:

- [`src/cymphony/linear.py`](../src/cymphony/linear.py)
- [`src/cymphony/runners/base.py`](../src/cymphony/runners/base.py)
- [`src/cymphony/runners/claude.py`](../src/cymphony/runners/claude.py)
- [`src/cymphony/runners/codex.py`](../src/cymphony/runners/codex.py)
- prompt rendering pieces in [`src/cymphony/workflow.py`](../src/cymphony/workflow.py)
- domain models in [`src/cymphony/models.py`](../src/cymphony/models.py), though they should be cleaned up over time

### Extract

Behavior that currently exists but should move behind narrower boundaries:

- Linear comment and transition logic out of
  [`src/cymphony/orchestrator.py`](../src/cymphony/orchestrator.py)
- execution and QA worktree policy out of
  [`src/cymphony/workspace.py`](../src/cymphony/workspace.py) and
  [`src/cymphony/orchestrator.py`](../src/cymphony/orchestrator.py)
- workflow selection and state transition rules out of
  [`src/cymphony/orchestrator.py`](../src/cymphony/orchestrator.py)
- PR and branch publication logic out of hooks and runtime orchestration

### Replace

These areas should be replaced as primary design centers:

- [`src/cymphony/orchestrator.py`](../src/cymphony/orchestrator.py)
- QA mode branching spread through the orchestrator
- the implicit "review is just another execution mode" design

### Defer

These areas can remain temporarily and be cleaned up after the workflow core is stable:

- [`src/cymphony/server.py`](../src/cymphony/server.py)
- workflow config setup UX
- persistence and dashboard details not directly tied to the new workflow contract

## Migration Phases

### Phase 1: Freeze Product Contract

Goal:

- agree on the workflow spec

Deliverables:

- [`docs/spec-agent-workflows.md`](./spec-agent-workflows.md)
- this refactor plan

### Phase 2: Introduce New Core Modules

Goal:

- create a stable landing zone for the refactor

Deliverables:

- `src/cymphony/state_machine.py`
- `src/cymphony/workflows/execution.py`
- `src/cymphony/workflows/qa_review.py`
- `src/cymphony/services/linear_service.py`
- `src/cymphony/services/workspace_service.py`
- `src/cymphony/services/pr_service.py`

At this phase the modules may still be thin wrappers or stubs.

### Phase 3: Route Execution Through `ExecutionWorkflow`

Goal:

- move the `To Do -> In Progress -> QA Review` path out of the legacy orchestrator

Required behavior:

- claim issue in `To Do`
- move to `In Progress`
- gather repo and Linear context
- render and post plan
- reuse execution worktree
- run implementation agent
- commit / push / create or update PR
- move to `QA Review`

### Phase 4: Route QA Through `QAReviewWorkflow`

Goal:

- move the `QA Review -> In Review | To Do` path into an isolated workflow

Required behavior:

- create a fresh QA worktree for every QA run
- gather PR and repo review context
- run verification commands as part of review
- produce pass or changes-requested decision
- post QA comment to Linear
- move issue to `In Review` or `To Do`

### Phase 5: Shrink the Legacy Orchestrator

Goal:

- reduce the orchestrator to polling, dispatching, and run supervision

The legacy orchestrator should no longer own:

- plan posting behavior
- QA decision logic
- state machine rules
- worktree policy details

### Phase 6: Clean Up Legacy Paths

Goal:

- remove duplicate logic after migration is stable

Targets:

- old QA mode branches
- obsolete helpers
- redundant config fields that no longer express the product cleanly

## Immediate Coding Priorities

1. Add the new workflow and service module structure.
2. Add a small state machine with explicit workflow selection.
3. Add tests for workflow selection and state transition rules.
4. Move execution-only behavior behind `ExecutionWorkflow`.
5. Move QA-only behavior behind `QAReviewWorkflow`.

## Non-Goals For The First Refactor Slice

Do not try to solve all of this in one pass.

The first slice should not:

- rewrite the entire HTTP server
- replace all config parsing
- redesign every domain model
- remove the old orchestrator immediately

The first slice should create separation of responsibilities and prove the new
workflow core can coexist with the current app while behavior is migrated.

