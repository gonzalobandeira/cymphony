"""Domain models for Cymphony (spec §4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Issue tracker model
# ---------------------------------------------------------------------------

@dataclass
class BlockerRef:
    """Blocker reference within an issue (spec §4.1.1)."""
    id: str | None
    identifier: str | None
    state: str | None


@dataclass
class Comment:
    """A comment on an issue."""
    author: str
    body: str
    created_at: datetime | None


@dataclass
class Issue:
    """Normalized issue record (spec §4.1.1)."""
    id: str
    identifier: str
    title: str
    project_name: str | None
    description: str | None
    priority: int | None
    state: str
    branch_name: str | None
    url: str | None
    labels: list[str]
    blocked_by: list[BlockerRef]
    comments: list[Comment]
    created_at: datetime | None
    updated_at: datetime | None


# ---------------------------------------------------------------------------
# Workflow / config models
# ---------------------------------------------------------------------------

@dataclass
class WorkflowDefinition:
    """Parsed WORKFLOW.md (spec §4.1.2)."""
    config: dict[str, Any]
    prompt_template: str


@dataclass
class TrackerConfig:
    kind: str
    endpoint: str
    api_key: str
    project_slug: str
    active_states: list[str]
    terminal_states: list[str]
    assignee: str | None


@dataclass
class PollingConfig:
    interval_ms: int


@dataclass
class WorkspaceConfig:
    root: str


@dataclass
class HooksConfig:
    after_create: str | None
    before_run: str | None
    after_run: str | None
    before_remove: str | None
    timeout_ms: int


SUPPORTED_PROVIDERS = ("claude", "codex")


@dataclass
class AgentConfig:
    max_concurrent_agents: int
    max_turns: int
    max_retry_backoff_ms: int
    max_concurrent_agents_by_state: dict[str, int]
    provider: str = "claude"


@dataclass
class CodingAgentConfig:
    """Config for the coding agent subprocess."""
    command: str
    turn_timeout_ms: int
    read_timeout_ms: int
    stall_timeout_ms: int
    dangerously_skip_permissions: bool
    provider: str = "claude"


@dataclass
class PreflightConfig:
    """Configuration for repo preflight checks before dispatch."""
    enabled: bool
    required_clis: list[str]
    required_env_vars: list[str]
    expect_clean_worktree: bool
    base_branch: str


@dataclass
class ServerConfig:
    port: int | None


@dataclass
class QAReviewConfig:
    """Configuration for the agent-driven QA review lane (spec §4.1.9.1).

    When enabled, the workflow becomes:
        Todo -> In Progress -> QA Review -> (Todo | In Review)

    Instead of transitioning directly to ``success`` after implementation,
    the issue moves to ``dispatch`` (the QA review state).  A separate QA
    agent run then transitions the issue to ``success`` (QA passed) or
    ``failure`` (QA failed, back to development).
    """
    enabled: bool = False
    dispatch: str | None = "QA Review"
    success: str | None = "In Review"
    failure: str | None = "Todo"
    max_bounces: int = 2
    max_retries: int = 2


@dataclass
class TransitionsConfig:
    """Declarative workflow state transitions (spec §4.1.9).

    Each field maps a lifecycle event to the Linear workflow state name
    the issue should be moved to.  A ``None`` value means "do not
    transition" for that event.
    """
    dispatch: str | None = "In Progress"
    success: str | None = "In Review"
    failure: str | None = None
    blocked: str | None = None
    cancelled: str | None = None
    qa_review: QAReviewConfig = field(default_factory=QAReviewConfig)

    def resolve(self, event: str) -> str | None:
        """Look up the target state for a lifecycle event.

        Returns the configured state name, or ``None`` if no transition
        is configured (meaning the orchestrator should skip the state change).
        """
        return getattr(self, event, None)


@dataclass
class ServiceConfig:
    """Fully typed runtime configuration (spec §4.1.3)."""
    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    coding_agent: CodingAgentConfig
    server: ServerConfig
    preflight: PreflightConfig
    transitions: TransitionsConfig = field(default_factory=TransitionsConfig)


# ---------------------------------------------------------------------------
# QA review decision contract
# ---------------------------------------------------------------------------

class ReviewDecision(str, Enum):
    """Machine-readable outcome of a QA review run."""
    PASS = "pass"
    CHANGES_REQUESTED = "changes_requested"


@dataclass
class ReviewResult:
    """Parsed result from a QA review agent run."""
    decision: ReviewDecision | None
    summary: str | None = None
    error: str | None = None
    raw_output: str | None = None


# ---------------------------------------------------------------------------
# Workspace model
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    """Filesystem workspace for one issue (spec §4.1.4)."""
    path: str
    workspace_key: str
    created_now: bool


# ---------------------------------------------------------------------------
# Agent / session models
# ---------------------------------------------------------------------------

class ExecutionMode(str, Enum):
    """Whether the orchestrator is running a build (implementation) or review (QA) flow."""
    BUILD = "build"
    REVIEW = "review"


class AgentEventType(str, Enum):
    SESSION_STARTED = "session_started"
    STARTUP_FAILED = "startup_failed"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_INPUT_REQUIRED = "turn_input_required"
    NOTIFICATION = "notification"
    OTHER_MESSAGE = "other_message"
    MALFORMED = "malformed"


@dataclass
class AgentEvent:
    """Structured event emitted by the agent runner to the orchestrator."""
    event: AgentEventType
    timestamp: datetime
    session_id: str | None = None
    pid: int | None = None
    usage: dict[str, int] | None = None
    message: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class LiveSession:
    """State tracked while a Claude Code subprocess is running (spec §4.1.6)."""
    session_id: str | None
    pid: int | None
    last_event: AgentEventType | None
    last_event_timestamp: datetime | None
    last_message: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    last_reported_input_tokens: int
    last_reported_output_tokens: int
    last_reported_total_tokens: int
    turn_count: int
    plan_comment_id: str | None = None
    latest_plan: str | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator state
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    PREPARING_WORKSPACE = "PreparingWorkspace"
    PLANNING = "Planning"
    BUILDING_PROMPT = "BuildingPrompt"
    LAUNCHING_AGENT = "LaunchingAgentProcess"
    INITIALIZING_SESSION = "InitializingSession"
    STREAMING_TURN = "StreamingTurn"
    FINISHING = "Finishing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    STALLED = "Stalled"
    CANCELED = "CanceledByReconciliation"


@dataclass
class RunningEntry:
    """One active worker in the orchestrator (spec §4.1.6 + §4.1.8)."""
    issue_id: str
    identifier: str
    issue: Issue
    task: asyncio.Task | None  # type: ignore[type-arg]
    session: LiveSession
    retry_attempt: int | None  # None = first run
    started_at: datetime
    review_result: ReviewResult | None = None
    mode: ExecutionMode = field(default=ExecutionMode.BUILD)
    status: RunStatus = field(default=RunStatus.PREPARING_WORKSPACE)
    qa_review_bounce_count: int = 0


@dataclass
class RetryEntry:
    """Scheduled retry state (spec §4.1.7)."""
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: float  # monotonic clock
    error: str | None
    mode: str = "build"
    state: str | None = None
    run_status: str | None = None
    session_id: str | None = None
    turn_count: int = 0
    last_event: str | None = None
    last_message: str | None = None
    last_event_at: datetime | None = None
    workspace_path: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    started_at: datetime | None = None
    retry_attempt: int | None = None
    plan_comment_id: str | None = None
    latest_plan: str | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    issue_title: str | None = None
    issue_url: str | None = None
    issue_description: str | None = None
    issue_labels: list[str] = field(default_factory=list)
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    qa_review_bounce_count: int = 0


@dataclass
class ProblemRecord:
    """Recent operator-visible problem captured by the orchestrator."""
    kind: str
    severity: str  # "error", "warning", "info"
    summary: str
    detail: str
    observed_at: datetime
    issue_id: str | None = None
    issue_identifier: str | None = None


@dataclass
class SkippedEntry:
    """Issue manually skipped by an operator."""
    issue_id: str
    identifier: str
    created_at: datetime
    reason: str


@dataclass
class TransitionRecord:
    """Recorded state transition for an issue."""
    timestamp: datetime
    issue_id: str
    issue_identifier: str
    from_state: str | None
    to_state: str
    trigger: str  # e.g. "dispatch", "success", "failure", "blocked", "cancelled"
    success: bool = True


@dataclass
class ControlAction:
    """Auditable operator control action."""
    timestamp: datetime
    action: str
    scope: str
    outcome: str
    issue_id: str | None = None
    issue_identifier: str | None = None
    detail: str | None = None


@dataclass
class CodexTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0


@dataclass
class OrchestratorState:
    """Single authoritative in-memory state (spec §4.1.8)."""
    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    qa_review_bounces: dict[str, int] = field(default_factory=dict)
    qa_review_comment_ids: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, SkippedEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    dispatch_paused: bool = False
    shutdown_requested: bool = False
    control_actions: list[ControlAction] = field(default_factory=list)
    codex_totals: CodexTotals = field(default_factory=CodexTotals)
    codex_rate_limits: dict[str, Any] | None = None
    last_candidates: list[Issue] = field(default_factory=list)
    last_validation_errors: list[str] = field(default_factory=list)
    last_preflight_errors: list[dict[str, str]] = field(default_factory=list)
    recent_problems: list[ProblemRecord] = field(default_factory=list)
    transition_history: list[TransitionRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class CymphonyError(Exception):
    """Base error."""
    code: str = "cymphony_error"


class WorkflowError(CymphonyError):
    """Workflow / config loading errors (spec §5.5)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TrackerError(CymphonyError):
    """Issue tracker errors (spec §11.4)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceError(CymphonyError):
    """Workspace lifecycle errors."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentError(CymphonyError):
    """Agent runner errors (spec §10.6)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PreflightError(CymphonyError):
    """Repo preflight check failure."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
