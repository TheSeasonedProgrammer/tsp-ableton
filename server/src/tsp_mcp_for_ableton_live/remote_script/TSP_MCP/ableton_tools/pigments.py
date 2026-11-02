"""Arturia Pigments presets.

Pigments only reports "Default" in Device.presets, so Live's API can't pick its presets. Its preset
files hold the plugin's VST3 state though, so one wrapped in a .vstpreset loads through Live's browser.
"""

import os
import re
import struct
import sys

from ableton_tools._registry import tool
from ableton_tools import _util as u
from ableton_tools import browser

PRESET_ROOT = os.path.join(os.environ.get("PROGRAMDATA", "/Library"), "Arturia", "Presets", "Pigments")
SKIPPED = ("Tutorials",)
HEADER_SIZE = 4096
# "ArtuAVISKat1Proc", from Live's plugin database.
CLASS_ID = b"41727475415649534B61743150726F63"
PRESET_FOLDER = ("TSP MCP", "Pigments")
UNSAFE = re.compile(r'[<>:"/\\|?*]')

_headers = {}


def _text(data, position):
    end = data.index(b" ", position)
    size = int(data[position:end])
    return data[end + 1:end + 1 + size].decode("utf-8", "replace"), end + 2 + size


def _number(data, position):
    end = data.index(b" ", position)
    return int(data[position:end]), end + 1


def parse_header(data):
    """Read a preset's metadata. It's a boost text archive of length-prefixed strings."""
    position = data.index(b"archive ") + len(b"archive ")
    for _ in range(5):
        _, position = _number(data, position)
    name, position = _text(data, position)
    bank, position = _text(data, position)
    _, position = _number(data, position)
    author, position = _text(data, position)
    kind, position = _text(data, position)
    header = {"name": name, "bank": bank, "author": author, "type": kind, "tags": [], "description": ""}
    try:
        for _ in range(2):
            _, position = _number(data, position)
        count, position = _number(data, position)
        _, position = _number(data, position)
        for _ in range(count):
            tag, position = _text(data, position)
            header["tags"].append(tag)
        for _ in range(2):
            _, position = _number(data, position)
        header["description"], position = _text(data, position)
    except ValueError:
        pass  # Only the fields above are worth failing over; the header can end before these.
    return header


def vstpreset(state):
    """Wrap plugin state in a .vstpreset: header, Comp and an empty Cont chunk, then the chunk list."""
    start = 48
    end = start + len(state)
    chunks = ((b"Comp", start, len(state)), (b"Cont", end, 0))
    table = b"List" + struct.pack("<i", len(chunks))
    for tag, offset, size in chunks:
        table += tag + struct.pack("<qq", offset, size)
    return b"VST3" + struct.pack("<i", 1) + CLASS_ID + struct.pack("<q", end) + state + table


def _catalog():
    """Every preset, read fresh so new ones appear. Headers are cached, since Live waits on this."""
    if not os.path.isdir(PRESET_ROOT):
        raise ValueError("no Pigments presets at %s" % PRESET_ROOT)
    presets = []
    for folder, _folders, names in os.walk(PRESET_ROOT):
        relative = os.path.relpath(folder, PRESET_ROOT).replace(os.sep, "/")
        if relative.split("/")[0] in SKIPPED:
            continue
        for name in names:
            if "." in name:
                continue
            path = os.path.join(folder, name)
            if path not in _headers:
                with open(path, "rb") as stream:
                    data = stream.read(HEADER_SIZE)
                try:
                    _headers[path] = parse_header(data)
                except ValueError:
                    _headers[path] = None
            if _headers[path] is not None:
                presets.append(dict(_headers[path], name=name, path="%s/%s" % (relative, name)))
    return sorted(presets, key=lambda preset: preset["path"].lower())


def _find(presets, wanted):
    wanted = wanted.lower()
    matches = [preset for preset in presets if wanted in (preset["path"].lower(), preset["name"].lower())]
    if not matches:
        raise ValueError("no Pigments preset '%s'; search with list_pigments_presets" % wanted)
    if len(matches) > 1:
        raise ValueError("several presets are called '%s'; use one of these paths: %s" % (wanted, ", ".join(p["path"] for p in matches)))
    return matches[0]


def _user_library():
    # Installed at <User Library>/Remote Scripts/TSP_MCP. In dev mode only ableton_tools is elsewhere.
    script = os.path.abspath(sys.modules["TSP_MCP"].__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(script)))


def _write_preset(preset):
    """Write the .vstpreset unless it's already there, and return its file name."""
    with open(os.path.join(PRESET_ROOT, *preset["path"].split("/")), "rb") as stream:
        data = vstpreset(stream.read())
    name = UNSAFE.sub("_", preset["path"].replace("/", " - ")) + ".vstpreset"
    folder = os.path.join(_user_library(), *PRESET_FOLDER)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if os.path.isfile(path):
        with open(path, "rb") as stream:
            if stream.read() == data:
                return name
    with open(path, "wb") as stream:
        stream.write(data)
    return name


@tool("list_pigments_presets", "Find Arturia Pigments presets, factory and user. Every word of query must appear in the "
                               "name, type, bank, author, tags or description. Many presets have no type, so searching "
                               "query finds more than type does.",
      {"query": {"type": "string", "description": "Words to look for, such as \"warm pad\"."},
       "type": {"type": "string", "description": "Exact preset type: Bass, Brass, Keys, Lead, Organ, Pad, Percussion, Sequence, SFX, Strings, Template."},
       "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 30."}})
def list_pigments_presets(args):
    words = args.get("query", "").lower().split()
    kind = args.get("type", "").lower()
    found = []
    presets = _catalog()
    for preset in presets:
        text = " ".join([preset["name"], preset["type"], preset["bank"], preset["author"], preset["description"]] + preset["tags"]).lower()
        if all(word in text for word in words) and (not kind or preset["type"].lower() == kind):
            found.append(preset)
    return {"total": len(presets), "matched": len(found), "presets": found[:args.get("limit", 30)]}


@tool("load_pigments_preset", "Load a Pigments preset onto a track, as a fresh Pigments instance. It replaces the track's "
                              "instrument, including any parameters configured in Live's plugin panel, and keeps the clips. "
                              "A preset's first load can return loaded: false while Live's browser finds the new file, "
                              "which takes a few seconds. Call again with the same arguments when it does.",
      {"track": u.TRACK,
       "preset": {"type": "string", "description": "Preset name, or its path from list_pigments_presets if the name isn't unique."}},
      ("track", "preset"), write=True)
def load_pigments_preset(args):
    track = u.find_track(args["track"])
    preset = _find(_catalog(), args["preset"])
    name = _write_preset(preset)
    path = ["user_library"] + list(PRESET_FOLDER) + [name]
    try:
        browser._item_at(path)
    except ValueError:
        return {"loaded": False, "preset": preset["path"], "message": "Live's browser hasn't found the preset yet. Call again in a few seconds."}
    browser.load(track, path)
    return {"loaded": True, "preset": preset["path"], "track": track.name, "devices": [device.name for device in track.devices]}
