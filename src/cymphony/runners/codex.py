"""Codex CLI runner implementation.

Implements the BaseAgentRunner contract for OpenAI Codex CLI, handling:
  - Command construction (approval-mode, quiet, model selection)
  - Environment setup (OPENAI_API_KEY preservation)
  - Codex event parsing into the shared AgentEvent model
"""

from __future__ import annotations

import json
import logging
import os

from ..models import (
    AgentEvent,
    AgentEventType,
    CodingAgentConfig,
)
from .base import BaseAgentRunner, _now

logger = logging.getLogger(__name__)


class CodexAgentRunner(BaseAgentRunner):
    """Runs OpenAI Codex CLI as a subprocess and streams AgentEvents."""

    def _build_command(
        self,
        prompt: str,
        workspace_path: str,
        session_id: str | None,
        title: str,
    ) -> list[str]:
        """Build the codex CLI command list.

        Codex CLI uses --approval-mode full-auto for autonomous operation
        (equivalent to Claude's --dangerously-skip-permissions) and --quiet
        to suppress interactive prompts.
        """
        cmd = [self._config.command]

        if self._config.dangerously_skip_permissions:
            cmd.extend(["--approval-mode", "full-auto"])

        cmd.extend(["--quiet", prompt])

        if session_id:
            cmd.extend(["--resume", session_id])

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the Codex subprocess environment.

        Preserves OPENAI_API_KEY and other auth vars. Does not strip
        CLAUDECODE sentinel — Codex is not affected by it.
        """
        return dict(os.environ)

    def _parse_event(
        self,
        line: str,
        current_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        """Parse one stdout line from Codex CLI."""
        return parse_codex_stream_event(
            line, current_session_id, issue_id, issue_identifier, pid
        )


# ---------------------------------------------------------------------------
# Codex event parser
# ---------------------------------------------------------------------------

def parse_codex_stream_event(
    line: str,
    current_session_id: str | None,
    issue_id: str,
    issue_identifier: str,
    pid: int,
) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
    """Parse one JSON line from Codex CLI output.

    Codex CLI emits newline-delimited JSON events with a top-level "type" field.
    Maps Codex event types to the shared AgentEvent model:

      Codex type             → AgentEventType
      ──────────────────────   ─────────────────────
      session.created        → SESSION_STARTED
      message                → NOTIFICATION
      completed / success    → TURN_COMPLETED
      error                  → TURN_FAILED
      input_required         → TURN_INPUT_REQUIRED

    Returns (event, new_session_id, turn_succeeded, turn_error).
    """
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        logger.debug(
            f"action=codex_malformed_line "
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

    if msg_type == "session.created":
        # Session start — extract session_id
        session_data = msg.get("session", {})
        new_session_id = (
            session_data.get("id")
            or msg.get("session_id")
            or current_session_id
        )
        event = AgentEvent(
            event=AgentEventType.SESSION_STARTED,
            timestamp=_now(),
            session_id=new_session_id,
            pid=pid,
        )

    elif msg_type == "message":
        # Agent message — extract text summary for observability
        content = msg.get("content", "")
        role = msg.get("role", "")
        if isinstance(content, list):
            summary = _summarize_codex_content(content)
        elif isinstance(content, str):
            summary = content[:300]
        else:
            summary = str(content)[:300]

        # Only emit notifications for assistant messages
        if role != "system":
            event = AgentEvent(
                event=AgentEventType.NOTIFICATION,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
                message=summary,
                raw=msg,
            )

    elif msg_type == "function_call":
        # Tool/function call — emit as notification
        name = msg.get("name", "?")
        event = AgentEvent(
            event=AgentEventType.NOTIFICATION,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            message=f"[tool: {name}]",
            raw=msg,
        )

    elif msg_type in ("completed", "success"):
        # Turn completed successfully
        turn_succeeded = True
        usage = _extract_codex_usage(msg)
        event = AgentEvent(
            event=AgentEventType.TURN_COMPLETED,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            usage=usage,
        )

    elif msg_type == "error":
        # Turn failed
        error_msg = msg.get("message", "") or msg.get("error", "")
        turn_error = f"codex error: {error_msg}" if error_msg else "codex error"
        event = AgentEvent(
            event=AgentEventType.TURN_FAILED,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            message=turn_error,
        )

    elif msg_type == "input_required":
        # Agent requested user input — will trigger hard fail in base runner
        event = AgentEvent(
            event=AgentEventType.TURN_INPUT_REQUIRED,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
        )

    else:
        # Unknown event type — pass through
        event = AgentEvent(
            event=AgentEventType.OTHER_MESSAGE,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            raw=msg,
        )

    return event, new_session_id, turn_succeeded, turn_error


def _summarize_codex_content(content: list) -> str:
    """Extract a short text summary from Codex message content blocks."""
    parts = []
    for block in content[:3]:
        if isinstance(block, dict):
            if block.get("type") == "text":
                text = str(block.get("text", ""))[:200]
                parts.append(text)
            elif block.get("type") == "function_call":
                parts.append(f"[tool: {block.get('name', '?')}]")
        elif isinstance(block, str):
            parts.append(block[:200])
    return " ".join(parts)[:300]


def _extract_codex_usage(msg: dict) -> dict[str, int] | None:
    """Extract token usage from a Codex completion event."""
    usage = msg.get("usage")
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        }
    return None
