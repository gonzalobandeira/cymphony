"""Optional HTTP server for observability and control (spec §14, §16)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, TYPE_CHECKING

from aiohttp import web

from .config import build_config, validate_dispatch_config
from .linear import LinearClient
from .models import Issue, WorkflowDefinition
from .workflow import load_workflow, save_workflow

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_DEFAULT_SETUP_FORM = {
    "tracker_kind": "linear",
    "tracker_api_key": "$LINEAR_API_KEY",
    "project_slug": "",
    "assignee": "",
    "active_states": "Todo, In Progress",
    "terminal_states": "Done, Cancelled, Canceled, Duplicate, Closed",
    "poll_interval_ms": "30000",
    "workspace_root": "~/symphony_workspaces",
    "max_concurrent_agents": "5",
    "max_turns": "20",
    "max_retry_backoff_ms": "300000",
    "provider": "claude",
    "command": "claude",
    "turn_timeout_ms": "3600000",
    "stall_timeout_ms": "300000",
    "dangerously_skip_permissions": True,
    "after_create": "",
    "before_run": "",
    "after_run": "",
    "before_remove": "",
    "hooks_timeout_ms": "120000",
    "server_port": "8080",
    "qa_review_enabled": False,
    "qa_review_dispatch": "QA Review",
    "qa_review_success": "In Review",
    "qa_review_failure": "Todo",
    "qa_agent_provider": "",
    "qa_agent_command": "",
    "qa_agent_turn_timeout_ms": "",
    "qa_agent_stall_timeout_ms": "",
    "review_prompt": "",
    "prompt_template": """You are a senior software engineer working on the project.\n\n## Issue\n\n**Title:** {{ issue.title }}\n**Identifier:** {{ issue.identifier }}\n**State:** {{ issue.state }}\n{% if issue.description %}\n**Description:**\n{{ issue.description }}\n{% endif %}\n\n## Instructions\n\n1. Read the issue carefully.\n2. Create and checkout a branch named `agent/{{ issue.identifier | lower }}`.\n3. Implement the requested change.\n4. Update tests as needed.\n5. Keep changes minimal and consistent with the codebase.\n""",
}


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


def _redirect(location: str) -> web.Response:
    raise web.HTTPFound(location)


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
<details class="issue-drilldown" data-id="{escape(str(entry.get('issue_identifier') or ''), quote=True)}">
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


def _workflow_form_data(
    workflow_path: Path,
    workflow: WorkflowDefinition | None = None,
    form_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = dict(_DEFAULT_SETUP_FORM)
    data["workflow_path"] = str(workflow_path)

    if workflow is not None:
        raw = workflow.config
        tracker = raw.get("tracker") or {}
        polling = raw.get("polling") or {}
        workspace = raw.get("workspace") or {}
        agent = raw.get("agent") or {}
        codex = raw.get("codex") or {}
        hooks = raw.get("hooks") or {}
        server = raw.get("server") or {}
        transitions = raw.get("transitions") or {}
        qa_review = transitions.get("qa_review") or {}

        data.update(
            {
                "tracker_kind": str(tracker.get("kind") or "linear"),
                "tracker_api_key": str(tracker.get("api_key") or "$LINEAR_API_KEY"),
                "project_slug": str(tracker.get("project_slug") or ""),
                "assignee": str(tracker.get("assignee") or ""),
                "active_states": ", ".join(str(v) for v in (tracker.get("active_states") or [])) or data["active_states"],
                "terminal_states": ", ".join(str(v) for v in (tracker.get("terminal_states") or [])) or data["terminal_states"],
                "poll_interval_ms": str(polling.get("interval_ms") or data["poll_interval_ms"]),
                "workspace_root": str(workspace.get("root") or data["workspace_root"]),
                "max_concurrent_agents": str(agent.get("max_concurrent_agents") or data["max_concurrent_agents"]),
                "max_turns": str(agent.get("max_turns") or data["max_turns"]),
                "max_retry_backoff_ms": str(agent.get("max_retry_backoff_ms") or data["max_retry_backoff_ms"]),
                "provider": str(agent.get("provider") or data["provider"]),
                "command": str(codex.get("command") or data["command"]),
                "turn_timeout_ms": str(codex.get("turn_timeout_ms") or data["turn_timeout_ms"]),
                "stall_timeout_ms": str(codex.get("stall_timeout_ms") or data["stall_timeout_ms"]),
                "dangerously_skip_permissions": bool(codex.get("dangerously_skip_permissions", True)),
                "after_create": str(hooks.get("after_create") or ""),
                "before_run": str(hooks.get("before_run") or ""),
                "after_run": str(hooks.get("after_run") or ""),
                "before_remove": str(hooks.get("before_remove") or ""),
                "hooks_timeout_ms": str(hooks.get("timeout_ms") or data["hooks_timeout_ms"]),
                "server_port": str(server.get("port") or data["server_port"]),
                "qa_review_enabled": bool(qa_review.get("enabled", False)),
                "qa_review_dispatch": str(qa_review.get("dispatch") or data["qa_review_dispatch"]),
                "qa_review_success": str(qa_review.get("success") or data["qa_review_success"]),
                "qa_review_failure": str(qa_review.get("failure") or data["qa_review_failure"]),
                "qa_agent_provider": str((qa_review.get("agent") or {}).get("provider") or ""),
                "qa_agent_command": str((qa_review.get("agent") or {}).get("command") or ""),
                "qa_agent_turn_timeout_ms": str((qa_review.get("agent") or {}).get("turn_timeout_ms") or ""),
                "qa_agent_stall_timeout_ms": str((qa_review.get("agent") or {}).get("stall_timeout_ms") or ""),
                "review_prompt": str(raw.get("review_prompt") or ""),
                "prompt_template": workflow.prompt_template or data["prompt_template"],
            }
        )

    if form_overrides:
        data.update(form_overrides)

    return data


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _build_workflow_from_form(form: dict[str, object]) -> WorkflowDefinition:
    tracker = {
        "kind": str(form.get("tracker_kind") or "linear"),
        "api_key": str(form.get("tracker_api_key") or "$LINEAR_API_KEY"),
        "project_slug": str(form.get("project_slug") or "").strip(),
        "active_states": _split_csv(str(form.get("active_states") or "")),
        "terminal_states": _split_csv(str(form.get("terminal_states") or "")),
    }
    assignee = str(form.get("assignee") or "").strip()
    if assignee:
        tracker["assignee"] = assignee

    hooks = {
        "timeout_ms": int(str(form.get("hooks_timeout_ms") or "120000")),
    }
    for key in ("after_create", "before_run", "after_run", "before_remove"):
        value = str(form.get(key) or "").rstrip()
        if value:
            hooks[key] = value

    config = {
        "tracker": tracker,
        "polling": {
            "interval_ms": int(str(form.get("poll_interval_ms") or "30000")),
        },
        "workspace": {
            "root": str(form.get("workspace_root") or "").strip(),
        },
        "agent": {
            "max_concurrent_agents": int(str(form.get("max_concurrent_agents") or "5")),
            "max_turns": int(str(form.get("max_turns") or "20")),
            "max_retry_backoff_ms": int(str(form.get("max_retry_backoff_ms") or "300000")),
            "provider": str(form.get("provider") or "claude").strip().lower(),
        },
        "codex": {
            "command": str(form.get("command") or "claude").strip(),
            "turn_timeout_ms": int(str(form.get("turn_timeout_ms") or "3600000")),
            "stall_timeout_ms": int(str(form.get("stall_timeout_ms") or "300000")),
            "dangerously_skip_permissions": bool(form.get("dangerously_skip_permissions")),
        },
        "hooks": hooks,
        "server": {
            "port": int(str(form.get("server_port") or "8080")),
        },
    }

    qa_enabled = bool(form.get("qa_review_enabled"))
    qa_dispatch = str(form.get("qa_review_dispatch") or "").strip()
    qa_success = str(form.get("qa_review_success") or "").strip()
    qa_failure = str(form.get("qa_review_failure") or "").strip()
    qa_agent_provider = str(form.get("qa_agent_provider") or "").strip()
    qa_agent_command = str(form.get("qa_agent_command") or "").strip()
    qa_agent_turn_timeout = str(form.get("qa_agent_turn_timeout_ms") or "").strip()
    qa_agent_stall_timeout = str(form.get("qa_agent_stall_timeout_ms") or "").strip()
    if qa_enabled or qa_dispatch or qa_success or qa_failure:
        qa_review_block: dict[str, Any] = {
            "enabled": qa_enabled,
            "dispatch": qa_dispatch or None,
            "success": qa_success or None,
            "failure": qa_failure or None,
        }
        # Build QA agent override only when at least one field is set
        qa_agent_block: dict[str, Any] = {}
        if qa_agent_provider:
            qa_agent_block["provider"] = qa_agent_provider
        if qa_agent_command:
            qa_agent_block["command"] = qa_agent_command
        if qa_agent_turn_timeout:
            qa_agent_block["turn_timeout_ms"] = int(qa_agent_turn_timeout)
        if qa_agent_stall_timeout:
            qa_agent_block["stall_timeout_ms"] = int(qa_agent_stall_timeout)
        if qa_agent_block:
            qa_review_block["agent"] = qa_agent_block

        config["transitions"] = {"qa_review": qa_review_block}

    review_prompt = str(form.get("review_prompt") or "").strip()
    if review_prompt:
        config["review_prompt"] = review_prompt

    return WorkflowDefinition(
        config=config,
        prompt_template=str(form.get("prompt_template") or "").strip(),
    )


def _validate_workflow_form(form: dict[str, object]) -> list[str]:
    errors: list[str] = []
    try:
        workflow = _build_workflow_from_form(form)
    except ValueError:
        return ["Numeric fields must contain valid integers."]

    tracker = workflow.config["tracker"]
    if tracker.get("kind") != "linear":
        errors.append("tracker.kind must be 'linear'.")
    if not tracker.get("api_key"):
        errors.append("tracker.api_key is required.")
    if not tracker.get("project_slug"):
        errors.append("tracker.project_slug is required.")
    if not workflow.config["workspace"].get("root"):
        errors.append("workspace.root is required.")
    if not workflow.config["codex"].get("command"):
        errors.append("codex.command is required.")

    from .models import SUPPORTED_PROVIDERS
    agent_provider = workflow.config.get("agent", {}).get("provider", "claude")
    if agent_provider not in SUPPORTED_PROVIDERS:
        errors.append(f"agent.provider must be one of: {', '.join(SUPPORTED_PROVIDERS)}.")

    config = build_config(workflow)
    if config.agent.max_concurrent_agents <= 0:
        errors.append("agent.max_concurrent_agents must be greater than zero.")
    if config.agent.max_turns <= 0:
        errors.append("agent.max_turns must be greater than zero.")
    if config.polling.interval_ms <= 0:
        errors.append("polling.interval_ms must be greater than zero.")
    for error in validate_dispatch_config(config).errors:
        if error == "tracker.api_key is missing or resolved to empty string":
            continue
        errors.append(error)

    return errors


def _checkbox_checked(value: object) -> str:
    return " checked" if bool(value) else ""


def _render_setup_page(
    *,
    values: dict[str, object],
    errors: list[str] | None = None,
    saved: bool = False,
    setup_mode: bool,
) -> str:
    title = "Set Up Cymphony" if setup_mode else "Workflow Settings"
    subtitle = (
        "Create a WORKFLOW.md so the service can start."
        if setup_mode
        else "Edit the current workflow. Running services will reload changes when the file updates."
    )
    action = "/setup" if setup_mode else "/settings"
    success = ""
    if saved:
        success_message = (
            "Workflow saved. Restart Cymphony to leave setup mode."
            if setup_mode
            else "Workflow saved."
        )
        success = f"<div class='notice success'>{escape(success_message)}</div>"

    error_html = ""
    if errors:
        items = "".join(f"<li>{escape(err)}</li>" for err in errors)
        error_html = f"<div class='notice error'><strong>Fix these issues:</strong><ul>{items}</ul></div>"

    def field(name: str) -> str:
        return escape(str(values.get(name) or ""))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; background: #f6f7f9; color: #111827; margin: 0; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px 56px; }}
    h1 {{ margin: 0 0 8px; }}
    p {{ color: #4b5563; }}
    form {{ display: grid; gap: 20px; }}
    .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .card {{ background: #fff; border: 1px solid #d1d5db; border-radius: 12px; padding: 18px; }}
    label {{ display: block; font-size: 14px; font-weight: 600; margin-bottom: 6px; }}
    input, textarea, select {{ width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; font: inherit; }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .full {{ grid-column: 1 / -1; }}
    .notice {{ border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; }}
    .error {{ background: #fef2f2; border: 1px solid #fecaca; }}
    .success {{ background: #ecfdf5; border: 1px solid #a7f3d0; }}
    .actions {{ display: flex; gap: 12px; align-items: center; }}
    button {{ background: #111827; color: white; border: 0; border-radius: 8px; padding: 10px 16px; font: inherit; cursor: pointer; }}
    .muted {{ font-size: 13px; color: #6b7280; }}
    .check {{ display: flex; align-items: center; gap: 10px; }}
    .check input {{ width: auto; }}
    code {{ background: #e5e7eb; border-radius: 4px; padding: 1px 4px; }}
  </style>
</head>
<body>
<main>
  <h1>{escape(title)}</h1>
  <p>{escape(subtitle)}</p>
  <p class="muted">Workflow file: <code>{escape(str(values.get("workflow_path") or ""))}</code></p>
  {success}
  {error_html}
  <form method="post" action="{action}">
    <div class="grid">
      <section class="card">
        <label for="project_slug">Linear project slug</label>
        <input id="project_slug" name="project_slug" value="{field("project_slug")}" required />
      </section>
      <section class="card">
        <label for="assignee">Assignee filter</label>
        <input id="assignee" name="assignee" value="{field("assignee")}" placeholder="optional display name" />
      </section>
      <section class="card">
        <label for="tracker_api_key">Tracker API key value</label>
        <input id="tracker_api_key" name="tracker_api_key" value="{field("tracker_api_key")}" required />
        <div class="muted">Use <code>$LINEAR_API_KEY</code> to load from the environment.</div>
      </section>
      <section class="card">
        <label for="workspace_root">Workspace root</label>
        <input id="workspace_root" name="workspace_root" value="{field("workspace_root")}" required />
      </section>
      <section class="card">
        <label for="active_states">Active states</label>
        <input id="active_states" name="active_states" value="{field("active_states")}" required />
      </section>
      <section class="card">
        <label for="terminal_states">Terminal states</label>
        <input id="terminal_states" name="terminal_states" value="{field("terminal_states")}" required />
      </section>
      <section class="card">
        <label for="poll_interval_ms">Poll interval (ms)</label>
        <input id="poll_interval_ms" name="poll_interval_ms" value="{field("poll_interval_ms")}" required />
      </section>
      <section class="card">
        <label for="server_port">HTTP port</label>
        <input id="server_port" name="server_port" value="{field("server_port")}" required />
      </section>
      <section class="card">
        <label for="max_concurrent_agents">Max concurrent agents</label>
        <input id="max_concurrent_agents" name="max_concurrent_agents" value="{field("max_concurrent_agents")}" required />
      </section>
      <section class="card">
        <label for="max_turns">Max turns</label>
        <input id="max_turns" name="max_turns" value="{field("max_turns")}" required />
      </section>
      <section class="card">
        <label for="max_retry_backoff_ms">Max retry backoff (ms)</label>
        <input id="max_retry_backoff_ms" name="max_retry_backoff_ms" value="{field("max_retry_backoff_ms")}" required />
      </section>
      <section class="card">
        <label for="provider">Agent provider</label>
        <select id="provider" name="provider">
          <option value="claude"{"" if field("provider") == "codex" else " selected"}>claude</option>
          <option value="codex"{" selected" if field("provider") == "codex" else ""}>codex</option>
        </select>
      </section>
      <section class="card">
        <label for="command">Agent command</label>
        <input id="command" name="command" value="{field("command")}" required />
      </section>
      <section class="card">
        <label for="turn_timeout_ms">Turn timeout (ms)</label>
        <input id="turn_timeout_ms" name="turn_timeout_ms" value="{field("turn_timeout_ms")}" required />
      </section>
      <section class="card">
        <label for="stall_timeout_ms">Stall timeout (ms)</label>
        <input id="stall_timeout_ms" name="stall_timeout_ms" value="{field("stall_timeout_ms")}" required />
      </section>
      <section class="card">
        <label for="hooks_timeout_ms">Hooks timeout (ms)</label>
        <input id="hooks_timeout_ms" name="hooks_timeout_ms" value="{field("hooks_timeout_ms")}" required />
      </section>
      <section class="card">
        <label>Permissions</label>
        <label class="check"><input type="checkbox" name="dangerously_skip_permissions" value="1"{_checkbox_checked(values.get("dangerously_skip_permissions"))} />Dangerously skip permissions</label>
      </section>
      <section class="card">
        <label>QA review lane</label>
        <label class="check"><input type="checkbox" name="qa_review_enabled" value="1"{_checkbox_checked(values.get("qa_review_enabled"))} />Enable implementation → QA Review → human review</label>
        <div class="muted">When enabled, successful implementation runs move into the QA review state first.</div>
      </section>
      <section class="card">
        <label for="qa_review_dispatch">QA review dispatch state</label>
        <input id="qa_review_dispatch" name="qa_review_dispatch" value="{field("qa_review_dispatch")}" />
      </section>
      <section class="card">
        <label for="qa_review_success">QA review pass state</label>
        <input id="qa_review_success" name="qa_review_success" value="{field("qa_review_success")}" />
      </section>
      <section class="card">
        <label for="qa_review_failure">QA review failure state</label>
        <input id="qa_review_failure" name="qa_review_failure" value="{field("qa_review_failure")}" />
      </section>
      <section class="card">
        <label for="qa_agent_provider">QA agent provider (optional override)</label>
        <input id="qa_agent_provider" name="qa_agent_provider" value="{field("qa_agent_provider")}" placeholder="inherit from main" />
        <div class="muted">Leave blank to use the main agent provider.</div>
      </section>
      <section class="card">
        <label for="qa_agent_command">QA agent command (optional override)</label>
        <input id="qa_agent_command" name="qa_agent_command" value="{field("qa_agent_command")}" placeholder="inherit from main" />
        <div class="muted">Leave blank to use the main agent command.</div>
      </section>
      <section class="card">
        <label for="qa_agent_turn_timeout_ms">QA agent turn timeout (ms, optional)</label>
        <input id="qa_agent_turn_timeout_ms" name="qa_agent_turn_timeout_ms" value="{field("qa_agent_turn_timeout_ms")}" placeholder="inherit from main" />
      </section>
      <section class="card">
        <label for="qa_agent_stall_timeout_ms">QA agent stall timeout (ms, optional)</label>
        <input id="qa_agent_stall_timeout_ms" name="qa_agent_stall_timeout_ms" value="{field("qa_agent_stall_timeout_ms")}" placeholder="inherit from main" />
      </section>
      <section class="card full">
        <label for="after_create">after_create hook</label>
        <textarea id="after_create" name="after_create">{field("after_create")}</textarea>
      </section>
      <section class="card full">
        <label for="before_run">before_run hook</label>
        <textarea id="before_run" name="before_run">{field("before_run")}</textarea>
      </section>
      <section class="card full">
        <label for="after_run">after_run hook</label>
        <textarea id="after_run" name="after_run">{field("after_run")}</textarea>
      </section>
      <section class="card full">
        <label for="before_remove">before_remove hook</label>
        <textarea id="before_remove" name="before_remove">{field("before_remove")}</textarea>
      </section>
      <section class="card full">
        <label for="review_prompt">QA review prompt template</label>
        <textarea id="review_prompt" name="review_prompt">{field("review_prompt")}</textarea>
        <div class="muted">Optional. Used only for issues in the QA review dispatch state.</div>
      </section>
      <section class="card full">
        <label for="prompt_template">Prompt template</label>
        <textarea id="prompt_template" name="prompt_template" style="min-height: 280px">{field("prompt_template")}</textarea>
      </section>
    </div>
    <div class="actions">
      <button type="submit">Save Workflow</button>
      {"<a href='/'>Back to dashboard</a>" if not setup_mode else ""}
    </div>
  </form>
</main>
</body>
</html>"""


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
                "project": issue.project_name,
                "url": issue.url,
                "last_worked_on": issue.updated_at.isoformat() if issue.updated_at else None,
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


def _render_linear_link(url: str | None) -> str:
    if not url:
        return "-"
    return f"<a href='{escape(url)}' target='_blank' rel='noreferrer'>Open</a>"


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
        f"<div class='table-wrap'>{body}</div></section>"
    )


def _render_operator_cards(
    title: str,
    subtitle: str,
    rows: list[dict[str, object]],
    *,
    empty: str,
    mode: str,
) -> str:
    if not rows:
        body = f"<p class='empty'>{escape(empty)}</p>"
    else:
        cards: list[str] = []
        for row in rows:
            if mode == "running":
                meta = [
                    ("Tracker state", str(row.get("state") or "")),
                    ("Run status", str(row.get("run_status") or "")),
                    ("Turns", str(row.get("turn_count") or 0)),
                    ("Started", _format_timestamp(row.get("started_at"))),
                    ("Session", str(row.get("session_id") or "-")),
                ]
                action_html = _issue_controls(str(row.get("issue_identifier") or ""), include_cancel=True)
                drilldown = _render_issue_drilldown(row)
            else:
                meta = [
                    ("Attempt", str(row.get("attempt") or "")),
                    ("Due in", _format_relative_due(row.get("due_at"), _now_utc())),
                    ("Why", str(row.get("error") or "Continuation retry")),
                    ("Started", _format_timestamp(row.get("started_at"))),
                    ("Last event", _format_timestamp(row.get("last_event_at"))),
                ]
                action_html = _issue_controls(str(row.get("issue_identifier") or ""))
                drilldown = _render_issue_drilldown(
                    row,
                    retry_due=_format_relative_due(row.get("due_at"), _now_utc()),
                )

            meta_html = "".join(
                "<div class='operator-meta-item'>"
                f"<span class='operator-meta-label'>{escape(label)}</span>"
                f"<span class='operator-meta-value'>{escape(value)}</span>"
                "</div>"
                for label, value in meta
            )
            cards.append(
                "<article class='operator-card'>"
                "<div class='operator-card-head'>"
                f"<div class='operator-meta'>{meta_html}</div>"
                f"<div class='issue-actions'>{action_html}</div>"
                "</div>"
                f"{drilldown}"
                "</article>"
            )
        body = f"<div class='operator-card-list'>{''.join(cards)}</div>"

    return (
        "<section class='panel'>"
        f"<div class='panel-head'><h2>{escape(title)}</h2><p>{escape(subtitle)}</p></div>"
        f"{body}</section>"
    )


def _render_dashboard(groups: dict[str, object]) -> str:
    summary = groups["summary"]
    totals = groups["totals"]
    generated_at = _format_timestamp(groups.get("generated_at"))
    now = _now_utc()
    controls = groups.get("controls", {})
    dispatch_paused = bool(controls.get("dispatch_paused"))
    shutdown_requested = bool(controls.get("shutdown_requested"))
    recent_controls = list(controls.get("recent_actions", []))

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

    # --- Workflow config section ---
    wf_config = groups.get("workflow_config") or {}
    wf_transitions = wf_config.get("transitions") or {}
    wf_qa = wf_transitions.get("qa_review") or {}
    wf_active = ", ".join(str(s) for s in (wf_config.get("active_states") or []))
    wf_terminal = ", ".join(str(s) for s in (wf_config.get("terminal_states") or []))
    transition_rule_rows = []
    for event_name in ("dispatch", "success", "failure", "blocked", "cancelled"):
        target = wf_transitions.get(event_name)
        transition_rule_rows.append([
            escape(event_name),
            escape(str(target)) if target else "<span class='muted'>not configured</span>",
        ])
    for event_name in ("dispatch", "success", "failure"):
        target = wf_qa.get(event_name)
        transition_rule_rows.append([
            escape(f"qa_review.{event_name}"),
            escape(str(target)) if target else "<span class='muted'>not configured</span>",
        ])

    workflow_config_section = (
        "<section class='panel'>"
        "<div class='panel-head'><h2>Workflow Configuration</h2>"
        "<p>Active states, terminal states, and transition rules from WORKFLOW.md.</p></div>"
        "<div class='kv'><span class='k'>Active states</span>"
        f"<span class='v'>{escape(wf_active) or '<span class=\"muted\">none</span>'}</span></div>"
        "<div class='kv'><span class='k'>Terminal states</span>"
        f"<span class='v'>{escape(wf_terminal) or '<span class=\"muted\">none</span>'}</span></div>"
        "<div class='kv'><span class='k'>QA review lane</span>"
        f"<span class='v'>{'enabled' if wf_qa.get('enabled') else 'disabled'}</span></div>"
        "<div class='table-wrap' style='margin-top: 10px'>"
        "<table><thead><tr><th>Event</th><th>Target state</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{row[0]}</td><td>{row[1]}</td></tr>"
            for row in transition_rule_rows
        )
        + "</tbody></table></div></section>"
    )

    # --- Recent transitions section ---
    transition_history = list(groups.get("transition_history") or [])[:20]
    transition_rows = [
        [
            escape(str(row.get("issue_identifier") or "")),
            escape(str(row.get("trigger") or "")),
            escape(str(row.get("from_state") or "?")),
            escape(str(row.get("to_state") or "")),
            "<span class='pill active'>ok</span>" if row.get("success") else "<span class='pill paused'>fail</span>",
            escape(_format_timestamp(row.get("timestamp"))),
        ]
        for row in transition_history
    ]

    queue_sections = []

    queue_sections.append(workflow_config_section)
    queue_sections.append(
        _render_table(
            f"Recent Transitions ({len(transition_rows)})",
            "State transitions applied to issues by the orchestrator.",
            ["Issue", "Trigger", "From", "To", "Result", "At"],
            transition_rows,
            "No transitions recorded yet.",
        )
    )

    for key, title, subtitle, empty in [
        ("ready", "Ready To Dispatch", "Work that can start as soon as capacity is available.", "No immediately dispatchable issues."),
        ("waiting", "Waiting", "Eligible work that is queued behind current capacity limits.", "No queued work is waiting for slots."),
        ("blocked", "Blocked", "Issues still gated by unresolved dependencies or tracker state.", "No active blockers."),
        ("recently_completed", "Recently Completed", "Recent terminal-state work for quick operator confirmation.", "No recent completions found."),
    ]:
        headers = (
            ["Issue", "State", "Project", "Last worked on", "Linear"]
            if key == "recently_completed"
            else ["Issue", "State", "Priority", "Reason", "Updated"]
        )
        rows = []
        for item in groups[key]:
            if key == "recently_completed":
                rows.append([
                    _render_issue_link(item["identifier"], item["title"], item.get("url")),
                    escape(str(item.get("state") or "")),
                    escape(str(item.get("project") or "-")),
                    escape(_format_timestamp(item.get("last_worked_on"))),
                    _render_linear_link(item.get("url")),
                ])
            else:
                rows.append([
                    _render_issue_link(item["identifier"], item["title"], item.get("url")),
                    escape(str(item.get("state") or "")),
                    escape(_render_priority(item.get("priority")) if "priority" in item else "-"),
                    escape(str(item.get("reason") or "")),
                    escape(_format_timestamp(item.get("updated_at"))),
                ])
        queue_sections.append(
            _render_table(
                title,
                subtitle,
                headers,
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
<title>Cymphony Operator Dashboard</title>
<style>
  :root {{
    --bg: #f3efe6;
    --bg-accent: radial-gradient(circle at top right, rgba(22, 163, 74, 0.12), transparent 28%), radial-gradient(circle at left top, rgba(14, 116, 144, 0.14), transparent 24%), #f3efe6;
    --panel: rgba(255, 252, 246, 0.92);
    --panel-strong: #fffaf0;
    --ink: #1f2933;
    --muted: #4a5568;
    --line: rgba(31, 41, 51, 0.22);
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
    border-radius: 14px;
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
    font-size: 0.84rem;
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
    padding: 14px 18px;
    overflow: hidden;
  }}
  .table-wrap {{
    overflow-x: auto;
  }}
  .control-toolbar, .issue-actions {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .control-toolbar {{
    justify-content: space-between;
  }}
  .control-group {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .control-group + .control-group {{
    border-left: 1px solid var(--line);
    padding-left: 10px;
    margin-left: 4px;
  }}
  .control-group-label {{
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    white-space: nowrap;
  }}
  [data-tooltip] {{
    position: relative;
  }}
  [data-tooltip]:hover::after,
  [data-tooltip]:focus-visible::after,
  [data-tooltip]:focus-within::after {{
    content: attr(data-tooltip);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--ink);
    color: #fff;
    font-size: 0.78rem;
    font-weight: 400;
    line-height: 1.35;
    padding: 5px 10px;
    border-radius: 8px;
    white-space: nowrap;
    pointer-events: none;
    z-index: 10;
    box-shadow: 0 2px 8px rgba(31, 41, 51, 0.18);
  }}
  [data-tooltip]:hover::before,
  [data-tooltip]:focus-visible::before,
  [data-tooltip]:focus-within::before {{
    content: "";
    position: absolute;
    bottom: calc(100% + 1px);
    left: 50%;
    transform: translateX(-50%);
    border: 5px solid transparent;
    border-top-color: var(--ink);
    pointer-events: none;
    z-index: 10;
  }}
  .caution-button {{
    border-color: rgba(180, 120, 0, 0.3);
    background: rgba(180, 120, 0, 0.07);
    color: #92600a;
  }}
  .caution-button:hover {{
    border-color: rgba(180, 120, 0, 0.45);
    background: rgba(180, 120, 0, 0.12);
  }}
  .issue-actions {{
    min-width: 180px;
    justify-content: flex-end;
  }}
  .small {{ font-size: 0.9rem; }}
  .muted {{ color: var(--muted); }}
  .operator-card-list {{
    display: grid;
    gap: 16px;
  }}
  .operator-card {{
    padding: 18px;
    border-radius: 18px;
    border: 1px solid var(--line);
    background: linear-gradient(180deg, rgba(255, 250, 240, 0.98), rgba(249, 244, 234, 0.92));
  }}
  .operator-card-head {{
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 16px;
    margin-bottom: 12px;
  }}
  .operator-meta {{
    flex: 1;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px 14px;
  }}
  .operator-meta-item {{
    display: grid;
    gap: 4px;
  }}
  .operator-meta-label {{
    color: var(--muted);
    font-size: 0.84rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
  }}
  .operator-meta-value {{
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 1rem;
    font-weight: 600;
    word-break: break-word;
  }}
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
    align-items: start;
  }}
  .detail-card {{
    background: var(--panel-strong);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 14px;
    min-width: 0;
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
    align-items: start;
    gap: 12px;
    padding: 4px 0;
    border-bottom: 1px solid var(--line);
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.9rem;
    min-width: 0;
  }}
  .k {{
    color: var(--muted);
    flex: 0 0 110px;
  }}
  .v {{
    flex: 1 1 auto;
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
    font-size: 0.84rem;
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
    padding: 5px 11px;
    font: inherit;
    font-size: 0.88rem;
    cursor: pointer;
  }}
  button:disabled {{
    opacity: 0.45;
    cursor: not-allowed;
  }}
  button:hover {{
    border-color: rgba(15, 118, 110, 0.35);
    background: #f7f2e7;
  }}
  .danger-button {{
    border-color: rgba(185, 28, 28, 0.28);
    background: rgba(185, 28, 28, 0.08);
    color: var(--danger);
  }}
  .danger-button:hover {{
    border-color: rgba(185, 28, 28, 0.42);
    background: rgba(185, 28, 28, 0.14);
  }}
  .switch-form {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }}
  .switch-label {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    color: var(--muted);
  }}
  .switch-label input {{
    appearance: none;
    width: 44px;
    height: 26px;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: rgba(31, 41, 51, 0.1);
    position: relative;
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease;
  }}
  .switch-label input::after {{
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: white;
    box-shadow: 0 1px 3px rgba(31, 41, 51, 0.2);
    transition: transform 120ms ease;
  }}
  .switch-label input:checked {{
    background: rgba(185, 28, 28, 0.72);
    border-color: rgba(185, 28, 28, 0.72);
  }}
  .switch-label input:checked::after {{
    transform: translateX(18px);
  }}
  .pill {{
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 4px 10px;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.82rem;
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
    margin-bottom: 10px;
  }}
  .panel-head h2 {{
    margin: 0;
    font-size: 1.05rem;
    letter-spacing: -0.02em;
  }}
  .panel-head p {{
    font-size: 0.84rem;
  }}
  table {{
    width: 100%;
    min-width: 640px;
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
    font-size: 0.82rem;
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
    .operator-card-head {{ flex-direction: column; }}
    .issue-actions {{ justify-content: flex-start; }}
    .control-toolbar {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
    .control-group + .control-group {{ border-left: none; padding-left: 0; margin-left: 0; border-top: 1px solid var(--line); padding-top: 8px; }}
  }}
  @media (max-width: 700px) {{
    main {{ padding: 18px; }}
    .stats {{ grid-template-columns: 1fr; }}
    .hero-card h1 {{ font-size: 2rem; }}
    table {{ font-size: 0.9rem; }}
    .operator-meta {{ grid-template-columns: 1fr 1fr; }}
    .detail-wide {{ grid-column: span 1; }}
    .kv {{
      flex-direction: column;
      gap: 2px;
    }}
    .k, .v {{
      flex: initial;
      text-align: left;
    }}
  }}
  .cym-toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--ink);
    color: #fff;
    padding: 10px 18px;
    border-radius: 10px;
    font-family: "Avenir Next", "Segoe UI", sans-serif;
    font-size: 0.88rem;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity 0.25s, transform 0.25s;
    z-index: 9999;
    pointer-events: none;
  }}
  .cym-toast.visible {{
    opacity: 1;
    transform: translateY(0);
  }}
</style>
<script>
window.cym = {{
  _refreshTimer: null,
  _INTERVAL: 15000,
  _paused: false,

  /** Show a brief toast message. */
  toast: function(msg) {{
    var el = document.createElement("div");
    el.className = "cym-toast";
    el.textContent = msg;
    document.body.appendChild(el);
    requestAnimationFrame(function() {{ el.classList.add("visible"); }});
    setTimeout(function() {{
      el.classList.remove("visible");
      setTimeout(function() {{ el.remove(); }}, 300);
    }}, 2000);
  }},

  /** POST an action, show feedback, then refresh the dashboard content in place. */
  post: function(url, body) {{
    var label = url.split("/").pop().replace(/[-_]/g, " ");
    fetch(url, {{
      method: "POST",
      headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
      body: body || ""
    }}).then(function(r) {{
      cym.toast(r.ok ? "Done: " + label : "Failed: " + label);
      cym.refresh();
    }}).catch(function() {{
      cym.toast("Error: " + label);
    }});
  }},

  killApp: function() {{
    var armed = document.getElementById("kill-arm");
    if (!armed || !armed.checked) return;
    cym.post("/api/v1/app/kill", "confirm_kill=true");
  }},

  syncKillButton: function() {{
    var armed = document.getElementById("kill-arm");
    var button = document.getElementById("kill-app-button");
    if (!armed || !button) return;
    button.disabled = !armed.checked;
  }},

  /** Fetch the dashboard HTML and swap <main> content in place. */
  refresh: function() {{
    fetch("/", {{headers: {{"Accept": "text/html"}}}}).then(function(r) {{
      return r.text();
    }}).then(function(html) {{
      var parser = new DOMParser();
      var doc = parser.parseFromString(html, "text/html");
      var fresh = doc.querySelector("main");
      var current = document.querySelector("main");
      if (!fresh || !current) return;

      // Remember which <details> elements are open (by data-id, fall back to index).
      var openSet = new Set();
      current.querySelectorAll("details[data-id]").forEach(function(d) {{
        if (d.open) openSet.add(d.getAttribute("data-id"));
      }});

      // Remember scroll position.
      var scrollY = window.scrollY;

      // Swap content.
      current.innerHTML = fresh.innerHTML;

      // Restore open state.
      current.querySelectorAll("details[data-id]").forEach(function(d) {{
        if (openSet.has(d.getAttribute("data-id"))) d.open = true;
      }});

      // Restore scroll position.
      window.scrollTo(0, scrollY);
    }}).catch(function() {{}});
  }},

  toggleAutoRefresh: function() {{
    cym._paused = !cym._paused;
    var btn = document.getElementById("pause-refresh");
    if (btn) btn.textContent = cym._paused ? "Resume Auto-Refresh" : "Pause Auto-Refresh";
    if (cym._paused) {{
      clearInterval(cym._refreshTimer);
      cym._refreshTimer = null;
      cym.toast("Auto-refresh paused");
    }} else {{
      cym.startAutoRefresh();
      cym.toast("Auto-refresh resumed");
    }}
  }},

  startAutoRefresh: function() {{
    if (cym._refreshTimer) clearInterval(cym._refreshTimer);
    cym._refreshTimer = setInterval(cym.refresh, cym._INTERVAL);
  }}
}};
document.addEventListener("DOMContentLoaded", function() {{
  cym.startAutoRefresh();
  cym.syncKillButton();
  var armed = document.getElementById("kill-arm");
  if (armed) armed.addEventListener("change", cym.syncKillButton);
}});
</script>
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
        </div>
        <div class="control-toolbar">
          <div class="control-group">
            <span class="control-group-label">Status</span>
            <span class="pill {'paused' if dispatch_paused else 'active'}" title="Current dispatch state" data-tooltip="Current dispatch state">{'Paused' if dispatch_paused else 'Active'}</span>
          </div>
          <div class="control-group">
            <span class="control-group-label">View</span>
            {_post_button("/api/v1/refresh", "Refresh Now", tooltip="Fetch the latest orchestration state immediately.")}
            <button type="button" id="pause-refresh" title="Pause the automatic 15-second dashboard refresh" data-tooltip="Pause the automatic 15-second dashboard refresh" onclick="cym.toggleAutoRefresh()">Pause Auto-Refresh</button>
          </div>
          <div class="control-group">
            <span class="control-group-label">Dispatch</span>
            {_post_button("/api/v1/dispatch/pause", "Pause", tooltip="Stop launching new work; active agents continue.", css_class="caution-button")}
            {_post_button("/api/v1/dispatch/resume", "Resume", tooltip="Allow the orchestrator to start queued work again.")}
          </div>
          <div class="control-group">
            <span class="control-group-label">Shutdown</span>
            {_kill_app_switch(shutdown_requested)}
          </div>
        </div>
      </section>
      {_render_operator_cards("Running", "Active workers and current execution status.", list(groups["running"]), empty="No active agents.", mode="running")}
      {_render_operator_cards("Retrying", "Retries scheduled after failures or continuation hand-offs.", list(groups["retrying"]), empty="No retries scheduled.", mode="retrying")}
    </div>
    <div class="stack">
      {''.join(queue_sections)}
    </div>
  </section>

  <p class="footer">
    <a href="/api/v1/state">JSON state</a>
    · Live refresh every 15 s (pausable)
    · Input tokens {totals.get("input_tokens", 0):,}
    · Output tokens {totals.get("output_tokens", 0):,}
  </p>
</main>
</body>
</html>"""


def build_app(
    orchestrator: "Orchestrator" | None,
    *,
    workflow_path: Path,
    setup_mode: bool = False,
    setup_error: str | None = None,
) -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application()
    app["orchestrator"] = orchestrator
    app["workflow_path"] = workflow_path
    app["setup_mode"] = setup_mode
    app["setup_error"] = setup_error
    app.router.add_get("/", _handle_root)
    app.router.add_get("/setup", _handle_setup_get)
    app.router.add_post("/setup", _handle_setup_post)
    app.router.add_get("/settings", _handle_settings_get)
    app.router.add_post("/settings", _handle_settings_post)
    if orchestrator is not None:
        app.router.add_get("/api/v1/state", _handle_state)
        app.router.add_post("/api/v1/refresh", _handle_refresh)
        app.router.add_post("/api/v1/dispatch/pause", _handle_pause_dispatch)
        app.router.add_post("/api/v1/dispatch/resume", _handle_resume_dispatch)
        app.router.add_post("/api/v1/app/kill", _handle_shutdown_app)
        app.router.add_post("/api/v1/issues/{identifier}/cancel", _handle_cancel_worker)
        app.router.add_post("/api/v1/issues/{identifier}/requeue", _handle_requeue_issue)
        app.router.add_post("/api/v1/issues/{identifier}/skip", _handle_skip_issue)
        app.router.add_get("/api/v1/{identifier}", _handle_issue)
    return app


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _require_orchestrator(request: web.Request) -> "Orchestrator":
    orch = request.app.get("orchestrator")
    if orch is None:
        raise web.HTTPServiceUnavailable(
            text=json.dumps(
                {
                    "error": {
                        "code": "setup_mode",
                        "message": "Cymphony is running in setup mode.",
                    }
                }
            ),
            content_type="application/json",
        )
    return orch


async def _handle_root(request: web.Request) -> web.Response:
    if request.app.get("setup_mode"):
        return await _handle_setup_get(request)
    return await _handle_dashboard(request)


def _load_current_workflow_or_none(workflow_path: Path) -> WorkflowDefinition | None:
    try:
        return load_workflow(workflow_path)
    except Exception:
        return None


async def _handle_setup_get(request: web.Request) -> web.Response:
    workflow_path = Path(request.app["workflow_path"])
    workflow = _load_current_workflow_or_none(workflow_path)
    values = _workflow_form_data(workflow_path, workflow)
    errors: list[str] = []
    setup_error = request.app.get("setup_error")
    if setup_error:
        errors.append(str(setup_error))
    saved = request.query.get("saved") == "1"
    return _html_response(
        _render_setup_page(
            values=values,
            errors=errors or None,
            saved=saved,
            setup_mode=True,
        )
    )


async def _handle_settings_get(request: web.Request) -> web.Response:
    if request.app.get("setup_mode"):
        return _redirect("/setup")
    workflow_path = Path(request.app["workflow_path"])
    workflow = load_workflow(workflow_path)
    values = _workflow_form_data(workflow_path, workflow)
    saved = request.query.get("saved") == "1"
    return _html_response(
        _render_setup_page(
            values=values,
            saved=saved,
            setup_mode=False,
        )
    )


async def _save_workflow_from_request(request: web.Request, *, setup_mode: bool) -> web.Response:
    workflow_path = Path(request.app["workflow_path"])
    submitted = await request.post()
    form = {
        "tracker_kind": "linear",
        "tracker_api_key": submitted.get("tracker_api_key", "$LINEAR_API_KEY"),
        "project_slug": submitted.get("project_slug", ""),
        "assignee": submitted.get("assignee", ""),
        "active_states": submitted.get("active_states", ""),
        "terminal_states": submitted.get("terminal_states", ""),
        "poll_interval_ms": submitted.get("poll_interval_ms", ""),
        "workspace_root": submitted.get("workspace_root", ""),
        "max_concurrent_agents": submitted.get("max_concurrent_agents", ""),
        "max_turns": submitted.get("max_turns", ""),
        "max_retry_backoff_ms": submitted.get("max_retry_backoff_ms", ""),
        "command": submitted.get("command", ""),
        "turn_timeout_ms": submitted.get("turn_timeout_ms", ""),
        "stall_timeout_ms": submitted.get("stall_timeout_ms", ""),
        "dangerously_skip_permissions": submitted.get("dangerously_skip_permissions") == "1",
        "qa_review_enabled": submitted.get("qa_review_enabled") == "1",
        "qa_review_dispatch": submitted.get("qa_review_dispatch", ""),
        "qa_review_success": submitted.get("qa_review_success", ""),
        "qa_review_failure": submitted.get("qa_review_failure", ""),
        "qa_agent_provider": submitted.get("qa_agent_provider", ""),
        "qa_agent_command": submitted.get("qa_agent_command", ""),
        "qa_agent_turn_timeout_ms": submitted.get("qa_agent_turn_timeout_ms", ""),
        "qa_agent_stall_timeout_ms": submitted.get("qa_agent_stall_timeout_ms", ""),
        "after_create": submitted.get("after_create", ""),
        "before_run": submitted.get("before_run", ""),
        "after_run": submitted.get("after_run", ""),
        "before_remove": submitted.get("before_remove", ""),
        "hooks_timeout_ms": submitted.get("hooks_timeout_ms", ""),
        "server_port": submitted.get("server_port", ""),
        "review_prompt": submitted.get("review_prompt", ""),
        "prompt_template": submitted.get("prompt_template", ""),
    }

    errors = _validate_workflow_form(form)
    values = _workflow_form_data(workflow_path, form_overrides=form)
    if errors:
        return _html_response(
            _render_setup_page(
                values=values,
                errors=errors,
                setup_mode=setup_mode,
            )
        )

    workflow = _build_workflow_from_form(form)
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    save_workflow(workflow_path, workflow.config, workflow.prompt_template)
    return _redirect(("/setup" if setup_mode else "/settings") + "?saved=1")


async def _handle_setup_post(request: web.Request) -> web.Response:
    return await _save_workflow_from_request(request, setup_mode=True)


async def _handle_settings_post(request: web.Request) -> web.Response:
    if request.app.get("setup_mode"):
        return _redirect("/setup")
    return await _save_workflow_from_request(request, setup_mode=False)


async def _handle_state(request: web.Request) -> web.Response:
    """GET /api/v1/state — full orchestrator snapshot."""
    orch = _require_orchestrator(request)
    snap = orch.snapshot()
    return _json_response(snap)


async def _handle_refresh(request: web.Request) -> web.Response:
    """POST /api/v1/refresh — trigger immediate poll."""
    orch = _require_orchestrator(request)
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
    orch = _require_orchestrator(request)
    return _json_response(orch.pause_dispatching(), status=202)


async def _handle_resume_dispatch(request: web.Request) -> web.Response:
    """POST /api/v1/dispatch/resume — resume new dispatches."""
    orch = _require_orchestrator(request)
    return _json_response(orch.resume_dispatching(), status=202)


async def _handle_shutdown_app(request: web.Request) -> web.Response:
    """POST /api/v1/app/kill — stop the orchestrator process."""
    orch: Orchestrator = request.app["orchestrator"]
    form = await request.post()
    if form.get("confirm_kill") != "true":
        return _json_response(
            {
                "ok": False,
                "action": "shutdown_app",
                "scope": "global",
                "detail": "confirm_kill must be enabled before killing the app",
            },
            status=400,
        )
    result = await orch.shutdown_app()
    return _json_response(result, status=202)


async def _handle_cancel_worker(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/cancel — cancel a running worker."""
    orch = _require_orchestrator(request)
    result = await orch.cancel_worker(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_requeue_issue(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/requeue — release issue for redispatch."""
    orch = _require_orchestrator(request)
    result = await orch.requeue_issue(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_skip_issue(request: web.Request) -> web.Response:
    """POST /api/v1/issues/<identifier>/skip — mark issue as skipped."""
    orch = _require_orchestrator(request)
    result = await orch.skip_issue(request.match_info["identifier"])
    return _json_response(result, status=202 if result.get("ok") else 404)


async def _handle_issue(request: web.Request) -> web.Response:
    """GET /api/v1/<identifier> — per-issue debug details."""
    identifier = request.match_info["identifier"].upper()
    orch = _require_orchestrator(request)
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
    orch = _require_orchestrator(request)
    snap = orch.snapshot()
    groups = await _load_operator_groups(orch, snap)
    groups["waiting_reasons"] = list(snap.get("waiting", []))
    groups["recent_problems"] = list(snap.get("problems", []))
    groups["skipped"] = list(snap.get("skipped", []))
    groups["controls"] = dict(snap.get("controls", {}))
    groups["workflow_config"] = dict(snap.get("workflow_config", {}))
    groups["transition_history"] = list(snap.get("transition_history", []))
    return _html_response(_render_dashboard(groups))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def start_server(
    orchestrator: "Orchestrator",
    port: int,
    workflow_path: Path,
) -> web.AppRunner:
    """Start the HTTP server and return the runner (caller must keep reference)."""
    app = build_app(orchestrator, workflow_path=workflow_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"action=http_server_started host=127.0.0.1 port={port}")
    return runner


async def start_setup_server(
    workflow_path: Path,
    port: int,
    setup_error: str | None,
) -> web.AppRunner:
    """Start the setup server when startup validation cannot produce an orchestrator."""
    app = build_app(
        None,
        workflow_path=workflow_path,
        setup_mode=True,
        setup_error=setup_error,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"action=http_setup_server_started host=127.0.0.1 port={port}")
    return runner


def _post_button(
    action: str,
    label: str,
    *,
    tooltip: str = "",
    css_class: str = "",
) -> str:
    safe_action = html.escape(action, quote=True)
    safe_label = html.escape(label)
    safe_tooltip = html.escape(tooltip, quote=True)
    tooltip_attr = (
        f" title='{safe_tooltip}' data-tooltip='{safe_tooltip}'" if tooltip else ""
    )
    class_attr = f" class='{html.escape(css_class, quote=True)}'" if css_class else ""
    return (
        f"<button type='button'{class_attr}{tooltip_attr}"
        f" onclick=\"cym.post('{safe_action}')\">"
        f"{safe_label}</button>"
    )


def _kill_app_switch(shutdown_requested: bool) -> str:
    checked = " checked" if shutdown_requested else ""
    disabled = " disabled" if shutdown_requested else ""
    button_disabled = " disabled" if shutdown_requested or not checked else ""
    button_label = "Kill Requested" if shutdown_requested else "Kill App"
    return (
        "<div class='switch-form'>"
        "<label class='switch-label' title='Enable the kill switch to allow shutdown'"
        " data-tooltip='Enable the kill switch to allow shutdown'>"
        f"<input type='checkbox' id='kill-arm' value='true' title='Enable the kill switch to allow shutdown'{checked}{disabled}>"
        "<span>Arm</span>"
        "</label>"
        f"<button type='button' id='kill-app-button' class='danger-button'"
        f" title='Terminate the Cymphony process (requires arming first)'"
        f" data-tooltip='Terminate the Cymphony process (requires arming first)'"
        f"{button_disabled} "
        "onclick=\"cym.killApp()\">"
        f"{escape(button_label)}</button>"
        "</div>"
    )


def _issue_controls(
    identifier: str,
    *,
    include_cancel: bool = False,
    requeue_only: bool = False,
) -> str:
    safe_identifier = html.escape(identifier, quote=True)
    buttons = [
        _post_button(
            f"/api/v1/issues/{safe_identifier}/requeue",
            "Requeue",
            tooltip="Move this issue back to the ready queue for another attempt.",
        )
    ]
    if not requeue_only:
        buttons.append(
            _post_button(
                f"/api/v1/issues/{safe_identifier}/skip",
                "Skip",
                tooltip="Skip this issue so the orchestrator will not pick it up.",
                css_class="caution-button",
            )
        )
    if include_cancel:
        buttons.insert(
            0,
            _post_button(
                f"/api/v1/issues/{safe_identifier}/cancel",
                "Cancel",
                tooltip="Abort the running agent worker for this issue.",
                css_class="danger-button",
            ),
        )
    return f"<div class='issue-actions'>{''.join(buttons)}</div>"
