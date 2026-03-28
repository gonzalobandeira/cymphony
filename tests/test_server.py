from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cymphony import server


class _FakeOrchestrator:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        return self._snapshot


class _FakeRequest:
    def __init__(self, orchestrator: _FakeOrchestrator) -> None:
        self.app = {"orchestrator": orchestrator}


@pytest.mark.asyncio
async def test_dashboard_retry_queue_renders_timing_from_due_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "_now_utc", lambda: now)
    request = _FakeRequest(
        _FakeOrchestrator(
            {
                "running": [],
                "retrying": [
                    {
                        "issue_identifier": "BAP-154",
                        "attempt": 2,
                        "due_at": (now + timedelta(seconds=90)).isoformat(),
                        "error": "network blip",
                    }
                ],
                "codex_totals": {},
            }
        )
    )

    response = await server._handle_dashboard(request)

    assert response.content_type == "text/html"
    assert "BAP-154" in response.text
    assert "in 90s" in response.text
    assert "network blip" in response.text
