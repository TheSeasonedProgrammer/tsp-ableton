"""Installs the Remote Script into Live's User Library and registers the MCP server.

Standard library only, so the tests can run this on Live's Python version.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from importlib import metadata
from pathlib import Path

PACKAGE = "tsp-mcp-for-ableton-live"
SCRIPT = "TSP_MCP"
SERVER_NAME = "tsp-ableton"
SOURCE = Path(__file__).parent / "remote_script" / SCRIPT
CHECKOUT = SOURCE.parents[3]
ENABLE_STEPS = (
    "Finish in Live:\n"
    "  1. Restart Live.\n"
    "  2. Open Settings > Link, Tempo & MIDI.\n"
    "  3. In an empty Control Surface slot, pick TSP MCP (Input and Output: None)."
)


class InstallError(Exception):
    pass


def _preference_files() -> list[Path]:
    """Library.cfg from each installed Live version, most recently used first."""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Ableton"
        pattern = "Live */Preferences/Library.cfg"
    else:
        root = Path.home() / "Library" / "Preferences" / "Ableton"
        pattern = "Live */Library.cfg"
    # By last write, since version folders don't sort as text.
    return sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)


def find_user_library() -> Path:
    files = _preference_files()
    if not files:
        raise InstallError("No Live preferences found. Open Live once, or pass --user-library <folder>.")
    try:
        project = ElementTree.parse(files[0]).getroot().find("ContentLibrary/UserLibrary/LibraryProject")
        return Path(project.find("ProjectPath").get("Value")) / project.find("ProjectName").get("Value")
    except (AttributeError, OSError, TypeError, ElementTree.ParseError) as error:
        raise InstallError(f"Couldn't read the User Library from {files[0]} ({error}). Pass --user-library <folder>.") from error


def _script_folder(user_library: Path) -> Path:
    if not user_library.is_dir():
        raise InstallError(f"User Library not found at {user_library}. Pass --user-library <folder>.")
    return user_library / "Remote Scripts" / SCRIPT


def install_script(user_library: Path, dev: bool = False) -> Path:
    """Copy the Remote Script. With dev, it loads ableton_tools from this checkout and enables "inspect"."""
    target = _script_folder(user_library)
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__", "dev_path.txt"))
    if dev:
        (target / "dev_path.txt").write_text(str(SOURCE), encoding="utf-8")
    return target


def server_command(dev: bool = False) -> list[str]:
    if dev:
        if not (CHECKOUT / "pyproject.toml").is_file():
            raise InstallError("--dev needs a source checkout, run with: uv run --directory <checkout>/server")
        return ["uv", "run", "--directory", str(CHECKOUT), PACKAGE]
    # Pinned so the server always matches the Remote Script it was installed with.
    return ["uvx", f"{PACKAGE}@{metadata.version(PACKAGE)}"]


def _claude(*args: str) -> subprocess.CompletedProcess[str] | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    return subprocess.run([claude, "mcp", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def register_with_claude(command: list[str]) -> bool:
    """Register for all of the user's projects, replacing an earlier version. False without Claude Code."""
    if _claude("remove", SERVER_NAME, "--scope", "user") is None:
        return False
    result = _claude("add", SERVER_NAME, "--scope", "user", "--", *command)
    if result.returncode != 0:
        raise InstallError(f"claude mcp add failed: {(result.stderr or result.stdout).strip()}")
    return True


def install(user_library: Path | None = None, dev: bool = False, register: bool = True) -> None:
    command = server_command(dev)
    target = install_script(user_library or find_user_library(), dev)
    print(f"Installed the Remote Script to {target}")
    if not register:
        print("Skipped registering the MCP server.")
    elif register_with_claude(command):
        print(f"Registered the MCP server with Claude Code as {SERVER_NAME}.")
    else:
        config = {"mcpServers": {SERVER_NAME: {"command": command[0], "args": command[1:]}}}
        print(f"Claude Code wasn't found. Add this to your MCP client's config:\n{json.dumps(config, indent=2)}")
    print(ENABLE_STEPS)


def uninstall(user_library: Path | None = None) -> None:
    target = _script_folder(user_library or find_user_library())
    if target.exists():
        shutil.rmtree(target)
        print(f"Removed {target}. Restart Live to finish.")
    else:
        print(f"No Remote Script at {target}.")
    result = _claude("remove", SERVER_NAME, "--scope", "user")
    if result is not None and result.returncode == 0:
        print(f"Removed {SERVER_NAME} from Claude Code.")
