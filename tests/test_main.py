from __future__ import annotations

import os
from pathlib import Path

from cymphony.__main__ import _dotenv_candidates, _load_dotenv


def test_dotenv_candidates_include_repo_root_for_local_config(tmp_path: Path) -> None:
    workflow_path = tmp_path / ".cymphony" / "workflow.md"
    candidates = _dotenv_candidates(workflow_path)

    assert candidates == [
        (tmp_path / ".cymphony" / ".env").resolve(),
        (tmp_path / ".env").resolve(),
    ]


def test_load_dotenv_reads_repo_root_for_local_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_path = tmp_path / ".cymphony" / "workflow.md"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("---\n---\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LINEAR_API_KEY=test-key\n", encoding="utf-8")

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    _load_dotenv(workflow_path)

    assert os.environ["LINEAR_API_KEY"] == "test-key"


def test_load_dotenv_prefers_process_env_over_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow_path = tmp_path / ".cymphony" / "workflow.md"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("---\n---\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LINEAR_API_KEY=file-value\n", encoding="utf-8")

    monkeypatch.setenv("LINEAR_API_KEY", "process-value")

    _load_dotenv(workflow_path)

    assert os.environ["LINEAR_API_KEY"] == "process-value"
