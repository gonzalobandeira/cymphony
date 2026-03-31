from __future__ import annotations

from pathlib import Path

import pytest

from cymphony.models import HooksConfig, WorkspaceConfig
from cymphony.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_run_hook_normalizes_crlf_scripts(tmp_path: Path) -> None:
    manager = WorkspaceManager(
        type(
            "Config",
            (),
            {
                "workspace": WorkspaceConfig(root=str(tmp_path / "workspaces")),
                "hooks": HooksConfig(
                    after_create=None,
                    before_run=None,
                    after_run=None,
                    before_remove=None,
                    timeout_ms=1000,
                ),
            },
        )()
    )
    target = tmp_path / "hook-cwd"
    target.mkdir()
    await manager._run_hook(
        "after_run",
        "printf 'ok' > result.txt\r\nif [ -f result.txt ]; then\r\n  exit 0\r\nfi",
        target,
    )
    assert (target / "result.txt").read_text(encoding="utf-8") == "ok"
