from __future__ import annotations

from datetime import datetime, timezone

from cymphony.models import Comment, Issue, WorkflowDefinition
from cymphony.workflow import render_prompt


def _build_issue(*, comments: list[Comment]) -> Issue:
    return Issue(
        id="issue-1",
        identifier="BAP-155",
        title="Either enforce read_timeout_ms or remove the dead config surface",
        project_name="Cymphony",
        description="Remove the dead config surface.",
        priority=2,
        state="Todo",
        branch_name=None,
        url="https://linear.test/BAP-155",
        labels=["improvement"],
        blocked_by=[],
        comments=comments,
        created_at=None,
        updated_at=None,
    )


def test_render_prompt_highlights_latest_qa_feedback() -> None:
    workflow = WorkflowDefinition(
        config={},
        prompt_template=(
            "Title: {{ issue.title }}\n"
            "{% if issue.latest_qa_feedback %}"
            "Reviewer Feedback To Address:\n"
            "{{ issue.latest_qa_feedback.body }}\n"
            "{% endif %}"
            "Comments:\n"
            "{% for c in issue.comments %}- {{ c.body }}\n{% endfor %}"
        ),
    )
    issue = _build_issue(
        comments=[
            Comment(
                author="Gonzalo Bandeira",
                body="**Implementation complete**",
                created_at=datetime(2026, 3, 31, 18, 48, 22, tzinfo=timezone.utc),
            ),
            Comment(
                author="Gonzalo Bandeira",
                body=(
                    "**QA review requested changes**\n"
                    "Decision: `changes_requested`\n\n"
                    "Revert unrelated deletions."
                ),
                created_at=datetime(2026, 3, 31, 19, 56, 49, tzinfo=timezone.utc),
            ),
        ],
    )

    prompt = render_prompt(workflow, issue, attempt=2)

    assert "Reviewer Feedback To Address:" in prompt
    assert "Revert unrelated deletions." in prompt
    assert "**QA review requested changes**" in prompt


def test_render_prompt_omits_feedback_section_without_qa_comment() -> None:
    workflow = WorkflowDefinition(
        config={},
        prompt_template=(
            "{% if issue.latest_qa_feedback %}HAS FEEDBACK{% else %}NO FEEDBACK{% endif %}"
        ),
    )
    issue = _build_issue(
        comments=[
            Comment(
                author="Gonzalo Bandeira",
                body="General implementation note",
                created_at=datetime(2026, 3, 31, 18, 48, 22, tzinfo=timezone.utc),
            )
        ],
    )

    prompt = render_prompt(workflow, issue, attempt=1)

    assert prompt == "NO FEEDBACK"
