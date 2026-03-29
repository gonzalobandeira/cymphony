"""WORKFLOW.md loader, parser, template renderer, and file watcher (spec §5, §6.2)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Awaitable

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .models import WorkflowDefinition, WorkflowError

logger = logging.getLogger(__name__)

# Default workflow path (spec §5.1)
DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"

OnChangeCallback = Callable[[WorkflowDefinition], Awaitable[None]]


class _WorkflowDumper(yaml.SafeDumper):
    pass


def _represent_workflow_str(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_WorkflowDumper.add_representer(str, _represent_workflow_str)


def load_workflow(path: str | Path) -> WorkflowDefinition:
    """Load and parse a WORKFLOW.md file (spec §5.2).

    Returns WorkflowDefinition with config dict and prompt_template string.
    Raises WorkflowError on any failure.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(
            "missing_workflow_file",
            f"Cannot read workflow file {path}: {exc}",
        ) from exc

    config: dict[str, Any]
    prompt_template: str

    if text.startswith("---"):
        # Split on second ---
        lines = text.splitlines(keepends=True)
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.rstrip() == "---":
                end_idx = i
                break

        if end_idx is not None:
            front_matter_text = "".join(lines[1:end_idx])
            body_lines = lines[end_idx + 1 :]
        else:
            # No closing ---, treat whole file as prompt
            front_matter_text = None
            body_lines = lines
    else:
        front_matter_text = None
        body_lines = text.splitlines(keepends=True)

    if front_matter_text is not None:
        try:
            parsed = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as exc:
            raise WorkflowError(
                "workflow_parse_error",
                f"YAML parse error in {path}: {exc}",
            ) from exc

        if parsed is None:
            config = {}
        elif not isinstance(parsed, dict):
            raise WorkflowError(
                "workflow_front_matter_not_a_map",
                f"Front matter in {path} must be a YAML map, got {type(parsed).__name__}",
            )
        else:
            config = parsed
    else:
        config = {}

    prompt_template = "".join(body_lines).strip()
    return WorkflowDefinition(config=config, prompt_template=prompt_template)


def dump_workflow(config: dict[str, Any], prompt_template: str) -> str:
    """Serialize workflow config and prompt template back to WORKFLOW.md text."""
    front_matter = yaml.dump(
        config,
        Dumper=_WorkflowDumper,
        sort_keys=False,
        allow_unicode=False,
    ).strip()
    prompt_body = prompt_template.strip() or "You are working on an issue from Linear."
    return f"---\n{front_matter}\n---\n{prompt_body}\n"


def save_workflow(path: str | Path, config: dict[str, Any], prompt_template: str) -> None:
    """Write a WORKFLOW.md file."""
    Path(path).write_text(
        dump_workflow(config, prompt_template),
        encoding="utf-8",
    )


def render_prompt(
    workflow: WorkflowDefinition,
    issue: Any,
    attempt: int | None,
) -> str:
    """Render the prompt template with strict variable checking (spec §5.4).

    Raises WorkflowError on template parse or render failure.
    """
    template_str = workflow.prompt_template
    if not template_str:
        return "You are working on an issue from Linear."

    env = Environment(undefined=StrictUndefined, autoescape=False)

    try:
        tmpl = env.from_string(template_str)
    except TemplateSyntaxError as exc:
        raise WorkflowError(
            "template_parse_error",
            f"Template syntax error: {exc}",
        ) from exc

    # Convert issue to dict with string keys for template compatibility
    issue_dict = _issue_to_dict(issue)

    try:
        return tmpl.render(issue=issue_dict, attempt=attempt)
    except UndefinedError as exc:
        raise WorkflowError(
            "template_render_error",
            f"Template render error (unknown variable/filter): {exc}",
        ) from exc


_PLAN_PROMPT_TEMPLATE = """\
You are about to work on the following Linear issue:

**Title**: {{ issue.title }}

{% if issue.description %}
**Description**:
{{ issue.description }}
{% endif %}

Your task right now is PLANNING ONLY. Do not read any files, write any code, or make any changes.

Use the TodoWrite tool to create a step-by-step checklist of everything you will need to do to complete this issue. Each item should be a concrete, actionable step.

Once you have written the plan with TodoWrite, you are done — stop immediately.
"""


def render_plan_prompt(workflow: WorkflowDefinition, issue: Any) -> str:  # noqa: ARG001
    """Render the planning prompt that instructs the agent to produce a TodoWrite checklist only."""
    env = Environment(undefined=StrictUndefined, autoescape=False)
    tmpl = env.from_string(_PLAN_PROMPT_TEMPLATE)
    issue_dict = _issue_to_dict(issue)
    return tmpl.render(issue=issue_dict).strip()


_REVIEW_PROMPT_TEMPLATE = """\
You are a senior QA reviewer for the **{{ issue.title }}** issue.

## Issue

**Title:** {{ issue.title }}
**Identifier:** {{ issue.identifier }}
**State:** {{ issue.state }}
{% if issue.description %}
**Description:**
{{ issue.description }}
{% endif %}
{% if issue.comments %}

**Comments:**
{% for c in issue.comments %}
- **{{ c.author }}** ({{ c.created_at }}): {{ c.body }}
{% endfor %}
{% endif %}

## Review instructions

You are running in **review mode**. Your job is to review the implementation — NOT to write new code.

1. Read the issue description and any reviewer comments carefully.
2. Explore the workspace: check the branch, recent commits, and changed files.
3. Review the code changes for correctness, style, test coverage, and adherence to the issue requirements.
4. If the implementation is acceptable, comment with your approval on the Linear issue using the create-linear-task skill or by updating the issue state.
5. If changes are needed, leave a detailed comment on the Linear issue describing what must be fixed, then stop.

Do NOT create new branches, push code, or open PRs. Your output is a review verdict only.
"""


_REVIEW_DECISION_CONTRACT = """

## Decision format (REQUIRED)

After completing your review, you MUST write a file called `REVIEW_RESULT.json` at the
workspace root with your decision. The file must contain valid JSON with this exact structure:

**If the changes look good:**
```json
{"decision": "pass", "summary": "Brief explanation of why the changes are acceptable."}
```

**If changes are needed:**
```json
{"decision": "changes_requested", "summary": "Brief explanation of what needs to change."}
```

The `decision` field is REQUIRED and must be exactly `"pass"` or `"changes_requested"`.
The `summary` field is optional but recommended.

Do NOT write any other value for `decision`. Do NOT skip writing the file.
"""


def render_review_prompt(workflow: WorkflowDefinition, issue: Any) -> str:
    """Render the QA review prompt for review-mode workers."""
    # Use the review_prompt from workflow config if provided, otherwise use built-in template.
    review_template = (workflow.config.get("review_prompt") or "").strip()
    if not review_template:
        review_template = _REVIEW_PROMPT_TEMPLATE

    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        tmpl = env.from_string(review_template)
    except TemplateSyntaxError as exc:
        raise WorkflowError(
            "review_template_parse_error",
            f"Review template syntax error: {exc}",
        ) from exc

    issue_dict = _issue_to_dict(issue)
    try:
        rendered = tmpl.render(issue=issue_dict).strip()
    except UndefinedError as exc:
        raise WorkflowError(
            "review_template_render_error",
            f"Review template render error: {exc}",
        ) from exc
    return f"{rendered}\n{_REVIEW_DECISION_CONTRACT}".strip()


def _issue_to_dict(issue: Any) -> dict[str, Any]:
    """Recursively convert issue dataclass to template-friendly dict."""
    from .models import Issue, BlockerRef
    import dataclasses

    if dataclasses.is_dataclass(issue) and not isinstance(issue, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(issue):
            val = getattr(issue, f.name)
            if isinstance(val, list):
                result[f.name] = [_issue_to_dict(v) for v in val]
            elif dataclasses.is_dataclass(val) and not isinstance(val, type):
                result[f.name] = _issue_to_dict(val)
            elif hasattr(val, "isoformat"):
                result[f.name] = val.isoformat()
            else:
                result[f.name] = val
        return result
    return issue


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

class WorkflowWatcher:
    """Watches WORKFLOW.md for changes and triggers async reload (spec §6.2)."""

    def __init__(
        self,
        workflow_path: str | Path,
        on_change: OnChangeCallback,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._path = Path(workflow_path).resolve()
        self._on_change = on_change
        self._loop = loop or asyncio.get_event_loop()
        self._observer: Observer | None = None

    def start(self) -> None:
        handler = _WorkflowFileHandler(self._path, self._on_change, self._loop)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._path.parent), recursive=False)
        self._observer.start()
        logger.info(f"action=workflow_watch_started path={self._path}")

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("action=workflow_watch_stopped")


class _WorkflowFileHandler(FileSystemEventHandler):
    def __init__(
        self,
        path: Path,
        on_change: OnChangeCallback,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self._path = path
        self._on_change = on_change
        self._loop = loop

    def on_modified(self, event: FileSystemEvent) -> None:
        if Path(str(event.src_path)).resolve() == self._path:
            self._trigger()

    def on_created(self, event: FileSystemEvent) -> None:
        if Path(str(event.src_path)).resolve() == self._path:
            self._trigger()

    def _trigger(self) -> None:
        try:
            workflow = load_workflow(self._path)
            asyncio.run_coroutine_threadsafe(self._on_change(workflow), self._loop)
            logger.info(f"action=workflow_reloaded path={self._path}")
        except WorkflowError as exc:
            logger.error(
                f"action=workflow_reload_failed path={self._path} "
                f"error_code={exc.code} error={exc}"
            )


def resolve_workflow_path(explicit_path: str | None) -> Path:
    """Resolve workflow file path with precedence rules (spec §5.1)."""
    if explicit_path:
        return Path(explicit_path).resolve()
    cwd_path = Path.cwd() / DEFAULT_WORKFLOW_FILENAME
    return cwd_path.resolve()
