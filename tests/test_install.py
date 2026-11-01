"""Tests the installer against temporary folders and a fake Claude Code.

    uv run --no-project --python 3.11 python -m unittest discover -s tests
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server" / "src"))

from tsp_mcp_for_ableton_live import install as installer

LIBRARY_CFG = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton><ContentLibrary><UserLibrary><LibraryProject>
<ProjectPath Value="%s" /><ProjectName Value="User Library" />
</LibraryProject></UserLibrary></ContentLibrary></Ableton>
"""


def quietly(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        function(*args)
    return output.getvalue()


class Installer(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.library = self.home / "User Library"
        self.library.mkdir()
        self.target = self.library / "Remote Scripts" / installer.SCRIPT

    def write_preferences(self, version, library_parent, mtime):
        path = self.home / "Ableton" / version / "Preferences" / "Library.cfg"
        path.parent.mkdir(parents=True)
        path.write_text(LIBRARY_CFG % library_parent, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def find_library(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"APPDATA": str(self.home)}):
            return installer.find_user_library()

    def test_user_library_comes_from_the_last_used_live(self):
        self.write_preferences("Live 12.10.0", "C:\\old", 1000)
        self.write_preferences("Live 12.4.16", str(self.home), 2000)
        self.assertEqual(self.find_library(), self.library)

    def test_no_preferences_says_what_to_do(self):
        with self.assertRaisesRegex(installer.InstallError, "--user-library"):
            self.find_library()

    def test_broken_preferences_say_what_to_do(self):
        self.write_preferences("Live 12.4.16", "x", 1000).write_text("not xml", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "--user-library"):
            self.find_library()

    def test_missing_library_is_refused(self):
        with self.assertRaisesRegex(installer.InstallError, "not found"):
            installer.install_script(self.home / "nowhere")

    def test_install_copies_the_script_without_dev_mode(self):
        target = installer.install_script(self.library)
        self.assertEqual(target, self.target)
        self.assertTrue((target / "__init__.py").is_file())
        self.assertTrue((target / "ableton_tools" / "notes.py").is_file())
        self.assertFalse((target / "dev_path.txt").exists())
        self.assertEqual(list(target.rglob("__pycache__")), [])

    def test_dev_install_points_at_the_checkout(self):
        target = installer.install_script(self.library, dev=True)
        self.assertEqual(Path((target / "dev_path.txt").read_text(encoding="utf-8")), installer.SOURCE)

    def test_reinstall_replaces_the_old_copy(self):
        installer.install_script(self.library, dev=True)
        (self.target / "stale.py").write_text("", encoding="utf-8")
        installer.install_script(self.library)
        self.assertFalse((self.target / "stale.py").exists())
        self.assertFalse((self.target / "dev_path.txt").exists())

    def test_server_command_is_pinned_to_this_version(self):
        with mock.patch.object(installer.metadata, "version", return_value="1.2.3"):
            self.assertEqual(installer.server_command(), ["uvx", "tsp-mcp-for-ableton-live@1.2.3"])

    def test_dev_server_command_runs_the_checkout(self):
        self.assertEqual(installer.server_command(dev=True), ["uv", "run", "--directory", str(ROOT / "server"), "tsp-mcp-for-ableton-live"])

    def run_claude(self, returncode=0):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], returncode, "", "refused"))
        return mock.patch.object(installer.shutil, "which", return_value="claude"), mock.patch.object(installer.subprocess, "run", run), run

    def test_register_replaces_an_earlier_registration(self):
        which, patched, run = self.run_claude()
        with which, patched:
            self.assertTrue(installer.register_with_claude(["uvx", "pkg@1"]))
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["claude", "mcp", "remove", "tsp-ableton", "--scope", "user"],
            ["claude", "mcp", "add", "tsp-ableton", "--scope", "user", "--", "uvx", "pkg@1"],
        ])

    def test_failed_registration_reports_why(self):
        which, patched, _run = self.run_claude(returncode=1)
        with which, patched, self.assertRaisesRegex(installer.InstallError, "refused"):
            installer.register_with_claude(["uvx", "pkg@1"])

    def test_without_claude_code_the_config_is_printed(self):
        with mock.patch.object(installer.shutil, "which", return_value=None), mock.patch.object(installer, "server_command", return_value=["uvx", "pkg@1"]):
            output = quietly(installer.install, self.library)
        self.assertIn('"command": "uvx"', output)
        self.assertIn("pick TSP MCP", output)
        self.assertTrue(self.target.is_dir())

    def test_no_register_never_calls_claude(self):
        with mock.patch.object(installer, "server_command", return_value=["uvx", "pkg@1"]), mock.patch.object(installer, "_claude") as claude:
            quietly(installer.install, self.library, False, False)
        claude.assert_not_called()

    def test_uninstall_removes_the_script_and_registration(self):
        installer.install_script(self.library)
        which, patched, run = self.run_claude()
        with which, patched:
            quietly(installer.uninstall, self.library)
        self.assertFalse(self.target.exists())
        self.assertEqual(run.call_args.args[0], ["claude", "mcp", "remove", "tsp-ableton", "--scope", "user"])


if __name__ == "__main__":
    unittest.main()
