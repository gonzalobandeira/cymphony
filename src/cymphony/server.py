"""Optional HTTP server for observability and control (spec §14, §16)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, TYPE_CHECKING

from aiohttp import web

from .config import _DEFAULT_LINEAR_ENDPOINT, build_config, validate_dispatch_config
from .linear import LinearClient
from .models import Issue, TrackerConfig, WorkflowDefinition
from .workflow import (
    LOCAL_CONFIG_FILENAME,
    LOCAL_CONFIG_DIR,
    load_example_workflow,
    load_workflow,
    save_workflow,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_DEFAULT_SETUP_FORM = {
    "tracker_kind": "linear",
    "tracker_api_key": "$LINEAR_API_KEY",
    "project_slug": "",
    "assignee": "",
    "poll_interval_ms": "30000",
    "max_concurrent_agents": "5",
    "max_turns": "20",
    "max_retry_backoff_ms": "300000",
    "provider": "claude",
    "command": "",
    "turn_timeout_ms": "3600000",
    "read_timeout_ms": "60000",
    "stall_timeout_ms": "300000",
    "dangerously_skip_permissions": True,
    "server_port": "8080",
    "qa_review_enabled": False,
    "qa_agent_provider": "",
    "qa_agent_command": "",
    "qa_agent_turn_timeout_ms": "",
    "qa_agent_read_timeout_ms": "",
    "qa_agent_stall_timeout_ms": "",
    "qa_agent_dangerously_skip_permissions": False,
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


def _render_key_value(label: str, value: str | None, *, escape_value: bool = True) -> str:
    if not value:
        return ""
    rendered_value = escape(value) if escape_value else value
    return (
        f"<div class='kv'>"
        f"<span class='k'>{escape(label)}</span>"
        f"<span class='v'>{rendered_value}</span>"
        f"</div>"
    )


def _render_issue_comments(comments: list[dict]) -> str:
    if not comments:
        return "<p class='empty small'>No issue comments captured.</p>"

    items = []
    for comment in comments:
        author = escape(str(comment.get("author") or "Unknown"))
        created_at = _format_timestamp(str(comment.get("created_at") or ""))
        body = escape(str(comment.get("body") or ""))
        items.append(
            f"<li><strong>{author}</strong>"
            f"{f' <span class=\"muted\">{created_at}</span>' if created_at else ''}"
            f"<pre>{body}</pre></li>"
        )
    return f"<ul class='event-list'>{''.join(items)}</ul>"


def _event_label(event_name: str | None) -> str:
    label = str(event_name or "unknown").replace("_", " ").strip()
    return label.title() if label else "Unknown"


def _compact_event_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())


def _event_preview(message: str, *, limit: int = 160) -> str:
    compact = _compact_event_text(message)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _render_recent_events(events: list[dict]) -> str:
    if not events:
        return "<p class='empty small'>No recent runtime events yet.</p>"

    items = []
    for index, event in enumerate(reversed(events)):
        label = escape(_event_label(event.get("event")))
        timestamp = _format_timestamp(str(event.get("timestamp") or ""))
        message = str(event.get("message") or "")
        preview = _event_preview(message)
        preview_html = (
            f"<div class='event-preview'>{escape(preview)}</div>"
            if preview else ""
        )

        usage = event.get("usage") or {}
        usage_text = ""
        if usage:
            usage_text = (
                f"tokens {usage.get('input_tokens', 0)} in / "
                f"{usage.get('output_tokens', 0)} out"
            )
        details = " · ".join(part for part in [timestamp, escape(usage_text)] if part)

        full_message_html = ""
        if message and _compact_event_text(message) != preview:
            event_id = f"event-{index}"
            full_message_html = (
                f"<details class='event-message'>"
                f"<summary id='{event_id}'>Full message</summary>"
                f"<pre>{escape(message)}</pre>"
                f"</details>"
            )
        elif message and ("\n" in message or len(message) > 160):
            full_message_html = (
                f"<details class='event-message'>"
                f"<summary>Full message</summary>"
                f"<pre>{escape(message)}</pre>"
                f"</details>"
            )

        items.append(
            "<li class='event-item'>"
            f"<div class='event-head'><strong>{label}</strong></div>"
            f"{preview_html}"
            f"{f'<div class=\"muted event-meta\">{details}</div>' if details else ''}"
            f"{full_message_html}"
            "</li>"
        )
    return f"<ul class='event-list recent-events'>{''.join(items)}</ul>"


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
        _render_key_value("Started", _format_timestamp(entry.get("started_at")), escape_value=False),
        _render_key_value("Last event at", _format_timestamp(entry.get("last_event_at")), escape_value=False),
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


def _extract_workflow_fields(
    data: dict[str, object],
    workflow: WorkflowDefinition,
) -> None:
    """Extract active workflow-config fields into the flat form-data dict *in place*."""
    raw = workflow.config
    tracker = raw.get("tracker") or {}
    polling = raw.get("polling") or {}
    agent = raw.get("agent") or {}
    runner = raw.get("runner") or {}
    server = raw.get("server") or {}
    transitions = raw.get("transitions") or {}
    qa_review = transitions.get("qa_review") or {}
    qa_agent = qa_review.get("agent") or {}

    data.update(
        {
            "tracker_kind": str(tracker.get("kind") or "linear"),
            "tracker_api_key": str(tracker.get("api_key") or "$LINEAR_API_KEY"),
            "project_slug": str(tracker.get("project_slug") or ""),
            "assignee": str(tracker.get("assignee") or ""),
            "poll_interval_ms": str(polling.get("interval_ms") or data["poll_interval_ms"]),
            "max_concurrent_agents": str(agent.get("max_concurrent_agents") or data["max_concurrent_agents"]),
            "max_turns": str(agent.get("max_turns") or data["max_turns"]),
            "max_retry_backoff_ms": str(agent.get("max_retry_backoff_ms") or data["max_retry_backoff_ms"]),
            "provider": str(agent.get("provider") or data["provider"]),
            "command": str(runner.get("command") or data["command"]),
            "turn_timeout_ms": str(runner.get("turn_timeout_ms") or data["turn_timeout_ms"]),
            "read_timeout_ms": str(runner.get("read_timeout_ms") or data["read_timeout_ms"]),
            "stall_timeout_ms": str(runner.get("stall_timeout_ms") or data["stall_timeout_ms"]),
            "dangerously_skip_permissions": bool(runner.get("dangerously_skip_permissions", True)),
            "server_port": str(server.get("port") or data["server_port"]),
            "qa_review_enabled": bool(qa_review.get("enabled", False)),
            "qa_agent_provider": str(qa_agent.get("provider") or ""),
            "qa_agent_command": str(qa_agent.get("command") or ""),
            "qa_agent_turn_timeout_ms": str(qa_agent.get("turn_timeout_ms") or ""),
            "qa_agent_read_timeout_ms": str(qa_agent.get("read_timeout_ms") or ""),
            "qa_agent_stall_timeout_ms": str(qa_agent.get("stall_timeout_ms") or ""),
            "qa_agent_dangerously_skip_permissions": bool(qa_agent.get("dangerously_skip_permissions", False)),
            "review_prompt": str(workflow.review_prompt_template or ""),
            "prompt_template": workflow.prompt_template or data["prompt_template"],
        }
    )


def _workflow_form_data(
    workflow_path: Path,
    workflow: WorkflowDefinition | None = None,
    example_workflow: WorkflowDefinition | None = None,
    form_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the flat dict used to populate the setup/settings form.

    Precedence (highest wins):
      1. ``form_overrides`` — user-submitted values (on validation error re-render)
      2. ``workflow`` — the current local config
      3. ``example_workflow`` — repo-level ``config.example.yml``
      4. ``_DEFAULT_SETUP_FORM`` — hardcoded safe defaults
    """
    data: dict[str, object] = dict(_DEFAULT_SETUP_FORM)
    data["workflow_path"] = str(workflow_path)

    # Layer: example template (lower priority than local config)
    if example_workflow is not None:
        _extract_workflow_fields(data, example_workflow)

    # Layer: local config (higher priority than example)
    if workflow is not None:
        _extract_workflow_fields(data, workflow)

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
    }
    assignee = str(form.get("assignee") or "").strip()
    if assignee:
        tracker["assignee"] = assignee

    config = {
        "tracker": tracker,
        "polling": {
            "interval_ms": int(str(form.get("poll_interval_ms") or "30000")),
        },
        "agent": {
            "max_concurrent_agents": int(str(form.get("max_concurrent_agents") or "5")),
            "max_turns": int(str(form.get("max_turns") or "20")),
            "max_retry_backoff_ms": int(str(form.get("max_retry_backoff_ms") or "300000")),
            "provider": str(form.get("provider") or "claude").strip().lower(),
        },
        "runner": {
            "command": str(form.get("command") or "").strip(),
            "turn_timeout_ms": int(str(form.get("turn_timeout_ms") or "3600000")),
            "read_timeout_ms": int(str(form.get("read_timeout_ms") or "60000")),
            "stall_timeout_ms": int(str(form.get("stall_timeout_ms") or "300000")),
            "dangerously_skip_permissions": bool(form.get("dangerously_skip_permissions")),
        },
        "server": {
            "port": int(str(form.get("server_port") or "8080")),
        },
    }

    qa_enabled = bool(form.get("qa_review_enabled"))
    qa_agent_provider = str(form.get("qa_agent_provider") or "").strip()
    qa_agent_command = str(form.get("qa_agent_command") or "").strip()
    qa_agent_turn_timeout = str(form.get("qa_agent_turn_timeout_ms") or "").strip()
    qa_agent_read_timeout = str(form.get("qa_agent_read_timeout_ms") or "").strip()
    qa_agent_stall_timeout = str(form.get("qa_agent_stall_timeout_ms") or "").strip()
    qa_agent_skip_perms = bool(form.get("qa_agent_dangerously_skip_permissions"))
    if qa_enabled:
        qa_review_block: dict[str, Any] = {
            "enabled": qa_enabled,
        }
        # Build QA agent override only when at least one field is set
        qa_agent_block: dict[str, Any] = {}
        if qa_agent_provider:
            qa_agent_block["provider"] = qa_agent_provider
        if qa_agent_command:
            qa_agent_block["command"] = qa_agent_command
        if qa_agent_turn_timeout:
            qa_agent_block["turn_timeout_ms"] = int(qa_agent_turn_timeout)
        if qa_agent_read_timeout:
            qa_agent_block["read_timeout_ms"] = int(qa_agent_read_timeout)
        if qa_agent_stall_timeout:
            qa_agent_block["stall_timeout_ms"] = int(qa_agent_stall_timeout)
        if qa_agent_skip_perms:
            qa_agent_block["dangerously_skip_permissions"] = True
        if qa_agent_block:
            qa_review_block["agent"] = qa_agent_block

        config["transitions"] = {"qa_review": qa_review_block}

    review_prompt = str(form.get("review_prompt") or "").strip()

    return WorkflowDefinition(
        config=config,
        prompt_template=str(form.get("prompt_template") or "").strip(),
        review_prompt_template=review_prompt or None,
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
        f"Create a config in {LOCAL_CONFIG_DIR}/{LOCAL_CONFIG_FILENAME} so the service can start."
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
    .field-required label::after {{ content: " *"; color: #dc2626; }}
    .field-optional label::after {{ content: " (optional)"; color: #6b7280; font-weight: 400; font-size: 12px; }}
    .loading-indicator {{ color: #6b7280; font-size: 13px; font-style: italic; margin-top: 4px; }}
    .fetch-error {{ color: #dc2626; font-size: 13px; margin-top: 4px; }}
    .state-checkboxes {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
    .state-checkboxes label {{ display: inline-flex; align-items: center; gap: 4px; font-weight: 400; font-size: 13px;
      padding: 4px 10px; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer; background: #fff; }}
    .state-checkboxes label:has(input:checked) {{ background: #ecfdf5; border-color: #a7f3d0; }}
    .state-checkboxes input {{ width: auto; margin: 0; }}
    .toggle-manual {{ font-size: 12px; color: #6b7280; cursor: pointer; text-decoration: underline; margin-top: 4px; display: inline-block; }}
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
      <section class="card field-required">
        <label for="tracker_api_key">Tracker API key value</label>
        <input id="tracker_api_key" name="tracker_api_key" value="{field("tracker_api_key")}" required />
        <div class="muted">Use <code>$LINEAR_API_KEY</code> to load from the environment.</div>
        <button type="button" id="load-linear-data" style="margin-top:8px;background:#4b5563;font-size:13px;padding:6px 12px;">Load Linear data</button>
        <div id="load-linear-status" class="loading-indicator" style="display:none;"></div>
      </section>
      <section class="card field-required">
        <label for="project_slug">Linear project</label>
        <select id="project_slug_select" style="display:none;" aria-label="Select a project"></select>
        <input id="project_slug" name="project_slug" value="{field("project_slug")}" required />
        <span class="toggle-manual" id="project_slug_toggle" style="display:none;"></span>
      </section>
      <section class="card field-optional">
        <label for="assignee">Assignee filter</label>
        <select id="assignee_select" style="display:none;" aria-label="Select an assignee">
          <option value="">No filter (all assignees)</option>
        </select>
        <input id="assignee" name="assignee" value="{field("assignee")}" placeholder="optional display name" />
        <span class="toggle-manual" id="assignee_toggle" style="display:none;"></span>
      </section>
      <section class="card field-required">
        <label for="poll_interval_ms">Poll interval (ms)</label>
        <input id="poll_interval_ms" name="poll_interval_ms" value="{field("poll_interval_ms")}" required />
      </section>
      <section class="card field-required">
        <label for="server_port">HTTP port</label>
        <input id="server_port" name="server_port" value="{field("server_port")}" required />
      </section>
      <section class="card field-required">
        <label for="max_concurrent_agents">Max concurrent agents</label>
        <input id="max_concurrent_agents" name="max_concurrent_agents" value="{field("max_concurrent_agents")}" required />
      </section>
      <section class="card field-required">
        <label for="max_turns">Max turns</label>
        <input id="max_turns" name="max_turns" value="{field("max_turns")}" required />
      </section>
      <section class="card field-required">
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
        <label for="command">Agent command <small>(blank = auto from provider)</small></label>
        <input id="command" name="command" value="{field("command")}" placeholder="auto" />
      </section>
      <section class="card">
        <label for="turn_timeout_ms">Turn timeout (ms)</label>
        <input id="turn_timeout_ms" name="turn_timeout_ms" value="{field("turn_timeout_ms")}" required />
      </section>
      <section class="card">
        <label for="read_timeout_ms">Read timeout (ms)</label>
        <input id="read_timeout_ms" name="read_timeout_ms" value="{field("read_timeout_ms")}" required />
      </section>
      <section class="card">
        <label for="stall_timeout_ms">Stall timeout (ms)</label>
        <input id="stall_timeout_ms" name="stall_timeout_ms" value="{field("stall_timeout_ms")}" required />
      </section>
      <section class="card">
        <label>Permissions</label>
        <label class="check"><input type="checkbox" name="dangerously_skip_permissions" value="1"{_checkbox_checked(values.get("dangerously_skip_permissions"))} />Dangerously skip permissions</label>
      </section>
      <section class="card">
        <label>QA review lane</label>
        <label class="check"><input type="checkbox" name="qa_review_enabled" value="1"{_checkbox_checked(values.get("qa_review_enabled"))} />Enable execution → QA review → human review</label>
        <div class="muted">When enabled, successful implementation runs move into the QA review state first.</div>
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
        <label for="qa_agent_read_timeout_ms">QA agent read timeout (ms, optional)</label>
        <input id="qa_agent_read_timeout_ms" name="qa_agent_read_timeout_ms" value="{field("qa_agent_read_timeout_ms")}" placeholder="inherit from main" />
      </section>
      <section class="card">
        <label for="qa_agent_stall_timeout_ms">QA agent stall timeout (ms, optional)</label>
        <input id="qa_agent_stall_timeout_ms" name="qa_agent_stall_timeout_ms" value="{field("qa_agent_stall_timeout_ms")}" placeholder="inherit from main" />
      </section>
      <section class="card">
        <label class="check"><input type="checkbox" name="qa_agent_dangerously_skip_permissions" value="1"{_checkbox_checked(values.get("qa_agent_dangerously_skip_permissions"))} />QA agent: dangerously skip permissions</label>
        <div class="muted">Leave unchecked to inherit from main agent.</div>
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
<script>
(function() {{
  var apiKey = document.getElementById("tracker_api_key");
  var loadBtn = document.getElementById("load-linear-data");
  var loadStatus = document.getElementById("load-linear-status");

  function qs(sel) {{ return document.querySelector(sel); }}
  function show(el) {{ el.style.display = ""; }}
  function hide(el) {{ el.style.display = "none"; }}

  /* ---- toggle between select and manual input ---- */
  function setupToggle(name, selectEl, inputEl, toggleEl) {{
    var useManual = false;
    toggleEl.textContent = "Switch to manual entry";
    toggleEl.onclick = function() {{
      useManual = !useManual;
      if (useManual) {{
        inputEl.value = selectEl.value;
        hide(selectEl);
        show(inputEl);
        inputEl.name = name;
        selectEl.name = "";
        toggleEl.textContent = "Switch to selector";
      }} else {{
        if (inputEl.value) {{
          selectEl.value = inputEl.value;
        }}
        show(selectEl);
        hide(inputEl);
        selectEl.name = name;
        inputEl.name = "";
        toggleEl.textContent = "Switch to manual entry";
      }}
    }};
  }}

  /* ---- state checkboxes ---- */
  function setupStateCheckboxes(containerId, inputEl, toggleEl, states, currentValues) {{
    var container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = "";
    var currentSet = {{}};
    currentValues.forEach(function(v) {{ currentSet[v.trim().toLowerCase()] = v.trim(); }});

    states.forEach(function(state) {{
      var lbl = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = state;
      if (currentSet[state.toLowerCase()]) cb.checked = true;
      cb.addEventListener("change", syncStateInput.bind(null, containerId, inputEl));
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + state));
      container.appendChild(lbl);
    }});
    show(container);
    show(toggleEl);

    var useManual = false;
    toggleEl.textContent = "Switch to manual entry";
    toggleEl.onclick = function() {{
      useManual = !useManual;
      if (useManual) {{
        hide(container);
        show(inputEl);
        toggleEl.textContent = "Switch to selector";
      }} else {{
        show(container);
        hide(inputEl);
        syncStateInput(containerId, inputEl);
        toggleEl.textContent = "Switch to manual entry";
      }}
    }};
    // Start with checkboxes visible, text hidden
    hide(inputEl);
    syncStateInput(containerId, inputEl);
  }}

  function syncStateInput(containerId, inputEl) {{
    var container = document.getElementById(containerId);
    var checked = container.querySelectorAll("input:checked");
    var vals = [];
    checked.forEach(function(cb) {{ vals.push(cb.value); }});
    inputEl.value = vals.join(", ");
  }}

  /* ---- populate selects ---- */
  function populateProjectSelect(projects, currentSlug) {{
    var sel = qs("#project_slug_select");
    var inp = qs("#project_slug");
    var toggle = qs("#project_slug_toggle");
    if (!sel || !projects.length) return;

    sel.innerHTML = '<option value="">Select a project\u2026</option>';
    projects.forEach(function(p) {{
      var opt = document.createElement("option");
      opt.value = p.slugId;
      opt.textContent = p.name + " (" + p.slugId + ")";
      if (p.slugId === currentSlug) opt.selected = true;
      sel.appendChild(opt);
    }});
    show(sel);
    show(toggle);
    hide(inp);
    sel.name = "project_slug";
    inp.name = "";
    setupToggle("project_slug", sel, inp, toggle);
  }}

  function populateAssigneeSelect(members, currentAssignee) {{
    var sel = qs("#assignee_select");
    var inp = qs("#assignee");
    var toggle = qs("#assignee_toggle");
    if (!sel || !members.length) return;

    sel.innerHTML = '<option value="">No filter (all assignees)</option>';
    members.forEach(function(m) {{
      var opt = document.createElement("option");
      opt.value = m.displayName;
      opt.textContent = m.displayName;
      if (m.displayName.toLowerCase() === (currentAssignee || "").toLowerCase()) opt.selected = true;
      sel.appendChild(opt);
    }});
    show(sel);
    show(toggle);
    hide(inp);
    sel.name = "assignee";
    inp.name = "";
    setupToggle("assignee", sel, inp, toggle);
  }}

  /* ---- fetch all data ---- */
  function loadLinearData() {{
    show(loadStatus);
    loadStatus.textContent = "Loading Linear data\u2026";
    loadStatus.className = "loading-indicator";

    var key = encodeURIComponent(apiKey.value || "");
    var base = "/api/v1/setup/";

    Promise.all([
      fetch(base + "projects?api_key=" + key).then(function(r) {{ return r.json(); }}),
      fetch(base + "members?api_key=" + key).then(function(r) {{ return r.json(); }}),
      fetch(base + "states?api_key=" + key).then(function(r) {{ return r.json(); }})
    ]).then(function(results) {{
      var projData = results[0];
      var membData = results[1];
      var stateData = results[2];
      var currentProject = qs("#project_slug_select").value || qs("#project_slug").value;
      var currentAssignee = qs("#assignee_select").value || qs("#assignee").value;

      var errors = [];
      if (projData.ok && projData.projects.length) {{
        populateProjectSelect(projData.projects, currentProject);
      }} else if (!projData.ok) {{
        errors.push("Projects: " + (projData.error || "unknown error"));
      }}

      if (membData.ok && membData.members.length) {{
        populateAssigneeSelect(membData.members, currentAssignee);
      }} else if (!membData.ok) {{
        errors.push("Members: " + (membData.error || "unknown error"));
      }}

      if (!stateData.ok) {{
        errors.push("States: " + (stateData.error || "unknown error"));
      }}

      if (errors.length) {{
        loadStatus.textContent = "Some data could not be loaded: " + errors.join("; ") + ". You can still enter values manually.";
        loadStatus.className = "fetch-error";
      }} else {{
        loadStatus.textContent = "Linear data loaded.";
        setTimeout(function() {{ hide(loadStatus); }}, 3000);
      }}
    }}).catch(function(err) {{
      loadStatus.textContent = "Failed to connect: " + err.message + ". Enter values manually.";
      loadStatus.className = "fetch-error";
    }});
  }}

  loadBtn.addEventListener("click", loadLinearData);
}})();
</script>
</body>
</html>"""


def _format_timestamp(raw: str | None) -> str:
    """Return an HTML ``<time>`` element with a ``data-utc`` attribute.

    The element's text content is the UTC-formatted display string.  Client-side
    JavaScript can read ``data-utc`` and re-render it in the user's chosen
    timezone.  Because this returns raw HTML, callers must **not** escape the
    result.
    """
    if not raw:
        return escape("Unknown")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return escape(raw)
    utc_dt = parsed.astimezone(timezone.utc)
    iso_str = utc_dt.isoformat()
    display = utc_dt.strftime("%Y-%m-%d %H:%M UTC")
    return f'<time class="cym-ts" data-utc="{escape(iso_str)}">{escape(display)}</time>'


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
                    ("Tracker state", escape(str(row.get("state") or ""))),
                    ("Run status", escape(str(row.get("run_status") or ""))),
                    ("Turns", escape(str(row.get("turn_count") or 0))),
                    ("Started", _format_timestamp(row.get("started_at"))),
                    ("Session", escape(str(row.get("session_id") or "-"))),
                ]
                action_html = _issue_controls(str(row.get("issue_identifier") or ""), include_cancel=True)
                drilldown = _render_issue_drilldown(row)
            else:
                meta = [
                    ("Attempt", escape(str(row.get("attempt") or ""))),
                    ("Due in", escape(_format_relative_due(row.get("due_at"), _now_utc()))),
                    ("Why", escape(str(row.get("error") or "Continuation retry"))),
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
                f"<span class='operator-meta-value'>{value}</span>"
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


def _render_problems_panel(problems: list[dict[str, object]]) -> str:
    """Render a high-priority panel for active operator problems."""
    if not problems:
        return ""

    error_count = sum(1 for problem in problems if problem.get("severity") == "error")
    warning_count = sum(1 for problem in problems if problem.get("severity") == "warning")

    counts_parts: list[str] = []
    if error_count:
        counts_parts.append(f"{error_count} error{'s' if error_count != 1 else ''}")
    if warning_count:
        counts_parts.append(f"{warning_count} warning{'s' if warning_count != 1 else ''}")
    subtitle = " · ".join(counts_parts) if counts_parts else f"{len(problems)} problem{'s' if len(problems) != 1 else ''}"

    rows: list[str] = []
    for problem in problems:
        severity = escape(str(problem.get("severity") or "error"))
        kind = escape(str(problem.get("kind") or ""))
        summary = escape(str(problem.get("summary") or ""))
        detail = escape(str(problem.get("detail") or ""))
        observed_at = _format_timestamp(problem.get("observed_at"))
        issue_identifier = problem.get("issue_identifier")

        issue_cell = "-"
        if issue_identifier:
            safe_identifier = escape(str(issue_identifier))
            issue_cell = f"<a href='/api/v1/{escape(str(issue_identifier), quote=True)}'>{safe_identifier}</a>"

        rows.append(
            f"<tr class='problem-{severity}'>"
            f"<td><span class='severity-badge severity-{severity}'>{severity.upper()}</span></td>"
            f"<td>{issue_cell}</td>"
            f"<td><strong>{summary}</strong><div class='muted small'>{detail}</div></td>"
            f"<td class='muted'>{kind}</td>"
            f"<td class='muted'>{observed_at}</td>"
            "</tr>"
        )

    return (
        "<section class='panel problems-panel'>"
        f"<div class='panel-head'><h2>Problems ({len(problems)})</h2><p>{escape(subtitle)}</p></div>"
        "<div class='table-wrap'>"
        "<table><thead><tr><th>Severity</th><th>Issue</th><th>Problem</th><th>Kind</th><th>Observed</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></section>"
    )


def _render_config_section(groups: dict[str, object]) -> str:
    """Render a read-only view of the current workflow configuration."""
    config = groups.get("workflow_config") or {}
    if not config:
        return "<p class='empty'>No configuration data available.</p>"

    sections: list[str] = []
    for section_key, section_label in [
        ("tracker", "Tracker"),
        ("polling", "Polling"),
        ("agent", "Agent"),
        ("runner", "Runner"),
        ("server", "Server"),
    ]:
        block = config.get(section_key)
        if not block or not isinstance(block, dict):
            continue
        rows = "".join(
            _render_key_value(str(k), str(v))
            for k, v in block.items()
            if v is not None
        )
        if rows:
            sections.append(
                f"<section class='detail-card'>"
                f"<h3>{escape(section_label)}</h3>{rows}</section>"
            )

    # Active/terminal states
    active_states = config.get("active_states")
    terminal_states = config.get("terminal_states")
    if active_states or terminal_states:
        state_rows = ""
        if active_states:
            state_rows += _render_key_value("Active states", ", ".join(str(s) for s in active_states))
        if terminal_states:
            state_rows += _render_key_value("Terminal states", ", ".join(str(s) for s in terminal_states))
        sections.append(f"<section class='detail-card'><h3>States</h3>{state_rows}</section>")

    # Transitions
    transitions = config.get("transitions")
    if transitions and isinstance(transitions, dict):
        t_rows = ""
        for tk, tv in transitions.items():
            if tk == "qa_review" and isinstance(tv, dict):
                for qk, qv in tv.items():
                    t_rows += _render_key_value(f"qa_review.{qk}", str(qv))
            elif tv is not None:
                t_rows += _render_key_value(str(tk), str(tv))
        if t_rows:
            sections.append(f"<section class='detail-card'><h3>Transitions</h3>{t_rows}</section>")

    if not sections:
        return "<p class='empty'>No configuration data available.</p>"

    return f"<div class='config-grid'>{''.join(sections)}</div>"


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
            _format_timestamp(row.get("created_at")),
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
    recent_control_rows = [
        [
            _format_timestamp(row.get("timestamp")),
            escape(str(row.get("action") or "")),
            escape(str(row.get("scope") or "")),
            escape(str(row.get("outcome") or "")),
            escape(str(row.get("issue_identifier") or "-")),
            escape(str(row.get("detail") or "")),
        ]
        for row in recent_controls[:10]
    ]

    # --- Recent transitions section ---
    transition_history = list(groups.get("transition_history") or [])[:20]
    transition_rows = [
        [
            escape(str(row.get("issue_identifier") or "")),
            escape(str(row.get("trigger") or "")),
            escape(str(row.get("from_state") or "?")),
            escape(str(row.get("to_state") or "")),
            "<span class='pill active'>ok</span>" if row.get("success") else "<span class='pill paused'>fail</span>",
            _format_timestamp(row.get("timestamp")),
        ]
        for row in transition_history
    ]

    queue_sections = []

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
                    _format_timestamp(item.get("last_worked_on")),
                    _render_linear_link(item.get("url")),
                ])
            else:
                rows.append([
                    _render_issue_link(item["identifier"], item["title"], item.get("url")),
                    escape(str(item.get("state") or "")),
                    escape(_render_priority(item.get("priority")) if "priority" in item else "-"),
                    escape(str(item.get("reason") or "")),
                    _format_timestamp(item.get("updated_at")),
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
    --bg: #f4f6f9;
    --panel: #ffffff;
    --panel-strong: #f9fafb;
    --ink: #111827;
    --muted: #6b7280;
    --line: #e5e7eb;
    --good: #059669;
    --warn: #d97706;
    --danger: #dc2626;
    --accent: #0891b2;
    --accent-soft: rgba(8, 145, 178, 0.08);
    --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-lg: 0 4px 12px rgba(0,0,0,0.08);
    --radius: 10px;
    --sans: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: var(--sans);
    color: var(--ink);
    background: var(--bg);
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  form {{ display: inline; margin: 0; }}

  /* ---- Top bar ---- */
  .topbar {{
    background: var(--panel);
    border-bottom: 1px solid var(--line);
    padding: 0 24px;
    display: flex;
    align-items: center;
    gap: 24px;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: var(--shadow);
  }}
  .topbar-brand {{
    font-weight: 700;
    font-size: 1.05rem;
    color: var(--ink);
    padding: 14px 0;
    white-space: nowrap;
  }}
  .tab-nav {{
    display: flex;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
    flex: 1;
  }}
  .tab-nav li {{
    margin: 0;
  }}
  .tab-nav a {{
    display: block;
    padding: 14px 18px;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--muted);
    text-decoration: none;
    border-bottom: 2px solid transparent;
    transition: color 0.15s, border-color 0.15s;
    white-space: nowrap;
  }}
  .tab-nav a:hover {{
    color: var(--ink);
    text-decoration: none;
  }}
  .tab-nav a.active {{
    color: var(--accent);
    border-bottom-color: var(--accent);
    font-weight: 600;
  }}
  .topbar-meta {{
    display: flex;
    gap: 16px;
    align-items: center;
    font-size: 0.82rem;
    color: var(--muted);
    white-space: nowrap;
  }}
  .topbar-meta .pill {{
    font-size: 0.75rem;
  }}

  /* ---- Main content ---- */
  main {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  /* ---- Stats row ---- */
  .stats {{
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .stat {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
    box-shadow: var(--shadow);
  }}
  .stat strong {{
    display: block;
    font-size: 1.75rem;
    line-height: 1;
    margin-bottom: 6px;
    font-weight: 700;
  }}
  .stat span {{
    color: var(--muted);
    font-size: 0.82rem;
    font-weight: 500;
  }}
  .stat.attention strong {{ color: var(--danger); }}
  .stat.ready strong {{ color: var(--good); }}
  .stat.waiting strong {{ color: var(--warn); }}
  .stat.running strong {{ color: var(--accent); }}

  /* ---- Overview cards ---- */
  .overview-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .overview-card {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 20px;
    box-shadow: var(--shadow);
  }}
  .overview-card h3 {{
    margin: 0 0 12px;
    font-size: 0.88rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
  }}

  /* ---- Panels ---- */
  .panel {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 16px 20px;
    overflow: hidden;
    margin-bottom: 16px;
  }}
  .panel-head {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 12px;
  }}
  .panel-head h2 {{
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
  }}
  .panel-head p {{
    margin: 0;
    font-size: 0.82rem;
    color: var(--muted);
  }}

  /* ---- Problems ---- */
  .problems-panel {{
    border-left: 4px solid var(--danger);
    background: linear-gradient(135deg, rgba(220, 38, 38, 0.03), var(--panel));
  }}
  .problems-panel .panel-head h2 {{
    color: var(--danger);
  }}
  .severity-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .severity-error {{
    background: rgba(220, 38, 38, 0.1);
    color: var(--danger);
  }}
  .severity-warning {{
    background: rgba(217, 119, 6, 0.1);
    color: var(--warn);
  }}
  .severity-info {{
    background: rgba(8, 145, 178, 0.1);
    color: var(--accent);
  }}
  .problem-error {{ border-left: 3px solid var(--danger); }}
  .problem-warning {{ border-left: 3px solid var(--warn); }}
  .problem-info {{ border-left: 3px solid var(--accent); }}

  /* ---- Tables ---- */
  .table-wrap {{ overflow-x: auto; }}
  table {{
    width: 100%;
    min-width: 640px;
    border-collapse: collapse;
    font-size: 0.88rem;
  }}
  th, td {{
    padding: 10px 8px;
    text-align: left;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }}
  th {{
    color: var(--muted);
    font-size: 0.76rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}
  tbody tr:last-child td {{ border-bottom: 0; }}
  tbody tr:hover {{ background: var(--accent-soft); }}

  /* ---- Controls ---- */
  .control-toolbar, .issue-actions {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .control-toolbar {{ gap: 0; }}
  .control-group {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .control-group + .control-group {{
    border-left: 1px solid var(--line);
    padding-left: 12px;
    margin-left: 6px;
  }}
  .control-group-label {{
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    font-weight: 600;
    white-space: nowrap;
  }}

  /* ---- Buttons ---- */
  button {{
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--ink);
    border-radius: 6px;
    padding: 6px 12px;
    font: inherit;
    font-size: 0.82rem;
    font-weight: 500;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }}
  button:disabled {{
    opacity: 0.4;
    cursor: not-allowed;
  }}
  button:hover:not(:disabled) {{
    border-color: var(--accent);
    background: var(--accent-soft);
  }}
  .danger-button {{
    border-color: rgba(220, 38, 38, 0.3);
    background: rgba(220, 38, 38, 0.05);
    color: var(--danger);
  }}
  .danger-button:hover:not(:disabled) {{
    border-color: rgba(220, 38, 38, 0.5);
    background: rgba(220, 38, 38, 0.1);
  }}
  .caution-button {{
    border-color: rgba(217, 119, 6, 0.3);
    background: rgba(217, 119, 6, 0.05);
    color: #92400e;
  }}
  .caution-button:hover:not(:disabled) {{
    border-color: rgba(217, 119, 6, 0.5);
    background: rgba(217, 119, 6, 0.1);
  }}

  /* ---- Tooltips ---- */
  [data-tooltip] {{ position: relative; }}
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
    font-size: 0.75rem;
    font-weight: 400;
    line-height: 1.35;
    padding: 5px 10px;
    border-radius: 6px;
    white-space: nowrap;
    pointer-events: none;
    z-index: 10;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
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

  /* ---- Selects ---- */
  #tz-select {{
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--ink);
    border-radius: 6px;
    padding: 6px 12px;
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
  }}
  #tz-select:hover {{
    border-color: var(--accent);
  }}

  /* ---- Switches ---- */
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
    color: var(--muted);
    font-size: 0.82rem;
  }}
  .switch-label input {{
    appearance: none;
    width: 40px;
    height: 22px;
    border-radius: 999px;
    border: 1px solid var(--line);
    background: #e5e7eb;
    position: relative;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }}
  .switch-label input::after {{
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: white;
    box-shadow: 0 1px 2px rgba(0,0,0,0.15);
    transition: transform 0.15s;
  }}
  .switch-label input:checked {{
    background: var(--danger);
    border-color: var(--danger);
  }}
  .switch-label input:checked::after {{
    transform: translateX(18px);
  }}

  /* ---- Pills ---- */
  .pill {{
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 0.78rem;
    font-weight: 600;
  }}
  .pill.active {{
    background: rgba(5, 150, 105, 0.1);
    color: var(--good);
  }}
  .pill.paused {{
    background: rgba(220, 38, 38, 0.1);
    color: var(--danger);
  }}

  /* ---- Issue actions ---- */
  .issue-actions {{
    min-width: 180px;
    justify-content: flex-end;
  }}

  /* ---- Operator cards ---- */
  .operator-card-list {{
    display: grid;
    gap: 12px;
  }}
  .operator-card {{
    padding: 16px;
    border-radius: var(--radius);
    border: 1px solid var(--line);
    background: var(--panel-strong);
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
    gap: 8px 14px;
  }}
  .operator-meta-item {{
    display: grid;
    gap: 2px;
  }}
  .operator-meta-label {{
    color: var(--muted);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }}
  .operator-meta-value {{
    font-size: 0.9rem;
    font-weight: 600;
    word-break: break-word;
  }}

  /* ---- Drilldowns ---- */
  .issue-drilldown summary {{
    cursor: pointer;
    font-weight: 600;
    font-size: 0.88rem;
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
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 14px;
    min-width: 0;
  }}
  .detail-wide {{
    grid-column: span 2;
  }}
  .detail-card h3 {{
    margin: 0 0 8px;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--accent);
  }}

  /* ---- Key-value rows ---- */
  .kv {{
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 12px;
    padding: 5px 0;
    border-bottom: 1px solid var(--line);
    font-size: 0.85rem;
    min-width: 0;
  }}
  .k {{
    color: var(--muted);
    flex: 0 0 110px;
    font-weight: 500;
  }}
  .v {{
    flex: 1 1 auto;
    text-align: right;
    word-break: break-word;
  }}

  /* ---- Tags ---- */
  .tag-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }}
  .tag {{
    display: inline-block;
    padding: 3px 8px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-size: 0.78rem;
    font-weight: 500;
  }}

  /* ---- Events ---- */
  .event-list {{
    margin: 0;
    padding-left: 18px;
  }}
  .recent-events {{
    padding-left: 0;
    list-style: none;
    display: grid;
    gap: 8px;
  }}
  .event-item {{
    padding: 8px 0;
    border-bottom: 1px solid var(--line);
  }}
  .event-item:last-child {{
    border-bottom: 0;
    padding-bottom: 0;
  }}
  .event-head {{
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 4px;
  }}
  .event-preview {{
    font-size: 0.88rem;
    line-height: 1.4;
    word-break: break-word;
  }}
  .event-meta {{
    margin-top: 4px;
    font-size: 0.8rem;
  }}
  .event-message {{ margin-top: 8px; }}
  .event-message summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.82rem;
    font-weight: 600;
  }}
  .event-message pre {{ margin-top: 8px; }}
  pre {{
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    padding: 10px;
    border-radius: 6px;
    background: var(--panel-strong);
    border: 1px solid var(--line);
    font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
    font-size: 0.8rem;
  }}

  /* ---- Config grid ---- */
  .config-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
  }}

  /* ---- Misc ---- */
  .small {{ font-size: 0.82rem; }}
  .muted {{ color: var(--muted); }}
  .empty {{
    margin: 0;
    color: var(--muted);
    font-style: italic;
  }}
  .footer {{
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 0.82rem;
  }}

  /* ---- Toast ---- */
  .cym-toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--ink);
    color: #fff;
    padding: 10px 18px;
    border-radius: 8px;
    font-size: 0.82rem;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity 0.2s, transform 0.2s;
    z-index: 9999;
    pointer-events: none;
  }}
  .cym-toast.visible {{
    opacity: 1;
    transform: translateY(0);
  }}

  /* ---- Responsive ---- */
  @media (max-width: 1100px) {{
    .overview-grid {{ grid-template-columns: 1fr; }}
    .stats {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .operator-card-head {{ flex-direction: column; }}
    .issue-actions {{ justify-content: flex-start; }}
    .control-toolbar {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
    .control-group + .control-group {{
      border-left: none; padding-left: 0; margin-left: 0;
      border-top: 1px solid var(--line); padding-top: 8px;
    }}
    .topbar {{ padding: 0 16px; gap: 12px; }}
    .tab-nav a {{ padding: 12px 12px; font-size: 0.84rem; }}
  }}
  @media (max-width: 700px) {{
    main {{ padding: 16px; }}
    .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    table {{ font-size: 0.82rem; }}
    .operator-meta {{ grid-template-columns: 1fr 1fr; }}
    .detail-wide {{ grid-column: span 1; }}
    .config-grid {{ grid-template-columns: 1fr; }}
    .kv {{
      flex-direction: column;
      gap: 2px;
    }}
    .k, .v {{
      flex: initial;
      text-align: left;
    }}
    .topbar {{
      flex-wrap: wrap;
      gap: 0;
    }}
    .topbar-brand {{ padding: 10px 0; }}
    .tab-nav {{ order: 3; width: 100%; border-top: 1px solid var(--line); }}
    .tab-nav a {{ padding: 10px 12px; font-size: 0.82rem; }}
    .topbar-meta {{ display: none; }}
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

      // Remember active tab.
      var activeTab = cym._activeTab || "overview";

      // Swap content.
      current.innerHTML = fresh.innerHTML;

      // Restore open state.
      current.querySelectorAll("details[data-id]").forEach(function(d) {{
        if (openSet.has(d.getAttribute("data-id"))) d.open = true;
      }});

      // Restore scroll position.
      window.scrollTo(0, scrollY);

      // Restore active tab.
      cym.switchTab(activeTab);

      // Re-apply timezone to new content.
      cym.restoreTimezone();
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
  }},

  /** Switch to a specific tab by name. */
  switchTab: function(tabName) {{
    cym._activeTab = tabName;
    document.querySelectorAll(".tab-content").forEach(function(el) {{
      el.classList.toggle("active", el.id === "tab-" + tabName);
    }});
    document.querySelectorAll(".tab-nav a").forEach(function(el) {{
      el.classList.toggle("active", el.getAttribute("data-tab") === tabName);
    }});
    try {{ history.replaceState(null, "", "#" + tabName); }} catch(e) {{}}
  }},

  /** Apply the chosen timezone to all <time class="cym-ts"> elements. */
  applyTimezone: function(tz) {{
    document.querySelectorAll("time.cym-ts[data-utc]").forEach(function(el) {{
      var utc = el.getAttribute("data-utc");
      if (!utc) return;
      try {{
        var d = new Date(utc);
        if (isNaN(d.getTime())) return;
        el.textContent = d.toLocaleString("sv-SE", {{
          timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
          hour: "2-digit", minute: "2-digit", hour12: false
        }}).replace(",", "") + " " + tz.replace(/^.*\\//, "");
      }} catch(e) {{}}
    }});
  }},

  /** Set timezone, persist, and apply. */
  setTimezone: function(tz) {{
    try {{ localStorage.setItem("cym_tz", tz); }} catch(e) {{}}
    cym.applyTimezone(tz);
  }},

  /** Restore saved timezone and apply to page. */
  restoreTimezone: function() {{
    var tz = "UTC";
    try {{ tz = localStorage.getItem("cym_tz") || "UTC"; }} catch(e) {{}}
    var sel = document.getElementById("tz-select");
    if (sel) sel.value = tz;
    cym.applyTimezone(tz);
  }}
}};
document.addEventListener("DOMContentLoaded", function() {{
  cym.startAutoRefresh();
  cym.syncKillButton();
  cym.restoreTimezone();
  var armed = document.getElementById("kill-arm");
  if (armed) armed.addEventListener("change", cym.syncKillButton);
  // Activate tab from URL hash or default to overview.
  var hash = (location.hash || "").replace("#", "") || "overview";
  cym.switchTab(hash);
}});
</script>
</head>
<body>
<nav class="topbar">
  <span class="topbar-brand">Cymphony</span>
  <ul class="tab-nav">
    <li><a href="#overview" data-tab="overview" class="active" onclick="cym.switchTab('overview');return false;">Overview</a></li>
    <li><a href="#tasks" data-tab="tasks" onclick="cym.switchTab('tasks');return false;">Tasks</a></li>
    <li><a href="#config" data-tab="config" onclick="cym.switchTab('config');return false;">Config</a></li>
  </ul>
  <div class="topbar-meta">
    <span>{escape(str(summary["capacity_in_use"]))}</span>
    <span class="pill {'paused' if dispatch_paused else 'active'}">{'Paused' if dispatch_paused else 'Active'}</span>
  </div>
</nav>
<main>
  <!-- ==================== OVERVIEW TAB ==================== -->
  <div id="tab-overview" class="tab-content active">
    <section class="stats">
      <div class="stat running"><strong>{summary["running"]}</strong><span>Running now</span></div>
      <div class="stat"><strong>{summary["retrying"]}</strong><span>Retrying</span></div>
      <div class="stat ready"><strong>{summary["ready"]}</strong><span>Ready next</span></div>
      <div class="stat waiting"><strong>{summary["waiting"]}</strong><span>Waiting</span></div>
      <div class="stat attention"><strong>{summary["needs_attention"]}</strong><span>Needs attention</span></div>
      <div class="stat"><strong>{totals.get("total_tokens", 0):,}</strong><span>Total tokens</span></div>
    </section>

    {_render_problems_panel(list(groups.get("recent_problems", [])))}

    <div class="overview-grid">
      <div class="overview-card">
        <h3>System</h3>
        <div class='kv'><span class='k'>Snapshot</span><span class='v'>{generated_at}</span></div>
        <div class='kv'><span class='k'>Capacity</span><span class='v'>{escape(str(summary["capacity_in_use"]))}</span></div>
        <div class='kv'><span class='k'>Runtime</span><span class='v'>{escape(_format_elapsed_seconds(totals.get("seconds_running")))}</span></div>
        <div class='kv'><span class='k'>Dispatch</span><span class='v'>{'Paused' if dispatch_paused else 'Active'}</span></div>
        <div class='kv'><span class='k'>Input tokens</span><span class='v'>{totals.get("input_tokens", 0):,}</span></div>
        <div class='kv'><span class='k'>Output tokens</span><span class='v'>{totals.get("output_tokens", 0):,}</span></div>
      </div>
      <div class="overview-card">
        <h3>Controls</h3>
        <div class="control-toolbar">
          <div class="control-group">
            <span class="control-group-label">Status</span>
            <span class="pill {'paused' if dispatch_paused else 'active'}" title="Current dispatch state" data-tooltip="Current dispatch state">{'Paused' if dispatch_paused else 'Active'}</span>
          </div>
          <div class="control-group">
            <span class="control-group-label">View</span>
            {_post_button("/api/v1/refresh", "Refresh Now", tooltip="Fetch the latest orchestration state immediately.")}
            <button type="button" id="pause-refresh" title="Pause the automatic 15-second dashboard refresh" data-tooltip="Pause the automatic 15-second dashboard refresh" onclick="cym.toggleAutoRefresh()">Pause Auto-Refresh</button>
            <select id="tz-select" title="Display timestamps in this timezone" onchange="cym.setTimezone(this.value)">
              <option value="UTC">UTC</option>
              <option value="Europe/London">Europe/London</option>
              <option value="Europe/Berlin">Europe/Berlin</option>
              <option value="Europe/Paris">Europe/Paris</option>
              <option value="Europe/Madrid">Europe/Madrid</option>
              <option value="Europe/Rome">Europe/Rome</option>
              <option value="Europe/Amsterdam">Europe/Amsterdam</option>
              <option value="Europe/Zurich">Europe/Zurich</option>
              <option value="Europe/Athens">Europe/Athens</option>
              <option value="Europe/Helsinki">Europe/Helsinki</option>
              <option value="Europe/Moscow">Europe/Moscow</option>
              <option value="Asia/Dubai">Asia/Dubai</option>
              <option value="Asia/Kolkata">Asia/Kolkata</option>
              <option value="Asia/Shanghai">Asia/Shanghai</option>
              <option value="Asia/Tokyo">Asia/Tokyo</option>
              <option value="Australia/Sydney">Australia/Sydney</option>
              <option value="Pacific/Auckland">Pacific/Auckland</option>
              <option value="America/New_York">America/New_York</option>
              <option value="America/Chicago">America/Chicago</option>
              <option value="America/Denver">America/Denver</option>
              <option value="America/Los_Angeles">America/Los_Angeles</option>
              <option value="America/Sao_Paulo">America/Sao_Paulo</option>
            </select>
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
      </div>
    </div>

    {_render_operator_cards("Running", "Active workers and current execution status.", list(groups["running"]), empty="No active agents.", mode="running")}
    {_render_operator_cards("Retrying", "Retries scheduled after failures or continuation hand-offs.", list(groups["retrying"]), empty="No retries scheduled.", mode="retrying")}
  </div>

  <!-- ==================== TASKS TAB ==================== -->
  <div id="tab-tasks" class="tab-content">
    {''.join(queue_sections)}
  </div>

  <!-- ==================== CONFIG TAB ==================== -->
  <div id="tab-config" class="tab-content">
    <section class="panel">
      <div class="panel-head">
        <h2>Active Config</h2>
        <p><a href="/settings">Edit settings</a></p>
      </div>
      {_render_config_section(groups)}
    </section>
  </div>

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
    # Setup discovery endpoints (BAP-189) — available in all modes
    app.router.add_get("/api/v1/setup/projects", _handle_setup_projects)
    app.router.add_get("/api/v1/setup/members", _handle_setup_members)
    app.router.add_get("/api/v1/setup/states", _handle_setup_states)
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
    example = load_example_workflow(workflow_path) if workflow is None else None
    values = _workflow_form_data(workflow_path, workflow, example_workflow=example)
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
        "poll_interval_ms": submitted.get("poll_interval_ms", ""),
        "max_concurrent_agents": submitted.get("max_concurrent_agents", ""),
        "max_turns": submitted.get("max_turns", ""),
        "max_retry_backoff_ms": submitted.get("max_retry_backoff_ms", ""),
        "provider": submitted.get("provider", "claude"),
        "command": submitted.get("command", ""),
        "turn_timeout_ms": submitted.get("turn_timeout_ms", ""),
        "read_timeout_ms": submitted.get("read_timeout_ms", ""),
        "stall_timeout_ms": submitted.get("stall_timeout_ms", ""),
        "dangerously_skip_permissions": submitted.get("dangerously_skip_permissions") == "1",
        "qa_review_enabled": submitted.get("qa_review_enabled") == "1",
        "qa_agent_provider": submitted.get("qa_agent_provider", ""),
        "qa_agent_command": submitted.get("qa_agent_command", ""),
        "qa_agent_turn_timeout_ms": submitted.get("qa_agent_turn_timeout_ms", ""),
        "qa_agent_read_timeout_ms": submitted.get("qa_agent_read_timeout_ms", ""),
        "qa_agent_stall_timeout_ms": submitted.get("qa_agent_stall_timeout_ms", ""),
        "qa_agent_dangerously_skip_permissions": submitted.get("qa_agent_dangerously_skip_permissions") == "1",
        "server_port": submitted.get("server_port", ""),
        "review_prompt": submitted.get("review_prompt", ""),
        "prompt_template": submitted.get("prompt_template", ""),
    }

    errors = _validate_workflow_form(form)
    workflow = _load_current_workflow_or_none(workflow_path)
    example = load_example_workflow(workflow_path) if workflow is None else None
    values = _workflow_form_data(
        workflow_path, workflow, example_workflow=example, form_overrides=form,
    )
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
    save_workflow(
        workflow_path,
        workflow.config,
        workflow.prompt_template,
        workflow.review_prompt_template,
    )
    return _redirect(("/setup" if setup_mode else "/settings") + "?saved=1")


async def _handle_setup_post(request: web.Request) -> web.Response:
    return await _save_workflow_from_request(request, setup_mode=True)


async def _handle_settings_post(request: web.Request) -> web.Response:
    if request.app.get("setup_mode"):
        return _redirect("/setup")
    return await _save_workflow_from_request(request, setup_mode=False)


# ---------------------------------------------------------------------------
# Setup discovery endpoints (BAP-189)
# ---------------------------------------------------------------------------

def _setup_linear_client(request: web.Request) -> LinearClient:
    """Build a minimal LinearClient from the api_key query param or current workflow."""
    api_key = request.query.get("api_key", "").strip()
    if not api_key:
        # Try to pull from current workflow file
        workflow_path = Path(request.app["workflow_path"])
        wf = _load_current_workflow_or_none(workflow_path)
        if wf:
            api_key = (wf.config.get("tracker") or {}).get("api_key") or ""
    if not api_key:
        api_key = "$LINEAR_API_KEY"

    # Resolve env-var references like $LINEAR_API_KEY
    if api_key.startswith("$"):
        api_key = os.environ.get(api_key[1:], "")

    config = TrackerConfig(
        kind="linear",
        endpoint=_DEFAULT_LINEAR_ENDPOINT,
        api_key=api_key,
        project_slug="",
        active_states=[],
        terminal_states=[],
        assignee=None,
    )
    return LinearClient(config)


async def _handle_setup_projects(request: web.Request) -> web.Response:
    """GET /api/v1/setup/projects — list Linear projects."""
    try:
        client = _setup_linear_client(request)
        projects = await client.fetch_projects()
        return _json_response({"ok": True, "projects": projects})
    except Exception as exc:
        logger.warning(f"action=setup_projects_failed error={exc}")
        return _json_response({"ok": False, "error": str(exc), "projects": []})


async def _handle_setup_members(request: web.Request) -> web.Response:
    """GET /api/v1/setup/members — list Linear organisation members."""
    try:
        client = _setup_linear_client(request)
        members = await client.fetch_members()
        return _json_response({"ok": True, "members": members})
    except Exception as exc:
        logger.warning(f"action=setup_members_failed error={exc}")
        return _json_response({"ok": False, "error": str(exc), "members": []})


async def _handle_setup_states(request: web.Request) -> web.Response:
    """GET /api/v1/setup/states — list Linear workflow state names."""
    try:
        client = _setup_linear_client(request)
        states = await client.fetch_all_workflow_state_names()
        return _json_response({"ok": True, "states": states})
    except Exception as exc:
        logger.warning(f"action=setup_states_failed error={exc}")
        return _json_response({"ok": False, "error": str(exc), "states": []})


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
