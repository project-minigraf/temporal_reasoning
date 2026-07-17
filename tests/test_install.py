import json
import subprocess
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import install


class TestCheckMinigrafPackage:
    def test_returns_true_when_already_installed(self):
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            assert install.check_minigraf_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = install.check_minigraf_package()
        assert mock_run.called
        assert result is True

    def test_returns_false_when_pip_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = install.check_minigraf_package()
        assert result is False


class TestCheckMcpPackage:
    def test_returns_true_when_already_installed(self):
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            assert install.check_mcp_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = install.check_mcp_package()
        assert mock_run.called

    def test_returns_false_when_pip_fails(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = install.check_mcp_package()
        assert result is False


class TestCheckMcpServerImportable:
    def test_returns_true_when_mcp_server_importable(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert install.check_mcp_server_importable() is True

    def test_returns_false_when_import_fails(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"No module named 'mcp_server'"
        with patch("subprocess.run", return_value=mock_result):
            assert install.check_mcp_server_importable() is False


class TestSetupMcpJson:
    def test_uses_git_ingestion_and_bm25_extras(self, tmp_path):
        install.setup_mcp_json(str(tmp_path))
        with open(tmp_path / ".mcp.json") as f:
            config = json.load(f)
        args = config["mcpServers"]["temporal-reasoning"]["args"]
        assert args == ["temporal-reasoning[git-ingestion,bm25]"]


class TestBuildPluginStub:
    def test_stub_mcp_json_uses_git_ingestion_and_bm25_extras(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install.os.path, "expanduser", lambda p: str(tmp_path))
        stub_dir = install._build_plugin_stub()
        with open(install.os.path.join(stub_dir, ".mcp.json")) as f:
            config = json.load(f)
        args = config["mcpServers"]["temporal-reasoning"]["args"]
        assert args == ["temporal-reasoning[git-ingestion,bm25]"]


class TestSyncLists:
    def test_mcp_server_in_files_to_sync(self):
        assert "mcp_server.py" in install.FILES_TO_SYNC

    def test_minigraf_not_in_files_to_sync(self):
        assert "minigraf.py" not in install.FILES_TO_SYNC

    def test_hooks_in_dirs_to_sync(self):
        assert "hooks" in install.DIRS_TO_SYNC


class TestResolveHarness:
    def test_missing_harness_returns_none(self):
        assert install._resolve_harness([]) is None

    def test_missing_harness_value_returns_none(self):
        assert install._resolve_harness(["--harness"]) is None

    def test_invalid_harness_value_returns_none(self):
        assert install._resolve_harness(["--harness", "vim"]) is None

    def test_valid_claude_code_harness(self):
        assert install._resolve_harness(["--harness", "claude-code"]) == "claude-code"

    def test_valid_opencode_harness(self):
        assert install._resolve_harness(["--harness", "opencode"]) == "opencode"

    def test_valid_codex_harness(self):
        assert install._resolve_harness(["--harness", "codex"]) == "codex"

    def test_harness_value_alongside_other_flags(self):
        argv = ["--target", "/some/path", "--harness", "codex", "--force"]
        assert install._resolve_harness(argv) == "codex"


class TestGetTargetDir:
    def test_harness_value_not_mistaken_for_target_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["install.py", "--harness", "codex"])
        assert install._get_target_dir() == install.os.getcwd()

    def test_explicit_target_still_works_with_harness(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            sys, "argv",
            ["install.py", "--harness", "opencode", "--target", str(tmp_path)],
        )
        assert install._get_target_dir() == install.os.path.abspath(str(tmp_path))

    def test_bare_positional_path_still_works_with_harness(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            sys, "argv",
            ["install.py", "--harness", "claude-code", str(tmp_path)],
        )
        assert install._get_target_dir() == install.os.path.abspath(str(tmp_path))


class TestSyncFilesHarnessScoping:
    @pytest.mark.parametrize("harness,expected_dir,other_dirs", [
        ("claude-code", ".claude/skills/temporal-reasoning",
         [".agents/skills/temporal-reasoning", ".opencode/skills/temporal-reasoning",
          "skills/temporal-reasoning"]),
        ("opencode", ".opencode/skills/temporal-reasoning",
         [".agents/skills/temporal-reasoning", ".claude/skills/temporal-reasoning",
          "skills/temporal-reasoning"]),
        ("codex", ".agents/skills/temporal-reasoning",
         [".opencode/skills/temporal-reasoning", ".claude/skills/temporal-reasoning",
          "skills/temporal-reasoning"]),
    ])
    def test_only_selected_harness_dir_is_written(self, tmp_path, harness, expected_dir, other_dirs):
        install._sync_files(str(tmp_path), harness)
        assert (tmp_path / expected_dir / "SKILL.md").exists()
        for other in other_dirs:
            assert not (tmp_path / other).exists()

    def test_does_not_touch_preexisting_root_skills_dir(self, tmp_path):
        """Acceptance criterion (#132): a pre-existing root-level skills/ directory
        must not be overwritten, moved, or deleted by any harness's install."""
        preexisting = tmp_path / "skills" / "temporal-reasoning" / "SKILL.md"
        preexisting.parent.mkdir(parents=True)
        preexisting.write_text("pre-existing sentinel content")

        for harness in install.SUPPORTED_HARNESSES:
            install._sync_files(str(tmp_path), harness)

        assert preexisting.read_text() == "pre-existing sentinel content"


class TestMainHarnessGating:
    def _patch_common(self, monkeypatch):
        monkeypatch.setattr(install, "ensure_venv", lambda: True)
        monkeypatch.setattr(install, "check_python_version", lambda: True)
        monkeypatch.setattr(install, "check_minigraf_package", lambda: True)
        monkeypatch.setattr(install, "check_mcp_package", lambda: True)
        monkeypatch.setattr(install, "check_tree_sitter_packages", lambda: True)
        monkeypatch.setattr(install, "check_mcp_server_importable", lambda: True)

    def test_non_claude_harness_skips_claude_specific_setup(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch)
        mcp_json = MagicMock(return_value=True)
        settings_json = MagicMock(return_value=True)
        settings_local = MagicMock(return_value=True)
        register = MagicMock(return_value=True)
        monkeypatch.setattr(install, "setup_mcp_json", mcp_json)
        monkeypatch.setattr(install, "setup_claude_settings_json", settings_json)
        monkeypatch.setattr(install, "setup_claude_settings", settings_local)
        monkeypatch.setattr(install, "register_plugin_with_claude", register)

        install.main(str(tmp_path), "opencode")

        mcp_json.assert_not_called()
        settings_json.assert_not_called()
        settings_local.assert_not_called()
        register.assert_not_called()

    def test_claude_code_harness_runs_claude_specific_setup(self, monkeypatch, tmp_path):
        self._patch_common(monkeypatch)
        mcp_json = MagicMock(return_value=True)
        settings_json = MagicMock(return_value=True)
        settings_local = MagicMock(return_value=True)
        register = MagicMock(return_value=True)
        monkeypatch.setattr(install, "setup_mcp_json", mcp_json)
        monkeypatch.setattr(install, "setup_claude_settings_json", settings_json)
        monkeypatch.setattr(install, "setup_claude_settings", settings_local)
        monkeypatch.setattr(install, "register_plugin_with_claude", register)

        install.main(str(tmp_path), "claude-code")

        mcp_json.assert_called_once_with(str(tmp_path))
        settings_json.assert_called_once_with(str(tmp_path))
        settings_local.assert_called_once_with(str(tmp_path))
        register.assert_called_once()
