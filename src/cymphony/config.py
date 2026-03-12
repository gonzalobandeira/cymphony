"""Typed configuration layer (spec §6)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .models import (
    AgentConfig,
    CodingAgentConfig,
    HooksConfig,
    PollingConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
    WorkflowError,
    WorkspaceConfig,
)

# ---------------------------------------------------------------------------
# Defaults (spec §6.4)
# ---------------------------------------------------------------------------

_DEFAULT_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_DEFAULT_ACTIVE_STATES = ["Todo", "In Progress"]
_DEFAULT_TERMINAL_STATES = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
_DEFAULT_POLL_INTERVAL_MS = 30_000
_DEFAULT_HOOKS_TIMEOUT_MS = 60_000
_DEFAULT_MAX_CONCURRENT_AGENTS = 10
_DEFAULT_MAX_TURNS = 20
_DEFAULT_MAX_RETRY_BACKOFF_MS = 300_000
_DEFAULT_CLAUDE_COMMAND = "claude"
_DEFAULT_TURN_TIMEOUT_MS = 3_600_000
_DEFAULT_READ_TIMEOUT_MS = 5_000
_DEFAULT_STALL_TIMEOUT_MS = 300_000


def _default_workspace_root() -> str:
    return str(Path(tempfile.gettempdir()) / "symphony_workspaces")


# ---------------------------------------------------------------------------
# Value coercion helpers
# ---------------------------------------------------------------------------

def _resolve_env(value: str) -> str:
    """Expand $VAR_NAME references in a string value (spec §6.1)."""
    if value.startswith("$"):
        var_name = value[1:]
        resolved = os.environ.get(var_name, "")
        return resolved
    return value


def _expand_path(value: str) -> str:
    """Expand ~ and $VAR in path-like values (spec §6.1)."""
    if value.startswith("$"):
        value = _resolve_env(value)
    if value.startswith("~"):
        value = os.path.expanduser(value)
    return value


def _to_int(value: Any, default: int) -> int:
    """Coerce to int, returning default on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _to_str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list):
        return [str(v) for v in value]
    return default


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------

def build_config(workflow: WorkflowDefinition, server_port_override: int | None = None) -> ServiceConfig:
    """Build ServiceConfig from a WorkflowDefinition (spec §6)."""
    raw = workflow.config

    tracker_raw: dict[str, Any] = raw.get("tracker") or {}
    polling_raw: dict[str, Any] = raw.get("polling") or {}
    workspace_raw: dict[str, Any] = raw.get("workspace") or {}
    hooks_raw: dict[str, Any] = raw.get("hooks") or {}
    agent_raw: dict[str, Any] = raw.get("agent") or {}
    codex_raw: dict[str, Any] = raw.get("codex") or {}
    server_raw: dict[str, Any] = raw.get("server") or {}

    # --- tracker ---
    kind = _to_str(tracker_raw.get("kind"), "")
    endpoint = _to_str(tracker_raw.get("endpoint"), _DEFAULT_LINEAR_ENDPOINT)
    raw_api_key = _to_str(tracker_raw.get("api_key"), "$LINEAR_API_KEY")
    api_key = _resolve_env(raw_api_key) if raw_api_key.startswith("$") else raw_api_key
    project_slug = _to_str(tracker_raw.get("project_slug"), "")
    active_states = _to_str_list(tracker_raw.get("active_states"), _DEFAULT_ACTIVE_STATES)
    terminal_states = _to_str_list(tracker_raw.get("terminal_states"), _DEFAULT_TERMINAL_STATES)
    assignee = _to_str(tracker_raw.get("assignee"), "") or None

    tracker = TrackerConfig(
        kind=kind,
        endpoint=endpoint,
        api_key=api_key,
        project_slug=project_slug,
        active_states=active_states,
        terminal_states=terminal_states,
        assignee=assignee,
    )

    # --- polling ---
    polling = PollingConfig(
        interval_ms=_to_int(polling_raw.get("interval_ms"), _DEFAULT_POLL_INTERVAL_MS),
    )

    # --- workspace ---
    raw_root = workspace_raw.get("root")
    if raw_root:
        workspace_root = _expand_path(str(raw_root))
    else:
        workspace_root = _default_workspace_root()

    workspace = WorkspaceConfig(root=workspace_root)

    # --- hooks ---
    hooks_timeout_raw = hooks_raw.get("timeout_ms")
    hooks_timeout = _to_int(hooks_timeout_raw, _DEFAULT_HOOKS_TIMEOUT_MS)
    if hooks_timeout <= 0:
        hooks_timeout = _DEFAULT_HOOKS_TIMEOUT_MS

    hooks = HooksConfig(
        after_create=hooks_raw.get("after_create") or None,
        before_run=hooks_raw.get("before_run") or None,
        after_run=hooks_raw.get("after_run") or None,
        before_remove=hooks_raw.get("before_remove") or None,
        timeout_ms=hooks_timeout,
    )

    # --- agent ---
    per_state_raw: Any = agent_raw.get("max_concurrent_agents_by_state") or {}
    per_state: dict[str, int] = {}
    if isinstance(per_state_raw, dict):
        for k, v in per_state_raw.items():
            try:
                iv = int(v)
                if iv > 0:
                    per_state[str(k).lower()] = iv
            except (TypeError, ValueError):
                pass  # ignore invalid entries per spec

    agent = AgentConfig(
        max_concurrent_agents=_to_int(
            agent_raw.get("max_concurrent_agents"), _DEFAULT_MAX_CONCURRENT_AGENTS
        ),
        max_turns=_to_int(agent_raw.get("max_turns"), _DEFAULT_MAX_TURNS),
        max_retry_backoff_ms=_to_int(
            agent_raw.get("max_retry_backoff_ms"), _DEFAULT_MAX_RETRY_BACKOFF_MS
        ),
        max_concurrent_agents_by_state=per_state,
    )

    # --- coding agent (codex in spec) ---
    command = _to_str(codex_raw.get("command"), _DEFAULT_CLAUDE_COMMAND)
    if not command:
        command = _DEFAULT_CLAUDE_COMMAND

    coding_agent = CodingAgentConfig(
        command=command,
        turn_timeout_ms=_to_int(codex_raw.get("turn_timeout_ms"), _DEFAULT_TURN_TIMEOUT_MS),
        read_timeout_ms=_to_int(codex_raw.get("read_timeout_ms"), _DEFAULT_READ_TIMEOUT_MS),
        stall_timeout_ms=_to_int(codex_raw.get("stall_timeout_ms"), _DEFAULT_STALL_TIMEOUT_MS),
        dangerously_skip_permissions=bool(
            codex_raw.get("dangerously_skip_permissions", True)
        ),
    )

    # --- server (optional extension) ---
    if server_port_override is not None:
        server_port: int | None = server_port_override
    else:
        raw_port = server_raw.get("port")
        server_port = _to_int(raw_port, 0) if raw_port is not None else None

    server = ServerConfig(port=server_port)

    return ServiceConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        agent=agent,
        coding_agent=coding_agent,
        server=server,
    )


# ---------------------------------------------------------------------------
# Dispatch preflight validation (spec §6.3)
# ---------------------------------------------------------------------------

class ValidationResult:
    def __init__(self) -> None:
        self.ok = True
        self.errors: list[str] = []

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def __repr__(self) -> str:
        if self.ok:
            return "ValidationResult(ok=True)"
        return f"ValidationResult(ok=False, errors={self.errors})"


def validate_dispatch_config(config: ServiceConfig) -> ValidationResult:
    """Validate config before dispatch (spec §6.3)."""
    result = ValidationResult()

    if not config.tracker.kind:
        result.fail("tracker.kind is missing")
    elif config.tracker.kind != "linear":
        result.fail(f"tracker.kind={config.tracker.kind!r} is not supported (expected 'linear')")

    if not config.tracker.api_key:
        result.fail("tracker.api_key is missing or resolved to empty string")

    if config.tracker.kind == "linear" and not config.tracker.project_slug:
        result.fail("tracker.project_slug is required when tracker.kind=linear")

    if not config.coding_agent.command:
        result.fail("codex.command is missing or empty")

    return result


def has_valid_config(workflow: WorkflowDefinition) -> bool:
    """Return True if workflow has a valid, dispatchable config."""
    try:
        result = validate_dispatch_config(build_config(workflow))
        return result.ok
    except Exception:
        return False
