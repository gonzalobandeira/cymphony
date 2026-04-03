from __future__ import annotations

from cymphony.models import Issue
from cymphony.state_machine import IssueStateMachine


def _build_issue(state: str) -> Issue:
    return Issue(
        id="issue-1",
        identifier="BAP-1",
        title="Test",
        project_name=None,
        description=None,
        priority=None,
        state=state,
        branch_name=None,
        url=None,
        labels=[],
        blocked_by=[],
        comments=[],
        created_at=None,
        updated_at=None,
    )


def test_route_todo_issue_to_execution() -> None:
    machine = IssueStateMachine()
    route = machine.route(_build_issue("Todo"))
    assert route is not None
    assert route.workflow == "execution"
    assert route.target_state == "In Progress"


def test_route_todo_issue_to_execution_case_insensitive() -> None:
    machine = IssueStateMachine()
    route = machine.route(_build_issue("toDo"))
    assert route is not None
    assert route.workflow == "execution"


def test_route_qa_review_issue_to_qa_workflow() -> None:
    machine = IssueStateMachine()
    route = machine.route(_build_issue("QA Review"))
    assert route is not None
    assert route.workflow == "qa_review"
    assert route.target_state == "QA Review"


def test_ignore_non_trigger_states() -> None:
    machine = IssueStateMachine()
    assert machine.route(_build_issue("In Progress")) is None
    assert machine.route(_build_issue("In Review")) is None
