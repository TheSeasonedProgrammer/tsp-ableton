"""Live tools. Each module declares its tools with @tool; TSP_MCP imports them all."""

from ableton_tools._registry import TOOLS, limiter_warning


def register(registry):
    registry.update(TOOLS)
