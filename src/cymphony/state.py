"""Persist and restore orchestrator runtime state across restarts."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import RetryEntry, SkippedEntry

logger = logging.getLogger(__name__)

_STATE_VERSION = 1


def _datetime_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _iso_to_datetime(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _retry_entry_to_dict(entry: RetryEntry) -> dict[str, Any]:
    return {
        "issue_id": entry.issue_id,
        "identifier": entry.identifier,
        "attempt": entry.attempt,
        "error": entry.error,
        "state": entry.state,
        "run_status": entry.run_status,
        "session_id": entry.session_id,
        "turn_count": entry.turn_count,
        "last_event": entry.last_event,
        "last_message": entry.last_message,
        "last_event_at": _datetime_to_iso(entry.last_event_at),
        "workspace_path": entry.workspace_path,
        "tokens": dict(entry.tokens),
        "started_at": _datetime_to_iso(entry.started_at),
        "retry_attempt": entry.retry_attempt,
        "plan_comment_id": entry.plan_comment_id,
        "latest_plan": entry.latest_plan,
        "recent_events": list(entry.recent_events),
        "issue_title": entry.issue_title,
        "issue_url": entry.issue_url,
        "issue_description": entry.issue_description,
        "issue_labels": list(entry.issue_labels),
        "issue_comments": list(entry.issue_comments),
        "qa_review_bounce_count": entry.qa_review_bounce_count,
    }


def _dict_to_retry_entry(d: dict[str, Any]) -> RetryEntry:
    return RetryEntry(
        issue_id=d["issue_id"],
        identifier=d["identifier"],
        attempt=d["attempt"],
        due_at_ms=0.0,  # Will be recomputed on restore
        error=d.get("error"),
        state=d.get("state"),
        run_status=d.get("run_status"),
        session_id=d.get("session_id"),
        turn_count=d.get("turn_count", 0),
        last_event=d.get("last_event"),
        last_message=d.get("last_message"),
        last_event_at=_iso_to_datetime(d.get("last_event_at")),
        workspace_path=d.get("workspace_path"),
        tokens=d.get("tokens", {}),
        started_at=_iso_to_datetime(d.get("started_at")),
        retry_attempt=d.get("retry_attempt"),
        plan_comment_id=d.get("plan_comment_id"),
        latest_plan=d.get("latest_plan"),
        recent_events=d.get("recent_events", []),
        issue_title=d.get("issue_title"),
        issue_url=d.get("issue_url"),
        issue_description=d.get("issue_description"),
        issue_labels=d.get("issue_labels", []),
        issue_comments=d.get("issue_comments", []),
        qa_review_bounce_count=d.get("qa_review_bounce_count", 0),
    )


def _skipped_entry_to_dict(entry: SkippedEntry) -> dict[str, Any]:
    return {
        "issue_id": entry.issue_id,
        "identifier": entry.identifier,
        "created_at": entry.created_at.isoformat(),
        "reason": entry.reason,
    }


def _dict_to_skipped_entry(d: dict[str, Any]) -> SkippedEntry:
    return SkippedEntry(
        issue_id=d["issue_id"],
        identifier=d["identifier"],
        created_at=datetime.fromisoformat(d["created_at"]),
        reason=d.get("reason", "operator_skip"),
    )


class StateManager:
    """Manages persistence of orchestrator runtime state to a JSON file.

    State is saved atomically (write-to-temp + rename) to prevent corruption.
    Corrupt or missing state files are handled gracefully — the orchestrator
    starts fresh rather than crashing.
    """

    def __init__(self, state_file_path: Path) -> None:
        self._path = state_file_path

    @property
    def path(self) -> Path:
        return self._path

    def save(
        self,
        retry_attempts: dict[str, RetryEntry],
        qa_review_bounces: dict[str, int],
        skipped: dict[str, SkippedEntry],
        dispatch_paused: bool,
    ) -> None:
        """Persist current runtime state to disk atomically."""
        data: dict[str, Any] = {
            "version": _STATE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "retry_attempts": {
                issue_id: _retry_entry_to_dict(entry)
                for issue_id, entry in retry_attempts.items()
            },
            "qa_review_bounces": dict(qa_review_bounces),
            "skipped": {
                issue_id: _skipped_entry_to_dict(entry)
                for issue_id, entry in skipped.items()
            },
            "dispatch_paused": dispatch_paused,
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file in the same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".cymphony_state_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(self._path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self) -> dict[str, Any] | None:
        """Load persisted state from disk.

        Returns None if the file doesn't exist.
        Returns the parsed state dict on success.
        Raises no exceptions — logs warnings and returns None on corruption.
        """
        if not self._path.exists():
            return None

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                f"action=state_load_failed path={self._path} error={exc} "
                "(starting fresh)"
            )
            return None

        if not isinstance(data, dict):
            logger.warning(
                f"action=state_load_invalid path={self._path} "
                "reason=not_a_dict (starting fresh)"
            )
            return None

        version = data.get("version")
        if version != _STATE_VERSION:
            logger.warning(
                f"action=state_load_version_mismatch path={self._path} "
                f"expected={_STATE_VERSION} got={version} (starting fresh)"
            )
            return None

        return data

    def clear(self) -> None:
        """Remove the persisted state file."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"action=state_clear_failed path={self._path} error={exc}")

    def restore(self) -> tuple[
        dict[str, RetryEntry],
        dict[str, int],
        dict[str, SkippedEntry],
        bool,
    ]:
        """Load and deserialize persisted state.

        Returns (retry_attempts, qa_review_bounces, skipped, dispatch_paused).
        On any failure, returns empty collections and False.
        """
        data = self.load()
        if data is None:
            return {}, {}, {}, False

        retry_attempts: dict[str, RetryEntry] = {}
        qa_review_bounces: dict[str, int] = {}
        skipped: dict[str, SkippedEntry] = {}
        dispatch_paused = bool(data.get("dispatch_paused", False))

        raw_retries = data.get("retry_attempts", {})
        if isinstance(raw_retries, dict):
            for issue_id, entry_data in raw_retries.items():
                try:
                    retry_attempts[issue_id] = _dict_to_retry_entry(entry_data)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        f"action=state_restore_retry_skip issue_id={issue_id} "
                        f"error={exc}"
                    )

        raw_bounces = data.get("qa_review_bounces", {})
        if isinstance(raw_bounces, dict):
            for issue_id, count in raw_bounces.items():
                try:
                    qa_review_bounces[issue_id] = int(count)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        f"action=state_restore_qa_bounce_skip issue_id={issue_id} "
                        f"error={exc}"
                    )

        raw_skipped = data.get("skipped", {})
        if isinstance(raw_skipped, dict):
            for issue_id, entry_data in raw_skipped.items():
                try:
                    skipped[issue_id] = _dict_to_skipped_entry(entry_data)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        f"action=state_restore_skip_skip issue_id={issue_id} "
                        f"error={exc}"
                    )

        logger.info(
            f"action=state_restored path={self._path} "
            f"retry_attempts={len(retry_attempts)} qa_review_bounces={len(qa_review_bounces)} "
            f"skipped={len(skipped)} "
            f"dispatch_paused={dispatch_paused}"
        )

        return retry_attempts, qa_review_bounces, skipped, dispatch_paused
