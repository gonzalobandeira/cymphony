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
