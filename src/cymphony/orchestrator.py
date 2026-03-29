"""Orchestrator: poll loop, dispatch, reconciliation, retry (spec §7, §8, §14, §16)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent import create_runner
from .config import ServiceConfig, build_config, validate_dispatch_config
from .linear import LinearClient
from .logging_ import issue_log, session_log
from .models import (
    AgentEvent,
    AgentEventType,
    AgentError,
    ControlAction,
    CodexTotals,
    Issue,
    LiveSession,
    OrchestratorState,
    ProblemRecord,
    RetryEntry,
    RunningEntry,
    RunStatus,
    SkippedEntry,
    WorkflowDefinition,
    WorkflowError,
)
from .workflow import WorkflowWatcher, load_workflow, render_plan_prompt, render_prompt
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)

_MAX_RECENT_EVENTS = 12


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


# Continuation retry delay after clean exit (spec §8.4)
_CONTINUATION_RETRY_DELAY_MS = 1000.0
_MAX_RECENT_PROBLEMS = 25
_CONTROL_HISTORY_LIMIT = 50


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
        self._state_id_cache: dict[tuple[str, str], str] = {}  # (team_id, state_name) → Linear state ID
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
            self._server = await start_server(
                self,
                self._config.server.port,
                self._workflow_path,
            )

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
                self._state.last_validation_errors = list(validation.errors)
                for err in validation.errors:
                    self._record_problem(
                        kind="invalid_config",
                        summary="Dispatch configuration is invalid",
                        detail=err,
                    )
                for err in validation.errors:
                    logger.error(
                        f"action=dispatch_validation_failed error={err!r}"
                    )
                return
            self._state.last_validation_errors = []

            # 3. Fetch candidate issues
            try:
                client = LinearClient(self._config.tracker)
                issues = await client.fetch_candidate_issues()
            except Exception as exc:
                self._state.last_candidates = []
                self._record_problem(
                    kind="fetch_candidates_failed",
                    summary="Failed to refresh candidate issues",
                    detail=str(exc),
                )
                logger.error(f"action=fetch_candidates_failed error={exc}")
                return
            self._state.last_candidates = list(issues)

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
        if self._is_shutting_down():
            return True
        return self._enqueue_tick(delay_ms=0.0)

    def trigger_refresh(self) -> dict[str, Any]:
        """Queue an immediate refresh and record the operator action."""
        coalesced = self.request_immediate_poll()
        detail = "coalesced with pending tick" if coalesced else "tick queued"
        self._record_control(
            action="refresh",
            scope="global",
            outcome="accepted",
            detail=detail,
        )
        return {
            "ok": True,
            "action": "refresh",
            "scope": "global",
            "coalesced": coalesced,
            "detail": detail,
        }

    def pause_dispatching(self) -> dict[str, Any]:
        """Pause new dispatches while leaving active workers alone."""
        already_paused = self._state.dispatch_paused
        self._state.dispatch_paused = True
        outcome = "noop" if already_paused else "accepted"
        detail = "dispatching already paused" if already_paused else "dispatching paused"
        self._record_control(
            action="pause_dispatching",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "pause_dispatching",
            "scope": "global",
            "already_paused": already_paused,
            "detail": detail,
        }

    def resume_dispatching(self) -> dict[str, Any]:
        """Resume dispatching and immediately poll for eligible work."""
        was_paused = self._state.dispatch_paused
        self._state.dispatch_paused = False
        refresh = self.request_immediate_poll()
        outcome = "accepted" if was_paused else "noop"
        detail = (
            "dispatching resumed; refresh queued"
            if was_paused
            else "dispatching already active; refresh queued"
        )
        self._record_control(
            action="resume_dispatching",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "resume_dispatching",
            "scope": "global",
            "was_paused": was_paused,
            "refresh_coalesced": refresh,
            "detail": detail,
        }

    async def shutdown_app(self) -> dict[str, Any]:
        """Stop dispatching, cancel active work, and shut the orchestrator down."""
        already_requested = self._is_shutting_down()
        self._state.dispatch_paused = True
        self._state.shutdown_requested = True

        if self._tick_handle is not None and not self._tick_handle.cancelled():
            self._tick_handle.cancel()
        self._tick_handle = None
        self._tick_due_at_ms = None
        self._tick_rerun_requested = False
        self._state.retry_attempts.clear()

        running_issue_ids = list(self._state.running.keys())
        for issue_id in running_issue_ids:
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._shutdown_event.set()
        outcome = "noop" if already_requested else "accepted"
        detail = (
            "shutdown already requested"
            if already_requested
            else "shutdown requested; running workers cancelled"
        )
        self._record_control(
            action="shutdown_app",
            scope="global",
            outcome=outcome,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "shutdown_app",
            "scope": "global",
            "already_requested": already_requested,
            "cancelled_workers": len(running_issue_ids),
            "detail": detail,
        }

    async def cancel_worker(self, identifier: str) -> dict[str, Any]:
        """Cancel a running worker without requeueing it automatically."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match or match["kind"] != "running":
            detail = "issue is not currently running"
            self._record_control(
                action="cancel_worker",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "cancel_worker",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        await self._terminate_running_issue(issue_id, cleanup_workspace=False)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.retry_attempts.pop(issue_id, None)
        detail = "worker cancelled and issue released"
        self._record_control(
            action="cancel_worker",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "cancel_worker",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "detail": detail,
        }

    async def requeue_issue(self, identifier: str) -> dict[str, Any]:
        """Release an issue from manual holds and ask the scheduler to pick it up again."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match:
            detail = "issue is not currently tracked"
            self._record_control(
                action="requeue_issue",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "requeue_issue",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        if match["kind"] == "running":
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._state.retry_attempts.pop(issue_id, None)
        self._state.skipped.pop(issue_id, None)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        coalesced = self.request_immediate_poll()
        detail = "issue released for redispatch"
        self._record_control(
            action="requeue_issue",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "requeue_issue",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "refresh_coalesced": coalesced,
            "detail": detail,
        }

    async def skip_issue(self, identifier: str) -> dict[str, Any]:
        """Hold an issue out of dispatch until an operator requeues it."""
        issue_identifier = identifier.upper()
        match = self._find_tracked_issue(issue_identifier)
        if not match:
            detail = "issue is not currently tracked"
            self._record_control(
                action="skip_issue",
                scope="issue",
                outcome="rejected",
                issue_identifier=issue_identifier,
                detail=detail,
            )
            return {
                "ok": False,
                "action": "skip_issue",
                "scope": "issue",
                "issue_identifier": issue_identifier,
                "detail": detail,
            }

        issue_id = str(match["issue_id"])
        if match["kind"] == "running":
            await self._terminate_running_issue(issue_id, cleanup_workspace=False)

        self._state.retry_attempts.pop(issue_id, None)
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.skipped[issue_id] = SkippedEntry(
            issue_id=issue_id,
            identifier=issue_identifier,
            created_at=_now_utc(),
            reason="operator_skip",
        )
        detail = "issue marked as skipped"
        self._record_control(
            action="skip_issue",
            scope="issue",
            outcome="accepted",
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        return {
            "ok": True,
            "action": "skip_issue",
            "scope": "issue",
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "detail": detail,
        }

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
                    self._record_problem(
                        kind="inactive_state",
                        summary=f"Issue moved to inactive state {refreshed_issue.state!r}",
                        detail="Work was stopped by reconciliation because the issue is no longer in an active workflow state.",
                        issue_id=issue_id,
                        issue_identifier=entry.identifier,
                    )
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
        entry.session.latest_plan = body
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
                team_id = await client.fetch_issue_team_id(issue_id)
                if not team_id:
                    logger.warning(
                        f"action=state_transition_skipped issue_id={issue_id} "
                        f"state={state_name!r} reason=team_id_not_found"
                    )
                    return

                cache_key = (team_id, state_name.lower())
                state_id = self._state_id_cache.get(cache_key)
                if not state_id:
                    state_id = await client.fetch_team_workflow_state_id(team_id, state_name)
                if not state_id:
                    logger.warning(
                        f"action=state_transition_skipped issue_id={issue_id} "
                        f"state={state_name!r} team_id={team_id} reason=state_id_not_found"
                    )
                    return

                if cache_key not in self._state_id_cache:
                    self._state_id_cache[cache_key] = state_id
                await client.set_issue_state(issue_id, state_id)
                logger.info(
                    f"action=issue_state_set issue_id={issue_id} state={state_name!r} "
                    f"team_id={team_id}"
                )
            except Exception as exc:
                self._record_problem(
                    kind="transition_failed",
                    summary=f"State transition to {state_name!r} failed",
                    detail=str(exc),
                    issue_id=issue_id,
                )
                logger.warning(
                    f"action=state_transition_failed issue_id={issue_id} "
                    f"state={state_name!r} error={exc}"
                )

        asyncio.create_task(_do())

    # ------------------------------------------------------------------
    # Dispatch (spec §8.2, §16.4)
    # ------------------------------------------------------------------

    def _has_slots(self) -> bool:
        if self._state.dispatch_paused or self._is_shutting_down():
            return False
        global_available = max(
            self._state.max_concurrent_agents - len(self._state.running), 0
        )
        return global_available > 0

    def _is_dispatch_eligible(self, issue: Issue) -> bool:
        """Check non-slot dispatch eligibility (spec §8.2)."""
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
        if issue.id in self._state.skipped:
            return False

        # Blocker check for active work that should not run with unresolved dependencies.
        if state_lower in {"todo", "in progress"}:
            terminal_lower_set = set(terminal_lower)
            for blocker in issue.blocked_by:
                blocker_state = (blocker.state or "").lower()
                if blocker_state not in terminal_lower_set:
                    return False

        return True

    def _has_state_slot(self, issue: Issue) -> bool:
        """Check per-state slot availability for an issue."""
        state_lower = issue.state.lower()
        per_state = self._config.agent.max_concurrent_agents_by_state
        if state_lower not in per_state:
            return True

        state_count = sum(
            1 for e in self._state.running.values()
            if e.issue.state.lower() == state_lower
        )
        return state_count < per_state[state_lower]

    def _should_dispatch(self, issue: Issue) -> bool:
        """Check dispatch eligibility including slot availability (spec §8.2)."""
        return (
            self._is_dispatch_eligible(issue)
            and self._has_slots()
            and self._has_state_slot(issue)
        )

    def _is_continuation_retry(self, retry_entry: RetryEntry) -> bool:
        """Continuation retries come from a clean worker exit, not an error path."""
        return retry_entry.error is None

    async def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        """Claim and spawn worker for issue (spec §16.4)."""
        if self._is_shutting_down():
            return
        self._state.claimed.add(issue.id)
        self._state.completed.discard(issue.id)
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
        agent = create_runner(self._config.coding_agent)

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

        event_row = {
            "event": event.event.value,
            "timestamp": event.timestamp.isoformat(),
        }
        if event.message:
            event_row["message"] = event.message
        if event.usage:
            event_row["usage"] = {
                "input_tokens": event.usage.get("input_tokens", 0),
                "output_tokens": event.usage.get("output_tokens", 0),
                "cache_read_input_tokens": event.usage.get("cache_read_input_tokens", 0),
            }
        session.recent_events.append(event_row)
        if len(session.recent_events) > _MAX_RECENT_EVENTS:
            session.recent_events = session.recent_events[-_MAX_RECENT_EVENTS:]

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
                entry=entry,
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
                entry=entry,
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
        entry: RunningEntry | None = None,
    ) -> None:
        """Schedule a retry for an issue (spec §8.4)."""
        is_continuation = error is None

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
        retry_entry = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            error=error,
        )
        if entry is not None:
            retry_entry.state = entry.issue.state
            retry_entry.run_status = entry.status.value
            retry_entry.session_id = entry.session.session_id
            retry_entry.turn_count = entry.session.turn_count
            retry_entry.last_event = (
                entry.session.last_event.value if entry.session.last_event else None
            )
            retry_entry.last_message = entry.session.last_message
            retry_entry.last_event_at = entry.session.last_event_timestamp
            retry_entry.workspace_path = str(WorkspaceManager(self._config).get_path(entry.identifier))
            retry_entry.tokens = {
                "input_tokens": entry.session.input_tokens,
                "output_tokens": entry.session.output_tokens,
                "total_tokens": entry.session.total_tokens,
            }
            retry_entry.started_at = entry.started_at
            retry_entry.retry_attempt = entry.retry_attempt
            retry_entry.plan_comment_id = entry.session.plan_comment_id
            retry_entry.latest_plan = entry.session.latest_plan
            retry_entry.recent_events = list(entry.session.recent_events)
            retry_entry.issue_title = entry.issue.title
            retry_entry.issue_url = entry.issue.url
            retry_entry.issue_description = entry.issue.description
            retry_entry.issue_labels = list(entry.issue.labels)
            retry_entry.issue_comments = [
                {
                    "author": comment.author,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None,
                }
                for comment in entry.issue.comments
            ]
        elif existing is not None:
            retry_entry.state = existing.state
            retry_entry.run_status = existing.run_status
            retry_entry.session_id = existing.session_id
            retry_entry.turn_count = existing.turn_count
            retry_entry.last_event = existing.last_event
            retry_entry.last_message = existing.last_message
            retry_entry.last_event_at = existing.last_event_at
            retry_entry.workspace_path = existing.workspace_path
            retry_entry.tokens = dict(existing.tokens)
            retry_entry.started_at = existing.started_at
            retry_entry.retry_attempt = existing.retry_attempt
            retry_entry.plan_comment_id = existing.plan_comment_id
            retry_entry.latest_plan = existing.latest_plan
            retry_entry.recent_events = list(existing.recent_events)
            retry_entry.issue_title = existing.issue_title
            retry_entry.issue_url = existing.issue_url
            retry_entry.issue_description = existing.issue_description
            retry_entry.issue_labels = list(existing.issue_labels)
            retry_entry.issue_comments = list(existing.issue_comments)
        self._state.retry_attempts[issue_id] = retry_entry

        if is_continuation:
            issue_log(
                logger, logging.INFO,
                "continuation_retry_scheduled",
                issue_id, identifier,
                attempt=attempt,
                delay_ms=delay_ms,
            )
        else:
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
        if self._is_shutting_down():
            self._state.retry_attempts.pop(issue_id, None)
            return
        retry_entry = self._state.retry_attempts.pop(issue_id, None)
        if not retry_entry:
            return

        is_continuation = self._is_continuation_retry(retry_entry)

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
            self._state.completed.discard(issue_id)
            issue_log(
                logger, logging.INFO,
                "retry_claim_released_not_found",
                issue_id, retry_entry.identifier,
            )
            return

        # Retry timers re-open eligibility by clearing bookkeeping guards before
        # checking whether the issue is still a valid candidate.
        self._state.claimed.discard(issue_id)
        self._state.completed.discard(issue_id)

        if not self._is_dispatch_eligible(issue):
            # No longer active — release
            self._state.claimed.discard(issue_id)
            self._state.completed.discard(issue_id)
            issue_log(
                logger, logging.INFO,
                "continuation_retry_released_inactive"
                if is_continuation else "retry_claim_released_inactive",
                issue_id, retry_entry.identifier,
                state=issue.state,
            )
            return

        if not self._has_slots() or not self._has_state_slot(issue):
            if is_continuation:
                issue_log(
                    logger, logging.INFO,
                    "continuation_retry_waiting_for_slot",
                    issue_id, retry_entry.identifier,
                )
                await self._schedule_retry(
                    issue_id,
                    issue.identifier,
                    attempt=retry_entry.attempt,
                    delay_ms=_CONTINUATION_RETRY_DELAY_MS,
                    error=None,
                )
            else:
                await self._schedule_retry(
                    issue_id,
                    issue.identifier,
                    attempt=retry_entry.attempt + 1,
                    error="no available orchestrator slots",
                )
            return

        issue_log(
            logger, logging.INFO,
            "continuation_retry_redispatching"
            if is_continuation else "retry_dispatching",
            issue_id, retry_entry.identifier,
            attempt=retry_entry.attempt,
        )
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
        for entry in self._state.running.values():
            running_rows.append(self._snapshot_running_entry(entry, wm))

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
                "state": retry.state,
                "run_status": retry.run_status,
                "session_id": retry.session_id,
                "turn_count": retry.turn_count,
                "last_event": retry.last_event,
                "last_message": retry.last_message or "",
                "last_event_at": retry.last_event_at.isoformat() if retry.last_event_at else None,
                "workspace_path": retry.workspace_path,
                "tokens": retry.tokens,
                "started_at": retry.started_at.isoformat() if retry.started_at else None,
                "retry_attempt": retry.retry_attempt,
                "plan_comment_id": retry.plan_comment_id,
                "latest_plan": retry.latest_plan,
                "recent_events": retry.recent_events,
                "issue_title": retry.issue_title,
                "issue_url": retry.issue_url,
                "issue_description": retry.issue_description,
                "issue_labels": retry.issue_labels,
                "issue_comments": retry.issue_comments,
            })

        waiting_rows = self._build_waiting_rows(now)
        problem_rows = [
            {
                "kind": problem.kind,
                "summary": problem.summary,
                "detail": problem.detail,
                "issue_id": problem.issue_id,
                "issue_identifier": problem.issue_identifier,
                "observed_at": problem.observed_at.isoformat(),
            }
            for problem in self._state.recent_problems
        ]
        skipped_rows = []
        for issue_id, skipped in self._state.skipped.items():
            skipped_rows.append({
                "issue_id": issue_id,
                "issue_identifier": skipped.identifier,
                "reason": skipped.reason,
                "created_at": skipped.created_at.isoformat(),
            })

        control_rows = []
        for action in self._state.control_actions:
            control_rows.append({
                "timestamp": action.timestamp.isoformat(),
                "action": action.action,
                "scope": action.scope,
                "outcome": action.outcome,
                "issue_id": action.issue_id,
                "issue_identifier": action.issue_identifier,
                "detail": action.detail,
            })

        return {
            "generated_at": now.isoformat(),
            "counts": {
                "running": len(running_rows),
                "retrying": len(retrying_rows),
                "waiting": len(waiting_rows),
                "problems": len(problem_rows),
                "skipped": len(skipped_rows),
            },
            "controls": {
                "dispatch_paused": self._state.dispatch_paused,
                "shutdown_requested": self._state.shutdown_requested,
                "recent_actions": control_rows,
            },
            "running": running_rows,
            "retrying": retrying_rows,
            "waiting": waiting_rows,
            "problems": problem_rows,
            "skipped": skipped_rows,
            "codex_totals": {
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "total_tokens": totals.total_tokens,
                "seconds_running": round(totals.seconds_running + live_seconds, 2),
            },
            "rate_limits": self._state.codex_rate_limits,
        }

    def _record_problem(
        self,
        *,
        kind: str,
        summary: str,
        detail: str,
        issue_id: str | None = None,
        issue_identifier: str | None = None,
    ) -> None:
        if issue_identifier is None and issue_id:
            entry = self._state.running.get(issue_id)
            if entry:
                issue_identifier = entry.identifier
            else:
                retry = self._state.retry_attempts.get(issue_id)
                if retry:
                    issue_identifier = retry.identifier

        self._state.recent_problems.insert(
            0,
            ProblemRecord(
                kind=kind,
                summary=summary,
                detail=detail,
                observed_at=_now_utc(),
                issue_id=issue_id,
                issue_identifier=issue_identifier,
            ),
        )
        del self._state.recent_problems[_MAX_RECENT_PROBLEMS:]

    def _build_waiting_rows(self, now: datetime) -> list[dict[str, Any]]:
        waiting_rows: list[dict[str, Any]] = []

        for issue in _sort_for_dispatch(self._state.last_candidates):
            if issue.id in self._state.running:
                continue

            waiting_row = self._build_waiting_row(issue)
            if waiting_row is not None:
                waiting_rows.append(waiting_row)

        for issue_id, retry in self._state.retry_attempts.items():
            remaining_ms = max(retry.due_at_ms - _monotonic_ms(), 0)
            due_at = now + timedelta(milliseconds=remaining_ms)
            waiting_rows.append(
                {
                    "issue_id": issue_id,
                    "issue_identifier": retry.identifier,
                    "state": None,
                    "kind": "waiting_for_retry",
                    "summary": "Waiting for retry timer",
                    "detail": retry.error or "Waiting for continuation retry",
                    "attempt": retry.attempt,
                    "due_at": due_at.isoformat(),
                }
            )

        waiting_rows.sort(
            key=lambda row: (row.get("issue_identifier") or "", row.get("kind") or "")
        )
        return waiting_rows

    def _build_waiting_row(self, issue: Issue) -> dict[str, Any] | None:
        terminal_lower = {s.lower() for s in self._config.tracker.terminal_states}
        active_lower = {s.lower() for s in self._config.tracker.active_states}
        state_lower = issue.state.lower()

        row = {
            "issue_id": issue.id,
            "issue_identifier": issue.identifier,
            "state": issue.state,
            "attempt": None,
            "due_at": None,
        }

        if not (issue.id and issue.identifier and issue.title and issue.state):
            return {
                **row,
                "kind": "invalid_issue",
                "summary": "Issue data is incomplete",
                "detail": "Required issue fields are missing, so dispatch is skipped.",
            }

        if issue.id in self._state.retry_attempts:
            return None

        if state_lower not in active_lower:
            return {
                **row,
                "kind": "inactive_state",
                "summary": f"Issue is in inactive state {issue.state!r}",
                "detail": "Dispatch only runs for configured active states.",
            }

        if state_lower in terminal_lower:
            return {
                **row,
                "kind": "terminal_state",
                "summary": f"Issue is in terminal state {issue.state!r}",
                "detail": "Terminal issues are not eligible for dispatch.",
            }

        if issue.id in self._state.claimed:
            return {
                **row,
                "kind": "claimed",
                "summary": "Issue is already claimed",
                "detail": "The orchestrator has already reserved this issue for work.",
            }

        if state_lower in {"todo", "in progress"}:
            blockers = [
                blocker for blocker in issue.blocked_by
                if (blocker.state or "").lower() not in terminal_lower
            ]
            if blockers:
                blocker_desc = ", ".join(
                    f"{blocker.identifier or blocker.id or 'unknown'} ({blocker.state or 'unknown'})"
                    for blocker in blockers
                )
                return {
                    **row,
                    "kind": "blocked_by_dependency",
                    "summary": "Blocked by dependency",
                    "detail": blocker_desc,
                }

        if self._state.dispatch_paused:
            return {
                **row,
                "kind": "dispatch_paused",
                "summary": "Dispatching is paused",
                "detail": "Resume dispatching to make this issue eligible again.",
            }

        if not self._has_slots():
            capacity = self._state.max_concurrent_agents
            in_use = len(self._state.running)
            return {
                **row,
                "kind": "no_slots_available",
                "summary": "No global orchestrator slots available",
                "detail": f"{in_use}/{capacity} slots are currently in use.",
            }

        if not self._has_state_slot(issue):
            state_key = issue.state.lower()
            capacity = self._config.agent.max_concurrent_agents_by_state.get(state_key)
            in_use = sum(
                1 for entry in self._state.running.values()
                if entry.issue.state.lower() == state_key
            )
            return {
                **row,
                "kind": "no_state_slots_available",
                "summary": f"No slots available for state {issue.state!r}",
                "detail": (
                    f"{in_use}/{capacity} slots are currently in use for this state."
                    if capacity is not None
                    else "The per-state concurrency limit is currently saturated."
                ),
            }

        return None

    def _find_tracked_issue(self, identifier: str) -> dict[str, str] | None:
        """Locate an issue in running, retry, or skipped collections by identifier."""
        target = identifier.upper()
        for issue_id, entry in self._state.running.items():
            if entry.identifier.upper() == target:
                return {"kind": "running", "issue_id": issue_id}
        for issue_id, retry in self._state.retry_attempts.items():
            if retry.identifier.upper() == target:
                return {"kind": "retrying", "issue_id": issue_id}
        for issue_id, skipped in self._state.skipped.items():
            if skipped.identifier.upper() == target:
                return {"kind": "skipped", "issue_id": issue_id}
        return None

    def _record_control(
        self,
        action: str,
        scope: str,
        outcome: str,
        issue_id: str | None = None,
        issue_identifier: str | None = None,
        detail: str | None = None,
    ) -> None:
        record = ControlAction(
            timestamp=_now_utc(),
            action=action,
            scope=scope,
            outcome=outcome,
            issue_id=issue_id,
            issue_identifier=issue_identifier,
            detail=detail,
        )
        self._state.control_actions.append(record)
        if len(self._state.control_actions) > _CONTROL_HISTORY_LIMIT:
            self._state.control_actions = self._state.control_actions[-_CONTROL_HISTORY_LIMIT:]

        logger.info(
            "action=operator_control "
            f"control_action={action} scope={scope} outcome={outcome} "
            f"issue_id={issue_id!r} issue_identifier={issue_identifier!r} detail={detail!r}"
        )

    def _is_shutting_down(self) -> bool:
        return self._state.shutdown_requested or self._shutdown_event.is_set()

    def _snapshot_running_entry(
        self,
        entry: RunningEntry,
        wm: WorkspaceManager,
    ) -> dict[str, Any]:
        s = entry.session
        return {
            "issue_id": entry.issue_id,
            "issue_identifier": entry.identifier,
            "issue_title": entry.issue.title,
            "issue_url": entry.issue.url,
            "issue_description": entry.issue.description,
            "issue_labels": list(entry.issue.labels),
            "issue_comments": [
                {
                    "author": comment.author,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None,
                }
                for comment in entry.issue.comments
            ],
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
            "plan_comment_id": s.plan_comment_id,
            "latest_plan": s.latest_plan,
            "recent_events": list(s.recent_events),
            "tokens": {
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "total_tokens": s.total_tokens,
            },
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
