"""Structured logging utilities for Cymphony (spec §13.1)."""

from __future__ import annotations

import logging

# Root logger for Cymphony — all modules use child loggers
log = logging.getLogger("cymphony")


def issue_log(
    logger: logging.Logger,
    level: int,
    action: str,
    issue_id: str,
    issue_identifier: str,
    **extra: object,
) -> None:
    """Emit a structured log line with issue context (spec §13.1)."""
    parts = [f"action={action}", f"issue_id={issue_id}", f"issue_identifier={issue_identifier}"]
    for k, v in extra.items():
        parts.append(f"{k}={v!r}" if isinstance(v, str) and " " in v else f"{k}={v}")
    logger.log(level, " ".join(parts))


def session_log(
    logger: logging.Logger,
    level: int,
    action: str,
    issue_id: str,
    issue_identifier: str,
    session_id: str,
    **extra: object,
) -> None:
    """Emit a structured log line with session context (spec §13.1)."""
    parts = [
        f"action={action}",
        f"issue_id={issue_id}",
        f"issue_identifier={issue_identifier}",
        f"session_id={session_id}",
    ]
    for k, v in extra.items():
        parts.append(f"{k}={v!r}" if isinstance(v, str) and " " in v else f"{k}={v}")
    logger.log(level, " ".join(parts))
