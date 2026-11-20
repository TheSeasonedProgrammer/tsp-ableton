"""Command line entry point. With no command it runs the MCP server, which is how MCP clients start it."""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from tsp_mcp_for_ableton_live import install as installer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=installer.PACKAGE, description="TSP MCP for Ableton Live. With no command, runs the MCP server over stdio.")
    parser.add_argument("--version", action="version", version=metadata.version(installer.PACKAGE))
    commands = parser.add_subparsers(dest="command")

    install = commands.add_parser("install", help="Install the Remote Script into Live and register the MCP server with Claude Code.")
    install.add_argument("--user-library", type=Path, help="Live's User Library folder. Found from Live's preferences by default.")
    install.add_argument("--no-register", action="store_true", help="Only install the Remote Script.")
    install.add_argument("--dev", action="store_true", help="Load the tools from this checkout, so edits only need ableton_reload_tools.")

    uninstall = commands.add_parser("uninstall", help="Remove the Remote Script and the Claude Code registration.")
    uninstall.add_argument("--user-library", type=Path, help="Live's User Library folder. Found from Live's preferences by default.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "install":
            installer.install(args.user_library, args.dev, not args.no_register)
        elif args.command == "uninstall":
            installer.uninstall(args.user_library)
        else:
            from tsp_mcp_for_ableton_live.server import main as run_server

            run_server()
    except installer.InstallError as error:
        sys.exit(f"error: {error}")
