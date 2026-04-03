"""Typed configuration layer (spec §6)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
import shlex
from typing import Any

from .models import (
    SUPPORTED_PROVIDERS,
    AgentConfig,
    CodingAgentConfig,
    HooksConfig,
    PollingConfig,
    PreflightConfig,
    QAReviewConfig,
    RunnerConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TransitionsConfig,
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
_DEFAULT_COMMANDS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
}
_DEFAULT_CLAUDE_COMMAND = "claude"  # fallback for unknown providers
_DEFAULT_TURN_TIMEOUT_MS = 3_600_000
_DEFAULT_READ_TIMEOUT_MS = 5_000
_DEFAULT_STALL_TIMEOUT_MS = 300_000
_MISSING = object()


def _default_workspace_root() -> str:
    return str(Path(tempfile.gettempdir()) / "symphony_workspaces")


def _default_workspace_root_for_project(project_slug: str) -> str:
    slug = project_slug.strip() or "default"
    return os.path.expanduser(f"~/cymphony-workspaces/{slug}")


def _git_output(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    output = proc.stdout.strip()
    return output or None


def _repo_root() -> Path:
    root = _git_output(["rev-parse", "--show-toplevel"])
    return Path(root).resolve() if root else Path.cwd().resolve()


def _repo_clone_source(repo_root: Path) -> str:
    origin = _git_output(["remote", "get-url", "origin"])
    return origin or str(repo_root)


def _default_base_branch() -> str:
    origin_head = _git_output(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if origin_head and "/" in origin_head:
        return origin_head.rsplit("/", 1)[-1]
    current = _git_output(["branch", "--show-current"])
    return current or "main"


def _default_hooks(project_slug: str) -> HooksConfig:
    repo_root = _repo_root()
    clone_source = _repo_clone_source(repo_root)
    base_branch = _default_base_branch()
    return HooksConfig(
        after_create=f"git clone {clone_source} .",
        before_run=(
            f"git fetch origin\n"
            f"git checkout {base_branch}\n"
            f"git reset --hard origin/{base_branch}"
        ),
        after_run=(
            "BRANCH=$(git branch --show-current)\n"
            f"if [ \"$BRANCH\" != \"{base_branch}\" ]; then\n"
            "  git add -A\n"
            "  git commit -m \"chore: agent work [skip ci]\" || true\n"
            "  git push -u origin \"$BRANCH\" || true\n"
            f"  TITLE=$(git log --format=\"%s\" origin/{base_branch}..HEAD | tail -1)\n"
            "  gh pr create --title \"$TITLE\" --body \"\" --head \"$BRANCH\" || true\n"
            "fi"
        ),
        before_remove=None,
        timeout_ms=_DEFAULT_HOOKS_TIMEOUT_MS,
    )


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


def _to_optional_str(value: Any, default: str | None) -> str | None:
    """Coerce to optional str. Explicit null/false/empty string → None."""
    if value is _MISSING:
        return default
    if value is None or value is False or value == "":
        return None
    return str(value)


def _to_str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list):
        return [str(v) for v in value]
    return default


def _command_provider_conflict(command: str, provider: str) -> str | None:
    """Return the conflicting built-in provider name if the command is an obvious mismatch.

    We only reject the high-confidence case where the configured executable is exactly
    another built-in provider command. This catches misleading configs like
    ``provider=claude`` with ``command=codex`` while still allowing custom same-provider
    paths such as ``/usr/local/bin/my-codex``.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = [command]
    if not argv:
        return None

    executable = Path(argv[0]).name.lower()
    for built_in in _DEFAULT_COMMANDS:
        if built_in != provider and executable == built_in:
            return built_in
    return None


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
    runner_raw: dict[str, Any] = raw.get("runner") or {}
    server_raw: dict[str, Any] = raw.get("server") or {}
    preflight_raw: dict[str, Any] = raw.get("preflight") or {}

    # --- tracker ---
    kind = _to_str(tracker_raw.get("kind"), "")
    endpoint = _to_str(tracker_raw.get("endpoint"), _DEFAULT_LINEAR_ENDPOINT)
    raw_api_key = _to_str(tracker_raw.get("api_key"), "$LINEAR_API_KEY")
    api_key = _resolve_env(raw_api_key) if raw_api_key.startswith("$") else raw_api_key
    project_slug = _to_str(tracker_raw.get("project_slug"), "")
    assignee = _to_str(tracker_raw.get("assignee"), "") or None

    tracker = TrackerConfig(
        kind=kind,
        endpoint=endpoint,
        api_key=api_key,
        project_slug=project_slug,
        active_states=_to_str_list(
            tracker_raw.get("active_states"), list(_DEFAULT_ACTIVE_STATES)
        ),
        terminal_states=_to_str_list(
            tracker_raw.get("terminal_states"), list(_DEFAULT_TERMINAL_STATES)
        ),
        assignee=assignee,
    )

    # --- polling ---
    polling = PollingConfig(
        interval_ms=_to_int(polling_raw.get("interval_ms"), _DEFAULT_POLL_INTERVAL_MS),
    )

    # --- workspace / repo lifecycle ---
    workspace_root = _to_str(
        workspace_raw.get("root"),
        _default_workspace_root_for_project(project_slug),
    )
    workspace = WorkspaceConfig(root=_expand_path(workspace_root))

    default_hooks = _default_hooks(project_slug)
    hooks = HooksConfig(
        after_create=_to_optional_str(
            hooks_raw.get("after_create", _MISSING), default_hooks.after_create
        ),
        before_run=_to_optional_str(
            hooks_raw.get("before_run", _MISSING), default_hooks.before_run
        ),
        after_run=_to_optional_str(
            hooks_raw.get("after_run", _MISSING), default_hooks.after_run
        ),
        before_remove=_to_optional_str(
            hooks_raw.get("before_remove", _MISSING), default_hooks.before_remove
        ),
        timeout_ms=_to_int(
            hooks_raw.get("timeout_ms"), default_hooks.timeout_ms
        ),
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

    provider = _to_str(agent_raw.get("provider"), "claude").lower()

    agent = AgentConfig(
        max_concurrent_agents=_to_int(
            agent_raw.get("max_concurrent_agents"), _DEFAULT_MAX_CONCURRENT_AGENTS
        ),
        max_turns=_to_int(agent_raw.get("max_turns"), _DEFAULT_MAX_TURNS),
        max_retry_backoff_ms=_to_int(
            agent_raw.get("max_retry_backoff_ms"), _DEFAULT_MAX_RETRY_BACKOFF_MS
        ),
        max_concurrent_agents_by_state=per_state,
        provider=provider,
    )

    # --- runner (subprocess settings, provider-neutral) ---
    default_command = _DEFAULT_COMMANDS.get(provider, _DEFAULT_CLAUDE_COMMAND)
    command = _to_str(runner_raw.get("command"), default_command)
    if not command:
        command = default_command

    runner = RunnerConfig(
        command=command,
        turn_timeout_ms=_to_int(runner_raw.get("turn_timeout_ms"), _DEFAULT_TURN_TIMEOUT_MS),
        read_timeout_ms=_to_int(runner_raw.get("read_timeout_ms"), _DEFAULT_READ_TIMEOUT_MS),
        stall_timeout_ms=_to_int(runner_raw.get("stall_timeout_ms"), _DEFAULT_STALL_TIMEOUT_MS),
        dangerously_skip_permissions=bool(
            runner_raw.get("dangerously_skip_permissions", True)
        ),
    )
    main_runner = CodingAgentConfig(
        command=runner.command,
        turn_timeout_ms=runner.turn_timeout_ms,
        read_timeout_ms=runner.read_timeout_ms,
        stall_timeout_ms=runner.stall_timeout_ms,
        dangerously_skip_permissions=runner.dangerously_skip_permissions,
        provider=provider,
    )

    # --- server (optional extension) ---
    if server_port_override is not None:
        server_port: int | None = server_port_override
    else:
        raw_port = server_raw.get("port")
        server_port = _to_int(raw_port, 0) if raw_port is not None else None

    server = ServerConfig(port=server_port)

    # --- preflight ---
    preflight = PreflightConfig(
        enabled=bool(preflight_raw.get("enabled", True)),
        required_clis=_to_str_list(
            preflight_raw.get("required_clis"), ["git", "gh"]
        ),
        required_env_vars=_to_str_list(
            preflight_raw.get("required_env_vars"), []
        ),
        expect_clean_worktree=bool(
            preflight_raw.get("expect_clean_worktree", False)
        ),
        base_branch=_to_str(
            preflight_raw.get("base_branch"), _default_base_branch()
        ),
    )

    # --- transitions ---
    # --- qa_review sub-config ---
    transitions_raw: dict[str, Any] = raw.get("transitions") or {}
    qa_raw: Any = transitions_raw.get("qa_review")
    _QA_DEFAULTS = QAReviewConfig()
    if isinstance(qa_raw, dict):
        qa_enabled = qa_raw.get("enabled")

        # --- optional QA agent override ---
        qa_agent_raw: dict[str, Any] = qa_raw.get("agent") or {}
        qa_agent: CodingAgentConfig | None = None
        if qa_agent_raw:
            qa_provider = _to_str(qa_agent_raw.get("provider"), main_runner.provider)
            qa_agent = CodingAgentConfig(
                command=_to_str(qa_agent_raw.get("command"), main_runner.command) or main_runner.command,
                turn_timeout_ms=_to_int(qa_agent_raw.get("turn_timeout_ms"), main_runner.turn_timeout_ms),
                read_timeout_ms=_to_int(qa_agent_raw.get("read_timeout_ms"), main_runner.read_timeout_ms),
                stall_timeout_ms=_to_int(qa_agent_raw.get("stall_timeout_ms"), main_runner.stall_timeout_ms),
                dangerously_skip_permissions=bool(
                    qa_agent_raw.get("dangerously_skip_permissions", main_runner.dangerously_skip_permissions)
                ),
                provider=qa_provider,
            )

        qa_review = QAReviewConfig(
            enabled=bool(qa_enabled) if qa_enabled is not None else _QA_DEFAULTS.enabled,
            dispatch=_to_optional_str(
                qa_raw.get("dispatch", _MISSING), _QA_DEFAULTS.dispatch
            ),
            success=_to_optional_str(
                qa_raw.get("success", _MISSING), _QA_DEFAULTS.success
            ),
            failure=_to_optional_str(
                qa_raw.get("failure", _MISSING), _QA_DEFAULTS.failure
            ),
            max_bounces=_to_int(
                qa_raw.get("max_bounces"), _QA_DEFAULTS.max_bounces
            ),
            max_retries=_to_int(
                qa_raw.get("max_retries"), _QA_DEFAULTS.max_retries
            ),
            agent=qa_agent,
        )
    elif qa_raw is True:
        # Shorthand: `qa_review: true` enables with all defaults
        qa_review = QAReviewConfig(enabled=True)
    else:
        qa_review = QAReviewConfig()

    # A QA review lane is agent-driven only if the review state is dispatchable.
    if qa_review.enabled and qa_review.dispatch:
        active_lower = {state.lower() for state in tracker.active_states}
        if qa_review.dispatch.lower() not in active_lower:
            tracker.active_states = [*tracker.active_states, qa_review.dispatch]

    transitions = TransitionsConfig(qa_review=qa_review)

    return ServiceConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        agent=agent,
        runner=runner,
        server=server,
        preflight=preflight,
        transitions=transitions,
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

    if not config.runner.command:
        result.fail("runner.command is missing or empty")

    if config.agent.provider not in SUPPORTED_PROVIDERS:
        result.fail(
            f"agent.provider={config.agent.provider!r} is not supported "
            f"(expected one of {', '.join(SUPPORTED_PROVIDERS)})"
        )
    else:
        conflicting_provider = _command_provider_conflict(
            config.runner.command,
            config.agent.provider,
        )
        if conflicting_provider is not None:
            result.fail(
                "runner.command does not match agent.provider: "
                f"provider={config.agent.provider!r} cannot use command "
                f"{config.runner.command!r} because it selects the {conflicting_provider!r} CLI"
            )

    # --- qa_review validation ---
    qa = config.transitions.qa_review
    if qa.enabled and not qa.dispatch:
        result.fail(
            "transitions.qa_review.dispatch is required when qa_review is enabled"
        )

    if qa.agent is not None:
        if not qa.agent.command:
            result.fail("transitions.qa_review.agent.command is missing or empty")
        if qa.agent.provider not in SUPPORTED_PROVIDERS:
            result.fail(
                f"transitions.qa_review.agent.provider={qa.agent.provider!r} is not supported "
                f"(expected one of {', '.join(SUPPORTED_PROVIDERS)})"
            )
        else:
            conflicting_provider = _command_provider_conflict(
                qa.agent.command,
                qa.agent.provider,
            )
            if conflicting_provider is not None:
                result.fail(
                    "transitions.qa_review.agent.command does not match "
                    "transitions.qa_review.agent.provider: "
                    f"provider={qa.agent.provider!r} cannot use command "
                    f"{qa.agent.command!r} because it selects the {conflicting_provider!r} CLI"
                )

    return result
