# Cymphony Agent Workflow Spec

This document defines the intended product behavior for Cymphony at a workflow level. It is a design target for simplifying the current implementation.

## Product Goal

Cymphony is a tool for the programmer, where the programmer primarily reviews and approves work rather than writing implementation code directly.

The system uses agentic coding engines such as Codex or Claude to perform implementation and QA review work. Linear is the source of truth for task tracking and workflow state.

The product supports exactly two autonomous workflows:

1. `execution`
2. `qa_review`

The normal lifecycle is:

`Todo -> In Progress -> QA Review -> In Review`

With one QA rejection loop:

`QA Review -> Todo`

## Core Principles

- Linear is the workflow authority.
- The human programmer is the final reviewer, not the primary implementer.
- Execution and QA are distinct workflows with distinct runtime context.
- QA must operate with clean isolation from execution.
- The same issue may go through multiple execution and QA cycles before reaching human review.
- The same PR should be updated when QA requests changes.

## Linear States

Required states:

- `Todo`
- `In Progress`
- `QA Review`
- `In Review`

State meanings:

- `Todo`: issue is ready for implementation pickup
- `In Progress`: execution agent currently owns the issue
- `QA Review`: implementation is complete and awaiting QA review
- `In Review`: QA passed and issue is ready for human review

Allowed automated transitions:

- `Todo -> In Progress`
- `In Progress -> QA Review`
- `QA Review -> In Review`
- `QA Review -> Todo`

No other automatic transitions should be part of the default product contract.

## Workflow 1: Execution

### Trigger

An issue enters `Todo` and has label `cymphony`.

### Purpose

The execution workflow is responsible for implementation work: understanding the task, planning it, changing code, validating the changes, and opening or updating the issue PR.

### Execution Steps

1. The execution agent claims the issue.
2. The issue is moved to `In Progress` in Linear.
3. The execution agent gathers context from:
   - Linear issue title and description or any attachement in task
   - Linear comments
   - repository contents (AGENTS.md, CLAUDE.md, etc)
   - existing branch and PR context for the issue, if present
   - latest QA feedback if the issue was previously rejected in `QA Review`
4. The execution agent generates a plan before implementation begins.
5. That plan is posted to Linear as a comment.
6. The execution agent works in a dedicated execution worktree for the issue.
7. If the issue already has an execution worktree, it may be reused.
8. The execution agent implements the requested changes.
9. The execution agent runs relevant validation, including tests as appropriate.
10. The execution agent commits the changes.
11. The execution agent pushes the branch.
12. The execution agent creates a PR if one does not exist, or updates the existing PR if it does.
13. Linear is updated with the PR link if needed.
14. The issue is moved to `QA Review`.

### Execution Inputs

Required context:

- issue title
- issue description
- issue comments
- labels and metadata as needed
- repository context

Rework context:

- if the issue was returned from `QA Review` to `Todo`, the latest QA changes-requested feedback must be injected into the execution context
- the new execution plan should explicitly account for that feedback

### Execution Outputs

Required outputs:

- a Linear plan comment
- code changes in the execution branch
- commit history for the issue
- a PR for the issue
- a Linear update linking the issue to the PR
- transition of the issue to `QA Review`

## Workflow 2: QA Review

### Trigger

An issue enters `QA Review` and has label `cymphony`.

### Purpose

The QA workflow is responsible for independently reviewing the implementation produced by execution. It evaluates whether the changes satisfy the issue requirements and are ready for human review.

QA is not implementation. Its job is judgment, not authorship.

### QA Review Steps

1. The QA agent claims the issue in review mode.
2. The QA agent gathers context from:
   - Linear issue title and description
   - Linear comments
   - the current PR
   - changed files and commits in the PR
   - repository context needed to evaluate correctness
3. The QA agent runs in a clean QA worktree that is separate from the execution worktree.
4. The QA worktree must be created fresh for every QA run.
5. The QA agent may run verification commands needed for review, including:
   - tests
   - lint
   - type checks
   - other lightweight validation commands relevant to the repo
6. The QA agent evaluates:
   - correctness against the issue requirements
   - code quality
   - test coverage adequacy
   - likely regressions
   - consistency with project conventions
7. The QA agent posts a QA result comment to Linear.
8. The QA agent makes one of two decisions:
   - pass
   - changes requested
9. If pass, the issue is moved to `In Review`.
10. If changes are requested, the issue is moved back to `To Do`.

### QA Isolation Rules

- QA must never run in the execution worktree.
- QA must start from a clean, newly created worktree on every review run.
- QA must not inherit uncommitted execution state.
- QA must not rely on hidden runtime context from prior execution runs.

### QA Outputs

Required outputs:

- a Linear QA review comment
- a decision of `pass` or `changes requested`
- transition to `In Review` or `To Do`

## Worktree Model

### Execution Worktree

Execution uses one persistent worktree per issue.

Properties:

- tied to the issue identity
- reusable across multiple execution runs on the same issue
- associated with the issue branch and PR

Suggested path convention:

- `workspaces/execution/<ISSUE_IDENTIFIER>`

### QA Worktree

QA uses a brand-new worktree for every QA run.

Properties:

- never shared with execution
- created fresh each time
- deleted after the QA run completes, unless retained temporarily for debugging

Suggested path convention:

- `workspaces/qa/<ISSUE_IDENTIFIER>/<RUN_ID>`

### Branch / PR Policy

- execution updates the same issue branch over time
- QA rejection does not create a new PR
- after QA rejection, later execution runs update the same PR

## Linear Comment Contract

### Execution Plan Comment

The execution workflow must post a plan comment before making code changes.

The plan comment should:

- summarize understanding of the task
- include a concrete step-by-step plan
- mention relevant QA feedback if this is a rework cycle

### Execution Completion Comment

The execution workflow should post or update a completion note that includes:

- branch name
- PR link
- concise summary of implementation work
- validation performed

### QA Pass Comment

The QA workflow must leave a comment stating that QA passed.

It should include:

- concise rationale
- notable verification evidence when relevant
- any residual caveats worth surfacing to the human reviewer

### QA Changes Requested Comment

The QA workflow must leave a comment stating that changes are required.

It should include:

- concrete problems found
- actionable guidance for the next execution run
- enough detail for the next agent run to address the issues directly

## Rework Cycle

If QA rejects an issue:

1. QA posts a changes-requested comment to Linear.
2. The issue is moved from `QA Review` to `To Do`.
3. The next execution run reuses the execution worktree for that issue.
4. The next execution run updates the same branch and same PR.
5. The next execution plan must explicitly consider the latest QA feedback.

## Agent Pools

The system should support separate agent pools for execution and QA review.

Execution agent pool:

- used for implementation
- can be configured independently
- may use Codex or Claude

QA agent pool:

- used only for review
- can be configured independently from execution
- may use a different model, command, timeout, or risk posture

This separation is a core product requirement, not an implementation detail.

## Agent Responsibilities

### Execution Agent

The execution agent is responsible for:

- understanding the task
- gathering repo and Linear context
- producing a plan
- posting the plan to Linear
- implementing changes
- running validation
- committing and pushing changes
- creating or updating the PR

The execution agent is not responsible for final approval.

### QA Agent

The QA agent is responsible for:

- independently reviewing the implementation
- gathering PR and repo context for review
- running verification commands relevant to review
- deciding whether the implementation is acceptable
- posting a QA review result to Linear

The QA agent should not perform the normal implementation workflow.

## Non-Goals

The product is not intended to:

- replace human final review
- serve as a generic agent orchestration platform
- support many overlapping workflow variants by default
- let QA silently repair code instead of producing a review decision
- optimize for maximum flexibility at the cost of clarity

## Minimal Architectural Implications

The intended implementation should be built around explicit workflow boundaries.

Suggested responsibilities:

- `IssueStateMachine`: owns valid issue transitions and workflow selection
- `ExecutionWorkflow`: owns execution behavior end to end
- `QAReviewWorkflow`: owns QA behavior end to end
- `LinearService`: owns issue reads, comments, transitions, and PR link updates
- `WorkspaceService`: owns execution and QA worktree lifecycle
- `AgentRunner`: owns Claude/Codex process integration
- `PRService`: owns branch, push, and PR creation/update behavior

The key architectural rule is:

- orchestration decides what workflow to run
- each workflow owns its own behavior

## Current Design Intent

This spec is intentionally narrower than a general orchestration engine.

Cymphony should be designed as a focused Linear-driven implementation and QA pipeline where:

- execution writes code
- QA reviews code
- humans perform final approval
