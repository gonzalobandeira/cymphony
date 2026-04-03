"""Config loader, prompt renderer, and file watcher."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .models import WorkflowDefinition, WorkflowError

logger = logging.getLogger(__name__)

LOCAL_CONFIG_DIR = ".cymphony"
LOCAL_CONFIG_FILENAME = "config.yml"
EXAMPLE_CONFIG_FILENAME = "config.example.yml"
EXAMPLE_PROMPTS_DIRNAME = "prompts.example"
PROMPTS_DIRNAME = "prompts"
EXECUTION_PROMPT_FILENAME = "execution.md"
QA_REVIEW_PROMPT_FILENAME = "qa_review.md"


class ConfigSource(str, Enum):
    """Identifies which source provided the active workflow config."""

    CLI_OVERRIDE = "cli_override"
    LOCAL_CONFIG = "local_config"
    SETUP_REQUIRED = "setup_required"


OnChangeCallback = Callable[[WorkflowDefinition], Awaitable[None]]


class _WorkflowDumper(yaml.SafeDumper):
    pass


def _represent_workflow_str(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.Node:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_WorkflowDumper.add_representer(str, _represent_workflow_str)


def _parse_yaml_config(text: str, path: str | Path) -> dict[str, Any]:
    """Parse the YAML config format."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(
            "workflow_parse_error",
            f"YAML parse error in {path}: {exc}",
        ) from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise WorkflowError(
            "workflow_front_matter_not_a_map",
            f"Config in {path} must be a YAML map, got {type(parsed).__name__}",
        )
    return parsed


def _default_prompt_paths(config_path: Path) -> dict[str, Path]:
    prompts_dir = config_path.parent / PROMPTS_DIRNAME
    return {
        "execution": prompts_dir / EXECUTION_PROMPT_FILENAME,
        "qa_review": prompts_dir / QA_REVIEW_PROMPT_FILENAME,
    }


def _resolve_prompt_paths(config_path: Path, config: dict[str, Any]) -> dict[str, Path]:
    defaults = _default_prompt_paths(config_path)
    prompts_raw = config.get("prompts") or {}
    resolved = dict(defaults)
    if isinstance(prompts_raw, dict):
        execution_raw = prompts_raw.get("execution")
        qa_review_raw = prompts_raw.get("qa_review")
        if execution_raw:
            resolved["execution"] = (config_path.parent / str(execution_raw)).resolve()
        if qa_review_raw:
            resolved["qa_review"] = (config_path.parent / str(qa_review_raw)).resolve()
    return resolved


def _load_optional_prompt(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkflowError(
            "missing_workflow_file",
            f"Cannot read prompt file {path}: {exc}",
        ) from exc


def load_workflow(path: str | Path) -> WorkflowDefinition:
    """Load and parse a YAML config source."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(
            "missing_workflow_file",
            f"Cannot read config file {path}: {exc}",
        ) from exc

    if p.suffix.lower() not in {".yml", ".yaml"}:
        raise WorkflowError(
            "unsupported_workflow_format",
            f"Config file {path} must use .yml or .yaml.",
        )
    config = _parse_yaml_config(text, path)
    prompt_paths = _resolve_prompt_paths(p.resolve(), config)
    execution_prompt = _load_optional_prompt(prompt_paths["execution"])
    review_prompt = _load_optional_prompt(prompt_paths["qa_review"])

    return WorkflowDefinition(
        config=config,
        prompt_template=execution_prompt or "",
        review_prompt_template=review_prompt,
    )


def _dump_yaml_config(config: dict[str, Any]) -> str:
    return yaml.dump(
        config,
        Dumper=_WorkflowDumper,
        sort_keys=False,
        allow_unicode=False,
    )


def save_workflow(
    path: str | Path,
    config: dict[str, Any],
    prompt_template: str,
    review_prompt_template: str | None = None,
) -> None:
    """Write YAML config plus prompt files."""
    target = Path(path)
    if target.suffix.lower() not in {".yml", ".yaml"}:
        raise WorkflowError(
            "unsupported_workflow_format",
            f"Config file {path} must use .yml or .yaml.",
        )

    prompt_paths = _resolve_prompt_paths(target.resolve(), config)
    prompts_dir = prompt_paths["execution"].parent
    prompts_dir.mkdir(parents=True, exist_ok=True)

    yaml_config = dict(config)
    yaml_config["prompts"] = {
        "execution": str(prompt_paths["execution"].relative_to(target.parent)),
        "qa_review": str(prompt_paths["qa_review"].relative_to(target.parent)),
    }

    target.write_text(_dump_yaml_config(yaml_config), encoding="utf-8")
    prompt_paths["execution"].write_text(
        (prompt_template or "You are working on an issue from Linear.").strip() + "\n",
        encoding="utf-8",
    )
    if review_prompt_template and review_prompt_template.strip():
        prompt_paths["qa_review"].write_text(
            review_prompt_template.strip() + "\n",
            encoding="utf-8",
        )
    elif prompt_paths["qa_review"].exists():
        prompt_paths["qa_review"].unlink()


def render_prompt(
    workflow: WorkflowDefinition,
    issue: Any,
    attempt: int | None,
) -> str:
    """Render the execution prompt with strict variable checking."""
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

    issue_dict = _issue_to_dict(issue)
    issue_dict["latest_qa_feedback"] = _extract_latest_qa_feedback(issue_dict)

    try:
        rendered = tmpl.render(issue=issue_dict, attempt=attempt).strip()
    except UndefinedError as exc:
        raise WorkflowError(
            "template_render_error",
            f"Template render error (unknown variable/filter): {exc}",
        ) from exc
    return f"{_EXECUTION_SYSTEM_PROMPT}\n\n{rendered}".strip()


_PLAN_PROMPT_TEMPLATE = """\
You are about to work on the following Linear issue:

**Title**: {{ issue.title }}

{% if issue.description %}
**Description**:
{{ issue.description }}
{% endif %}

Your task right now is PLANNING ONLY. Do not read any files, write any code, or make any changes.

Use the planning checklist mechanism available in your runtime to create a step-by-step checklist of everything you will need to do to complete this issue. Each item should be a concrete, actionable step.

If your runtime exposes a native todo or planning list, use that. If it exposes TodoWrite, use TodoWrite. Once you have written the plan, you are done — stop immediately.
"""


_EXECUTION_SYSTEM_PROMPT = """\
## System Instructions

You are the implementation workflow agent inside Cymphony.

These rules are product invariants and override repo-local prompt wording:

1. Your role is implementation, not QA review.
2. Do not claim end-to-end validation from mocked tests, fake runs, or simulated evidence.
3. If the task requires external-system validation, only treat it as complete when the required real evidence has been produced.
4. Do not commit or intentionally preserve ephemeral runtime artifacts such as `REVIEW_RESULT.json`.
5. Reuse the issue branch when appropriate; do not create unrelated branches.
6. Resolve the stated issue requirements and the latest QA feedback, if present, before stopping.
7. Leave the workspace in a clean, ready-for-handoff state so Cymphony can perform post-run automation.
8. Do not stop at analysis if implementation work remains.
9. Do not post directly to Linear unless the runtime explicitly does that outside your prompt contract.
10. When uncertain, prefer truthful reporting over pretending work or verification happened.
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


_QA_SYSTEM_PROMPT = """\
## System Instructions

You are the QA workflow agent inside Cymphony.

These rules are product invariants and override repo-local prompt wording:

1. Your role is QA review only. Do not implement product changes during review.
2. Review the actual workspace contents and actual branch under review, not assumptions about what should be there.
3. Judge the work against the issue requirements and any prior QA feedback, not against invented extra scope.
4. If required behavior, evidence, or artifacts are missing, choose `changes_requested`.
5. Do not report a pass unless the implementation is genuinely ready for human review.
6. Do not claim E2E validation from mocked tests or simulated runs when the task required real validation.
7. Your only required machine-readable output is `REVIEW_RESULT.json` with a valid supported decision.
8. Do not commit ephemeral QA artifacts into the repository.
9. Be concise, concrete, and actionable in the review summary.
10. When uncertain, prefer truthful failure over false approval.
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
    review_template = (workflow.review_prompt_template or "").strip()
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
    return f"{_QA_SYSTEM_PROMPT}\n\n{rendered}\n{_REVIEW_DECISION_CONTRACT}".strip()


def _issue_to_dict(issue: Any) -> dict[str, Any]:
    """Recursively convert issue dataclass to template-friendly dict."""
    import dataclasses

    if dataclasses.is_dataclass(issue) and not isinstance(issue, type):
        result: dict[str, Any] = {}
        for field in dataclasses.fields(issue):
            value = getattr(issue, field.name)
            if isinstance(value, list):
                result[field.name] = [_issue_to_dict(item) for item in value]
            elif dataclasses.is_dataclass(value) and not isinstance(value, type):
                result[field.name] = _issue_to_dict(value)
            elif hasattr(value, "isoformat"):
                result[field.name] = value.isoformat()
            else:
                result[field.name] = value
        return result
    return issue


def _extract_latest_qa_feedback(issue: dict[str, Any]) -> dict[str, str] | None:
    """Return the latest QA changes-requested comment for the build prompt."""
    comments = issue.get("comments")
    if not isinstance(comments, list):
        return None

    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        if not body.startswith("**QA review requested changes**"):
            continue
        return {
            "author": str(comment.get("author") or "Unknown"),
            "created_at": str(comment.get("created_at") or ""),
            "body": body,
        }

    return None


class WorkflowWatcher:
    """Watch the config and prompt files for changes and reload them."""

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
        self._prompt_paths = self._discover_prompt_paths()

    def _discover_prompt_paths(self) -> set[Path]:
        try:
            workflow = load_workflow(self._path)
        except WorkflowError:
            return set()
        return {
            path.resolve()
            for path in _resolve_prompt_paths(self._path, workflow.config).values()
        }

    def start(self) -> None:
        handler = _WorkflowFileHandler(
            self._path,
            self._prompt_paths,
            self._on_change,
            self._loop,
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(self._path.parent), recursive=True)
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
        prompt_paths: set[Path],
        on_change: OnChangeCallback,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self._path = path
        self._prompt_paths = {p.resolve() for p in prompt_paths}
        self._on_change = on_change
        self._loop = loop

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._should_reload(Path(str(event.src_path))):
            self._trigger()

    def on_created(self, event: FileSystemEvent) -> None:
        if self._should_reload(Path(str(event.src_path))):
            self._trigger()

    def _should_reload(self, changed_path: Path) -> bool:
        resolved = changed_path.resolve()
        return resolved == self._path or resolved in self._prompt_paths

    def _trigger(self) -> None:
        try:
            workflow = load_workflow(self._path)
            self._prompt_paths = {
                path.resolve()
                for path in _resolve_prompt_paths(self._path, workflow.config).values()
            }
            asyncio.run_coroutine_threadsafe(self._on_change(workflow), self._loop)
            logger.info(f"action=workflow_reloaded path={self._path}")
        except WorkflowError as exc:
            logger.error(
                f"action=workflow_reload_failed path={self._path} "
                f"error_code={exc.code} error={exc}"
            )


def resolve_config_source(explicit_path: str | None) -> tuple[Path, ConfigSource]:
    """Resolve the config path and identify which source it came from."""
    if explicit_path:
        return Path(explicit_path).resolve(), ConfigSource.CLI_OVERRIDE

    cwd = Path.cwd()
    yaml_path = cwd / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME
    if yaml_path.exists():
        return yaml_path.resolve(), ConfigSource.LOCAL_CONFIG

    return yaml_path.resolve(), ConfigSource.SETUP_REQUIRED


def load_example_workflow(base: Path | None = None) -> WorkflowDefinition | None:
    """Load ``config.example.yml`` from the repo root if it exists."""
    root = (base or Path.cwd()).resolve()
    if root.name == LOCAL_CONFIG_FILENAME and root.parent.name == LOCAL_CONFIG_DIR:
        root = root.parent.parent
    elif root.suffix.lower() in {".yml", ".yaml"}:
        root = root.parent
    example = root / EXAMPLE_CONFIG_FILENAME
    if not example.exists():
        return None
    try:
        return load_workflow(example)
    except WorkflowError:
        logger.debug("action=load_example_workflow_failed path=%s", example)
        return None


def local_config_path(base: Path | None = None) -> Path:
    """Return the canonical local config path (``.cymphony/config.yml``)."""
    return (base or Path.cwd()) / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME
