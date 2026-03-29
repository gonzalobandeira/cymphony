"""Codex CLI runner implementation.

Implements the BaseAgentRunner contract for OpenAI Codex CLI.
Currently reuses the Claude stream-json parser as a stub — this will
diverge once the Codex output format is finalized.
"""

from __future__ import annotations

from ..models import (
    AgentEvent,
    CodingAgentConfig,
)
from .base import BaseAgentRunner
from .claude import parse_claude_stream_event


class CodexAgentRunner(BaseAgentRunner):
    """Runs OpenAI Codex CLI as a subprocess and streams AgentEvents."""

    def _build_command(
        self,
        prompt: str,
        workspace_path: str,
        session_id: str | None,
        title: str,
    ) -> list[str]:
        """Build the codex CLI command list."""
        cmd = [self._config.command, "--output-format", "stream-json", "--verbose", "--print", prompt]

        if session_id:
            cmd.extend(["--resume", session_id])

        if self._config.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        return cmd

    def _parse_event(
        self,
        line: str,
        current_session_id: str | None,
        issue_id: str,
        issue_identifier: str,
        pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        """Parse one stdout line from Codex CLI."""
        return parse_claude_stream_event(
            line, current_session_id, issue_id, issue_identifier, pid
        )
