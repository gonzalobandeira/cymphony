from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cymphony.models import Comment, Issue, WorkflowDefinition
from cymphony.workflow import load_workflow, render_prompt, save_workflow


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


def test_load_workflow_reads_split_yaml_and_prompt_files(tmp_path: Path) -> None:
    config_path = tmp_path / ".cymphony" / "config.yml"
    prompts_dir = config_path.parent / "prompts"
    prompts_dir.mkdir(parents=True)
    config_path.write_text(
        "tracker:\n"
        "  kind: linear\n"
        "  api_key: $LINEAR_API_KEY\n"
        "  project_slug: test-project\n",
        encoding="utf-8",
    )
    (prompts_dir / "execution.md").write_text("Execute {{ issue.identifier }}", encoding="utf-8")
    (prompts_dir / "qa_review.md").write_text("Review {{ issue.identifier }}", encoding="utf-8")

    workflow = load_workflow(config_path)

    assert workflow.config["tracker"]["project_slug"] == "test-project"
    assert workflow.prompt_template == "Execute {{ issue.identifier }}"
    assert workflow.review_prompt_template == "Review {{ issue.identifier }}"


def test_save_workflow_writes_split_yaml_and_prompt_files(tmp_path: Path) -> None:
    config_path = tmp_path / ".cymphony" / "config.yml"
    config_path.parent.mkdir(parents=True)

    save_workflow(
        config_path,
        {
            "tracker": {
                "kind": "linear",
                "api_key": "$LINEAR_API_KEY",
                "project_slug": "test-project",
            }
        },
        "Execute {{ issue.identifier }}",
        "Review {{ issue.identifier }}",
    )

    saved = load_workflow(config_path)

    assert saved.prompt_template == "Execute {{ issue.identifier }}"
    assert saved.review_prompt_template == "Review {{ issue.identifier }}"
    assert (config_path.parent / "prompts" / "execution.md").exists()
    assert (config_path.parent / "prompts" / "qa_review.md").exists()
