"""Repo preflight checks run before dispatch to fail fast on setup problems."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .models import PreflightConfig

logger = logging.getLogger(__name__)


@dataclass
class PreflightCheckResult:
    """Result of a single preflight check."""
    name: str
    ok: bool
    message: str


@dataclass
class PreflightResult:
    """Aggregated preflight result across all checks."""
    ok: bool = True
    checks: list[PreflightCheckResult] = field(default_factory=list)

    def add(self, check: PreflightCheckResult) -> None:
        self.checks.append(check)
        if not check.ok:
            self.ok = False

    @property
    def errors(self) -> list[PreflightCheckResult]:
        return [c for c in self.checks if not c.ok]


def check_required_cli(cli: str) -> PreflightCheckResult:
    """Verify a CLI tool is available on PATH."""
    found = shutil.which(cli) is not None
    if found:
        return PreflightCheckResult(
            name=f"cli:{cli}",
            ok=True,
            message=f"CLI '{cli}' is available",
        )
    return PreflightCheckResult(
        name=f"cli:{cli}",
        ok=False,
        message=f"Required CLI '{cli}' not found on PATH. Install it or update your PATH.",
    )


def check_env_var(var: str) -> PreflightCheckResult:
    """Verify an environment variable is set and non-empty."""
    value = os.environ.get(var, "")
    if value:
        return PreflightCheckResult(
            name=f"env:{var}",
            ok=True,
            message=f"Environment variable '{var}' is set",
        )
    return PreflightCheckResult(
        name=f"env:{var}",
        ok=False,
        message=f"Required environment variable '{var}' is not set or empty.",
    )


def check_git_repo(workspace_path: str) -> PreflightCheckResult:
    """Verify the workspace directory contains a git repository."""
    git_dir = Path(workspace_path) / ".git"
    if git_dir.exists():
        return PreflightCheckResult(
            name="git_repo",
            ok=True,
            message=f"Git repository found at {workspace_path}",
        )
    return PreflightCheckResult(
        name="git_repo",
        ok=False,
        message=(
            f"No .git directory found at {workspace_path}. "
            "Ensure the after_create hook clones the repository."
        ),
    )


async def check_git_remote(workspace_path: str) -> PreflightCheckResult:
    """Verify the workspace has at least one git remote configured."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "remote",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        remotes = stdout.decode().strip()
        if remotes:
            return PreflightCheckResult(
                name="git_remote",
                ok=True,
                message=f"Git remotes found: {remotes.replace(chr(10), ', ')}",
            )
        return PreflightCheckResult(
            name="git_remote",
            ok=False,
            message=(
                f"No git remotes configured in {workspace_path}. "
                "Add a remote (e.g. 'git remote add origin <url>')."
            ),
        )
    except Exception as exc:
        return PreflightCheckResult(
            name="git_remote",
            ok=False,
            message=f"Failed to check git remotes: {exc}",
        )


async def check_base_branch(workspace_path: str, base_branch: str) -> PreflightCheckResult:
    """Verify the base branch exists locally or in origin."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--verify", f"refs/heads/{base_branch}",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return PreflightCheckResult(
                name="base_branch",
                ok=True,
                message=f"Base branch '{base_branch}' exists locally",
            )

        # Check remote
        proc2 = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--verify", f"refs/remotes/origin/{base_branch}",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc2.communicate(), timeout=10)
        if proc2.returncode == 0:
            return PreflightCheckResult(
                name="base_branch",
                ok=True,
                message=f"Base branch '{base_branch}' exists on origin",
            )

        return PreflightCheckResult(
            name="base_branch",
            ok=False,
            message=(
                f"Base branch '{base_branch}' not found locally or on origin in {workspace_path}. "
                f"Ensure the branch exists and has been fetched."
            ),
        )
    except Exception as exc:
        return PreflightCheckResult(
            name="base_branch",
            ok=False,
            message=f"Failed to check base branch: {exc}",
        )


async def check_clean_worktree(workspace_path: str) -> PreflightCheckResult:
    """Verify the git worktree has no uncommitted changes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode().strip()
        if not output:
            return PreflightCheckResult(
                name="clean_worktree",
                ok=True,
                message="Git worktree is clean",
            )
        dirty_count = len(output.splitlines())
        return PreflightCheckResult(
            name="clean_worktree",
            ok=False,
            message=(
                f"Git worktree has {dirty_count} uncommitted change(s) in {workspace_path}. "
                "Ensure the before_run hook resets the worktree to a clean state."
            ),
        )
    except Exception as exc:
        return PreflightCheckResult(
            name="clean_worktree",
            ok=False,
            message=f"Failed to check worktree status: {exc}",
        )


async def run_preflight_checks(
    config: PreflightConfig,
    workspace_path: str | None = None,
) -> PreflightResult:
    """Run all configured preflight checks and return aggregated results.

    Args:
        config: Preflight configuration from .cymphony/config.yml.
        workspace_path: If provided, run git-repo checks against this path.
            When None, only CLI and env var checks are run (useful before
            workspace creation).
    """
    result = PreflightResult()

    # CLI checks
    for cli in config.required_clis:
        result.add(check_required_cli(cli))

    # Env var checks
    for var in config.required_env_vars:
        result.add(check_env_var(var))

    # Workspace-level git checks (only when a workspace path is provided)
    if workspace_path and Path(workspace_path).exists():
        repo_check = check_git_repo(workspace_path)
        result.add(repo_check)

        # Only run further git checks if .git exists
        if repo_check.ok:
            result.add(await check_git_remote(workspace_path))
            result.add(await check_base_branch(workspace_path, config.base_branch))
            if config.expect_clean_worktree:
                result.add(await check_clean_worktree(workspace_path))

    # Log results
    for check in result.checks:
        if check.ok:
            logger.debug(f"action=preflight_check_passed name={check.name}")
        else:
            logger.warning(
                f"action=preflight_check_failed name={check.name} "
                f"message={check.message!r}"
            )

    return result
