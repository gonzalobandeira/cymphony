"""QA review decision parsing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import ReviewDecision, ReviewResult

logger = logging.getLogger(__name__)

REVIEW_RESULT_FILENAME = "REVIEW_RESULT.json"
_MISSING_FILE_PREFIX = "Review result file not found: "

_VALID_DECISIONS = {d.value for d in ReviewDecision}


def parse_review_result(workspace_path: str) -> ReviewResult:
    """Read and validate the QA review result file from a workspace."""
    result_path = Path(workspace_path) / REVIEW_RESULT_FILENAME

    if not result_path.exists():
        error = f"{_MISSING_FILE_PREFIX}{result_path}"
        logger.warning(f"action=review_result_missing path={result_path}")
        return ReviewResult(decision=None, error=error)

    try:
        raw_text = result_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        error = f"Failed to read review result file: {exc}"
        logger.warning(f"action=review_result_read_error path={result_path} error={exc}")
        return ReviewResult(decision=None, error=error)

    if not raw_text:
        error = "Review result file is empty"
        logger.warning(f"action=review_result_empty path={result_path}")
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        error = f"Review result is not valid JSON: {exc}"
        logger.warning(f"action=review_result_invalid_json path={result_path} error={exc}")
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    if not isinstance(data, dict):
        error = f"Review result must be a JSON object, got {type(data).__name__}"
        logger.warning(f"action=review_result_not_object path={result_path}")
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    raw_decision = data.get("decision")
    if raw_decision is None:
        error = "Review result missing required 'decision' field"
        logger.warning(f"action=review_result_missing_decision path={result_path}")
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    if not isinstance(raw_decision, str):
        error = f"Review result 'decision' must be a string, got {type(raw_decision).__name__}"
        logger.warning(f"action=review_result_decision_wrong_type path={result_path}")
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    normalized = raw_decision.strip().lower()
    if normalized not in _VALID_DECISIONS:
        error = (
            f"Review result 'decision' is {raw_decision!r}, "
            f"expected one of: {', '.join(sorted(_VALID_DECISIONS))}"
        )
        logger.warning(
            "action=review_result_invalid_decision "
            f"path={result_path} decision={raw_decision!r}"
        )
        return ReviewResult(decision=None, error=error, raw_output=raw_text)

    summary = data.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = str(summary)

    decision = ReviewDecision(normalized)
    logger.info(
        "action=review_result_parsed "
        f"decision={decision.value} has_summary={summary is not None}"
    )
    return ReviewResult(decision=decision, summary=summary, raw_output=raw_text)


def is_review_result_missing(error: str | None) -> bool:
    """Return whether a review-result error means the artifact was never written."""
    return bool(error and error.startswith(_MISSING_FILE_PREFIX))
