"""Builds TSP Spatial.amxd, a Max for Live audio effect, from src/.

The device places a stereo signal (azimuth, width, distance) and sends measurements of
its output ten times a second as OSC over UDP to the TSP MCP server on 127.0.0.1.

    python build.py              writes devices/TSP Spatial.amxd
    python build.py --install    also copies it into Live's User Library

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOT = Path(__file__).parent
GEN_SOURCE = ROOT / "src" / "tsp_spatial.genexpr"
OUTPUT = ROOT / "devices" / "TSP Spatial.amxd"
INSTALL_FOLDER = ("TSP MCP", "Devices")
PORT = 8769
REPORT_MS = 100
# The measurements gen~ outputs after the two audio outlets, in order. The MCP server reads them in this order.
MEASUREMENTS = ("peak_l", "peak_r", "ms_l", "ms_r", "ms_lr", "ms_mid", "ms_side", "ms_low", "ms_high", "k_ms", "samplerate")
AUDIO_EFFECT = b"aaaa"

# name, minimum, maximum, initial, unit style (1 float, 5 percent), gen~ parameter
PARAMETERS = (
    ("Azimuth", -90.0, 90.0, 0.0, 1, "azimuth"),
    ("Width", 0.0, 200.0, 100.0, 5, "width"),
    ("Distance", 0.0, 100.0, 0.0, 5, "distance"),
)
APP_VERSION = {"major": 8, "minor": 1, "revision": 2, "architecture": "x64", "modernui": 1}
# Copied from Live's own "Max Audio Effect" template. Max crashes, taking Live with it, if the project
# block lacks any of these, so keep them all.
PROJECT = {
    "version": 1, "creationdate": 3590052493, "modificationdate": 3590052493, "viewrect": [0.0, 0.0, 300.0, 500.0],
    "autoorganize": 1, "hideprojectwindow": 1, "showdependencies": 1, "autolocalize": 0,
    "contents": {"patchers": {}}, "layout": {}, "searchpath": {}, "detailsvisible": 0,
    "amxdtype": int.from_bytes(AUDIO_EFFECT, "big"), "readonly": 0, "devpathtype": 0, "devpath": ".",
    "sortmode": 0, "viewmode": 0,
}
WINDOW = {
    "bglocked": 0, "default_fontface": 0, "gridonopen": 1, "gridsize": [8.0, 8.0], "gridsnaponopen": 1,
    "objectsnaponopen": 1, "statusbarvisible": 2, "toolbarvisible": 1, "lefttoolbarpinned": 0, "toptoolbarpinned": 0,
    "righttoolbarpinned": 0, "bottomtoolbarpinned": 0, "toolbars_unpinned_last_save": 0, "tallnewobj": 0,
    "boxanimatetime": 500, "enablehscroll": 1, "enablevscroll": 1, "digest": "", "tags": "", "style": "",
    "subpatcher_template": "",
}


class Patcher:
    """Collects boxes and patch cords, numbering the boxes obj-1, obj-2..."""

    def __init__(self) -> None:
        self.boxes: list[dict] = []
        self.lines: list[dict] = []

    def add(self, text: str | None, inlets: int, outlets: int, rect: tuple[float, float, float, float],
            maxclass: str = "newobj", outlettype: list[str] | None = None, **extra) -> str:
        box_id = "obj-%d" % (len(self.boxes) + 1)
        box = {"id": box_id, "maxclass": maxclass, "numinlets": inlets, "numoutlets": outlets,
               "patching_rect": list(rect)}
        if outlets:
            box["outlettype"] = outlettype or [""] * outlets
        if text is not None:
            box["text"] = text
        box.update(extra)
        self.boxes.append({"box": box})
        return box_id

    def connect(self, source: str, outlet: int, destination: str, inlet: int) -> None:
        self.lines.append({"patchline": {"source": [source, outlet], "destination": [destination, inlet]}})

    def patcher(self, **settings) -> dict:
        return dict({"fileversion": 1, "appversion": APP_VERSION, "boxes": self.boxes, "lines": self.lines}, **settings)


def gen_patcher(code: str) -> dict:
    """The gen~ subpatcher: two inputs into the codebox, and every codebox outlet to an out."""
    outlets = 2 + len(MEASUREMENTS)
    gen = Patcher()
    in_l = gen.add("in 1", 0, 1, (20, 20, 30, 22))
    in_r = gen.add("in 2", 0, 1, (200, 20, 30, 22))
    code_box = gen.add(None, 2, outlets, (20, 60, 600, 400), maxclass="codebox", code=code, fontname="Arial", fontsize=12.0)
    gen.connect(in_l, 0, code_box, 0)
    gen.connect(in_r, 0, code_box, 1)
    for index in range(outlets):
        out = gen.add("out %d" % (index + 1), 1, 0, (20 + index * 45, 480, 40, 22))
        gen.connect(code_box, index, out, 0)
    return gen.patcher(classnamespace="dsp.gen", rect=[50, 50, 700, 560])


def device_patcher(code: str) -> dict:
    p = Patcher()
    measured = len(MEASUREMENTS)

    # Audio: Live -> gen~ -> Live.
    plugin = p.add("plugin~", 2, 2, (20, 20, 60, 22), outlettype=["signal", "signal"])
    gen = p.add("gen~", 2, 2 + measured, (20, 200, 560, 22), outlettype=["signal"] * (2 + measured),
                patcher=gen_patcher(code))
    plugout = p.add("plugout~", 2, 2, (20, 420, 60, 22), outlettype=["signal", "signal"])
    p.connect(plugin, 0, gen, 0)
    p.connect(plugin, 1, gen, 1)
    p.connect(gen, 0, plugout, 0)
    p.connect(gen, 1, plugout, 1)

    # Dials: each sends "<gen~ parameter> <value>" to gen~. Max lists them by box id in "parameters".
    parameters = {}
    for index, (name, low, high, initial, unitstyle, gen_name) in enumerate(PARAMETERS):
        x = 20 + index * 150
        dial = p.add(None, 1, 2, (x, 100, 44, 48), maxclass="live.dial", outlettype=["", "float"],
                     parameter_enable=1, varname=name, presentation=1,
                     presentation_rect=[10 + index * 56, 10, 44, 48],
                     saved_attribute_attributes={"valueof": {
                         "parameter_longname": name, "parameter_shortname": name, "parameter_type": 0,
                         "parameter_mmin": low, "parameter_mmax": high,
                         "parameter_initial_enable": 1, "parameter_initial": [initial],
                         "parameter_unitstyle": unitstyle,
                     }})
        parameters[dial] = [name, name, 0]
        prepend = p.add("prepend " + gen_name, 1, 1, (x, 160, 100, 22))
        p.connect(dial, 0, prepend, 0)
        p.connect(prepend, 0, gen, 0)

    # Which track the device is on: the track's id and its name, kept up to date by live.observer.
    this_device = p.add("live.thisdevice", 1, 3, (620, 20, 90, 22), outlettype=["bang", "int", "int"])
    find_track = p.add("path this_device canonical_parent", 2, 1, (620, 50, 190, 22), maxclass="message")
    path = p.add("live.path", 1, 3, (620, 80, 90, 22), outlettype=["", "", ""])
    split = p.add("t l l", 1, 2, (620, 110, 50, 22))
    observer = p.add("live.observer", 2, 2, (760, 170, 90, 22), outlettype=["", ""])
    watch_name = p.add("property name", 2, 1, (760, 140, 90, 22), maxclass="message")
    route_id = p.add("route id", 2, 2, (620, 140, 60, 22))
    name_symbol = p.add("tosymbol", 1, 1, (760, 200, 60, 22))
    label = p.add("pak 0 s", 2, 1, (620, 230, 60, 22))
    set_address = p.add("set /tsp/analysis $1 $2", 2, 1, (620, 260, 190, 22), maxclass="message")
    p.connect(this_device, 0, find_track, 0)
    p.connect(this_device, 0, watch_name, 0)
    p.connect(watch_name, 0, observer, 0)
    p.connect(find_track, 0, path, 0)
    p.connect(path, 0, split, 0)
    p.connect(split, 1, observer, 1)
    p.connect(split, 0, route_id, 0)
    p.connect(route_id, 0, label, 0)
    p.connect(observer, 0, name_symbol, 0)
    p.connect(name_symbol, 0, label, 1)
    p.connect(label, 0, set_address, 0)

    # Reports: a metro bangs every snapshot~, rightmost first, so the left inlet of pack fires last.
    start = p.add("1", 2, 1, (900, 20, 30, 22), maxclass="message")
    metro = p.add("metro %d" % REPORT_MS, 2, 1, (900, 50, 60, 22), outlettype=["bang"])
    trigger = p.add("t" + " b" * measured, 1, measured, (900, 80, 240, 22), outlettype=["bang"] * measured)
    pack = p.add("pack" + " f" * measured, measured, 1, (20, 320, 560, 22))
    address = p.add("prepend /tsp/analysis 0 unknown", 1, 1, (20, 350, 200, 22))
    send = p.add("udpsend 127.0.0.1 %d" % PORT, 1, 0, (20, 380, 150, 22))
    p.connect(this_device, 0, start, 0)
    p.connect(start, 0, metro, 0)
    p.connect(metro, 0, trigger, 0)
    for index in range(measured):
        snapshot = p.add("snapshot~", 2, 1, (20 + index * 50, 260, 45, 22), outlettype=["float"])
        p.connect(gen, 2 + index, snapshot, 0)
        p.connect(trigger, index, snapshot, 0)
        p.connect(snapshot, 0, pack, index)
    p.connect(set_address, 0, address, 0)
    p.connect(pack, 0, address, 0)
    p.connect(address, 0, send, 0)

    p.add("TSP Spatial: reports to TSP MCP", 1, 0, (10, 62, 170, 18), maxclass="live.comment",
          presentation=1, presentation_rect=[10, 62, 170, 18], textjustification=0)

    return p.patcher(
        classnamespace="box",
        rect=[100, 100, 1200, 520],
        openrect=[0.0, 0.0, 0.0, 169.0],
        openinpresentation=1,
        default_fontsize=10.0,
        default_fontname="Arial Bold",
        devicewidth=180.0,
        description="Places a stereo signal and reports measurements to TSP MCP.",
        dependency_cache=[],
        latency=0,
        parameters=parameters,
        project=PROJECT,
        autosave=0,
        **WINDOW,
    )


def amxd(patcher: dict, kind: bytes = AUDIO_EFFECT) -> bytes:
    """The .amxd container: ampf header, empty meta chunk, then the patcher as null-terminated JSON."""
    body = (json.dumps({"patcher": patcher}, indent="\t") + "\n").encode("utf-8") + b"\0"
    return (b"ampf" + struct.pack("<I", 4) + kind
            + b"meta" + struct.pack("<I", 4) + b"\0\0\0\0"
            + b"ptch" + struct.pack("<I", len(body)) + body)


def build(output: Path = OUTPUT) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(amxd(device_patcher(GEN_SOURCE.read_text(encoding="utf-8"))))
    return output


def find_user_library() -> Path:
    """The User Library of the most recently used Live, from its Library.cfg."""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Ableton"
        pattern = "Live */Preferences/Library.cfg"
    else:
        root = Path.home() / "Library" / "Preferences" / "Ableton"
        pattern = "Live */Library.cfg"
    files = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("No Live preferences found. Open Live once, or pass --user-library <folder>.")
    project = ElementTree.parse(files[0]).getroot().find("ContentLibrary/UserLibrary/LibraryProject")
    return Path(project.find("ProjectPath").get("Value")) / project.find("ProjectName").get("Value")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the TSP Spatial Max for Live device.")
    parser.add_argument("--install", action="store_true", help="copy the device into Live's User Library")
    parser.add_argument("--user-library", type=Path, help="User Library folder, instead of reading Live's preferences")
    args = parser.parse_args()
    built = build()
    print("built", built)
    if args.install:
        library = args.user_library or find_user_library()
        if not library.is_dir():
            raise SystemExit("no User Library at %s" % library)
        target = library.joinpath(*INSTALL_FOLDER)
        target.mkdir(parents=True, exist_ok=True)
        print("installed", shutil.copy2(built, target / built.name))


if __name__ == "__main__":
    main()
