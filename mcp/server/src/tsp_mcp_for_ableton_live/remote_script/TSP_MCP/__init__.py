"""TSP MCP for Ableton Live: exposes Live tool handlers over a local socket.

Runs inside Ableton Live 12 (Python 3.11) as a Remote Script. It does not speak
MCP; the MCP server in server/ does that and forwards calls here. Protocol: one
JSON object per line over TCP on 127.0.0.1.

    -> {"id": 1, "method": "list_tools"}
    <- {"id": 1, "result": {"tools": [{"name", "description", "inputSchema"}]}}
    -> {"id": 2, "method": "call_tool", "params": {"name": "...", "arguments": {...}}}
    <- {"id": 2, "result": <tool return value>}
    <- {"id": 2, "error": {"type": "ValueError", "message": "..."}}

Tools come from the ableton_tools package next to this file.

There are no threads here. The Live API is only safe on Live's main thread, and
Python threads get so little time inside Live that a socket thread adds hundreds
of milliseconds per call. So update_display, which Live calls on the main thread
about ten times a second, polls non-blocking sockets and runs calls directly.

Dev mode: if dev_path.txt sits next to this file, ableton_tools is loaded from
the directory it names (e.g. the repo checkout) and the read-only "inspect"
method is enabled for exploring the Live API.
"""

import importlib
import json
import os
import pkgutil
import socket
import sys
import traceback

from _Framework.ControlSurface import ControlSurface

HOST = "127.0.0.1"
PORT = int(os.environ.get("ABLETON_BRIDGE_PORT", "8768"))
RECEIVE_SIZE = 65536
# Live builds the script as a Set opens, so wait for the Set before reading it. About a second.
LIMITER_CHECK_TICKS = 10

_tools = {}
_tools_error = None
_listener = None
_clients = []
_log = print
_show = None


# --- tool registry ------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEV_FILE = os.path.join(_HERE, "dev_path.txt")


def dev_mode():
    return os.path.isfile(_DEV_FILE)


def _own_tools_root():
    if dev_mode():
        with open(_DEV_FILE, "r", encoding="utf-8") as stream:
            path = stream.read().strip()
        if path:
            return path
    return _HERE


def _reload_package(package_name, root):
    """Freshly import package_name from root and every module in it."""
    for name in [n for n in sys.modules if n == package_name or n.startswith(package_name + ".")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    sys.path.insert(0, root)
    try:
        package = importlib.import_module(package_name)
        found = os.path.normcase(os.path.dirname(os.path.abspath(package.__file__)))
        expected = os.path.normcase(os.path.join(root, package_name))
        if found != expected:
            raise RuntimeError("'%s' resolved to %s, expected %s" % (package_name, found, expected))
        names = sorted(info.name for info in pkgutil.iter_modules([found]))
        # Helpers (leading underscore) first.
        modules = [importlib.import_module(package_name + "." + name) for name in sorted(names, key=lambda n: not n.startswith("_"))]
    finally:
        sys.path.remove(root)
    return package, modules


def load_tools():
    """(Re)load the ableton_tools package."""
    global _tools, _tools_error
    registry = {}
    error_text = None
    try:
        package, _modules = _reload_package("ableton_tools", _own_tools_root())
        package.register(registry)
    except Exception as error:
        error_text = "ableton_tools: %s: %s" % (type(error).__name__, error)
        _log(traceback.format_exc())
    if registry or not _tools:
        _tools = registry
    _tools_error = error_text
    return len(_tools)


# --- requests -----------------------------------------------------------------

def _live_version():
    import Live
    application = Live.Application.get_application()
    return "%d.%d.%d" % (application.get_major_version(), application.get_minor_version(), application.get_bugfix_version())


def _handle(request):
    method = request.get("method")
    if method == "list_tools":
        if _tools_error and not _tools:
            raise RuntimeError("tools failed to load: " + _tools_error)
        return {"tools": [
            {"name": name, "description": meta.get("description", ""), "inputSchema": meta.get("inputSchema", {"type": "object"})}
            for name, meta in sorted(_tools.items())
        ]}
    if method == "call_tool":
        params = request.get("params") or {}
        name = params.get("name")
        tool = _tools.get(name)
        if tool is None:
            raise ValueError("unknown tool: %s" % name)
        result = tool["handler"](params.get("arguments") or {})
        if _show and isinstance(result, dict) and result.get("warning"):
            _show(result["warning"])
        return result
    if method == "reload_tools":
        return {"count": load_tools(), "error": _tools_error}
    if method == "status":
        return {
            "bridge": "TSP MCP",
            "live": _live_version(),
            "python": sys.version.split()[0],
            "tools": len(_tools),
            "tools_error": _tools_error,
            "dev": dev_mode(),
        }
    if method == "inspect" and dev_mode():
        return _inspect(request.get("params") or {})
    raise ValueError("unknown method: %s" % method)


def _inspect(params):
    """Dev only: describe a dotted attribute path. Reads attributes, never calls anything.

    params: {"path": "Live.Clip.Clip"} or {"path": "song.tracks"}
    """
    import Live
    application = Live.Application.get_application()
    roots = {"Live": Live, "application": application, "song": application.get_document()}
    parts = (params.get("path") or "Live").split(".")
    target = roots[parts[0]]
    for part in parts[1:]:
        target = target[int(part)] if part.isdigit() else getattr(target, part)
    members = []
    for name in dir(target):
        if name.startswith("_"):
            continue
        try:
            value = getattr(target, name)
        except Exception as error:
            members.append({"name": name, "error": str(error)})
            continue
        doc = (getattr(value, "__doc__", None) or "").strip()
        members.append({"name": name, "kind": type(value).__name__, "doc": doc[:400]})
    return {"type": type(target).__name__, "repr": repr(target)[:300], "doc": (getattr(target, "__doc__", None) or "").strip()[:1500], "members": members}


def _reply_to(line):
    request_id = None
    try:
        request = json.loads(line.decode("utf-8"))
        request_id = request.get("id")
        reply = {"id": request_id, "result": _handle(request)}
    except Exception as error:
        reply = {"id": request_id, "error": {"type": type(error).__name__, "message": str(error)}}
    try:
        data = json.dumps(reply, ensure_ascii=False, default=str)
    except Exception as error:
        data = json.dumps({"id": request_id, "error": {"type": "SerializationError", "message": str(error)}})
    return data.encode("utf-8") + b"\n"


# --- sockets ------------------------------------------------------------------

class _Client:
    def __init__(self, connection):
        self.connection = connection
        self.incoming = b""
        self.outgoing = b""


def _serve(client):
    """Read what has arrived, run complete requests, send what fits. False once the client is gone."""
    try:
        while True:
            data = client.connection.recv(RECEIVE_SIZE)
            if not data:
                return False
            client.incoming += data
    except BlockingIOError:
        pass
    except OSError:
        return False
    while b"\n" in client.incoming:
        line, client.incoming = client.incoming.split(b"\n", 1)
        if line.strip():
            client.outgoing += _reply_to(line)
    try:
        while client.outgoing:
            client.outgoing = client.outgoing[client.connection.send(client.outgoing):]
    except BlockingIOError:
        pass
    except OSError:
        return False
    return True


def poll():
    """Accept connections and serve clients. Only call this on Live's main thread."""
    if _listener is None:
        return
    try:
        while True:
            connection, _address = _listener.accept()
            connection.setblocking(False)
            _clients.append(_Client(connection))
    except BlockingIOError:
        pass
    for client in list(_clients):
        if not _serve(client):
            client.connection.close()
            _clients.remove(client)


def start_server():
    global _listener
    if _listener is not None:
        return
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Live restarts the script whenever a Set opens. On macOS and Linux, connections the
        # old listener accepted can hold the port in TIME_WAIT, and without SO_REUSEADDR the
        # new bind fails. There it still refuses a second active listener. Not on Windows,
        # where SO_REUSEADDR would let two servers share the port.
        if os.name != "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
        listener.listen(8)
        listener.setblocking(False)
    except OSError:
        listener.close()
        raise
    _listener = listener
    _log("TSP MCP listening on %s:%d (%d tools)" % (HOST, PORT, len(_tools)))


def stop_server():
    global _listener
    listener, _listener = _listener, None
    if listener is not None:
        listener.close()
    for client in _clients:
        client.connection.close()
    del _clients[:]


# --- Live entry point ---------------------------------------------------------

class TspMcp(ControlSurface):
    def __init__(self, c_instance):
        global _log, _show
        ControlSurface.__init__(self, c_instance)
        _log = self.log_message
        _show = self.show_message
        load_tools()
        try:
            start_server()
            self.show_message("TSP MCP: listening on port %d" % PORT)
        except OSError as error:
            self.log_message("TSP MCP could not listen on %s:%d: %s" % (HOST, PORT, error))
            self.show_message("TSP MCP: port %d is in use" % PORT)
        self.schedule_message(LIMITER_CHECK_TICKS, self._check_limiter)

    def _check_limiter(self):
        package = sys.modules.get("ableton_tools")
        warning = package.limiter_warning() if package else None
        if warning:
            self.show_message(warning)

    def update_display(self):
        ControlSurface.update_display(self)
        poll()

    def disconnect(self):
        global _show
        _show = None
        stop_server()
        ControlSurface.disconnect(self)


def create_instance(c_instance):
    return TspMcp(c_instance)
