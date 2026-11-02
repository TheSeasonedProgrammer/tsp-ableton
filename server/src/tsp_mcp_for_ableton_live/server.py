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
import re
from importlib import metadata
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from jsonschema import Draft202012Validator
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from tsp_mcp_for_ableton_live import serum
from tsp_mcp_for_ableton_live.analysis import AnalysisStore
from tsp_mcp_for_ableton_live.install import InstallError, find_user_library

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
    types.Tool(
        name="list_serum_presets",
        description="Find Xfer Serum 2 presets (.SerumPreset) in Serum's preset folder. Every word of query must appear "
                    "in the name, folder, author, tags or description. Serum 1 .fxp presets aren't included.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for, such as \"pluck\" or \"bass reese\"."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 30."}},
            "additionalProperties": False},
    ),
    types.Tool(
        name="get_serum_preset",
        description="Show how a Serum 2 preset is built: every setting that differs from Serum's defaults, as "
                    "\"Module.kParamName\": value, such as \"Env0.kParamAttack\" (seconds) or "
                    "\"Oscillator1.kParamUnison\". Settings not listed are at their defaults.",
        input_schema={"type": "object", "properties": {
            "preset": {"type": "string", "description": "Preset name, or its path from list_serum_presets."}},
            "required": ["preset"], "additionalProperties": False},
    ),
    types.Tool(
        name="load_serum_preset",
        description="Load a Serum 2 preset onto a track as a fresh Serum 2 instance, replacing the track's instrument "
                    "and keeping its clips. The first load of a preset waits a few seconds for Live's browser. "
                    "Presets that use wavetables or samples from content that isn't installed open a Missing Files "
                    "window inside Serum, which the user closes; the rest of the sound still loads.",
        input_schema={"type": "object", "properties": {
            "track": {"type": ["integer", "string"], "description": "Track index (0-based) or name."},
            "preset": {"type": "string", "description": "Preset name, or its path from list_serum_presets."}},
            "required": ["track", "preset"], "additionalProperties": False},
    ),
    types.Tool(
        name="create_serum_preset",
        description="Make a new Serum 2 preset from an existing one with changed settings, saved under "
                    "Presets/User/TSP MCP so it also shows in Serum's browser. Settings use the names "
                    "get_serum_preset shows, such as {\"Env0.kParamAttack\": 0.8, \"Oscillator0.kParamUnison\": 7}; a "
                    "kParam missing from the base can be added. The reply lists names no local preset uses, "
                    "which may be misspelt. Give track to load it straight away.",
        input_schema={"type": "object", "properties": {
            "base": {"type": "string", "description": "Preset to start from: name or path."},
            "name": {"type": "string", "minLength": 1},
            "settings": {"type": "object", "additionalProperties": {"type": ["number", "string", "boolean"]}},
            "author": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "overwrite": {"type": "boolean", "description": "Replace a preset of the same name made earlier."},
            "track": {"type": ["integer", "string"], "description": "Load it onto this track afterwards."}},
            "required": ["base", "name", "settings"], "additionalProperties": False},
    ),
]
# How long load_serum_preset waits for Live's browser to find a new .vstpreset.
BROWSER_WAIT_SECONDS = 30.0


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
        if name == "get_audio_analysis" or name in SERUM_TOOLS:
            errors = validation_errors(name, arguments)
            if errors:
                return _text(f"invalid arguments for {name}: " + "; ".join(errors), is_error=True)
            if name == "get_audio_analysis":
                return _text(analysis.snapshot(arguments.get("track")))
            try:
                return _text(await SERUM_TOOLS[name](arguments))
            except (serum.SerumError, InstallError, OSError) as error:
                return _text(str(error), is_error=True)
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


_known_settings: dict[str, tuple[float, set[str]]] = {}


def _known_setting_names(presets: list[dict[str, Any]], folder: Path) -> set[str]:
    """Setting names used by any local preset, with module numbers removed, cached by file time."""
    names: set[str] = set()
    for preset in presets:
        path = folder / preset["path"]
        try:
            stamp = path.stat().st_mtime
            if _known_settings.get(preset["path"], (None,))[0] != stamp:
                found = {re.sub(r"\d+", "#", key) for key in serum.describe(path.read_bytes())["settings"]}
                _known_settings[preset["path"]] = (stamp, found)
            names |= _known_settings[preset["path"]][1]
        except (OSError, ValueError, serum.SerumError):
            continue
    return names


async def _list_serum(arguments: dict[str, Any]) -> dict[str, Any]:
    folder = serum.presets_folder()
    presets = serum.catalog(folder)
    found = serum.search(presets, arguments.get("query", ""))
    return {"folder": str(folder), "total": len(presets), "matched": len(found), "presets": found[:arguments.get("limit", 30)]}


async def _get_serum(arguments: dict[str, Any]) -> dict[str, Any]:
    folder = serum.presets_folder()
    preset = serum.find(serum.catalog(folder), arguments["preset"])
    return dict(serum.describe((folder / preset["path"]).read_bytes()), path=preset["path"])


async def _load_serum(arguments: dict[str, Any]) -> dict[str, Any]:
    folder = serum.presets_folder()
    preset = serum.find(serum.catalog(folder), arguments["preset"])
    return await _load_serum_file(folder / preset["path"], preset["path"], arguments["track"])


async def _load_serum_file(source: Path, relative: str, track: int | str) -> dict[str, Any]:
    data = serum.to_vstpreset(source.read_bytes())
    file_name = serum.safe_name(relative.removesuffix(serum.EXTENSION).replace("/", " - ")) + ".vstpreset"
    target = find_user_library() / "TSP MCP" / "Serum 2" / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.read_bytes() != data:
        target.write_bytes(data)
    folder_path = ["user_library", "TSP MCP", "Serum 2"]
    deadline = asyncio.get_running_loop().time() + BROWSER_WAIT_SECONDS
    while True:
        try:
            listing = await bridge_call("call_tool", {"name": "browse", "arguments": {"path": folder_path}})
            if any(item["name"] == file_name for item in listing["items"]):
                break
        except BridgeError:
            pass  # the folder itself may not be indexed yet
        if asyncio.get_running_loop().time() > deadline:
            raise serum.SerumError("Live's browser hasn't found %s after %.0f s; try again shortly" % (file_name, BROWSER_WAIT_SECONDS))
        await asyncio.sleep(1.0)
    loaded = await bridge_call("call_tool", {"name": "load_from_browser", "arguments": {"track": track, "path": folder_path + [file_name]}})
    result = {"loaded": relative, "track": loaded["track"], "devices": loaded["devices"]}
    if loaded.get("warning"):
        result["warning"] = loaded["warning"]
    return result


async def _create_serum(arguments: dict[str, Any]) -> dict[str, Any]:
    folder = serum.presets_folder()
    presets = serum.catalog(folder)
    base = serum.find(presets, arguments["base"])
    blob = serum.create((folder / base["path"]).read_bytes(), arguments["name"], arguments["settings"],
                        author=arguments.get("author"), description=arguments.get("description"), tags=arguments.get("tags"))
    target = folder.joinpath(*serum.CREATED_FOLDER, serum.safe_name(arguments["name"]) + serum.EXTENSION)
    if target.exists() and not arguments.get("overwrite"):
        raise serum.SerumError("%s already exists; pass overwrite: true to replace it" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    known = _known_setting_names(presets, folder)
    unknown = sorted(path for path in arguments["settings"] if re.sub(r"\d+", "#", path) not in known)
    relative = target.relative_to(folder).as_posix()
    result: dict[str, Any] = {"created": relative, "base": base["path"], "settings_changed": len(arguments["settings"])}
    if unknown:
        result["unrecognized"] = unknown
        result["note"] = "No local preset uses these names. Serum ignores names it doesn't know, so check the spelling."
    if "track" in arguments:
        result["load"] = await _load_serum_file(target, relative, arguments["track"])
    return result


SERUM_TOOLS = {"list_serum_presets": _list_serum, "get_serum_preset": _get_serum,
               "load_serum_preset": _load_serum, "create_serum_preset": _create_serum}


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
