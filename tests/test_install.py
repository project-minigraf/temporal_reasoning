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
    def test_uses_git_ingestion_extra(self, tmp_path):
        install.setup_mcp_json(str(tmp_path))
        with open(tmp_path / ".mcp.json") as f:
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
