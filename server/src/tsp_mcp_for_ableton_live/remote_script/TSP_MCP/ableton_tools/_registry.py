"""Tool registration. Every module in ableton_tools declares its tools with @tool."""

import functools

import Live

TOOLS = {}


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


def tool(name, description, properties=None, required=(), write=False):
    """Register fn as an MCP tool. Every handler runs on Live's main thread.

    properties: JSON Schema properties for the arguments object.
    write: group everything the tool changes into one undo step.
    """
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)

    def wrap(fn):
        handler = _in_undo_step(fn) if write else fn
        TOOLS[name] = {"handler": handler, "description": description, "inputSchema": schema}
        return fn

    return wrap
