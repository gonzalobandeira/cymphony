"""Claude Code runner implementation.

Implements the BaseAgentRunner contract for Claude CLI, handling:
  - Command construction (stream-json, resume, permissions)
  - Environment isolation (CLAUDECODE sentinel stripping, auth var preservation)
  - Stream-json event parsing (init, assistant, result subtypes)
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
        return parse_claude_stream_event(
            line, current_session_id, issue_id, issue_identifier, pid
        )


# ---------------------------------------------------------------------------
# Claude stream-json parser
# ---------------------------------------------------------------------------

def parse_claude_stream_event(
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
