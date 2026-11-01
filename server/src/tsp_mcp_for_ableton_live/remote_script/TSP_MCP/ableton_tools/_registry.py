"""Tool registration. Every module in ableton_tools declares its tools with @tool."""

import functools

import Live

TOOLS = {}
LIMITER_WARNING = "Hearing safety: no active Ableton Limiter on the Main track. Add one before playing this Set."


def _in_undo_step(fn):
    @functools.wraps(fn)
    def run(args):
        song = Live.Application.get_application().get_document()
        song.begin_undo_step()
        try:
            return fn(args)
        finally:
            song.end_undo_step()

    return run


def _has_active_limiter(devices):
    for device in devices:
        if not device.is_active:
            continue
        if device.class_name == "Limiter":
            return True
        if device.can_have_chains and any(_has_active_limiter(chain.devices) for chain in device.chains):
            return True
    return False


def limiter_warning():
    """The warning to show, or None if the Main track has an active Limiter, including inside a rack."""
    song = Live.Application.get_application().get_document()
    return None if _has_active_limiter(song.master_track.devices) else LIMITER_WARNING


def _with_limiter_warning(fn):
    @functools.wraps(fn)
    def run(args):
        result = fn(args)
        warning = limiter_warning()
        return dict(result, warning=warning) if warning and isinstance(result, dict) else result

    return run


def tool(name, description, properties=None, required=(), write=False, warn=False):
    """Register fn as an MCP tool. Every handler runs on Live's main thread.

    properties: JSON Schema properties for the arguments object.
    write: group everything the tool changes into one undo step.
    warn: add the limiter warning to the reply. Tools with write always do.
    """
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)

    def wrap(fn):
        handler = _in_undo_step(fn) if write else fn
        if write or warn:
            handler = _with_limiter_warning(handler)
        TOOLS[name] = {"handler": handler, "description": description, "inputSchema": schema}
        return fn

    return wrap
