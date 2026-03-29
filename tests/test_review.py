from __future__ import annotations

import json
from pathlib import Path

import pytest

from cymphony.models import ReviewDecision
from cymphony.review import REVIEW_RESULT_FILENAME, parse_review_result


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    return str(tmp_path)


def _write_result(workspace: str, content: str) -> None:
    Path(workspace, REVIEW_RESULT_FILENAME).write_text(content, encoding="utf-8")


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


def test_missing_file_returns_error(workspace: str) -> None:
    result = parse_review_result(workspace)
    assert result.decision is None
    assert "not found" in (result.error or "")


def test_invalid_json_returns_error(workspace: str) -> None:
    _write_result(workspace, "not json")
    result = parse_review_result(workspace)
    assert result.decision is None
    assert "not valid JSON" in (result.error or "")
    assert result.raw_output == "not json"


def test_invalid_decision_returns_error(workspace: str) -> None:
    _write_result(workspace, json.dumps({"decision": "approved"}))
    result = parse_review_result(workspace)
    assert result.decision is None
    assert "approved" in (result.error or "")
