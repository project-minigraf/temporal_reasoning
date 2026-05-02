import subprocess
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import install


class TestCheckMinigrafPackage:
    def test_returns_true_when_already_installed(self):
        with patch.dict("sys.modules", {"minigraf": MagicMock()}):
            assert install.check_minigraf_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch.dict("sys.modules", {"minigraf": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    result = install.check_minigraf_package()
        assert mock_run.called
        assert result is True

    def test_returns_false_when_pip_fails(self):
        with patch("builtins.__import__", side_effect=ImportError):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                result = install.check_minigraf_package()
        assert result is False


class TestCheckMcpPackage:
    def test_returns_true_when_already_installed(self):
        with patch.dict("sys.modules", {"mcp": MagicMock()}):
            assert install.check_mcp_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch("builtins.__import__", side_effect=ImportError):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = install.check_mcp_package()
        assert mock_run.called


class TestCheckMcpServerImportable:
    def test_returns_true_when_mcp_server_importable(self):
        with patch.dict("sys.modules", {"mcp_server": MagicMock()}):
            assert install.check_mcp_server_importable() is True

    def test_returns_false_when_import_fails(self):
        with patch("importlib.util.find_spec", return_value=None):
            with patch("builtins.__import__", side_effect=ImportError):
                assert install.check_mcp_server_importable() is False


class TestSyncLists:
    def test_mcp_server_in_files_to_sync(self):
        assert "mcp_server.py" in install.FILES_TO_SYNC

    def test_vulcan_not_in_files_to_sync(self):
        assert "vulcan.py" not in install.FILES_TO_SYNC

    def test_hooks_in_dirs_to_sync(self):
        assert "hooks" in install.DIRS_TO_SYNC
