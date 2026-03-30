"""Provider-agnostic agent runner base class (spec §10).

Trust posture (documented per spec §15.1):
  High-trust mode. The agent CLI is invoked with --dangerously-skip-permissions so that
  all tool calls are auto-approved. This is intended for trusted operator environments only.
  Operators running in untrusted or multi-tenant environments must review and tighten this posture.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from ..models import (
    AgentError,
    AgentEvent,
    AgentEventType,
    RunnerConfig,
)

logger = logging.getLogger(__name__)

# Max stdout line size for safe buffering (spec §10.1)
_MAX_LINE_BYTES = 10 * 1024 * 1024  # 10 MB

OnAgentEvent = Callable[[AgentEvent], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BaseAgentRunner(abc.ABC):
    """Provider-agnostic subprocess runner for coding agents.

    Subclasses must implement:
      - _build_command: construct the CLI invocation
      - _parse_event: parse one stdout line into an AgentEvent
    Optionally override:
      - _build_env: customize subprocess environment
    """

    def __init__(self, config: RunnerConfig) -> None:
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
        except AgentError as exc:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
            # Stall errors are already logged and evented inside _stream_turn;
            # other AgentErrors (e.g. turn_input_required) just propagate.
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

        Enforces ``stall_timeout_ms``: if the subprocess produces no stdout
        data for longer than the configured threshold the process is killed
        and an ``AgentError("stall_timeout", ...)`` is raised so the caller
        can distinguish a stalled subprocess from a full turn timeout.
        """
        session_id = initial_session_id
        turn_succeeded = False
        turn_error: str | None = None
        stderr_lines: list[str] = []
        stall_timeout_secs = self._config.stall_timeout_ms / 1000.0

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
        try:
            while True:
                try:
                    raw_chunk = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=stall_timeout_secs if stall_timeout_secs > 0 else None,
                    )
                except asyncio.TimeoutError:
                    # Subprocess stalled — no output within stall_timeout_ms
                    logger.warning(
                        f"action=agent_stall_detected "
                        f"issue_id={issue_id} issue_identifier={issue_identifier} "
                        f"pid={pid} stall_timeout_ms={self._config.stall_timeout_ms}"
                    )
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
                    await on_event(AgentEvent(
                        event=AgentEventType.TURN_FAILED,
                        timestamp=_now(),
                        session_id=session_id,
                        pid=pid,
                        message="stall_timeout",
                    ))
                    raise AgentError(
                        "stall_timeout",
                        f"Agent subprocess stalled (no output for {self._config.stall_timeout_ms}ms)",
                    )

                if not raw_chunk:
                    # EOF — process closed stdout
                    break

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
        finally:
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
