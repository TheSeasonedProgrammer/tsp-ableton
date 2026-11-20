"""Xfer Serum 2 presets: finding, describing, converting for Live, and writing new ones.

A .SerumPreset is "XferJson\\0", a little-endian u64 length, a JSON header, then a u32 payload size,
a u32 format number (2) and a zstd frame holding a CBOR map. The header's hash is the MD5 of the zstd
frame. Serum 2's VST3 state uses the same container: the processor state holds the sound keys plus
"component": "processor", and the controller state holds the UI keys and the preset's name. A preset
file has both, so converting one copies its keys' bytes into the two states without re-encoding them.

None of this is documented by Xfer. It was worked out from preset files and Live Sets, and tested
with Serum 2.1.4 in Live 12.4 on Windows.

Standard library only, apart from zstd: compression.zstd on Python 3.14, otherwise the zstandard
package. Without either, only frames of raw blocks can be read, which is what this module writes then.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

try:
    from compression import zstd as _zstd  # Python 3.14

    def _compress(data: bytes) -> bytes:
        return _zstd.compress(data)

    def _decompress(data: bytes) -> bytes:
        return _zstd.decompress(data)
except ImportError:
    try:
        import zstandard as _zstandard

        def _compress(data: bytes) -> bytes:
            return _zstandard.ZstdCompressor().compress(data)

        def _decompress(data: bytes) -> bytes:
            return _zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 28)
    except ImportError:
        _compress = None  # type: ignore[assignment]
        _decompress = None  # type: ignore[assignment]

MAGIC = b"XferJson\0"
EXTENSION = ".SerumPreset"
# The Serum 2 instrument's audio module class, from its moduleinfo.json.
CLASS_ID = b"56534558667350736572756D20320000"
# Top-level keys that belong to the UI (controller) state or only describe the preset file.
UI_KEYS = frozenset({"ClipPlayer", "Filter", "GranularOsc", "MultiSampleOsc", "Osc", "SerumGUI", "SpectralOsc", "WTOsc",
                     "arpBankDisplayName", "clipBankDisplayName", "fileType", "presetAuthor", "presetDescription", "presetName"})
CREATED_FOLDER = ("User", "TSP MCP")
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RAW_BLOCK = 128 * 1024


class SerumError(Exception):
    pass


# --- zstd -------------------------------------------------------------------------------------------

def _raw_frame(data: bytes) -> bytes:
    """A zstd frame of raw (stored) blocks: valid zstd, readable without a zstd library."""
    frame = b"\x28\xb5\x2f\xfd" + bytes([0xE0]) + struct.pack("<Q", len(data))  # single segment, 8-byte size
    blocks = [data[i:i + RAW_BLOCK] for i in range(0, len(data), RAW_BLOCK)] or [b""]
    for index, block in enumerate(blocks):
        last = 1 if index == len(blocks) - 1 else 0
        frame += struct.pack("<I", last | len(block) << 3)[:3] + block
    return frame


def _read_simple_frame(frame: bytes) -> bytes:
    """Read a zstd frame made only of raw and RLE blocks."""
    if frame[:4] != b"\x28\xb5\x2f\xfd":
        raise SerumError("not a zstd frame")
    descriptor = frame[4]
    single, has_checksum, dict_flag, size_flag = descriptor >> 5 & 1, descriptor >> 2 & 1, descriptor & 3, descriptor >> 6
    position = 5 + (0 if single else 1) + (0, 1, 2, 4)[dict_flag]
    position += (1 if single else 0, 2, 4, 8)[size_flag]
    out = bytearray()
    while True:
        header = int.from_bytes(frame[position:position + 3], "little")
        position += 3
        last, kind, size = header & 1, header >> 1 & 3, header >> 3
        if kind == 0:
            out += frame[position:position + size]
            position += size
        elif kind == 1:
            out += frame[position:position + 1] * size
            position += 1
        else:
            raise SerumError("this preset is zstd-compressed; install the zstandard package or use Python 3.14")
        if last:
            break
    if has_checksum:
        position += 4
    return bytes(out)


def compress(data: bytes) -> bytes:
    return _compress(data) if _compress else _raw_frame(data)


def decompress(frame: bytes) -> bytes:
    return _decompress(frame) if _decompress else _read_simple_frame(frame)


# --- CBOR -------------------------------------------------------------------------------------------

def _half(bits: int) -> float:
    sign, exponent, fraction = bits >> 15, bits >> 10 & 31, bits & 1023
    if exponent == 0:
        value = fraction / 1024 * 2 ** -14
    elif exponent == 31:
        value = float("nan") if fraction else float("inf")
    else:
        value = (1 + fraction / 1024) * 2 ** (exponent - 15)
    return -value if sign else value


def decode(data: bytes, position: int = 0) -> tuple[Any, int]:
    """One CBOR item from data at position: (value, position after it)."""
    head = data[position]
    major, info = head >> 5, head & 31
    position += 1
    if info < 24:
        argument = info
    elif info in (24, 25, 26, 27):
        width = 1 << (info - 24)
        raw = data[position:position + width]
        position += width
        if major == 7 and info >= 25:
            return (_half(int.from_bytes(raw, "big")) if info == 25 else
                    struct.unpack(">f" if info == 26 else ">d", raw)[0]), position
        argument = int.from_bytes(raw, "big")
    else:
        raise SerumError("indefinite-length CBOR isn't supported")
    if major == 0:
        return argument, position
    if major == 1:
        return -1 - argument, position
    if major == 2:
        return data[position:position + argument], position + argument
    if major == 3:
        return data[position:position + argument].decode("utf-8"), position + argument
    if major == 4:
        items = []
        for _ in range(argument):
            item, position = decode(data, position)
            items.append(item)
        return items, position
    if major == 5:
        mapping = {}
        for _ in range(argument):
            key, position = decode(data, position)
            mapping[key], position = decode(data, position)
        return mapping, position
    if major == 6:
        return decode(data, position)
    return {20: False, 21: True, 22: None}.get(argument, argument), position


def _head(major: int, argument: int) -> bytes:
    if argument < 24:
        return bytes([major << 5 | argument])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if argument < 1 << (8 * width):
            return bytes([major << 5 | info]) + argument.to_bytes(width, "big")
    raise SerumError("number too large for CBOR")


def encode(value: Any) -> bytes:
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        return _head(3, len(data)) + data
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(encode(item) for item in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(encode(k) + encode(v) for k, v in value.items())
    raise SerumError("can't store %r in a preset" % (value,))


def entries(payload: bytes) -> list[tuple[str, bytes]]:
    """The top-level map's keys, each with the raw bytes of its key and value, in order."""
    if payload[0] >> 5 != 5:
        raise SerumError("the preset's data isn't a map")
    info = payload[0] & 31
    if info < 24:
        count, position = info, 1
    else:
        width = 1 << (info - 24)
        count, position = int.from_bytes(payload[1:1 + width], "big"), 1 + width
    found = []
    for _ in range(count):
        start = position
        key, position = decode(payload, position)
        _, position = decode(payload, position)
        found.append((key, payload[start:position]))
    return found


def _map(items: list[tuple[str, bytes]]) -> bytes:
    items = sorted(items, key=lambda item: item[0].encode("utf-8"))
    return _head(5, len(items)) + b"".join(raw for _, raw in items)


def _entry(key: str, value: Any) -> tuple[str, bytes]:
    return key, encode(key) + encode(value)


# --- the container ------------------------------------------------------------------------------------

def split(blob: bytes) -> tuple[dict[str, Any], bytes]:
    """A preset or state's JSON header and its decompressed CBOR payload."""
    if not blob.startswith(MAGIC):
        raise SerumError("not a Serum 2 preset")
    length = struct.unpack_from("<Q", blob, len(MAGIC))[0]
    start = len(MAGIC) + 8
    header = json.loads(blob[start:start + length])
    size, _format = struct.unpack_from("<II", blob, start + length)
    payload = decompress(blob[start + length + 8:])
    if len(payload) != size:
        raise SerumError("the preset's data is %d bytes, not the %d its header says" % (len(payload), size))
    return header, payload


def join(header: dict[str, Any], payload: bytes) -> bytes:
    frame = compress(payload)
    header = dict(header, hash=hashlib.md5(frame).hexdigest())
    text = json.dumps({key: header[key] for key in sorted(header)}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return MAGIC + struct.pack("<Q", len(text)) + text + struct.pack("<II", len(payload), 2) + frame


def read_header(path: Path) -> dict[str, Any]:
    """Only the JSON header, without decompressing anything."""
    with open(path, "rb") as stream:
        start = stream.read(len(MAGIC) + 8)
        if not start.startswith(MAGIC):
            raise SerumError("not a Serum 2 preset")
        length = struct.unpack_from("<Q", start, len(MAGIC))[0]
        if length > 1 << 20:
            raise SerumError("header too large")
        return json.loads(stream.read(length))


def _common(header: dict[str, Any]) -> dict[str, Any]:
    return {"product": header.get("product", "Serum2"), "productVersion": header.get("productVersion", "2.0.0"),
            "url": header.get("url", "https://xferrecords.com/"), "vendor": header.get("vendor", "Xfer Records"),
            "version": header.get("version", 7.0)}


def to_vstpreset(blob: bytes) -> bytes:
    """A .SerumPreset as a .vstpreset that Live can load: processor state, then controller state."""
    header, payload = split(blob)
    items = entries(payload)
    common = _common(header)
    sound = [(key, raw) for key, raw in items if key not in UI_KEYS] + [_entry("component", "processor")]
    processor = join(dict(common, component="processor"), _map(sound))
    meta = {"presetName": header.get("presetName", ""), "presetAuthor": header.get("presetAuthor", ""),
            "presetDescription": header.get("presetDescription", "")}
    ui = [(key, raw) for key, raw in items if key in UI_KEYS and key not in meta and key != "fileType"]
    ui += [_entry(key, value) for key, value in dict(meta, component="controller", presetHasBeenEdited=False, **common).items()]
    controller = join(dict(common, component="controller", **meta), _map(ui))
    start = 48
    chunks = ((b"Comp", start, len(processor)), (b"Cont", start + len(processor), len(controller)))
    table = b"List" + struct.pack("<i", len(chunks)) + b"".join(tag + struct.pack("<qq", offset, size) for tag, offset, size in chunks)
    end = start + len(processor) + len(controller)
    return b"VST3" + struct.pack("<i", 1) + CLASS_ID + struct.pack("<q", end) + processor + controller + table


# --- describing and editing -------------------------------------------------------------------------

def _settings(node: Any, prefix: str, out: dict[str, Any]) -> None:
    """Flatten the plainParams under node into "Module.Sub.kParamName": value."""
    if isinstance(node, dict):
        params = node.get("plainParams")
        if isinstance(params, dict):
            for name, value in params.items():
                out[prefix + name] = value
        for key, value in node.items():
            if key == "plainParams":
                continue
            if key in ("name", "displayName", "relativePathToWT", "curveDisplayName", "destModuleParamName", "destModuleTypeString") and value:
                out[prefix + key] = value
            elif isinstance(value, (dict, list)):
                _settings(value, prefix + key + ".", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _settings(value, prefix + str(index) + ".", out)


def describe(blob: bytes) -> dict[str, Any]:
    """The preset's metadata and every setting that differs from Serum's defaults."""
    header, payload = split(blob)
    data, _ = decode(payload)
    settings: dict[str, Any] = {}
    for key in sorted(data):
        if key not in UI_KEYS:
            _settings(data[key], key + ".", settings)
    return {"name": header.get("presetName", ""), "author": header.get("presetAuthor", ""),
            "description": header.get("presetDescription", ""), "tags": header.get("tags", []),
            "settings": settings}


def _apply(data: dict[str, Any], path: str, value: Any) -> str:
    """Set one dotted setting. Returns the top-level key it changed."""
    parts = path.split(".")
    if len(parts) < 2:
        raise SerumError("'%s' needs a module, such as Env0.kParamAttack" % path)
    node: Any = data
    for part in parts[:-1]:
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                raise SerumError("'%s': no item %s" % (path, part))
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise SerumError("'%s': no '%s' in this preset. Its settings are listed by get_serum_preset." % (path, part))
    last = parts[-1]
    if not isinstance(node, dict):
        raise SerumError("'%s' doesn't name a module" % path)
    if last.startswith("kParam"):
        if not isinstance(node.get("plainParams"), dict):
            node["plainParams"] = {}
        node["plainParams"][last] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    elif last in node and isinstance(node[last], (str, int, float)) and not isinstance(node[last], bool):
        node[last] = value
    else:
        raise SerumError("'%s': only kParam settings and existing names can be set" % path)
    return parts[0]


def create(blob: bytes, name: str, changes: dict[str, Any], author: str | None = None,
           description: str | None = None, tags: list[str] | None = None) -> bytes:
    """A new preset: blob with changes applied and new metadata. Untouched modules keep their bytes."""
    header, payload = split(blob)
    items = dict(entries(payload))
    data, _ = decode(payload)
    changed = set()
    for path, value in changes.items():
        if path.split(".")[0] in UI_KEYS:
            raise SerumError("'%s' is a display setting, not part of the sound" % path)
        changed.add(_apply(data, path, value))
    for key in changed:
        items[key] = encode(key) + encode(data[key])
    meta = {"presetName": name, "presetAuthor": header.get("presetAuthor", "") if author is None else author,
            "presetDescription": header.get("presetDescription", "") if description is None else description,
            "tags": header.get("tags", []) if tags is None else tags}
    for key, value in meta.items():
        items[key] = encode(key) + encode(value)
    new_header = dict(header, **meta)
    new_header.pop("hash", None)
    return join(new_header, _map(list(items.items())))


# --- files ------------------------------------------------------------------------------------------

def _prefs_file() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Xfer" / "Serum 2" / "Serum2Prefs.json"
    return Path.home() / "Library" / "Application Support" / "Xfer" / "Serum 2" / "Serum2Prefs.json"


def presets_folder() -> Path:
    """Serum 2's Presets folder, from SERUM2_PRESETS, Serum's own preferences, or the default place."""
    if os.environ.get("SERUM2_PRESETS"):
        return Path(os.environ["SERUM2_PRESETS"])
    try:
        configured = json.loads(_prefs_file().read_text(encoding="utf-8")).get("Serum Presets Path")
        if configured:
            return Path(configured) / "Presets"
    except (OSError, ValueError):
        pass
    return Path.home() / "Documents" / "Xfer" / "Serum 2 Presets" / "Presets"


def catalog(folder: Path) -> list[dict[str, Any]]:
    if not folder.is_dir():
        raise SerumError("no Serum 2 presets at %s; set SERUM2_PRESETS to the Presets folder" % folder)
    found = []
    for path in folder.rglob("*" + EXTENSION):
        try:
            header = read_header(path)
        except (OSError, ValueError, SerumError):
            continue
        found.append({"name": header.get("presetName") or path.stem, "path": path.relative_to(folder).as_posix(),
                      "author": header.get("presetAuthor", ""), "tags": header.get("tags", []),
                      "description": header.get("presetDescription", "")})
    return sorted(found, key=lambda preset: preset["path"].lower())


def search(presets: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    words = query.lower().split()
    def text(preset: dict[str, Any]) -> str:
        return " ".join([preset["name"], preset["path"], preset["author"], preset["description"]] + list(preset["tags"])).lower()
    return [preset for preset in presets if all(word in text(preset) for word in words)]


def find(presets: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    key = wanted.lower().removesuffix(EXTENSION.lower())
    matches = [p for p in presets if key in (p["name"].lower(), p["path"].lower().removesuffix(EXTENSION.lower()),
                                              Path(p["path"]).stem.lower())]
    if not matches:
        raise SerumError("no Serum 2 preset '%s'; search with list_serum_presets" % wanted)
    if len(matches) > 1:
        raise SerumError("several presets match '%s'; use one of these paths: %s" % (wanted, ", ".join(p["path"] for p in matches)))
    return matches[0]


def safe_name(text: str) -> str:
    cleaned = UNSAFE.sub("_", text).strip(" .")
    if not cleaned:
        raise SerumError("the name has no usable characters")
    return cleaned
