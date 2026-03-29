"""Tests for the QA review decision contract (parse_review_result).

Covers: valid decisions, edge cases (case-insensitivity, whitespace),
missing files, empty files, malformed JSON, invalid decision values,
free-text output, raw_output preservation, and summary coercion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cymphony.models import ReviewDecision, ReviewResult
from cymphony.review import REVIEW_RESULT_FILENAME, parse_review_result


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    """Return a temporary workspace path."""
    return str(tmp_path)


def _write_result(workspace: str, content: str) -> None:
    """Write content to REVIEW_RESULT.json in the workspace."""
    Path(workspace, REVIEW_RESULT_FILENAME).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Valid decisions
# ---------------------------------------------------------------------------

def test_parse_pass_decision(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "pass", "summary": "LGTM"}))
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.PASS
    assert result.summary == "LGTM"
    assert result.error is None


def test_parse_changes_requested_decision(workspace: str) -> None:
    _write_result(
        workspace,
        json.dumps({"decision": "changes_requested", "summary": "Missing tests"}),
    )
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.CHANGES_REQUESTED
    assert result.summary == "Missing tests"
    assert result.error is None


def test_parse_pass_without_summary(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "pass"}))
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.PASS
    assert result.summary is None
    assert result.error is None


def test_parse_decision_case_insensitive(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "PASS"}))
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.PASS


def test_parse_decision_with_whitespace(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "  changes_requested  "}))
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.CHANGES_REQUESTED


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_missing_file_returns_error(workspace: str) -> None:
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "not found" in result.error


# ---------------------------------------------------------------------------
# Empty file
# ---------------------------------------------------------------------------

def test_empty_file_returns_error(workspace: str) -> None:
    _write_result(workspace, "")
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "empty" in result.error


def test_whitespace_only_file_returns_error(workspace: str) -> None:
    _write_result(workspace, "   \n  ")
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "empty" in result.error


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------

def test_invalid_json_returns_error(workspace: str) -> None:
    _write_result(workspace, "not json at all")
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "not valid JSON" in result.error
    assert result.raw_output == "not json at all"


def test_json_array_returns_error(workspace: str) -> None:
    _write_result(workspace, '["pass"]')
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "JSON object" in result.error


# ---------------------------------------------------------------------------
# Missing or invalid decision field
# ---------------------------------------------------------------------------

def test_missing_decision_field_returns_error(workspace: str) -> None:
    _write_result(workspace, json.dumps({"summary": "looks good"}))
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "missing" in result.error.lower()


def test_null_decision_returns_error(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": None}))
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "missing" in result.error.lower()


def test_numeric_decision_returns_error(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": 42}))
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "string" in result.error


def test_invalid_decision_value_returns_error(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "approved"}))
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None
    assert "approved" in result.error


# ---------------------------------------------------------------------------
# Free-text output (ambiguous, should not pass)
# ---------------------------------------------------------------------------

def test_freetext_output_returns_error(workspace: str) -> None:
    """Free-text output (not JSON) must not be interpreted as a decision."""
    _write_result(workspace, "The code looks good, I approve these changes.")
    result = parse_review_result(workspace)
    assert result.decision is None
    assert result.error is not None


# ---------------------------------------------------------------------------
# raw_output is preserved
# ---------------------------------------------------------------------------

def test_raw_output_preserved_on_success(workspace: str) -> None:
    content = json.dumps({"decision": "pass", "summary": "ok"})
    _write_result(workspace, content)
    result = parse_review_result(workspace)
    assert result.raw_output == content


def test_raw_output_preserved_on_error(workspace: str) -> None:
    _write_result(workspace, '{"bad": true}')
    result = parse_review_result(workspace)
    assert result.raw_output == '{"bad": true}'


# ---------------------------------------------------------------------------
# Non-string summary is coerced
# ---------------------------------------------------------------------------

def test_non_string_summary_coerced(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "pass", "summary": 123}))
    result = parse_review_result(workspace)
    assert result.decision == ReviewDecision.PASS
    assert result.summary == "123"
