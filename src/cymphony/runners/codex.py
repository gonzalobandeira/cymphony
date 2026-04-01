"""Codex CLI runner implementation.

Implements the BaseAgentRunner contract for OpenAI Codex CLI, handling:
  - Command construction for `codex exec` / `codex exec resume`
  - Environment setup (OPENAI_API_KEY preservation)
  - Codex JSONL event parsing into the shared AgentEvent model
"""

from __future__ import annotations

import json
import logging
import os

from ..models import (
    AgentEvent,
    AgentEventType,
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

        `codex exec --json` is the non-interactive JSONL interface.
        Continuation turns use the `exec resume` subcommand.
        """
        cmd = [self._config.command, "exec"]

        if session_id:
            cmd.extend(["resume", session_id])

        cmd.extend(["--json", prompt])

        if self._config.dangerously_skip_permissions:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")

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
    The non-interactive `codex exec --json` protocol currently includes:

      Codex type             → AgentEventType
      ──────────────────────   ─────────────────────
      thread.started         → SESSION_STARTED
      item.completed         → NOTIFICATION
      turn.completed         → TURN_COMPLETED
      turn.failed / error    → TURN_FAILED
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

    if msg_type == "thread.started":
        new_session_id = msg.get("thread_id") or current_session_id
        event = AgentEvent(
            event=AgentEventType.SESSION_STARTED,
            timestamp=_now(),
            session_id=new_session_id,
            pid=pid,
        )

    elif msg_type == "item.completed":
        item = msg.get("item", {})
        summary = _summarize_codex_item(item)
        if summary:
            event = AgentEvent(
                event=AgentEventType.NOTIFICATION,
                timestamp=_now(),
                session_id=current_session_id,
                pid=pid,
                message=summary,
                raw=msg,
            )

    elif msg_type == "turn.completed":
        turn_succeeded = True
        usage = _extract_codex_usage(msg)
        event = AgentEvent(
            event=AgentEventType.TURN_COMPLETED,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            usage=usage,
        )

    elif msg_type in ("turn.failed", "error"):
        error_msg = (
            msg.get("message", "")
            or msg.get("error", "")
            or msg.get("detail", "")
        )
        turn_error = f"codex error: {error_msg}" if error_msg else "codex error"
        event = AgentEvent(
            event=AgentEventType.TURN_FAILED,
            timestamp=_now(),
            session_id=current_session_id,
            pid=pid,
            message=turn_error,
        )

    elif msg_type == "input_required":
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


def _summarize_codex_item(item: dict) -> str | None:
    """Extract a short operator-facing summary from a completed Codex item."""
    item_type = item.get("type")
    if item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        if not text:
            return None
        return text[:3999].rstrip() + "…" if len(text) > 4000 else text
    if item_type == "command_execution":
        command = str(item.get("command") or "?")
        status = str(item.get("status") or "")
        exit_code = item.get("exit_code")
        suffix = f" exit={exit_code}" if exit_code is not None else ""
        summary = f"[command:{status}] {command}{suffix}".strip()
        return summary[:3999].rstrip() + "…" if len(summary) > 4000 else summary
    if item_type == "function_call":
        name = str(item.get("name") or "?")
        summary = f"[tool: {name}]"
        return summary
    return None


def _extract_codex_usage(msg: dict) -> dict[str, int] | None:
    """Extract token usage from a Codex completion event."""
    usage = msg.get("usage")
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_read_input_tokens": int(
                usage.get("cache_read_input_tokens", usage.get("cached_input_tokens", 0))
            ),
        }
    return None
