import json
import os
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


class TestPluginVersion:
    """`.claude-plugin/plugin.json` is the only source, and there is no fallback.

    A wrong PLUGIN_VERSION is not inert. It names the cache directory Claude
    Code is told to copy the stub into, and `_build_plugin_stub` deletes every
    cache directory whose name is not it -- so a stale fallback deletes the
    working install and registers a path nothing will ever populate. Silently:
    the script prints a tick and exits 0.
    """

    def _repo_with(self, tmp_path, monkeypatch, contents):
        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(contents)
        monkeypatch.setattr(install, "REPO_DIR", str(tmp_path))

    def test_reads_the_version_from_the_canonical_file(self, tmp_path, monkeypatch):
        self._repo_with(tmp_path, monkeypatch, '{"version": "9.9.9"}')
        assert install._plugin_version() == "9.9.9"

    def test_module_constant_matches_the_repo_file(self):
        with open(os.path.join(install.REPO_DIR, ".claude-plugin", "plugin.json")) as f:
            assert install.PLUGIN_VERSION == json.load(f)["version"]

    def test_refuses_when_plugin_json_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "REPO_DIR", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            install._plugin_version()
        assert ".claude-plugin/plugin.json" in str(exc.value)

    def test_refuses_when_plugin_json_is_malformed(self, tmp_path, monkeypatch):
        self._repo_with(tmp_path, monkeypatch, "{not json")
        with pytest.raises(SystemExit):
            install._plugin_version()

    def test_refuses_when_the_version_key_is_absent(self, tmp_path, monkeypatch):
        self._repo_with(tmp_path, monkeypatch, '{"name": "temporal-reasoning"}')
        with pytest.raises(SystemExit):
            install._plugin_version()

    def test_refuses_when_the_version_is_empty(self, tmp_path, monkeypatch):
        self._repo_with(tmp_path, monkeypatch, '{"version": ""}')
        with pytest.raises(SystemExit):
            install._plugin_version()


class TestPyprojectPyModules:
    def test_parses_declared_modules(self):
        modules = install._pyproject_py_modules()
        assert "fact_index" in modules
        assert "mcp_server" in modules

    def test_returns_empty_list_when_file_missing(self, monkeypatch):
        monkeypatch.setattr(install, "REPO_DIR", "/nonexistent/path/xyz")
        assert install._pyproject_py_modules() == []


class TestCheckEditableInstallCurrent:
    def test_returns_true_when_no_editable_install_present(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # pip show: not installed
            assert install.check_editable_install_current() is True
        assert mock_run.call_count == 1

    def test_returns_true_when_editable_install_resolves_all_modules(self, monkeypatch):
        monkeypatch.setattr(install, "_pyproject_py_modules", lambda: ["fact_index"])
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # pip show: present
                MagicMock(returncode=0),  # import probe: resolves fine
            ]
            assert install.check_editable_install_current() is True
        assert mock_run.call_count == 2

    def test_refreshes_and_succeeds_when_mapping_has_drifted(self, monkeypatch):
        monkeypatch.setattr(install, "_pyproject_py_modules", lambda: ["fact_index"])
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # pip show: present
                MagicMock(returncode=1),  # import probe: drifted (stale MAPPING)
                MagicMock(returncode=0),  # pip install -e .: succeeds
                MagicMock(returncode=0),  # import probe again: now resolves
            ]
            assert install.check_editable_install_current() is True
        assert mock_run.call_count == 4

    def test_returns_false_when_refresh_does_not_fix_it(self, monkeypatch):
        monkeypatch.setattr(install, "_pyproject_py_modules", lambda: ["fact_index"])
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # pip show: present
                MagicMock(returncode=1),  # import probe: drifted
                MagicMock(returncode=0),  # pip install -e .: succeeds
                MagicMock(returncode=1),  # import probe again: still broken
            ]
            assert install.check_editable_install_current() is False


class TestSetupMcpJson:
    def test_uses_git_ingestion_extra(self, tmp_path):
        install.setup_mcp_json(str(tmp_path))
        with open(tmp_path / ".mcp.json") as f:
            config = json.load(f)
        args = config["mcpServers"]["temporal-reasoning"]["args"]
        assert args == ["temporal-reasoning[git-ingestion]"]

    def test_sets_index_path_alongside_graph_path(self, tmp_path):
        install.setup_mcp_json(str(tmp_path))
        with open(tmp_path / ".mcp.json") as f:
            config = json.load(f)
        env = config["mcpServers"]["temporal-reasoning"]["env"]
        assert env["MINIGRAF_INDEX_PATH"] == f"{env['MINIGRAF_GRAPH_PATH']}.fts.sqlite3"


class TestBuildPluginStub:
    def test_stub_mcp_json_uses_git_ingestion_extra(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install.os.path, "expanduser", lambda p: str(tmp_path))
        stub_dir = install._build_plugin_stub()
        with open(install.os.path.join(stub_dir, ".mcp.json")) as f:
            config = json.load(f)
        args = config["mcpServers"]["temporal-reasoning"]["args"]
        assert args == ["temporal-reasoning[git-ingestion]"]


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
        monkeypatch.setattr(install, "check_editable_install_current", lambda: True)

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

    def test_update_failure_marks_setup_incomplete_for_claude_code(self, monkeypatch, tmp_path, capsys):
        self._patch_common(monkeypatch)
        monkeypatch.setattr(install, "setup_mcp_json", lambda *_: True)
        monkeypatch.setattr(install, "setup_claude_settings_json", lambda *_: True)
        monkeypatch.setattr(install, "setup_claude_settings", lambda *_: True)
        monkeypatch.setattr(install, "register_plugin_with_claude", lambda: True)

        with pytest.raises(SystemExit) as exc_info:
            install.main(str(tmp_path), "claude-code", update_ok=False)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Setup incomplete" in out
        assert "Setup complete!" not in out

    def test_update_failure_marks_setup_incomplete_for_non_claude_harness(self, monkeypatch, tmp_path, capsys):
        self._patch_common(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            install.main(str(tmp_path), "opencode", update_ok=False)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Setup incomplete" in out
        assert "Setup complete!" not in out

    def test_update_ok_true_still_completes_normally(self, monkeypatch, tmp_path, capsys):
        self._patch_common(monkeypatch)
        monkeypatch.setattr(install, "setup_mcp_json", lambda *_: True)
        monkeypatch.setattr(install, "setup_claude_settings_json", lambda *_: True)
        monkeypatch.setattr(install, "setup_claude_settings", lambda *_: True)
        monkeypatch.setattr(install, "register_plugin_with_claude", lambda: True)

        install.main(str(tmp_path), "claude-code", update_ok=True)

        out = capsys.readouterr().out
        assert "Setup complete!" in out


class TestHookTimeoutsAreSeconds:
    """Claude Code reads a command hook's `timeout` in SECONDS (#344). The
    installer wrote 5000/10000 believing milliseconds, i.e. an 83-minute and
    a 2.8-hour bound: one prepare_hook run was observed at 32 minutes before
    the user cancelled it."""

    @staticmethod
    def _timeouts(settings):
        return {
            event: [h["timeout"] for e in settings["hooks"][event] for h in e["hooks"]]
            for event in ("UserPromptSubmit", "Stop")
        }

    def test_fresh_install_writes_second_valued_timeouts(self, tmp_path):
        assert install.setup_claude_settings(str(tmp_path))
        settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert self._timeouts(settings) == {"UserPromptSubmit": [30], "Stop": [60]}

    def test_reinstall_corrects_a_millisecond_valued_timeout(self, tmp_path):
        # The path every existing user takes: the entry already references our
        # scripts, so it is updated in place rather than appended.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        old = {"hooks": {
            event: [{"matcher": "", "hooks": [{"type": "command",
                                               "command": f"python /x/hooks/{script}",
                                               "timeout": ms}]}]
            for event, (script, ms) in
            {"UserPromptSubmit": ("prepare_hook.py", 5000), "Stop": ("finalize_hook.py", 10000)}.items()
        }}
        (claude_dir / "settings.local.json").write_text(json.dumps(old))
        assert install.setup_claude_settings(str(tmp_path))
        settings = json.loads((claude_dir / "settings.local.json").read_text())
        assert self._timeouts(settings) == {"UserPromptSubmit": [30], "Stop": [60]}

    def test_sample_config_matches_installer(self):
        with open(os.path.join(install.REPO_DIR, "hooks", "claude-code.json")) as f:
            sample = json.load(f)
        assert self._timeouts(sample) == {"UserPromptSubmit": [30], "Stop": [60]}
