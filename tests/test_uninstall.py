import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install
import uninstall


class TestRemoveSkillDirs:
    def test_removes_every_harness_skill_dir_installed_by_install_py(self, tmp_path):
        """uninstall.py's SKILL_DIRS is declared "Must match install.py" — verify it
        actually does, by round-tripping every harness through install._sync_files
        then uninstall.remove_skill_dirs and checking each is gone (#132 regression:
        HARNESS_SKILL_DIRS changed but the separate, hand-maintained SKILL_DIRS list
        in uninstall.py was not updated to match)."""
        for harness in install.SUPPORTED_HARNESSES:
            install._sync_files(str(tmp_path), harness)

        for harness in install.SUPPORTED_HARNESSES:
            installed_dir = tmp_path / install.HARNESS_SKILL_DIRS[harness]
            assert installed_dir.exists(), f"setup failed for {harness}"

        uninstall.remove_skill_dirs(str(tmp_path))

        for harness in install.SUPPORTED_HARNESSES:
            installed_dir = tmp_path / install.HARNESS_SKILL_DIRS[harness]
            assert not installed_dir.exists(), f"{harness} skill dir survived uninstall"
