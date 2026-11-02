"""TSP MCP for Ableton Live.

Speaks MCP over stdio using the official SDK and forwards tool calls to the
TSP MCP Remote Script running inside Live (JSON lines over TCP). The tool
list comes from the bridge; the last one seen is cached so tools still list
while Live is closed, and calls then fail with a clear message.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from jsonschema import Draft202012Validator
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from tsp_mcp_for_ableton_live.analysis import AnalysisStore

HOST = os.environ.get("ABLETON_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABLETON_BRIDGE_PORT", "8768"))
CONNECT_TIMEOUT = 3.0
CALL_TIMEOUT = float(os.environ.get("ABLETON_CALL_TIMEOUT", "60"))
CACHE_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "tsp-mcp-for-ableton-live" / "tools.json"
LINE_LIMIT = 64 * 1024 * 1024
# Calls repeated for a tool reply holding retry_after.
RETRIES = 20
# Where TSP Spatial devices send their measurements. The device has 8769 built in.
ANALYSIS_PORT = int(os.environ.get("ABLETON_ANALYSIS_PORT", "8769"))

LOCAL_TOOLS = [
    types.Tool(
        name="ableton_bridge_status",
        description="Check whether Live and the TSP MCP Remote Script are reachable, which Live version is running, and how many tools it exposes.",
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="ableton_reload_tools",
        description="Re-import the ableton_tools modules inside Live after they change on disk, then refresh the tool list.",
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_audio_analysis",
        description="Measurements of the audio leaving each TSP Spatial Max for Live device, updated ten times a second: "
                    "momentary loudness (LUFS, approximate), peak and RMS per side in dBFS, left/right balance, "
                    "correlation (1 mono, 0 wide, below 0 out of phase), side-to-mid level, and the share of energy "
                    "below 150 Hz and above 5 kHz. Only measures while audio plays. TSP Spatial's Azimuth, Width "
                    "and Distance parameters are set with set_device_parameter.",
        input_schema={"type": "object", "properties": {
            "track": {"type": "string", "description": "Only this track's device."}}, "additionalProperties": False},
    ),
]


class BridgeUnavailable(Exception):
    pass


class BridgeError(Exception):
    pass


_ids = itertools.count(1)


async def bridge_call(method: str, params: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT) -> Any:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, PORT, limit=LINE_LIMIT), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as error:
        raise BridgeUnavailable(
            f"Ableton bridge not reachable at {HOST}:{PORT} ({error.__class__.__name__}). "
            "Open Live and pick TSP MCP as a Control Surface in Settings > Link, Tempo & MIDI."
        ) from error
    try:
        request_id = next(_ids)
        writer.write(json.dumps({"id": request_id, "method": method, "params": params or {}}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise BridgeUnavailable("Ableton bridge closed the connection without replying")
        reply = json.loads(line)
    except asyncio.TimeoutError as error:
        raise BridgeError(f"no reply from Live after {timeout:.0f}s; the call may still be running") from error
    finally:
        writer.close()
    if "error" in reply:
        error = reply["error"]
        raise BridgeError(f"{error.get('type', 'Error')}: {error.get('message', '')}")
    return reply.get("result")


class ToolCatalog:
    def __init__(self) -> None:
        self.tools: list[types.Tool] = []
        self.live = False  # True once the list came from a running bridge
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            self.tools = [self._tool(item) for item in raw]
        except (OSError, ValueError):
            self.tools = []

    @staticmethod
    def _tool(item: dict[str, Any]) -> types.Tool:
        return types.Tool(
            name=item["name"],
            description=item.get("description", ""),
            input_schema=item.get("inputSchema") or {"type": "object"},
        )

    async def refresh(self) -> bool:
        """Fetch the list from the bridge. Returns True if it changed."""
        result = await bridge_call("list_tools", timeout=30)
        raw = result["tools"]
        tools = [self._tool(item) for item in raw]
        changed = [t.model_dump() for t in tools] != [t.model_dump() for t in self.tools]
        self.tools, self.live = tools, True
        if changed:
            try:
                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                CACHE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
        return changed

    def schema(self, name: str) -> dict[str, Any] | None:
        for tool in itertools.chain(LOCAL_TOOLS, self.tools):
            if tool.name == name:
                return tool.input_schema
        return None


catalog = ToolCatalog()
analysis = AnalysisStore()


def validation_errors(name: str, arguments: dict[str, Any]) -> list[str]:
    """Check arguments against the tool's input schema; the SDK doesn't for lowlevel servers."""
    schema = catalog.schema(name)
    if not schema:
        return []
    errors = []
    for error in sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in error.absolute_path)
        errors.append(f"{where}: {error.message}" if where else error.message)
    return errors


def _text(value: Any, is_error: bool = False) -> types.CallToolResult:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


async def _notify_if_changed(ctx: ServerRequestContext, changed: bool) -> None:
    if changed:
        try:
            await ctx.session.send_tool_list_changed()
        except Exception:
            pass


async def list_tools(ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    try:
        await catalog.refresh()
    except (BridgeUnavailable, BridgeError):
        pass  # serve the cached list
    return types.ListToolsResult(tools=[*LOCAL_TOOLS, *catalog.tools])


async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    name, arguments = params.name, params.arguments or {}
    try:
        if name == "ableton_bridge_status":
            status = await bridge_call("status", timeout=10)
            was_live = catalog.live
            await _notify_if_changed(ctx, await catalog.refresh() or not was_live)
            return _text({**status, "address": f"{HOST}:{PORT}"})
        if name == "get_audio_analysis":
            errors = validation_errors(name, arguments)
            if errors:
                return _text(f"invalid arguments for {name}: " + "; ".join(errors), is_error=True)
            return _text(analysis.snapshot(arguments.get("track")))
        if name == "ableton_reload_tools":
            result = await bridge_call("reload_tools", timeout=120)
            await _notify_if_changed(ctx, await catalog.refresh())
            return _text(result, is_error=bool(result.get("error")))
        if not catalog.live:
            # First contact since starting from the cache: pick up any new tools.
            try:
                await _notify_if_changed(ctx, await catalog.refresh())
            except BridgeError:
                pass
        errors = validation_errors(name, arguments)
        if errors:
            return _text(f"invalid arguments for {name}: " + "; ".join(errors), is_error=True)
        request = {"name": name, "arguments": arguments}
        result = await bridge_call("call_tool", request)
        # A tool that needs Live to finish something on a later tick asks to be called again.
        for _attempt in range(RETRIES):
            if not (isinstance(result, dict) and result.get("retry_after")):
                break
            await asyncio.sleep(min(float(result["retry_after"]), 2.0))
            result = await bridge_call("call_tool", request)
        return _text(result)
    except (BridgeUnavailable, BridgeError) as error:
        return _text(str(error), is_error=True)


server = Server(
    "tsp-mcp-for-ableton-live",
    version=metadata.version("tsp-mcp-for-ableton-live"),
    instructions=(
        "Controls Ableton Live through its Remote Script API. Call ableton_bridge_status first, then get_session. "
        "Time is in beats (quarter notes). Nothing here can hear the audio or save the Live Set, "
        "so ask the user how it sounds and remind them to save. "
        "If a reply has a warning field, tell the user straight away."
    ),
    get_tool_input_schema=catalog.schema,
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


class _AnalysisProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, _address: Any) -> None:
        analysis.receive(data)


async def listen_for_analysis() -> asyncio.DatagramTransport | None:
    """Receive TSP Spatial reports. Another server already listening, such as a second session, keeps the port."""
    try:
        transport, _protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _AnalysisProtocol, local_addr=(HOST, ANALYSIS_PORT))
    except OSError as error:
        analysis.error = (f"Can't receive TSP Spatial reports: port {ANALYSIS_PORT} is in use ({error.strerror}), "
                          "probably by another session's server. Close it, or set ABLETON_ANALYSIS_PORT.")
        return None
    return transport


async def _run() -> None:
    transport = await listen_for_analysis()
    try:
        await _serve()
    finally:
        if transport is not None:
            transport.close()


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(NotificationOptions(tools_changed=True)),
        )


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
