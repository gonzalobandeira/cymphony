"""Optional HTTP server for observability and control (spec §14, §16)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

from aiohttp import web

from .linear import LinearClient
from .models import Issue

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_retry_timing(retry_row: dict) -> str:
    due_at_raw = retry_row.get("due_at")
    if not due_at_raw:
        return ""

    try:
        due_at = datetime.fromisoformat(str(due_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return str(due_at_raw)

    remaining_seconds = (due_at - _now_utc()).total_seconds()
    if remaining_seconds <= 0:
        return "due now"

    return f"in {math.ceil(remaining_seconds)}s"


def _format_waiting_timing(waiting_row: dict) -> str:
    due_at_raw = waiting_row.get("due_at")
    if not due_at_raw:
        return ""
    return _format_retry_timing(waiting_row)


def _json_response(data: object, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="application/json",
        text=json.dumps(data, default=str),
    )


def _html_response(html: str) -> web.Response:
    return web.Response(content_type="text/html", text=html)


def _find_issue_snapshot(snapshot: dict, identifier: str) -> dict | None:
    for entry in snapshot.get("running", []):
        if entry.get("issue_identifier") == identifier:
            return {
                "tracked": True,
                "status": "running",
                **entry,
            }

    for entry in snapshot.get("retrying", []):
        if entry.get("issue_identifier") == identifier:
            return {
                "tracked": True,
                "status": "retry_scheduled",
                "last_error": entry.get("error"),
                **entry,
            }

    return None


def _render_key_value(label: str, value: str | None) -> str:
    if not value:
        return ""
    return (
        f"<div class='kv'>"
        f"<span class='k'>{escape(label)}</span>"
        f"<span class='v'>{escape(value)}</span>"
        f"</div>"
    )


def _render_issue_comments(comments: list[dict]) -> str:
    if not comments:
        return "<p class='empty small'>No issue comments captured.</p>"

    items = []
    for comment in comments:
        author = escape(str(comment.get("author") or "Unknown"))
        created_at = escape(str(comment.get("created_at") or ""))
        body = escape(str(comment.get("body") or ""))
        items.append(
            f"<li><strong>{author}</strong>"
            f"{f' <span class=\"muted\">{created_at}</span>' if created_at else ''}"
            f"<pre>{body}</pre></li>"
        )
    return f"<ul class='event-list'>{''.join(items)}</ul>"


def _render_recent_events(events: list[dict]) -> str:
    if not events:
        return "<p class='empty small'>No recent runtime events yet.</p>"

    items = []
    for event in reversed(events):
        label = escape(str(event.get("event") or "unknown"))
        timestamp = escape(str(event.get("timestamp") or ""))
        message = escape(str(event.get("message") or ""))
        usage = event.get("usage") or {}
        usage_text = ""
        if usage:
            usage_text = (
                f"tokens in/out: {usage.get('input_tokens', 0)}/"
                f"{usage.get('output_tokens', 0)}"
            )
        details = " · ".join(part for part in [timestamp, message, usage_text] if part)
        items.append(
            f"<li><strong>{label}</strong>"
            f"{f'<div class=\"muted\">{details}</div>' if details else ''}"
            f"</li>"
        )
    return f"<ul class='event-list'>{''.join(items)}</ul>"


def _render_issue_drilldown(entry: dict, retry_due: str | None = None) -> str:
    title = entry.get("issue_title") or entry.get("issue_identifier") or "Issue"
    issue_url = entry.get("issue_url")
    labels = entry.get("issue_labels") or []
    tokens = entry.get("tokens") or {}
    latest_plan = entry.get("latest_plan")

    summary_bits = [
        f"status: {entry.get('run_status') or entry.get('status') or 'unknown'}",
        f"last event: {entry.get('last_event') or 'n/a'}",
    ]
    if retry_due:
        summary_bits.append(retry_due)

    issue_link = (
        f"<a href='{escape(str(issue_url))}' target='_blank' rel='noreferrer'>{escape(str(title))}</a>"
        if issue_url
        else escape(str(title))
    )
    labels_html = (
        "".join(f"<span class='tag'>{escape(str(label))}</span>" for label in labels)
        if labels
        else "<span class='empty small'>No labels</span>"
    )

    sections = [
        _render_key_value("Issue", entry.get("issue_identifier")),
        _render_key_value("State", entry.get("state")),
        _render_key_value("Run status", entry.get("run_status") or entry.get("status")),
        _render_key_value("Session", entry.get("session_id")),
        _render_key_value("Workspace", entry.get("workspace_path")),
        _render_key_value("Started", entry.get("started_at")),
        _render_key_value("Last event at", entry.get("last_event_at")),
        _render_key_value("Retry attempt", str(entry.get("retry_attempt")) if entry.get("retry_attempt") is not None else None),
        _render_key_value("Queued retry", retry_due),
        _render_key_value("Plan comment", entry.get("plan_comment_id")),
        _render_key_value("Last error", entry.get("error") or entry.get("last_error")),
    ]

    return f"""
<details class="issue-drilldown">
  <summary>{escape(str(entry.get("issue_identifier") or ""))} <span class="muted">{escape(" · ".join(summary_bits))}</span></summary>
  <div class="drill-grid">
    <section class="detail-card detail-wide">
      <h3>{issue_link}</h3>
      <div class="tag-row">{labels_html}</div>
      <pre>{escape(str(entry.get("issue_description") or "No issue description available."))}</pre>
    </section>
    <section class="detail-card">
      <h3>Runtime</h3>
      {''.join(sections)}
      <div class='kv'><span class='k'>Turns</span><span class='v'>{int(entry.get("turn_count") or 0)}</span></div>
      <div class='kv'><span class='k'>Tokens</span><span class='v'>{int(tokens.get("input_tokens", 0))}/{int(tokens.get("output_tokens", 0))}/{int(tokens.get("total_tokens", 0))} in/out/total</span></div>
      <div class='kv'><span class='k'>Last message</span><span class='v'>{escape(str(entry.get("last_message") or "")) or "—"}</span></div>
    </section>
    <section class="detail-card">
      <h3>Latest Plan</h3>
      <pre>{escape(str(latest_plan or "No TodoWrite plan captured yet."))}</pre>
    </section>
    <section class="detail-card">
      <h3>Recent Events</h3>
      {_render_recent_events(entry.get("recent_events") or [])}
    </section>
    <section class="detail-card detail-wide">
      <h3>Issue Comments</h3>
      {_render_issue_comments(entry.get("issue_comments") or [])}
    </section>
  </div>
</details>"""


def _format_timestamp(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_relative_due(raw: str | None, now: datetime) -> str:
    if not raw:
        return "Pending"
    try:
        due_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw

    delta_seconds = int((due_at - now).total_seconds())
    if delta_seconds <= 0:
        return "Now"

    minutes, seconds = divmod(delta_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _format_elapsed_seconds(value: float | int | None) -> str:
    if value is None:
        return "0s"
    total_seconds = max(int(value), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _summarize_blockers(issue: Issue, terminal_states: set[str]) -> str:
    unresolved = [
        blocker.identifier or blocker.id or "Unknown"
        for blocker in issue.blocked_by
        if (blocker.state or "").lower() not in terminal_states
    ]
    if not unresolved:
        return "Ready"
    if len(unresolved) == 1:
        return f"Waiting on {unresolved[0]}"
    return f"Waiting on {', '.join(unresolved[:2])} +{len(unresolved) - 2}"


def _build_operator_groups(
    snapshot: dict,
    active_issues: list[Issue],
    completed_issues: list[Issue],
    *,
    max_concurrent_agents: int,
    max_concurrent_agents_by_state: dict[str, int],
    active_states: list[str],
    terminal_states: list[str],
) -> dict[str, object]:
    running_rows = list(snapshot.get("running", []))
    retry_rows = list(snapshot.get("retrying", []))
    running_ids = {row.get("issue_id") for row in running_rows}
    retry_ids = {row.get("issue_id") for row in retry_rows}
    active_lower = {state.lower() for state in active_states}
    terminal_lower = {state.lower() for state in terminal_states}

    remaining_global_slots = max(max_concurrent_agents - len(running_rows), 0)
    running_by_state: dict[str, int] = {}
    for row in running_rows:
        state_name = str(row.get("state") or "").lower()
        running_by_state[state_name] = running_by_state.get(state_name, 0) + 1

    ready: list[dict[str, str | int | None]] = []
    waiting: list[dict[str, str | int | None]] = []
    blocked: list[dict[str, str | int | None]] = []

    def sort_key(issue: Issue) -> tuple[int, float, str]:
        priority = issue.priority if issue.priority is not None else 9999
        created = issue.created_at.timestamp() if issue.created_at else float("inf")
        return (priority, created, issue.identifier)

    for issue in sorted(active_issues, key=sort_key):
        if issue.id in running_ids or issue.id in retry_ids:
            continue
        if issue.state.lower() not in active_lower:
            continue

        unresolved_blockers = [
            blocker for blocker in issue.blocked_by
            if (blocker.state or "").lower() not in terminal_lower
        ]
        issue_view = {
            "identifier": issue.identifier,
            "title": issue.title,
            "state": issue.state,
            "priority": issue.priority,
            "url": issue.url,
            "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        }

        if unresolved_blockers:
            blocked.append({
                **issue_view,
                "reason": _summarize_blockers(issue, terminal_lower),
            })
            continue

        state_limit = max_concurrent_agents_by_state.get(issue.state.lower())
        state_used = running_by_state.get(issue.state.lower(), 0)
        has_state_slot = state_limit is None or state_used < state_limit

        if remaining_global_slots > 0 and has_state_slot:
            ready.append({
                **issue_view,
                "reason": "Dispatchable now",
            })
            remaining_global_slots -= 1
            running_by_state[issue.state.lower()] = state_used + 1
            continue

        reason = "Waiting for global capacity"
        if not has_state_slot and state_limit is not None:
            reason = f"Waiting for {issue.state} capacity"
        waiting.append({
            **issue_view,
            "reason": reason,
        })

    recently_completed = sorted(
        completed_issues,
        key=lambda issue: issue.updated_at.timestamp() if issue.updated_at else float("-inf"),
        reverse=True,
    )[:8]

    intervention_count = len(blocked) + sum(
        1 for retry in retry_rows if retry.get("error")
    )

    return {
        "generated_at": snapshot.get("generated_at"),
        "running": running_rows,
        "retrying": retry_rows,
        "ready": ready,
        "waiting": waiting,
        "blocked": blocked,
        "recently_completed": [
            {
                "identifier": issue.identifier,
                "title": issue.title,
                "state": issue.state,
                "url": issue.url,
                "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
            }
            for issue in recently_completed
        ],
        "totals": dict(snapshot.get("codex_totals", {})),
        "summary": {
            "running": len(running_rows),
            "retrying": len(retry_rows),
            "blocked": len(blocked),
            "ready": len(ready),
            "waiting": len(waiting),
            "recently_completed": len(recently_completed),
            "needs_attention": intervention_count,
            "capacity_in_use": f"{len(running_rows)}/{max_concurrent_agents}",
        },
    }


async def _load_operator_groups(
    orchestrator: "Orchestrator",
    snapshot: dict,
) -> dict[str, object]:
    client = LinearClient(orchestrator._config.tracker)

    active_issues: list[Issue] = []
    completed_issues: list[Issue] = []
    try:
        active_issues, completed_issues = await asyncio.gather(
            client.fetch_candidate_issues(),
            client.fetch_issues_by_states(orchestrator._config.tracker.terminal_states),
        )
    except Exception as exc:
        logger.warning(f"action=dashboard_issue_load_failed error={exc}")

    return _build_operator_groups(
        snapshot,
        active_issues,
        completed_issues,
        max_concurrent_agents=orchestrator._config.agent.max_concurrent_agents,
        max_concurrent_agents_by_state=orchestrator._config.agent.max_concurrent_agents_by_state,
        active_states=orchestrator._config.tracker.active_states,
        terminal_states=orchestrator._config.tracker.terminal_states,
    )


def _render_priority(priority: int | None) -> str:
    if priority is None:
        return "P?"
    return f"P{priority}"


def _render_issue_link(identifier: str, title: str, url: str | None) -> str:
    label = f"{escape(identifier)} - {escape(title)}"
    if not url:
        return label
    return f"<a href='{escape(url)}'>{label}</a>"


def _render_table(title: str, subtitle: str, headers: list[str], rows: list[list[str]], empty: str) -> str:
    table_body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    body = (
        f"<p class='empty'>{escape(empty)}</p>"
        if not rows else
        "<table><thead><tr>"
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + table_body
        + "</tbody></table>"
    )
    return (
        "<section class='panel'>"
        f"<div class='panel-head'><h2>{escape(title)}</h2><p>{escape(subtitle)}</p></div>"
        f"{body}</section>"
    )


def _render_dashboard(groups: dict[str, object]) -> str:
    summary = groups["summary"]
    totals = groups["totals"]
    generated_at = _format_timestamp(groups.get("generated_at"))
    now = datetime.now(timezone.utc)
    controls = groups.get("controls", {})
    dispatch_paused = bool(controls.get("dispatch_paused"))
    recent_controls = list(controls.get("recent_actions", []))

    running_rows = [
        [
            _render_issue_drilldown(row),
            escape(str(row.get("state") or "")),
            escape(str(row.get("run_status") or "")),
            escape(str(row.get("turn_count") or 0)),
            escape(_format_timestamp(row.get("started_at"))),
            escape(str(row.get("session_id") or "-")),
            _issue_controls(str(row.get("issue_identifier") or ""), include_cancel=True),
        ]
        for row in groups["running"]
    ]

    retry_rows = [
        [
            _render_issue_drilldown(row, retry_due=_format_relative_due(row.get("due_at"), now)),
            escape(str(row.get("attempt") or "")),
            escape(_format_relative_due(row.get("due_at"), now)),
            escape(str(row.get("error") or "Continuation retry")),
            _issue_controls(str(row.get("issue_identifier") or "")),
        ]
        for row in groups["retrying"]
    ]
    skipped_rows = [
        [
            escape(str(row.get("issue_identifier") or "")),
            escape(str(row.get("reason") or "")),
            escape(_format_timestamp(row.get("created_at"))),
            _issue_controls(str(row.get("issue_identifier") or ""), requeue_only=True),
        ]
        for row in groups.get("skipped", [])
    ]
    waiting_reason_rows = [
        [
            escape(str(row.get("issue_identifier") or "")),
            escape(str(row.get("summary") or "")),
            escape(str(row.get("detail") or "")),
            escape(_format_waiting_timing(row)),
        ]
        for row in groups.get("waiting_reasons", [])
    ]
    problem_rows = [
        [
            escape(str(row.get("issue_identifier") or "-")),
            escape(str(row.get("summary") or "")),
            escape(str(row.get("detail") or "")),
            escape(_format_timestamp(row.get("observed_at"))),
        ]
        for row in groups.get("recent_problems", [])
    ]
    recent_control_rows = [
        [
            escape(_format_timestamp(row.get("timestamp"))),
            escape(str(row.get("action") or "")),
            escape(str(row.get("scope") or "")),
            escape(str(row.get("outcome") or "")),
            escape(str(row.get("issue_identifier") or "-")),
            escape(str(row.get("detail") or "")),
        ]
        for row in recent_controls[:10]
    ]

    queue_sections = []
    for key, title, subtitle, empty in [
        ("ready", "Ready To Dispatch", "Work that can start as soon as capacity is available.", "No immediately dispatchable issues."),
        ("waiting", "Waiting", "Eligible work that is queued behind current capacity limits.", "No queued work is waiting for slots."),
        ("blocked", "Blocked", "Issues still gated by unresolved dependencies or tracker state.", "No active blockers."),
        ("recently_completed", "Recently Completed", "Recent terminal-state work for quick operator confirmation.", "No recent completions found."),
    ]:
        rows = []
        for item in groups[key]:
            rows.append([
                _render_issue_link(item["identifier"], item["title"], item.get("url")),
                escape(str(item.get("state") or "")),
                escape(_render_priority(item.get("priority")) if "priority" in item else "-"),
                escape(item.get("reason") or _format_timestamp(item.get("updated_at"))),
            ])
        fourth_header = "Reason" if key != "recently_completed" else "Updated"
        queue_sections.append(
            _render_table(
                title,
                subtitle,
                ["Issue", "State", "Priority", fourth_header],
                rows,
                empty,
            )
        )
    queue_sections.append(
        _render_table(
            f"Waiting Reasons ({len(waiting_reason_rows)})",
            "Snapshot-level explanations for issues that are not dispatching yet.",
            ["Issue", "Reason", "Detail", "Timing"],
            waiting_reason_rows,
            "No waiting reasons captured in the latest snapshot.",
        )
    )
    queue_sections.append(
        _render_table(
            f"Recent Problems ({len(problem_rows)})",
            "Recent operator-visible orchestration issues for quick follow-up.",
            ["Issue", "Problem", "Detail", "Observed"],
            problem_rows,
            "No recent orchestration problems captured.",
        )
    )
    queue_sections.append(
        _render_table(
            f"Skipped ({len(skipped_rows)})",
            "Issues manually skipped by an operator until they are requeued.",
            ["Issue", "Reason", "Skipped", "Actions"],
            skipped_rows,
            "No issues are currently skipped.",
        )
    )
    queue_sections.append(
        _render_table(
            f"Recent Controls ({len(recent_control_rows)})",
            "Recent operator actions and their outcomes.",
            ["At", "Action", "Scope", "Outcome", "Issue", "Detail"],
            recent_control_rows,
            "No operator actions recorded yet.",
        )
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Cymphony Operator Dashboard</title>
<style>
  :root {{
    --bg: #f3efe6;
    --bg-accent: radial-gradient(circle at top right, rgba(22, 163, 74, 0.12), transparent 28%), radial-gradient(circle at left top, rgba(14, 116, 144, 0.14), transparent 24%), #f3efe6;
    --panel: rgba(255, 252, 246, 0.92);
    --panel-strong: #fffaf0;
    --ink: #1f2933;
    --muted: #5f6c72;
    --line: rgba(31, 41, 51, 0.12);
    --good: #166534;
    --warn: #b45309;
    --danger: #b91c1c;
    --accent: #0f766e;
    --shadow: 0 18px 40px rgba(31, 41, 51, 0.08);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
    color: var(--ink);
    background: var(--bg-accent);
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  form {{ display: inline; margin: 0; }}
  main {{ max-width: 1440px; margin: 0 auto; padding: 32px; }}
  .hero {{
    display: grid;
    grid-template-columns: 2.2fr 1fr;
    gap: 18px;
    margin-bottom: 20px;
  }}
  .hero-card, .meta-card, .panel, .stat {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 20px;
    box-shadow: var(--shadow);
  }}
  .hero-card {{
    padding: 28px;
    background: linear-gradient(135deg, rgba(15, 118, 110, 0.08), rgba(255, 250, 240, 0.96));
  }}
  .hero-card h1 {{
    margin: 0 0 10px;
    font-size: 2.6rem;
    line-height: 1;
    letter-spacing: -0.04em;
  }}
  .hero-card p, .meta-card p, .panel-head p {{
    margin: 0;
    color: var(--muted);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .meta-card {{
    padding: 22px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    justify-content: center;
  }}
  .meta-label {{
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .meta-value {{
    font-size: 1.1rem;
    font-weight: 700;
  }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 14px;
    margin-bottom: 20px;
  }}
  .stat {{
    padding: 18px;
    background: var(--panel-strong);
  }}
  .stat strong {{
    display: block;
    font-size: 2rem;
    line-height: 1;
    margin-bottom: 8px;
  }}
  .stat span {{
    color: var(--muted);
    font-size: 0.92rem;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .stat.attention strong {{ color: var(--danger); }}
  .stat.ready strong {{ color: var(--good); }}
  .stat.waiting strong {{ color: var(--warn); }}
  .stat.running strong {{ color: var(--accent); }}
  .layout {{
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 18px;
  }}
  .stack {{
    display: grid;
    gap: 18px;
  }}
  .panel {{
    padding: 20px 22px;
    overflow: hidden;
  }}
  .control-toolbar, .issue-actions {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .issue-actions {{
    min-width: 180px;
  }}
  .small {{ font-size: 0.9rem; }}
  .muted {{ color: var(--muted); }}
  .issue-drilldown summary {{
    cursor: pointer;
    font-weight: 700;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .issue-drilldown[open] summary {{
    margin-bottom: 12px;
  }}
  .drill-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
    margin-top: 12px;
  }}
  .detail-card {{
    background: var(--panel-strong);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 14px;
  }}
  .detail-wide {{
    grid-column: span 2;
  }}
  .detail-card h3 {{
    margin: 0 0 8px;
    font-size: 0.95rem;
    color: var(--accent);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .kv {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 4px 0;
    border-bottom: 1px solid var(--line);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.9rem;
  }}
  .k {{
    color: var(--muted);
  }}
  .v {{
    text-align: right;
    word-break: break-word;
  }}
  .tag-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }}
  .tag {{
    display: inline-block;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(15, 118, 110, 0.1);
    color: var(--accent);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.78rem;
  }}
  .event-list {{
    margin: 0;
    padding-left: 18px;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  pre {{
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    padding: 10px;
    border-radius: 12px;
    background: rgba(31, 41, 51, 0.04);
    font-family: "SFMono-Regular", "Menlo", monospace;
    font-size: 0.84rem;
  }}
  button {{
    border: 1px solid var(--line);
    background: var(--panel-strong);
    color: var(--ink);
    border-radius: 999px;
    padding: 8px 12px;
    font: inherit;
    cursor: pointer;
  }}
  button:hover {{
    border-color: rgba(15, 118, 110, 0.35);
    background: #f7f2e7;
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 8px 12px;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.9rem;
    font-weight: 700;
  }}
  .pill.active {{
    background: rgba(22, 101, 52, 0.12);
    color: var(--good);
  }}
  .pill.paused {{
    background: rgba(185, 28, 28, 0.12);
    color: var(--danger);
  }}
  .panel-head {{
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: 12px;
    margin-bottom: 16px;
  }}
  .panel-head h2 {{
    margin: 0;
    font-size: 1.15rem;
    letter-spacing: -0.02em;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.96rem;
  }}
  th, td {{
    padding: 11px 8px;
    text-align: left;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }}
  th {{
    color: var(--muted);
    font-size: 0.76rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  .empty {{
    margin: 0;
    color: var(--muted);
    font-style: italic;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .footer {{
    margin-top: 18px;
    color: var(--muted);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.9rem;
  }}
  @media (max-width: 1100px) {{
    .hero, .layout {{ grid-template-columns: 1fr; }}
    .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  }}
  @media (max-width: 700px) {{
    main {{ padding: 18px; }}
    .stats {{ grid-template-columns: 1fr; }}
    .hero-card h1 {{ font-size: 2rem; }}
    table {{ font-size: 0.9rem; }}
  }}
</style>
</head>
<body>
<main>
  <section class="hero">
    <div class="hero-card">
      <h1>Cymphony Operator Board</h1>
      <p>Scan the live system by operator intent: what is moving, what is ready next, what is blocked, and what needs intervention.</p>
    </div>
    <aside class="meta-card">
      <div>
        <div class="meta-label">Snapshot</div>
        <div class="meta-value">{escape(generated_at)}</div>
      </div>
      <div>
        <div class="meta-label">Capacity</div>
        <div class="meta-value">{escape(str(summary["capacity_in_use"]))}</div>
      </div>
      <div>
        <div class="meta-label">Runtime</div>
        <div class="meta-value">{escape(_format_elapsed_seconds(totals.get("seconds_running")))}</div>
      </div>
      <div>
        <div class="meta-label">Dispatch</div>
        <div class="meta-value">{'Paused' if dispatch_paused else 'Active'}</div>
      </div>
    </aside>
  </section>

  <section class="stats">
    <div class="stat running"><strong>{summary["running"]}</strong><span>Running now</span></div>
    <div class="stat"><strong>{summary["retrying"]}</strong><span>Retrying</span></div>
    <div class="stat ready"><strong>{summary["ready"]}</strong><span>Ready next</span></div>
    <div class="stat waiting"><strong>{summary["waiting"]}</strong><span>Waiting</span></div>
    <div class="stat attention"><strong>{summary["needs_attention"]}</strong><span>Needs attention</span></div>
    <div class="stat"><strong>{totals.get("total_tokens", 0):,}</strong><span>Total tokens</span></div>
  </section>

  <section class="layout">
    <div class="stack">
      <section class="panel">
        <div class="panel-head">
          <h2>Operator Controls</h2>
          <p>Intervene safely without leaving the dashboard.</p>
        </div>
        <div class="control-toolbar">
          <span class="pill {'paused' if dispatch_paused else 'active'}">{'Paused' if dispatch_paused else 'Active'}</span>
          {_post_button("/api/v1/refresh", "Refresh Now")}
          {_post_button("/api/v1/dispatch/pause", "Pause Dispatching")}
          {_post_button("/api/v1/dispatch/resume", "Resume Dispatching")}
        </div>
      </section>
      {_render_table("Running", "Active workers and current execution status.", ["Issue", "Tracker State", "Run Status", "Turns", "Started", "Session", "Actions"], running_rows, "No active agents.")}
      {_render_table("Retrying", "Retries scheduled after failures or continuation hand-offs.", ["Issue", "Attempt", "Due In", "Why", "Actions"], retry_rows, "No retries scheduled.")}
    </div>
    <div class="stack">
      {''.join(queue_sections)}
    </div>
  </section>

  <p class="footer">
    <a href="/api/v1/state">JSON state</a>
    · Auto-refresh every 15 seconds
    · Input tokens {totals.get("input_tokens", 0):,}
    · Output tokens {totals.get("output_tokens", 0):,}
  </p>
</main>
</body>
</html>"""


def build_app(orchestrator: "Orchestrator") -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application()
    app["orchestrator"] = orchestrator
    app.router.add_get("/", _handle_dashboard)
    app.router.add_get("/api/v1/state", _handle_state)
    app.router.add_post("/api/v1/refresh", _handle_refresh)
    app.router.add_post("/api/v1/dispatch/pause", _handle_pause_dispatch)
    app.router.add_post("/api/v1/dispatch/resume", _handle_resume_dispatch)
    app.router.add_post("/api/v1/issues/{identifier}/cancel", _handle_cancel_worker)
    app.router.add_post("/api/v1/issues/{identifier}/requeue", _handle_requeue_issue)
    app.router.add_post("/api/v1/issues/{identifier}/skip", _handle_skip_issue)
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
    result = orch.trigger_refresh()
    return _json_response(
        {
            **result,
            "queued": True,
            "requested_at": _now_utc().isoformat(),
            "operations": ["reconcile", "dispatch"],
        },
        status=202,
    )


async def _handle_pause_dispatch(request: web.Request) -> web.Response:
    """POST /api/v1/dispatch/pause — pause new dispatches."""
    orch: Orchestrator = request.app["orchestrator"]
    return _json_response(orch.pause_dispatching(), status=202)


async def _handle_resume_dispatch(request: web.Request) -> web.Response:
    """POST /api/v1/dispatch/resume — resume new dispatches."""
    orch: Orchestrator = request.app["orchestrator"]
    return _json_response(orch.resume_dispatching(), status=202)


async def _handle_cancel_worker(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/cancel — cancel a running worker."""
    orch: Orchestrator = request.app["orchestrator"]
    result = await orch.cancel_worker(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_requeue_issue(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/requeue — release issue for redispatch."""
    orch: Orchestrator = request.app["orchestrator"]
    result = await orch.requeue_issue(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_skip_issue(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/skip — mark issue as skipped."""
    orch: Orchestrator = request.app["orchestrator"]
    result = await orch.skip_issue(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_issue(request: web.Request) -> web.Response:
    """GET /api/v1/<identifier> — per-issue debug details."""
    identifier = request.match_info["identifier"].upper()
    orch: Orchestrator = request.app["orchestrator"]
    snap = orch.snapshot()
    issue_data = _find_issue_snapshot(snap, identifier)

    if issue_data is None:
        for entry in snap.get("waiting", []):
            if entry.get("issue_identifier") == identifier:
                issue_data = {
                    "tracked": True,
                    "status": entry.get("kind"),
                    **entry,
                }
                break

    if issue_data is None:
        return _json_response(
            {
                "error": {
                    "code": "issue_not_found",
                    "message": f"Issue {identifier} is not tracked",
                }
            },
            status=404,
        )

    return _json_response(issue_data)


async def _handle_dashboard(request: web.Request) -> web.Response:
    """GET / — human-readable HTML dashboard."""
    orch: Orchestrator = request.app["orchestrator"]
    snap = orch.snapshot()
    groups = await _load_operator_groups(orch, snap)
    groups["waiting_reasons"] = list(snap.get("waiting", []))
    groups["recent_problems"] = list(snap.get("problems", []))
    groups["skipped"] = list(snap.get("skipped", []))
    groups["controls"] = dict(snap.get("controls", {}))
    return _html_response(_render_dashboard(groups))


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


def _post_button(action: str, label: str) -> str:
    safe_action = html.escape(action, quote=True)
    safe_label = html.escape(label)
    return (
        f"<form method='post' action='{safe_action}'>"
        f"<button type='submit'>{safe_label}</button>"
        "</form>"
    )


def _issue_controls(
    identifier: str,
    *,
    include_cancel: bool = False,
    requeue_only: bool = False,
) -> str:
    safe_identifier = html.escape(identifier, quote=True)
    buttons = [_post_button(f"/api/v1/issues/{safe_identifier}/requeue", "Requeue")]
    if not requeue_only:
        buttons.append(_post_button(f"/api/v1/issues/{safe_identifier}/skip", "Skip"))
    if include_cancel:
        buttons.insert(0, _post_button(f"/api/v1/issues/{safe_identifier}/cancel", "Cancel Worker"))
    return f"<div class='issue-actions'>{''.join(buttons)}</div>"
