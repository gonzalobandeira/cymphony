"""Tests for config resolution precedence and legacy migration (BAP-187)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cymphony.workflow import (
    ConfigSource,
    DEFAULT_WORKFLOW_FILENAME,
    LOCAL_CONFIG_DIR,
    LOCAL_WORKFLOW_FILENAME,
    load_workflow,
    local_config_path,
    migrate_legacy_workflow,
    resolve_config_source,
    resolve_workflow_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_WORKFLOW = """\
---
tracker:
  kind: linear
  api_key: test_key
  project_slug: test-project
codex:
  command: claude
---
Hello {{ issue.title }}
"""


def _write(path: Path, content: str = _MINIMAL_WORKFLOW) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_workflow_path tests
# ---------------------------------------------------------------------------


class TestResolveWorkflowPath:
    """Tests for resolve_workflow_path() precedence."""

    def test_explicit_cli_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "my-workflow.md"
        _write(explicit)
        result = resolve_workflow_path(str(explicit))
        assert result == explicit.resolve()

    def test_local_config_preferred_over_legacy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME, "---\n---\nlocal")
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, "---\n---\nlegacy")
        result = resolve_workflow_path(None)
        assert result == (tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME).resolve()

    def test_falls_back_to_legacy_when_no_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, "---\n---\nlegacy")
        result = resolve_workflow_path(None)
        assert result == (tmp_path / DEFAULT_WORKFLOW_FILENAME).resolve()

    def test_returns_local_target_when_nothing_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = resolve_workflow_path(None)
        assert result == (tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME).resolve()


# ---------------------------------------------------------------------------
# resolve_config_source tests
# ---------------------------------------------------------------------------


class TestResolveConfigSource:
    """Tests for resolve_config_source() which returns (path, source)."""

    def test_cli_override_source(self, tmp_path: Path) -> None:
        explicit = _write(tmp_path / "override.md")
        path, source = resolve_config_source(str(explicit))
        assert source == ConfigSource.CLI_OVERRIDE
        assert path == explicit.resolve()

    def test_local_config_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME)
        path, source = resolve_config_source(None)
        assert source == ConfigSource.LOCAL_CONFIG

    def test_legacy_committed_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME)
        path, source = resolve_config_source(None)
        assert source == ConfigSource.LEGACY_COMMITTED

    def test_setup_required_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        path, source = resolve_config_source(None)
        assert source == ConfigSource.SETUP_REQUIRED
        # Path should point to the local config target
        assert str(path).endswith(f"{LOCAL_CONFIG_DIR}/{LOCAL_WORKFLOW_FILENAME}")

    def test_local_config_takes_precedence_over_legacy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME)
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME)
        _, source = resolve_config_source(None)
        assert source == ConfigSource.LOCAL_CONFIG


# ---------------------------------------------------------------------------
# local_config_path tests
# ---------------------------------------------------------------------------


class TestLocalConfigPath:

    def test_default_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = local_config_path()
        assert result == tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME

    def test_explicit_base(self, tmp_path: Path) -> None:
        result = local_config_path(tmp_path / "myrepo")
        assert result == tmp_path / "myrepo" / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME


# ---------------------------------------------------------------------------
# migrate_legacy_workflow tests
# ---------------------------------------------------------------------------


class TestMigrateLegacyWorkflow:

    def test_no_op_when_local_config_exists(self, tmp_path: Path) -> None:
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME, "local")
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, "legacy")
        result = migrate_legacy_workflow(tmp_path)
        assert result is None
        # Local config should be unchanged
        assert (tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME).read_text() == "local"

    def test_no_op_when_no_legacy_exists(self, tmp_path: Path) -> None:
        result = migrate_legacy_workflow(tmp_path)
        assert result is None

    def test_copies_legacy_to_local(self, tmp_path: Path) -> None:
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, _MINIMAL_WORKFLOW)
        result = migrate_legacy_workflow(tmp_path)
        assert result is not None
        assert result == tmp_path / LOCAL_CONFIG_DIR / LOCAL_WORKFLOW_FILENAME
        assert result.read_text(encoding="utf-8") == _MINIMAL_WORKFLOW
        # Legacy file should still exist (not moved)
        assert (tmp_path / DEFAULT_WORKFLOW_FILENAME).exists()

    def test_creates_config_dir(self, tmp_path: Path) -> None:
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME)
        config_dir = tmp_path / LOCAL_CONFIG_DIR
        assert not config_dir.exists()
        migrate_legacy_workflow(tmp_path)
        assert config_dir.is_dir()

    def test_migrated_config_is_loadable(self, tmp_path: Path) -> None:
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, _MINIMAL_WORKFLOW)
        new_path = migrate_legacy_workflow(tmp_path)
        assert new_path is not None
        workflow = load_workflow(new_path)
        assert workflow.config["tracker"]["kind"] == "linear"
        assert workflow.prompt_template == "Hello {{ issue.title }}"

    def test_idempotent_after_migration(self, tmp_path: Path) -> None:
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, _MINIMAL_WORKFLOW)
        first = migrate_legacy_workflow(tmp_path)
        assert first is not None
        second = migrate_legacy_workflow(tmp_path)
        assert second is None  # no-op because local config now exists


# ---------------------------------------------------------------------------
# Integration: resolve after migration
# ---------------------------------------------------------------------------


class TestResolutionAfterMigration:

    def test_migration_then_resolve_uses_local(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / DEFAULT_WORKFLOW_FILENAME, _MINIMAL_WORKFLOW)

        # Before migration, resolves to legacy
        _, source_before = resolve_config_source(None)
        assert source_before == ConfigSource.LEGACY_COMMITTED

        # Migrate
        migrate_legacy_workflow(tmp_path)

        # After migration, resolves to local config
        path, source_after = resolve_config_source(None)
        assert source_after == ConfigSource.LOCAL_CONFIG
        assert str(path).endswith(f"{LOCAL_CONFIG_DIR}/{LOCAL_WORKFLOW_FILENAME}")
