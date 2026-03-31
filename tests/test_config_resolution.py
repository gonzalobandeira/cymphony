"""Tests for config resolution precedence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cymphony.workflow import (
    ConfigSource,
    LOCAL_CONFIG_FILENAME,
    LOCAL_CONFIG_DIR,
    load_workflow,
    local_config_path,
    resolve_config_source,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_CONFIG = """\
tracker:
  kind: linear
  api_key: test_key
  project_slug: test-project
"""


def _write(path: Path, content: str = _MINIMAL_CONFIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_config_source tests
# ---------------------------------------------------------------------------


class TestResolveConfigSource:
    """Tests for resolve_config_source() which returns (path, source)."""

    def test_cli_override_source(self, tmp_path: Path) -> None:
        explicit = _write(tmp_path / "override.yml")
        path, source = resolve_config_source(str(explicit))
        assert source == ConfigSource.CLI_OVERRIDE
        assert path == explicit.resolve()

    def test_local_config_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME, "tracker:\n  kind: linear\n")
        path, source = resolve_config_source(None)
        assert source == ConfigSource.LOCAL_CONFIG

    def test_setup_required_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        path, source = resolve_config_source(None)
        assert source == ConfigSource.SETUP_REQUIRED
        assert str(path).endswith(f"{LOCAL_CONFIG_DIR}/{LOCAL_CONFIG_FILENAME}")


# ---------------------------------------------------------------------------
# local_config_path tests
# ---------------------------------------------------------------------------


class TestLocalConfigPath:

    def test_default_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = local_config_path()
        assert result == tmp_path / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME

    def test_explicit_base(self, tmp_path: Path) -> None:
        result = local_config_path(tmp_path / "myrepo")
        assert result == tmp_path / "myrepo" / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Integration: local config remains canonical
# ---------------------------------------------------------------------------


class TestResolutionWithLocalConfig:

    def test_local_config_remains_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / LOCAL_CONFIG_DIR / LOCAL_CONFIG_FILENAME, "tracker:\n  kind: linear\n")

        path, source_after = resolve_config_source(None)
        assert source_after == ConfigSource.LOCAL_CONFIG
        assert str(path).endswith(f"{LOCAL_CONFIG_DIR}/{LOCAL_CONFIG_FILENAME}")
