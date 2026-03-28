"""Orchestrator: poll loop, dispatch, reconciliation, retry (spec §7, §8, §14, §16)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent import AgentRunner
from .config import ServiceConfig, build_config, validate_dispatch_config
from .linear import LinearClient
from .logging_ import issue_log, session_log
from .models import (
    AgentEvent,
    AgentEventType,
    AgentError,
    CodexTotals,
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunningEntry,
    RunStatus,
    WorkflowDefinition,
    WorkflowError,
)
from .workflow import WorkflowWatcher, load_workflow, render_plan_prompt, render_prompt
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


# Continuation retry delay after clean exit (spec §8.4)
_CONTINUATION_RETRY_DELAY_MS = 1000.0


class Orchestrator:
    """Main orchestrator daemon (spec §7, §8, §16)."""

    def __init__(
        self,
        workflow_path: Path,
        config: ServiceConfig,
        workflow: WorkflowDefinition,
    ) -> None:
        self._workflow_path = workflow_path
        self._config = config
        self._workflow = workflow
        self._state = OrchestratorState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self._observers: list[Any] = []
        self._server: Any = None
        self._shutdown_event = asyncio.Event()
        self._state_id_cache: dict[str, str] = {}  # state_name → Linear state ID
        self._tick_task: asyncio.Task[None] | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._tick_due_at_ms: float | None = None
        self._tick_rerun_requested: bool = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the orchestrator event loop (spec §16.1)."""
        loop = asyncio.get_event_loop()

        # Start workflow file watcher for dynamic reload (spec §6.2)
        watcher = WorkflowWatcher(
            self._workflow_path,
            on_change=self._on_workflow_change,
            loop=loop,
        )
        watcher.start()

        # Start optional HTTP server
        if self._config.server.port is not None:
            from .server import start_server
            self._server = await start_server(self, self._config.server.port)

        # Startup terminal workspace cleanup (spec §8.6)
        await self._startup_terminal_cleanup()

        # Schedule immediate first tick
        self._enqueue_tick(delay_ms=0.0)

        try:
            await self._shutdown_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            watcher.stop()
            if self._server:
                await self._server.cleanup()

    async def _on_workflow_change(self, new_workflow: WorkflowDefinition) -> None:
        """Apply updated workflow config dynamically (spec §6.2)."""
        try:
            new_config = build_config(new_workflow, self._config.server.port)
            self._config = new_config
            self._workflow = new_workflow
            self._state.poll_interval_ms = new_config.polling.interval_ms
            self._state.max_concurrent_agents = new_config.agent.max_concurrent_agents
            logger.info("action=workflow_config_reapplied")
        except Exception as exc:
            logger.error(f"action=workflow_reapply_failed error={exc}")

    # ------------------------------------------------------------------
    # Startup cleanup
    # ------------------------------------------------------------------

    async def _startup_terminal_cleanup(self) -> None:
        """Remove workspaces for terminal-state issues on startup (spec §8.6)."""
        try:
            client = LinearClient(self._config.tracker)
            terminal_issues = await client.fetch_issues_by_states(
                self._config.tracker.terminal_states
            )
            wm = WorkspaceManager(self._config)
            removed = 0
            for issue in terminal_issues:
                ws_path = wm.get_path(issue.identifier)
                if ws_path.exists():
                    await wm.remove_workspace(issue.identifier)
                    removed += 1
            logger.info(
                f"action=startup_terminal_cleanup "
                f"project_slug={self._config.tracker.project_slug} "
                f"states={self._config.tracker.terminal_states} "
                f"matched={len(terminal_issues)} removed={removed}"
            )
        except Exception as exc:
            logger.warning(
                f"action=startup_terminal_cleanup_failed "
                f"project_slug={self._config.tracker.project_slug} "
                f"error={exc} (continuing)"
            )

    # ------------------------------------------------------------------
    # Poll tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Run serialized ticks, coalescing any overlap into one follow-up pass."""
        try:
            while True:
                self._tick_rerun_requested = False
                await self._tick_once()

                if self._tick_rerun_requested:
                    continue

                self._schedule_tick()
                break
        finally:
            rerun_requested = self._tick_rerun_requested
            self._tick_task = None
            if rerun_requested:
                self._enqueue_tick(delay_ms=0.0)

    async def _tick_once(self) -> None:
        """One poll-and-dispatch tick (spec §8.1, §16.2)."""
        try:
            # 1. Reconcile active runs
            await self._reconcile_running_issues()

            # 2. Dispatch preflight validation
            validation = validate_dispatch_config(self._config)
            if not validation.ok:
                for err in validation.errors:
                    logger.error(
                        f"action=dispatch_validation_failed error={err!r}"
                    )
                return

            # 3. Fetch candidate issues
            try:
                client = LinearClient(self._config.tracker)
                issues = await client.fetch_candidate_issues()
            except Exception as exc:
                logger.error(f"action=fetch_candidates_failed error={exc}")
                return

            # 4. Sort for dispatch (spec §8.2)
            sorted_issues = _sort_for_dispatch(issues)

            # 5. Dispatch eligible issues while slots remain
            for issue in sorted_issues:
                if not self._has_slots():
                    break
                if self._should_dispatch(issue):
                    await self._dispatch_issue(issue, attempt=None)

        except Exception as exc:
            logger.error(f"action=tick_error error={exc}", exc_info=True)

    def _schedule_tick(self, delay_ms: float | None = None) -> None:
        if delay_ms is None:
            delay_ms = float(self._state.poll_interval_ms)
        self._enqueue_tick(delay_ms)

    def _enqueue_tick(self, delay_ms: float) -> bool:
        """Queue the next tick without allowing concurrent tick execution."""
        if self._tick_task is not None and not self._tick_task.done():
            already_requested = self._tick_rerun_requested
            self._tick_rerun_requested = True
            return already_requested

        loop = asyncio.get_event_loop()
        due_at_ms = _monotonic_ms() + max(delay_ms, 0.0)

        if self._tick_handle is not None and not self._tick_handle.cancelled():
            existing_due_at_ms = self._tick_due_at_ms or due_at_ms
            if due_at_ms >= existing_due_at_ms:
                return True
            self._tick_handle.cancel()

        self._tick_due_at_ms = due_at_ms
        self._tick_handle = loop.call_later(
            max(delay_ms, 0.0) / 1000.0,
            self._start_tick_task,
        )
        return False

    def _start_tick_task(self) -> None:
        """Start one serialized tick runner."""
        self._tick_handle = None
        self._tick_due_at_ms = None

        if self._tick_task is not None and not self._tick_task.done():
            self._tick_rerun_requested = True
            return

        self._tick_task = asyncio.create_task(self._tick())

    def request_immediate_poll(self) -> bool:
        """Trigger an immediate poll tick (e.g. from HTTP POST /api/v1/refresh).

        Returns True if the request was coalesced (a poll was already pending),
        False if a new tick was scheduled.
        """
        return self._enqueue_tick(delay_ms=0.0)

    # ------------------------------------------------------------------
    # Reconciliation (spec §8.5)
    # ------------------------------------------------------------------

    async def _reconcile_running_issues(self) -> None:
        """Stall detection + tracker state refresh (spec §8.5, §16.3)."""
        # Part A: stall detection
        stall_timeout_ms = self._config.coding_agent.stall_timeout_ms
        if stall_timeout_ms > 0:
            now_ms = _monotonic_ms()
            stalled: list[str] = []
            for issue_id, entry in list(self._state.running.items()):
                last_event_ts = entry.session.last_event_timestamp
                if last_event_ts:
                    elapsed_ms = (
                        _now_utc() - last_event_ts
                    ).total_seconds() * 1000.0
                else:
                    elapsed_ms = (
                        _now_utc() - entry.started_at
                    ).total_seconds() * 1000.0

                if elapsed_ms > stall_timeout_ms:
                    stalled.append(issue_id)

            for issue_id in stalled:
                entry = self._state.running.get(issue_id)
                if entry:
                    entry.status = RunStatus.STALLED
                    issue_log(
                        logger, logging.WARNING,
                        "agent_stall_detected",
                        issue_id, entry.identifier,
                        stall_timeout_ms=stall_timeout_ms,
                    )
                    await self._terminate_running_issue(issue_id, cleanup_workspace=False)
                    await self._schedule_retry(
                        issue_id,
                        entry.identifier,
                        _next_attempt(entry.retry_attempt),
                        error="stall_timeout",
                    )

        # Part B: tracker state refresh
        running_ids = list(self._state.running.keys())
        if not running_ids:
            return

        try:
            client = LinearClient(self._config.tracker)
            refreshed = await client.fetch_issue_states_by_ids(running_ids)
        except Exception as exc:
            logger.debug(
                f"action=reconcile_state_refresh_failed error={exc} "
                f"(keeping workers running)"
            )
            return

        refreshed_by_id = {i.id: i for i in refreshed}
        active_lower = [s.lower() for s in self._config.tracker.active_states]
        terminal_lower = [s.lower() for s in self._config.tracker.terminal_states]

        for issue_id in list(self._state.running.keys()):
            refreshed_issue = refreshed_by_id.get(issue_id)
            if not refreshed_issue:
                continue

            state_lower = refreshed_issue.state.lower()
            if state_lower in terminal_lower:
                entry = self._state.running.get(issue_id)
                if entry:
                    issue_log(
                        logger, logging.INFO,
                        "reconcile_terminal_stop",
                        issue_id, entry.identifier,
                        state=refreshed_issue.state,
                    )
                await self._terminate_running_issue(issue_id, cleanup_workspace=True)
            elif state_lower in active_lower:
                if issue_id in self._state.running:
                    self._state.running[issue_id].issue = refreshed_issue
            else:
                entry = self._state.running.get(issue_id)
                if entry:
                    issue_log(
                        logger, logging.INFO,
                        "reconcile_inactive_stop",
                        issue_id, entry.identifier,
                        state=refreshed_issue.state,
                    )
                await self._terminate_running_issue(issue_id, cleanup_workspace=False)

    async def _terminate_running_issue(self, issue_id: str, cleanup_workspace: bool) -> None:
        """Cancel running task and optionally clean workspace."""
        entry = self._state.running.pop(issue_id, None)
        if not entry:
            return

        if entry.task and not entry.task.done():
            entry.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        elapsed = (_now_utc() - entry.started_at).total_seconds()
        self._state.codex_totals.seconds_running += elapsed

        if cleanup_workspace:
            wm = WorkspaceManager(self._config)
            try:
                await wm.remove_workspace(entry.identifier)
            except Exception as exc:
                logger.warning(
                    f"action=workspace_cleanup_failed "
                    f"identifier={entry.identifier} error={exc}"
                )

    def _render_todo_checklist(self, todos: list[dict]) -> str:
        """Render a TodoWrite todos array as a markdown checklist."""
        lines = ["**Agent Plan**\n"]
        for todo in todos:
            content = todo.get("content", "")
            status = todo.get("status", "pending")
            if status == "completed":
                lines.append(f"- [x] {content}")
            elif status == "in_progress":
                lines.append(f"- [ ] 🔄 {content} *(in progress)*")
            else:
                lines.append(f"- [ ] {content}")
        return "\n".join(lines)

    def _sync_todo_comment(self, issue_id: str, entry: RunningEntry, todos: list[dict]) -> None:
        """Fire-and-forget: sync TodoWrite todos to a Linear comment; never raises."""
        body = self._render_todo_checklist(todos)
        comment_id = entry.session.plan_comment_id

        async def _do() -> None:
            try:
                client = LinearClient(self._config.tracker)
                if comment_id is None:
                    new_id = await client.create_comment(issue_id, body)
                    entry.session.plan_comment_id = new_id
                    logger.info(
                        f"action=plan_comment_created issue_id={issue_id} comment_id={new_id}"
                    )
                else:
                    await client.update_comment(comment_id, body)
                    logger.info(
                        f"action=plan_comment_updated issue_id={issue_id} comment_id={comment_id}"
                    )
            except Exception as exc:
                logger.warning(
                    f"action=plan_comment_sync_failed issue_id={issue_id} error={exc}"
                )

        asyncio.create_task(_do())

    def _transition_issue_state(self, issue_id: str, state_name: str) -> None:
        """Fire-and-forget: move an issue to the named workflow state; never raises."""
        async def _do() -> None:
            try:
                client = LinearClient(self._config.tracker)
                state_id = self._state_id_cache.get(state_name)
                if not state_id:
                    state_id = await client.fetch_workflow_state_id(issue_id, state_name)
                    if not state_id:
                        logger.warning(
                            f"action=state_transition_skipped issue_id={issue_id} "
                            f"state={state_name!r} reason=state_id_not_found"
                        )
                        return
                    self._state_id_cache[state_name] = state_id
                await client.set_issue_state(issue_id, state_id)
                logger.info(
                    f"action=issue_state_set issue_id={issue_id} state={state_name!r}"
                )
            except Exception as exc:
                logger.warning(
                    f"action=state_transition_failed issue_id={issue_id} "
                    f"state={state_name!r} error={exc}"
                )

        asyncio.create_task(_do())

    # ------------------------------------------------------------------
    # Dispatch (spec §8.2, §16.4)
    # ------------------------------------------------------------------

    def _has_slots(self) -> bool:
        global_available = max(
            self._state.max_concurrent_agents - len(self._state.running), 0
        )
        return global_available > 0

    def _should_dispatch(self, issue: Issue) -> bool:
        """Check dispatch eligibility (spec §8.2)."""
        # Must have required fields
        if not (issue.id and issue.identifier and issue.title and issue.state):
            return False

        active_lower = [s.lower() for s in self._config.tracker.active_states]
        terminal_lower = [s.lower() for s in self._config.tracker.terminal_states]
        state_lower = issue.state.lower()

        if state_lower not in active_lower:
            return False
        if state_lower in terminal_lower:
            return False
        if issue.id in self._state.running:
            return False
        if issue.id in self._state.claimed:
            return False

        # Global slot check
        if not self._has_slots():
            return False

        # Per-state slot check
        per_state = self._config.agent.max_concurrent_agents_by_state
        if state_lower in per_state:
            state_count = sum(
                1 for e in self._state.running.values()
                if e.issue.state.lower() == state_lower
            )
            if state_count >= per_state[state_lower]:
                return False

        # Blocker check for Todo state (spec §8.2)
        if state_lower == "todo":
            terminal_lower_set = set(terminal_lower)
            for blocker in issue.blocked_by:
                blocker_state = (blocker.state or "").lower()
                if blocker_state not in terminal_lower_set:
                    return False

        return True

    async def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        """Claim and spawn worker for issue (spec §16.4)."""
        self._state.claimed.add(issue.id)
        self._state.retry_attempts.pop(issue.id, None)

        session = LiveSession(
            session_id=None,
            pid=None,
            last_event=None,
            last_event_timestamp=None,
            last_message=None,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            last_reported_input_tokens=0,
            last_reported_output_tokens=0,
            last_reported_total_tokens=0,
            turn_count=0,
        )
        entry = RunningEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue=issue,
            task=None,
            session=session,
            retry_attempt=attempt,
            started_at=_now_utc(),
        )
        self._state.running[issue.id] = entry

        task = asyncio.create_task(
            self._worker(issue, attempt, entry),
            name=f"worker-{issue.identifier}",
        )
        entry.task = task
        task.add_done_callback(
            lambda t: asyncio.create_task(
                self._on_worker_done(issue.id, issue.identifier, entry, t)
            )
        )

        issue_log(
            logger, logging.INFO,
            "issue_dispatched",
            issue.id, issue.identifier,
            attempt=attempt,
        )
        self._transition_issue_state(issue.id, "In Progress")

    # ------------------------------------------------------------------
    # Worker (spec §16.5)
    # ------------------------------------------------------------------

    async def _worker(
        self,
        issue: Issue,
        attempt: int | None,
        entry: RunningEntry,
    ) -> None:
        """Run one agent session for an issue (workspace + hooks + turns)."""
        wm = WorkspaceManager(self._config)
        agent = AgentRunner(self._config.coding_agent)

        # Prepare workspace
        entry.status = RunStatus.PREPARING_WORKSPACE
        try:
            workspace = await wm.create_for_issue(issue.identifier)
        except Exception as exc:
            entry.status = RunStatus.FAILED
            raise AgentError("workspace_error", f"Workspace creation failed: {exc}") from exc

        # before_run hook
        try:
            await wm.run_before_run_hook(workspace)
        except Exception as exc:
            entry.status = RunStatus.FAILED
            await wm.run_after_run_hook(workspace)
            raise AgentError("before_run_hook_error", str(exc)) from exc

        max_turns = self._config.agent.max_turns
        session_id: str | None = None
        turn_number = 1

        async def on_event(event: AgentEvent) -> None:
            await self._handle_agent_event(issue.id, entry, event)

        try:
            # Planning turn: agent produces a TodoWrite checklist only, no code changes.
            # Reset plan_comment_id so a new comment is always created for this plan,
            # even if a previous plan comment exists from an earlier attempt.
            entry.session.plan_comment_id = None
            entry.status = RunStatus.PLANNING
            plan_prompt = render_plan_prompt(self._workflow, issue)
            issue_log(
                logger, logging.INFO,
                "planning_turn_start",
                issue.id, issue.identifier,
            )
            title = f"{issue.identifier}: {issue.title}"
            session_id = await agent.run_turn(
                workspace_path=workspace.path,
                prompt=plan_prompt,
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                session_id=None,
                title=title,
                on_event=on_event,
            )
            issue_log(
                logger, logging.INFO,
                "planning_turn_completed",
                issue.id, issue.identifier,
                session_id=session_id,
            )
            entry.session.turn_count += 1
            turn_number += 1  # planning turn consumed one slot

            first_execution_turn = True
            while True:
                # Render full prompt on the first execution turn; continuation turns
                # send brief guidance so the original task description is not re-injected (spec §7.1).
                entry.status = RunStatus.BUILDING_PROMPT
                if first_execution_turn:
                    try:
                        prompt = render_prompt(self._workflow, issue, attempt)
                    except WorkflowError as exc:
                        entry.status = RunStatus.FAILED
                        raise AgentError("prompt_error", str(exc)) from exc
                    first_execution_turn = False
                else:
                    prompt = "Continue working on the task."

                entry.session.turn_count += 1
                issue_log(
                    logger, logging.INFO,
                    "turn_start",
                    issue.id, issue.identifier,
                    turn=turn_number,
                    max_turns=max_turns,
                    session_id=session_id,
                )

                entry.status = RunStatus.LAUNCHING_AGENT
                title = f"{issue.identifier}: {issue.title}"
                session_id = await agent.run_turn(
                    workspace_path=workspace.path,
                    prompt=prompt,
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    session_id=session_id,
                    title=title,
                    on_event=on_event,
                )
                entry.status = RunStatus.FINISHING

                # Check current issue state after turn
                try:
                    client = LinearClient(self._config.tracker)
                    refreshed = await client.fetch_issue_states_by_ids([issue.id])
                    if refreshed:
                        issue = refreshed[0]
                        entry.issue = issue
                except Exception as exc:
                    raise AgentError("issue_state_refresh_error", str(exc)) from exc

                active_lower = [s.lower() for s in self._config.tracker.active_states]
                if issue.state.lower() not in active_lower:
                    issue_log(
                        logger, logging.INFO,
                        "turn_loop_exit_inactive",
                        issue.id, issue.identifier,
                        state=issue.state,
                    )
                    break

                if turn_number >= max_turns:
                    issue_log(
                        logger, logging.INFO,
                        "turn_loop_exit_max_turns",
                        issue.id, issue.identifier,
                        max_turns=max_turns,
                    )
                    break

                turn_number += 1
                # Use attempt=None for continuation turns (spec §7.1)
                attempt = None

        finally:
            # Run after_run hook as an independent task so that a concurrent
            # reconciler cancel() on this worker task cannot interrupt it.
            hook_task = asyncio.create_task(wm.run_after_run_hook(workspace))
            try:
                await asyncio.shield(hook_task)
            except asyncio.CancelledError:
                # Worker was cancelled while hook was running; hook_task
                # continues independently in the event loop.
                raise

    async def _handle_agent_event(
        self,
        issue_id: str,
        entry: RunningEntry,
        event: AgentEvent,
    ) -> None:
        """Update live session from an agent event (spec §13.5)."""
        session = entry.session
        session.last_event = event.event
        session.last_event_timestamp = event.timestamp

        # Advance status on first event from the agent process
        if event.event == AgentEventType.SESSION_STARTED:
            entry.status = RunStatus.INITIALIZING_SESSION
        elif event.event == AgentEventType.NOTIFICATION:
            entry.status = RunStatus.STREAMING_TURN

        if event.session_id:
            session.session_id = event.session_id
        if event.pid:
            session.pid = event.pid
        if event.message:
            session.last_message = event.message

        # Detect TodoWrite tool calls in raw assistant events and sync to Linear
        raw = event.raw
        if raw and raw.get("type") == "assistant":
            message = raw.get("message") or {}
            content = message.get("content") or []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "TodoWrite"
                ):
                    todos = (block.get("input") or {}).get("todos") or []
                    if todos:
                        self._sync_todo_comment(issue_id, entry, todos)

        # Token accounting (spec §13.5)
        if event.usage:
            inp = event.usage.get("input_tokens", 0)
            out = event.usage.get("output_tokens", 0)
            total = inp + out

            # Track deltas to avoid double-counting
            inp_delta = max(inp - session.last_reported_input_tokens, 0)
            out_delta = max(out - session.last_reported_output_tokens, 0)

            session.last_reported_input_tokens = max(inp, session.last_reported_input_tokens)
            session.last_reported_output_tokens = max(out, session.last_reported_output_tokens)
            session.last_reported_total_tokens = max(total, session.last_reported_total_tokens)

            session.input_tokens += inp_delta
            session.output_tokens += out_delta
            session.total_tokens += inp_delta + out_delta

            self._state.codex_totals.input_tokens += inp_delta
            self._state.codex_totals.output_tokens += out_delta
            self._state.codex_totals.total_tokens += inp_delta + out_delta

    # ------------------------------------------------------------------
    # Worker exit (spec §16.6)
    # ------------------------------------------------------------------

    async def _on_worker_done(
        self,
        issue_id: str,
        identifier: str,
        entry: RunningEntry,
        task: asyncio.Task,  # type: ignore[type-arg]
    ) -> None:
        """Handle worker task completion (spec §16.6)."""
        elapsed = (_now_utc() - entry.started_at).total_seconds()
        self._state.codex_totals.seconds_running += elapsed
        self._state.running.pop(issue_id, None)

        exc = task.exception() if not task.cancelled() else None

        if task.cancelled() or (exc and isinstance(exc, asyncio.CancelledError)):
            # Cancelled by reconciliation — do not retry
            entry.status = RunStatus.CANCELED
            issue_log(
                logger, logging.INFO,
                "worker_cancelled",
                issue_id, identifier,
            )
            return

        if exc is None:
            # Normal exit → move to "In Review" if still in an active state,
            # then schedule continuation retry (spec §8.4)
            entry.status = RunStatus.SUCCEEDED
            self._state.completed.add(issue_id)
            issue_log(
                logger, logging.INFO,
                "worker_exited_normal",
                issue_id, identifier,
            )
            active_lower = [s.lower() for s in self._config.tracker.active_states]
            if entry.issue.state.lower() in active_lower:
                self._transition_issue_state(issue_id, "In Review")
            await self._schedule_retry(
                issue_id, identifier,
                attempt=1,
                delay_ms=_CONTINUATION_RETRY_DELAY_MS,
                error=None,
            )
        else:
            # Abnormal exit → exponential backoff retry
            entry.status = RunStatus.FAILED
            next_attempt = _next_attempt(entry.retry_attempt)
            error_str = str(exc)[:200]
            issue_log(
                logger, logging.WARNING,
                "worker_exited_abnormal",
                issue_id, identifier,
                attempt=next_attempt,
                error=error_str,
            )
            await self._schedule_retry(
                issue_id, identifier,
                attempt=next_attempt,
                error=error_str,
            )

    # ------------------------------------------------------------------
    # Retry scheduling (spec §8.4)
    # ------------------------------------------------------------------

    async def _schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Schedule a retry for an issue (spec §8.4)."""
        # Cancel existing retry timer
        existing = self._state.retry_attempts.pop(issue_id, None)
        if existing:
            logger.debug(
                f"action=retry_cancelled_for_new "
                f"issue_id={issue_id} identifier={identifier}"
            )

        if delay_ms is None:
            delay_ms = _backoff_delay_ms(
                attempt, self._config.agent.max_retry_backoff_ms
            )

        due_at_ms = _monotonic_ms() + delay_ms
        self._state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            error=error,
        )

        issue_log(
            logger, logging.INFO,
            "retry_scheduled",
            issue_id, identifier,
            attempt=attempt,
            delay_ms=delay_ms,
            error=error,
        )

        asyncio.get_event_loop().call_later(
            delay_ms / 1000.0,
            lambda: asyncio.create_task(self._on_retry_timer(issue_id)),
        )

    async def _on_retry_timer(self, issue_id: str) -> None:
        """Handle retry timer firing (spec §16.6)."""
        retry_entry = self._state.retry_attempts.pop(issue_id, None)
        if not retry_entry:
            return

        try:
            client = LinearClient(self._config.tracker)
            candidates = await client.fetch_candidate_issues()
        except Exception as exc:
            logger.warning(
                f"action=retry_poll_failed "
                f"issue_id={issue_id} error={exc}"
            )
            await self._schedule_retry(
                issue_id,
                retry_entry.identifier,
                attempt=retry_entry.attempt + 1,
                error="retry poll failed",
            )
            return

        issue = next((i for i in candidates if i.id == issue_id), None)

        if issue is None:
            # No longer a candidate — release claim
            self._state.claimed.discard(issue_id)
            issue_log(
                logger, logging.INFO,
                "retry_claim_released_not_found",
                issue_id, retry_entry.identifier,
            )
            return

        if not self._should_dispatch(issue):
            # No longer active — release
            self._state.claimed.discard(issue_id)
            issue_log(
                logger, logging.INFO,
                "retry_claim_released_inactive",
                issue_id, retry_entry.identifier,
                state=issue.state,
            )
            return

        if not self._has_slots():
            await self._schedule_retry(
                issue_id,
                issue.identifier,
                attempt=retry_entry.attempt + 1,
                error="no available orchestrator slots",
            )
            return

        await self._dispatch_issue(issue, attempt=retry_entry.attempt)

    # ------------------------------------------------------------------
    # Snapshot (for HTTP server)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return runtime snapshot for observability (spec §13.3)."""
        now = _now_utc()
        live_seconds = sum(
            (now - e.started_at).total_seconds()
            for e in self._state.running.values()
        )
        totals = self._state.codex_totals
        wm = WorkspaceManager(self._config)

        running_rows = []
        for issue_id, entry in self._state.running.items():
            s = entry.session
            running_rows.append({
                "issue_id": issue_id,
                "issue_identifier": entry.identifier,
                "state": entry.issue.state,
                "run_status": entry.status.value,
                "session_id": s.session_id,
                "turn_count": s.turn_count,
                "last_event": s.last_event.value if s.last_event else None,
                "last_message": s.last_message or "",
                "started_at": entry.started_at.isoformat(),
                "last_event_at": s.last_event_timestamp.isoformat()
                    if s.last_event_timestamp else None,
                "retry_attempt": entry.retry_attempt,
                "workspace_path": str(wm.get_path(entry.identifier)),
                "tokens": {
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "total_tokens": s.total_tokens,
                },
            })

        retrying_rows = []
        for issue_id, retry in self._state.retry_attempts.items():
            remaining_ms = max(retry.due_at_ms - _monotonic_ms(), 0)
            due_at = now + timedelta(milliseconds=remaining_ms)
            retrying_rows.append({
                "issue_id": issue_id,
                "issue_identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at": due_at.isoformat(),
                "error": retry.error,
            })

        return {
            "generated_at": now.isoformat(),
            "counts": {
                "running": len(running_rows),
                "retrying": len(retrying_rows),
            },
            "running": running_rows,
            "retrying": retrying_rows,
            "codex_totals": {
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "total_tokens": totals.total_tokens,
                "seconds_running": round(totals.seconds_running + live_seconds, 2),
            },
            "rate_limits": self._state.codex_rate_limits,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
    """Sort issues for dispatch (spec §8.2): priority asc, then oldest first."""
    def _key(i: Issue):
        prio = i.priority if i.priority is not None else 9999
        created = i.created_at.timestamp() if i.created_at else float("inf")
        return (prio, created, i.identifier)

    return sorted(issues, key=_key)


def _next_attempt(current: int | None) -> int:
    if current is None:
        return 1
    return current + 1


def _backoff_delay_ms(attempt: int, max_backoff_ms: int) -> float:
    """Exponential backoff: min(10000 * 2^(attempt-1), max_backoff_ms) (spec §8.4)."""
    delay = 10_000.0 * (2 ** (attempt - 1))
    return min(delay, float(max_backoff_ms))
