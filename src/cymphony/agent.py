"""Agent runner implementations (spec §10).

Trust posture (documented per spec §15.1):
  High-trust mode. The claude CLI is invoked with --dangerously-skip-permissions so that
  all tool calls are auto-approved. This is intended for trusted operator environments only.
  Operators running in untrusted or multi-tenant environments must review and tighten this posture.

Provider-agnostic runner interface:
  BaseAgentRunner defines the contract: _build_command, _build_env, and _parse_event.
  Each provider (Claude, Codex) implements these behind a common run_turn() method.
  The orchestrator uses create_runner() to get the right implementation from config.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from .models import (
    AgentError,
    AgentEvent,
    AgentEventType,
    CodingAgentConfig,
)

logger = logging.getLogger(__name__)

# Max stdout line size for safe buffering (spec §10.1)
_MAX_LINE_BYTES = 10 * 1024 * 1024  # 10 MB

OnAgentEvent = Callable[[AgentEvent], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Abstract runner interface
# ---------------------------------------------------------------------------

class BaseAgentRunner(abc.ABC):
    """Provider-agnostic subprocess runner for coding agents.

    Subclasses must implement:
      - _build_command: construct the CLI invocation
      - _parse_event: parse one stdout line into an AgentEvent
    Optionally override:
      - _build_env: customize subprocess environment
    """

    def __init__(self, config: CodingAgentConfig) -> None:
        self._config = config

    @abc.abstractmethod
    def _build_command(
        self,
        prompt: str,
        workspace_path: str,
        session_id: str | None,
        title: str,
    ) -> list[str]:
        """Build the provider CLI command list."""

    def _build_env(self) -> dict[str, str]:
        """Build the subprocess environment for this provider."""
        return dict(os.environ)

    @abc.abstractmethod
    def _parse_event(
        self,
        line: str,
        current_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        """Parse one stdout line into an AgentEvent.

        Returns (event, new_session_id, turn_succeeded, turn_error).
        """

    async def run_turn(
        self,
        workspace_path: str,
        prompt: str,
        issue_id: str,
        issue_identifier: str,
        session_id: str | None,
        title: str,
        on_event: OnAgentEvent,
    ) -> str:
        """Run one agent turn. Returns the session_id for continuation.

        Raises AgentError on failure, timeout, or user input required.
        Enforces:
          - subprocess cwd = workspace_path (spec §9.5 invariant 1)
          - turn_timeout_ms (spec §10.6)
        """
        ws = Path(workspace_path).resolve()

        # Safety invariant 1: cwd must be the workspace path (spec §9.5)
        if not ws.is_dir():
            raise AgentError(
                "invalid_workspace_cwd",
                f"Workspace path {ws} is not a directory",
            )

        cmd = self._build_command(prompt, str(ws), session_id, title)
        turn_timeout_secs = self._config.turn_timeout_ms / 1000.0

        logger.info(
            f"action=agent_turn_start "
            f"issue_id={issue_id} issue_identifier={issue_identifier} "
            f"session_id={session_id!r} workspace={ws}"
        )

        env = self._build_env()

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ws),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_MAX_LINE_BYTES,
            )
        except OSError as exc:
            raise AgentError(
                "agent_not_found",
                f"Failed to launch agent CLI: {exc}",
            ) from exc

        pid = proc.pid
        resolved_session_id: str | None = session_id
        turn_succeeded = False
        turn_error: str | None = None

        try:
            result_session_id, turn_succeeded, turn_error = await asyncio.wait_for(
                self._stream_turn(
                    proc=proc,
                    initial_session_id=session_id,
                    issue_id=issue_id,
                    issue_identifier=issue_identifier,
                    pid=pid,
                    on_event=on_event,
                ),
                timeout=turn_timeout_secs,
            )
            resolved_session_id = result_session_id or session_id
        except asyncio.TimeoutError:
            logger.warning(
                f"action=agent_turn_timeout "
                f"issue_id={issue_id} issue_identifier={issue_identifier} "
                f"turn_timeout_ms={self._config.turn_timeout_ms}"
            )
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
            await on_event(AgentEvent(
                event=AgentEventType.TURN_FAILED,
                timestamp=_now(),
                session_id=resolved_session_id,
                pid=pid,
                message="turn_timeout",
            ))
            raise AgentError("turn_timeout", f"Turn timed out after {self._config.turn_timeout_ms}ms")
        except AgentError:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise

        # Wait for process to exit if not already done
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()

        if not turn_succeeded:
            raise AgentError(
                "turn_failed",
                turn_error or "Agent turn did not complete successfully",
            )

        logger.info(
            f"action=agent_turn_completed "
            f"issue_id={issue_id} issue_identifier={issue_identifier} "
            f"session_id={resolved_session_id}"
        )
        return resolved_session_id or ""

    async def _stream_turn(
        self,
        proc: asyncio.subprocess.Process,
        initial_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
        on_event: OnAgentEvent,
    ) -> tuple[str | None, bool, str | None]:
        """Stream stdout lines from agent CLI, emit events, return (session_id, succeeded, error).

        stdout carries protocol messages (stream-json).
        stderr is diagnostics only — logged but not parsed.
        """
        session_id = initial_session_id
        turn_succeeded = False
        turn_error: str | None = None
        stderr_lines: list[str] = []

        # Read stderr as background task (log only, spec §10.3)
        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            async for line in proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    stderr_lines.append(text)
                    logger.debug(
                        f"action=agent_stderr "
                        f"issue_id={issue_id} pid={pid} line={text!r}"
                    )

        stderr_task = asyncio.create_task(_drain_stderr())

        assert proc.stdout is not None
        buffer = b""
        async for raw_chunk in proc.stdout:
            buffer += raw_chunk
            # Process complete newline-delimited lines
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                if not line_bytes.strip():
                    continue

                line_str = line_bytes.decode(errors="replace")
                event, sid, succeeded, err = self._parse_event(
                    line_str, session_id, issue_id, issue_identifier, pid
                )
                if sid:
                    session_id = sid
                if succeeded is True:
                    turn_succeeded = True
                if err is not None:
                    turn_error = err
                if event:
                    # Check for user input required — hard fail (spec §10.5)
                    if event.event == AgentEventType.TURN_INPUT_REQUIRED:
                        await on_event(event)
                        # Kill process
                        if proc.returncode is None:
                            proc.kill()
                        raise AgentError(
                            "turn_input_required",
                            "Agent requested user input — hard fail (high-trust policy)",
                        )
                    await on_event(event)

        await stderr_task

        if not turn_succeeded:
            stderr_summary = "; ".join(stderr_lines[-5:]) if stderr_lines else ""
            logger.warning(
                f"action=agent_turn_no_success "
                f"issue_id={issue_id} issue_identifier={issue_identifier} "
                f"pid={pid} exit_code={proc.returncode!r} "
                f"stderr={stderr_summary!r}"
            )

        return session_id, turn_succeeded, turn_error


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------

class ClaudeAgentRunner(BaseAgentRunner):
    """Runs Claude Code as a subprocess and streams AgentEvents (spec §10)."""

    def _build_command(
        self,
        prompt: str,
        workspace_path: str,
        session_id: str | None,
        title: str,
    ) -> list[str]:
        """Build the claude CLI command list."""
        cmd = [self._config.command, "--output-format", "stream-json", "--verbose", "--print", prompt]

        # Continuation turn: resume existing session (spec §10.3)
        if session_id:
            cmd.extend(["--resume", session_id])

        # High-trust: auto-approve all tool calls (documented posture)
        if self._config.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the Claude subprocess environment.

        Keep operator-selected Claude auth env vars intact for unattended runs while
        removing the sentinel that would cause nested CLI invocations to mis-detect
        the parent Codex environment.
        """
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        return env

    def _parse_event(
        self,
        line: str,
        current_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        """Parse one stream-json line from Claude CLI."""
        return _parse_claude_stream_event(
            line, current_session_id, issue_id, issue_identifier, pid
        )


# ---------------------------------------------------------------------------
# Codex runner (stub)
# ---------------------------------------------------------------------------

class CodexAgentRunner(BaseAgentRunner):
    """Runs OpenAI Codex CLI as a subprocess and streams AgentEvents.

    This is a stub implementation. The command building and event parsing
    will be filled in once the Codex CLI protocol is finalized.
    """

    def _build_command(
        self,
        prompt: str,
        workspace_path: str,
        session_id: str | None,
        title: str,
    ) -> list[str]:
        cmd = [self._config.command, "--quiet", prompt]
        if self._config.dangerously_skip_permissions:
            cmd.append("--full-auto")
        return cmd

    def _parse_event(
        self,
        line: str,
        current_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        """Parse one stdout line from Codex CLI.

        Codex uses the same newline-delimited JSON protocol as Claude, so
        we reuse the Claude parser for now. This will diverge once the Codex
        output format is finalized.
        """
        return _parse_claude_stream_event(
            line, current_session_id, issue_id, issue_identifier, pid
        )


# ---------------------------------------------------------------------------
# Runner factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[BaseAgentRunner]] = {
    "claude": ClaudeAgentRunner,
    "codex": CodexAgentRunner,
}


def create_runner(config: CodingAgentConfig) -> BaseAgentRunner:
    """Create the appropriate runner for the configured provider."""
    provider = config.provider
    runner_cls = _PROVIDERS.get(provider)
    if runner_cls is None:
        raise AgentError(
            "unknown_provider",
            f"Unknown agent provider {provider!r}. Supported: {', '.join(_PROVIDERS)}",
        )
    return runner_cls(config)


# Keep backward-compatible alias
AgentRunner = ClaudeAgentRunner


# ---------------------------------------------------------------------------
# Claude stream-json parser (private)
# ---------------------------------------------------------------------------

def _parse_claude_stream_event(
    line: str,
    current_session_id: str | None,
    issue_id: str,
    issue_identifier: str,
    pid: int,
) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
    """Parse one stream-json line from claude CLI.

    Returns (event, new_session_id, turn_succeeded, turn_error).
    """
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        logger.debug(
            f"action=agent_malformed_line "
            f"issue_id={issue_id} line={line[:200]!r}"
        )
        return (
            AgentEvent(
                event=AgentEventType.MALFORMED,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
                message=line[:200],
            ),
            None,
            None,
            None,
        )

    msg_type = msg.get("type", "")
    new_session_id: str | None = None
    turn_succeeded: bool | None = None
    turn_error: str | None = None
    event: AgentEvent | None = None

    if msg_type == "system" and msg.get("subtype") == "init":
        # First message — extract session_id
        new_session_id = msg.get("session_id") or current_session_id
        event = AgentEvent(
            event=AgentEventType.SESSION_STARTED,
            timestamp=_now(),
            session_id=new_session_id,
            pid=pid,
        )

    elif msg_type == "assistant":
        # Agent message — extract summary for observability
        content = msg.get("message", {}).get("content", [])
        summary = _summarize_content(content)
        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            message=summary,
            raw=msg,
        )

    elif msg_type == "result":
        subtype = msg.get("subtype", "")
        usage = _extract_usage(msg)

        if subtype == "success":
            turn_succeeded = True
            event = AgentEvent(
                event=AgentEventType.TURN_COMPLETED,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
                usage=usage,
            )
        elif "input_required" in subtype or "user_input" in subtype:
            # User input required — hard fail
            event = AgentEvent(
                event=AgentEventType.TURN_INPUT_REQUIRED,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
            )
        else:
            turn_error = f"result subtype={subtype!r}"
            event = AgentEvent(
                event=AgentEventType.TURN_FAILED,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
                usage=usage,
                message=turn_error,
            )

    else:
        # Unknown message type — pass through as OTHER_MESSAGE
        event = AgentEvent(
            event=AgentEventType.OTHER_MESSAGE,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            raw=msg,
        )

    return event, new_session_id, turn_succeeded, turn_error


def _summarize_content(content: list) -> str:
    """Extract a short text summary from assistant message content."""
    parts = []
    for block in content[:3]:  # limit to first 3 blocks
        if isinstance(block, dict):
            if block.get("type") == "text":
                text = str(block.get("text", ""))[:200]
                parts.append(text)
            elif block.get("type") == "tool_use":
                parts.append(f"[tool: {block.get('name', '?')}]")
    return " ".join(parts)[:300]


def _extract_usage(msg: dict) -> dict[str, int] | None:
    """Extract token usage from a result message."""
    usage = msg.get("usage")
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        }
    return None
