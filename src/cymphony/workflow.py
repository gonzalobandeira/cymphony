"""WORKFLOW.md loader, parser, template renderer, and file watcher (spec §5, §6.2)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from enum import Enum
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
EXAMPLE_WORKFLOW_FILENAME = "WORKFLOW.example.md"
LOCAL_CONFIG_DIR = ".cymphony"
LOCAL_WORKFLOW_FILENAME = "workflow.md"


class ConfigSource(str, Enum):
    """Identifies which source provided the active workflow config."""
    CLI_OVERRIDE = "cli_override"           # --workflow-path flag
    LOCAL_CONFIG = "local_config"           # .cymphony/workflow.md
    LEGACY_COMMITTED = "legacy_committed"   # WORKFLOW.md (deprecated)
    SETUP_REQUIRED = "setup_required"       # no config found

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
4. If the implementation is acceptable, record a `pass` verdict with a concise rationale in `REVIEW_RESULT.json`.
5. If changes are needed, record a `changes_requested` verdict with an actionable summary in `REVIEW_RESULT.json`.

Do NOT create new branches, push code, open PRs, or post directly to Linear. Cymphony will publish your review result to Linear after the run completes.
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
    """Resolve workflow file path with precedence rules (spec §5.1).

    Precedence (highest to lowest):
    1. CLI ``--workflow-path`` (explicit override)
    2. ``.cymphony/workflow.md`` (local generated config)
    3. ``WORKFLOW.md`` (legacy committed file — deprecated)
    4. ``.cymphony/workflow.md`` target path (for setup mode to create)
    """
    if explicit_path:
        return Path(explicit_path).resolve()

    cwd = Path.cwd()

    # Prefer local config directory
    local_path = cwd / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME
    if local_path.exists():
        return local_path.resolve()

    # Fall back to legacy committed WORKFLOW.md
    legacy_path = cwd / DEFAULT_WORKFLOW_FILENAME
    if legacy_path.exists():
        return legacy_path.resolve()

    # No config found — return the local config target path so setup mode
    # knows where to write the new config.
    return local_path.resolve()


def resolve_config_source(explicit_path: str | None) -> tuple[Path, ConfigSource]:
    """Resolve the workflow path and identify which source it came from.

    Returns ``(resolved_path, source)`` so callers can log the config
    origin and emit deprecation warnings for legacy paths.
    """
    if explicit_path:
        return Path(explicit_path).resolve(), ConfigSource.CLI_OVERRIDE

    cwd = Path.cwd()

    local_path = cwd / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME
    if local_path.exists():
        return local_path.resolve(), ConfigSource.LOCAL_CONFIG

    legacy_path = cwd / DEFAULT_WORKFLOW_FILENAME
    if legacy_path.exists():
        return legacy_path.resolve(), ConfigSource.LEGACY_COMMITTED

    return local_path.resolve(), ConfigSource.SETUP_REQUIRED


def load_example_workflow(base: Path | None = None) -> WorkflowDefinition | None:
    """Load ``WORKFLOW.example.md`` from the repo root if it exists.

    ``base`` may be either the repository root or a workflow/config path inside
    that repository. Returns the parsed workflow or ``None`` if the file
    doesn't exist or cannot be parsed.
    """
    root = (base or Path.cwd()).resolve()
    if root.name == LOCAL_WORKFLOW_FILENAME and root.parent.name == LOCAL_CONFIG_DIR:
        root = root.parent.parent
    elif root.suffix.lower() == ".md":
        root = root.parent
    example = root / EXAMPLE_WORKFLOW_FILENAME
    if not example.exists():
        return None
    try:
        return load_workflow(example)
    except WorkflowError:
        logger.debug("action=load_example_workflow_failed path=%s", example)
        return None


def local_config_path(base: Path | None = None) -> Path:
    """Return the canonical local config path (``.cymphony/workflow.md``)."""
    return (base or Path.cwd()) / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME


def migrate_legacy_workflow(base: Path | None = None) -> Path | None:
    """Copy a legacy ``WORKFLOW.md`` into ``.cymphony/workflow.md`` if needed.

    Returns the new path if migration occurred, or ``None`` if no migration
    was needed (either the local config already exists or there is no legacy
    file to migrate).
    """
    root = base or Path.cwd()
    local = root / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME
    legacy = root / DEFAULT_WORKFLOW_FILENAME

    if local.exists():
        return None  # already migrated or created by setup

    if not legacy.exists():
        return None  # nothing to migrate

    local.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(legacy), str(local))
    logger.warning(
        "action=migrate_legacy_workflow "
        f"from={legacy} to={local} "
        "hint='WORKFLOW.md is deprecated as the primary config source. "
        "Your config has been copied to .cymphony/workflow.md. "
        "Consider adding .cymphony/ to .gitignore and converting "
        "WORKFLOW.md to WORKFLOW.example.md as a template for new operators.'"
    )
    return local
