"""Optional HTTP server for observability and control (spec §14, §16)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


def _json_response(data: object, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(data, default=str),
    )


def _html_response(html: str) -> web.Response:
    return web.Response(content_type="text/html", text=html)


def build_app(orchestrator: "Orchestrator") -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application()
    app["orchestrator"] = orchestrator
    app.router.add_get("/", _handle_dashboard)
    app.router.add_get("/api/v1/state", _handle_state)
    app.router.add_post("/api/v1/refresh", _handle_refresh)
    app.router.add_get("/api/v1/{identifier}", _handle_issue)
    return app


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_state(request: web.Request) -> web.Response:
    """GET /api/v1/state — full orchestrator snapshot."""
    orch: Orchestrator = request.app["orchestrator"]
    snap = orch.snapshot()
    return _json_response(snap)


async def _handle_refresh(request: web.Request) -> web.Response:
    """POST /api/v1/refresh — trigger immediate poll."""
    orch: Orchestrator = request.app["orchestrator"]
    orch.request_immediate_poll()
    return _json_response({"ok": True, "message": "poll scheduled"}, status=202)


async def _handle_issue(request: web.Request) -> web.Response:
    """GET /api/v1/<identifier> — per-issue debug details."""
    identifier = request.match_info["identifier"].upper()
    orch: Orchestrator = request.app["orchestrator"]
    snap = orch.snapshot()

    # Look for issue in running or retrying lists (snapshot key: issue_identifier)
    issue_data: dict | None = None

    for entry in snap.get("running", []):
        if entry.get("issue_identifier") == identifier:
            issue_data = {"status": "running", **entry}
            break

    if issue_data is None:
        for entry in snap.get("retrying", []):
            if entry.get("issue_identifier") == identifier:
                issue_data = {"status": "retry_scheduled", **entry}
                break

    if issue_data is None:
        return _json_response({"error": "not_found", "identifier": identifier}, status=404)

    return _json_response(issue_data)


async def _handle_dashboard(request: web.Request) -> web.Response:
    """GET / — human-readable HTML dashboard."""
    orch: Orchestrator = request.app["orchestrator"]
    snap = orch.snapshot()

    running = snap.get("running", [])
    retrying = snap.get("retrying", [])
    totals = snap.get("codex_totals", {})

    running_rows = ""
    for r in running:
        session_id = r.get("session_id") or "—"
        running_rows += (
            f"<tr><td>{r.get('issue_identifier','')}</td>"
            f"<td>{r.get('state','')}</td>"
            f"<td>{r.get('turn_count','')}</td>"
            f"<td>{r.get('started_at','')}</td>"
            f"<td style='font-size:0.8em'>{session_id}</td></tr>\n"
        )

    retry_rows = ""
    for r in retrying:
        retry_rows += (
            f"<tr><td>{r.get('issue_identifier','')}</td>"
            f"<td>{r.get('attempt','')}</td>"
            f"<td>{r.get('due_in_seconds','')}s</td>"
            f"<td>{str(r.get('error',''))[:80]}</td></tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cymphony Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0f0f10; color: #e0e0e0; }}
  h1 {{ color: #a78bfa; }}
  h2 {{ color: #7dd3fc; border-bottom: 1px solid #333; padding-bottom: .3rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
  th {{ text-align: left; background: #1e1e2e; color: #94a3b8; padding: .5rem .75rem; }}
  td {{ padding: .4rem .75rem; border-bottom: 1px solid #1e1e2e; }}
  tr:hover td {{ background: #1a1a2e; }}
  .stat {{ display: inline-block; background: #1e1e2e; border-radius: 6px; padding: .5rem 1rem; margin: .25rem; }}
  .stat span {{ font-size: 1.5rem; font-weight: 700; color: #a78bfa; }}
  .empty {{ color: #555; font-style: italic; }}
</style>
</head>
<body>
<h1>Cymphony</h1>

<h2>Token Totals</h2>
<div>
  <div class="stat">Input tokens<br><span>{totals.get('input_tokens', 0):,}</span></div>
  <div class="stat">Output tokens<br><span>{totals.get('output_tokens', 0):,}</span></div>
  <div class="stat">Total tokens<br><span>{totals.get('total_tokens', 0):,}</span></div>
  <div class="stat">Running time<br><span>{totals.get('seconds_running', 0):.0f}s</span></div>
</div>

<h2>Running ({len(running)})</h2>
{"<p class='empty'>No active agents.</p>" if not running else f'''
<table>
<thead><tr><th>Issue</th><th>State</th><th>Turns</th><th>Started</th><th>Session ID</th></tr></thead>
<tbody>{running_rows}</tbody>
</table>'''}

<h2>Retry Queue ({len(retrying)})</h2>
{"<p class='empty'>No retries scheduled.</p>" if not retrying else f'''
<table>
<thead><tr><th>Issue</th><th>Attempt</th><th>Due In</th><th>Error</th></tr></thead>
<tbody>{retry_rows}</tbody>
</table>'''}

<p style="color:#555;font-size:.8rem">
  <a href="/api/v1/state" style="color:#7dd3fc">JSON state</a>
  &nbsp;·&nbsp; Auto-refresh: <a href="javascript:location.reload()" style="color:#7dd3fc">refresh</a>
</p>
</body>
</html>"""

    return _html_response(html)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def start_server(orchestrator: "Orchestrator", port: int) -> web.AppRunner:
    """Start the HTTP server and return the runner (caller must keep reference)."""
    app = build_app(orchestrator)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"action=http_server_started host=127.0.0.1 port={port}")
    return runner
