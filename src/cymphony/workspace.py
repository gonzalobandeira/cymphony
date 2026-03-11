"""Workspace manager: per-issue directory lifecycle and hooks (spec §9)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from .models import HooksConfig, ServiceConfig, Workspace, WorkspaceError

logger = logging.getLogger(__name__)

# Only these characters are allowed in workspace directory names (spec §4.2)
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-]")


def sanitize_workspace_key(identifier: str) -> str:
    """Replace unsafe characters with '_' (spec §4.2, §9.5 invariant 3)."""
    return _SAFE_CHARS.sub("_", identifier)


def workspace_path(root: str, identifier: str) -> Path:
    """Compute absolute workspace path for an issue identifier."""
    key = sanitize_workspace_key(identifier)
    return (Path(root) / key).resolve()


def assert_path_in_root(ws_path: Path, root: str) -> None:
    """Raise WorkspaceError if ws_path is not under root (spec §9.5 invariant 2)."""
    root_resolved = Path(root).resolve()
    try:
        ws_path.relative_to(root_resolved)
    except ValueError:
        raise WorkspaceError(
            "workspace_path_escape",
            f"Workspace path {ws_path} escapes workspace root {root_resolved}",
        )


class WorkspaceManager:
    """Manages per-issue workspace directories and lifecycle hooks (spec §9)."""

    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    @property
    def _root(self) -> str:
        return self._config.workspace.root

    @property
    def _hooks(self) -> HooksConfig:
        return self._config.hooks

    def get_path(self, identifier: str) -> Path:
        """Return the workspace path for an issue identifier."""
        return workspace_path(self._root, identifier)

    async def create_for_issue(self, identifier: str) -> Workspace:
        """Create or reuse workspace for identifier (spec §9.2).

        Raises WorkspaceError on path-escape, directory creation failure,
        or after_create hook failure.
        """
        ws_path = workspace_path(self._root, identifier)
        key = sanitize_workspace_key(identifier)

        # Safety invariant 2: must be under root
        assert_path_in_root(ws_path, self._root)

        # Ensure workspace root exists
        Path(self._root).mkdir(parents=True, exist_ok=True)

        created_now = False
        if ws_path.exists():
            if not ws_path.is_dir():
                raise WorkspaceError(
                    "workspace_not_a_directory",
                    f"Workspace path {ws_path} exists but is not a directory",
                )
            logger.info(
                f"action=workspace_reused "
                f"identifier={identifier} path={ws_path}"
            )
        else:
            ws_path.mkdir(parents=True, exist_ok=False)
            created_now = True
            logger.info(
                f"action=workspace_created "
                f"identifier={identifier} path={ws_path}"
            )

        workspace = Workspace(
            path=str(ws_path),
            workspace_key=key,
            created_now=created_now,
        )

        # Run after_create hook only on new workspace (spec §9.4)
        if created_now and self._hooks.after_create:
            try:
                await self._run_hook(
                    "after_create",
                    self._hooks.after_create,
                    ws_path,
                )
            except WorkspaceError:
                # Fatal: remove the partially-created workspace (spec §9.3)
                shutil.rmtree(ws_path, ignore_errors=True)
                raise

        return workspace

    async def run_before_run_hook(self, workspace: Workspace) -> None:
        """Run before_run hook — failure is fatal (spec §9.4)."""
        if self._hooks.before_run:
            await self._run_hook(
                "before_run", self._hooks.before_run, Path(workspace.path)
            )

    async def run_after_run_hook(self, workspace: Workspace) -> None:
        """Run after_run hook — failure is logged and ignored (spec §9.4)."""
        if self._hooks.after_run:
            try:
                await self._run_hook(
                    "after_run", self._hooks.after_run, Path(workspace.path)
                )
            except WorkspaceError as exc:
                logger.warning(
                    f"action=after_run_hook_failed "
                    f"path={workspace.path} error={exc} (ignored)"
                )

    async def remove_workspace(self, identifier: str) -> None:
        """Remove workspace directory (spec §9.5, §8.5 terminal cleanup)."""
        ws_path = workspace_path(self._root, identifier)
        if not ws_path.exists():
            return

        # Run before_remove hook — failure is logged and ignored (spec §9.4)
        if self._hooks.before_remove:
            try:
                await self._run_hook(
                    "before_remove", self._hooks.before_remove, ws_path
                )
            except WorkspaceError as exc:
                logger.warning(
                    f"action=before_remove_hook_failed "
                    f"identifier={identifier} error={exc} (ignored)"
                )

        shutil.rmtree(ws_path, ignore_errors=True)
        logger.info(
            f"action=workspace_removed identifier={identifier} path={ws_path}"
        )

    async def _run_hook(
        self,
        hook_name: str,
        script: str,
        cwd: Path,
    ) -> None:
        """Execute a hook script in the workspace directory (spec §9.4).

        Raises WorkspaceError on failure or timeout.
        """
        timeout_secs = self._hooks.timeout_ms / 1000.0
        logger.info(
            f"action=hook_start hook={hook_name} cwd={cwd}"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", script,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_secs
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise WorkspaceError(
                    f"{hook_name}_hook_timeout",
                    f"Hook {hook_name!r} timed out after {timeout_secs}s",
                )
        except OSError as exc:
            raise WorkspaceError(
                f"{hook_name}_hook_error",
                f"Hook {hook_name!r} failed to start: {exc}",
            ) from exc

        rc = proc.returncode
        stdout_text = (stdout or b"").decode(errors="replace")[:500]
        stderr_text = (stderr or b"").decode(errors="replace")[:500]

        if rc != 0:
            raise WorkspaceError(
                f"{hook_name}_hook_failed",
                f"Hook {hook_name!r} exited with code {rc}. "
                f"stdout={stdout_text!r} stderr={stderr_text!r}",
            )

        logger.info(
            f"action=hook_completed hook={hook_name} "
            f"cwd={cwd} exit_code={rc}"
        )
