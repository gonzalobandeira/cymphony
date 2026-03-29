"""Tests for stall detection in the base agent runner.

Verifies that:
  - A subprocess producing no output triggers stall_timeout
  - A subprocess that stalls after partial output triggers stall_timeout
  - A subprocess producing steady output does NOT trigger stall
  - Stall errors are distinct from turn timeout errors
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from cymphony.models import AgentError, AgentEvent, AgentEventType, CodingAgentConfig
from cymphony.runners.base import BaseAgentRunner


def _make_config(
    stall_timeout_ms: int = 200,
    turn_timeout_ms: int = 5000,
) -> CodingAgentConfig:
    return CodingAgentConfig(
        command=sys.executable,
        turn_timeout_ms=turn_timeout_ms,
        read_timeout_ms=1000,
        stall_timeout_ms=stall_timeout_ms,
        dangerously_skip_permissions=False,
        provider="test",
    )


class _TestRunner(BaseAgentRunner):
    """Minimal concrete runner for testing the base class streaming logic."""

    def __init__(self, config: CodingAgentConfig, script: str) -> None:
        super().__init__(config)
        self._script = script

    def _build_command(
        self, prompt: str, workspace_path: str, session_id: str | None, title: str,
    ) -> list[str]:
        return [self._config.command, "-c", self._script]

    def _parse_event(
        self, line: str, current_session_id: str | None,
        issue_id: str, issue_identifier: str, pid: int,
    ) -> tuple[AgentEvent | None, str | None, bool | None, str | None]:
        line = line.strip()
        if line == "DONE":
            from cymphony.runners.base import _now
            return (
                AgentEvent(
                    event=AgentEventType.TURN_COMPLETED,
                    timestamp=_now(),
                    session_id=current_session_id,
                    pid=pid,
                ),
                None,
                True,
                None,
            )
        if line:
            from cymphony.runners.base import _now
            return (
                AgentEvent(
                    event=AgentEventType.NOTIFICATION,
                    timestamp=_now(),
                    session_id=current_session_id,
                    pid=pid,
                    message=line,
                ),
                None,
                None,
                None,
            )
        return None, None, None, None


async def _collect_events(runner: _TestRunner, tmp_path) -> list[AgentEvent]:
    events: list[AgentEvent] = []

    async def on_event(ev: AgentEvent) -> None:
        events.append(ev)

    await runner.run_turn(
        workspace_path=str(tmp_path),
        prompt="test",
        issue_id="iss-1",
        issue_identifier="TEST-1",
        session_id=None,
        title="test",
        on_event=on_event,
    )
    return events


# ---------------------------------------------------------------------------
# No-output hang: subprocess produces nothing and sleeps forever
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_no_output(tmp_path) -> None:
    """A subprocess that produces no output should be killed within stall_timeout."""
    script = textwrap.dedent("""\
        import time
        time.sleep(60)
    """)
    runner = _TestRunner(_make_config(stall_timeout_ms=200), script)
    events: list[AgentEvent] = []

    async def on_event(ev: AgentEvent) -> None:
        events.append(ev)

    with pytest.raises(AgentError) as exc_info:
        await runner.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=on_event,
        )

    assert exc_info.value.code == "stall_timeout"
    assert any(e.message == "stall_timeout" for e in events)


# ---------------------------------------------------------------------------
# Partial-output hang: subprocess emits lines then stalls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_after_partial_output(tmp_path) -> None:
    """A subprocess that emits some output then hangs should trigger stall."""
    script = textwrap.dedent("""\
        import sys, time
        sys.stdout.write("line1\\n")
        sys.stdout.flush()
        sys.stdout.write("line2\\n")
        sys.stdout.flush()
        time.sleep(60)
    """)
    runner = _TestRunner(_make_config(stall_timeout_ms=200), script)
    events: list[AgentEvent] = []

    async def on_event(ev: AgentEvent) -> None:
        events.append(ev)

    with pytest.raises(AgentError) as exc_info:
        await runner.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=on_event,
        )

    assert exc_info.value.code == "stall_timeout"
    # Should have received the partial output events before stalling
    notification_events = [e for e in events if e.event == AgentEventType.NOTIFICATION]
    assert len(notification_events) >= 2
    # And a stall failure event
    assert any(
        e.event == AgentEventType.TURN_FAILED and e.message == "stall_timeout"
        for e in events
    )


# ---------------------------------------------------------------------------
# Steady output: no false stall trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_stall_with_steady_output(tmp_path) -> None:
    """A subprocess producing output faster than stall_timeout should complete normally."""
    script = textwrap.dedent("""\
        import sys, time
        for i in range(5):
            sys.stdout.write(f"line{i}\\n")
            sys.stdout.flush()
            time.sleep(0.05)
        sys.stdout.write("DONE\\n")
        sys.stdout.flush()
    """)
    runner = _TestRunner(_make_config(stall_timeout_ms=500), script)
    events = await _collect_events(runner, tmp_path)

    notification_events = [e for e in events if e.event == AgentEventType.NOTIFICATION]
    assert len(notification_events) == 5
    completed_events = [e for e in events if e.event == AgentEventType.TURN_COMPLETED]
    assert len(completed_events) == 1


# ---------------------------------------------------------------------------
# Stall fires before turn timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_fires_before_turn_timeout(tmp_path) -> None:
    """Stall timeout should fire well before the turn timeout."""
    script = textwrap.dedent("""\
        import time
        time.sleep(60)
    """)
    runner = _TestRunner(
        _make_config(stall_timeout_ms=200, turn_timeout_ms=10000),
        script,
    )

    with pytest.raises(AgentError) as exc_info:
        await runner.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=lambda ev: asyncio.sleep(0),
        )

    # Must be stall, not turn timeout
    assert exc_info.value.code == "stall_timeout"
    assert "stall" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Stall error is distinct from turn timeout error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_error_distinct_from_turn_timeout(tmp_path) -> None:
    """AgentError codes for stall vs turn timeout are different."""
    # Stall case: short stall timeout, long turn timeout
    stall_script = textwrap.dedent("""\
        import time
        time.sleep(60)
    """)
    runner = _TestRunner(
        _make_config(stall_timeout_ms=200, turn_timeout_ms=10000),
        stall_script,
    )
    with pytest.raises(AgentError) as stall_exc:
        await runner.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=lambda ev: asyncio.sleep(0),
        )

    # Turn timeout case: disable stall, short turn timeout
    timeout_script = textwrap.dedent("""\
        import time
        time.sleep(60)
    """)
    runner2 = _TestRunner(
        _make_config(stall_timeout_ms=0, turn_timeout_ms=200),
        timeout_script,
    )
    with pytest.raises(AgentError) as timeout_exc:
        await runner2.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=lambda ev: asyncio.sleep(0),
        )

    assert stall_exc.value.code == "stall_timeout"
    assert timeout_exc.value.code == "turn_timeout"
    assert stall_exc.value.code != timeout_exc.value.code


# ---------------------------------------------------------------------------
# Disabled stall timeout (stall_timeout_ms=0) falls through to turn timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stall_disabled_falls_to_turn_timeout(tmp_path) -> None:
    """When stall_timeout_ms=0, only the turn timeout fires."""
    script = textwrap.dedent("""\
        import time
        time.sleep(60)
    """)
    runner = _TestRunner(
        _make_config(stall_timeout_ms=0, turn_timeout_ms=300),
        script,
    )

    with pytest.raises(AgentError) as exc_info:
        await runner.run_turn(
            workspace_path=str(tmp_path),
            prompt="test",
            issue_id="iss-1",
            issue_identifier="TEST-1",
            session_id=None,
            title="test",
            on_event=lambda ev: asyncio.sleep(0),
        )

    assert exc_info.value.code == "turn_timeout"
