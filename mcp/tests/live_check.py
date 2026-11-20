"""Exercises the tools against a real, running Live. Run it with a Live Set you don't mind changing.

    uv run --no-project --python 3.11 python tests/live_check.py

It adds a temporary track, works on that, and deletes it again.
"""

import json
import socket
import sys

ADDRESS = ("127.0.0.1", 8768)
TRACK = "MCP live check"
failures = []


def send(method, params=None):
    with socket.create_connection(ADDRESS, timeout=60) as connection:
        connection.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
        with connection.makefile(encoding="utf-8") as stream:
            return json.loads(stream.readline())


def call(tool, /, **arguments):
    reply = send("call_tool", {"name": tool, "arguments": arguments})
    if "error" in reply:
        raise RuntimeError("%s: %s" % (reply["error"]["type"], reply["error"]["message"]))
    return reply["result"]


def check(label, function):
    try:
        result = function()
    except Exception as error:
        failures.append(label)
        print("FAIL %-28s %s" % (label, error))
        return None
    print("ok   %-28s %s" % (label, json.dumps(result, ensure_ascii=False)[:120]))
    return result


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def beat_check():
    written = call("write_drum_pattern", track=TRACK, slot=0, bars=2, name="Check", swing=56, humanize_ms=4, humanize_velocity=8, seed=1, pattern={
        "kick": "x...|..x.|x...|....", "snare": "....|x...|....|x..o", "closed_hat": "x.x.|x.x.|x.x.|x.X."})
    notes = call("get_notes", track=TRACK, slot=0)
    expect(notes["count"] == written["notes_written"], "wrote %d, read back %d" % (written["notes_written"], notes["count"]))
    return written


def edit_check():
    note = call("get_notes", track=TRACK, slot=0)["notes"][0]
    call("edit_notes", track=TRACK, slot=0, notes=[{"id": note["id"], "velocity": 33}])
    edited = [n for n in call("get_notes", track=TRACK, slot=0)["notes"] if n["id"] == note["id"]]
    expect(edited and edited[0]["velocity"] == 33, "edit did not stick: %s" % edited)
    return edited[0]


def undo_check():
    before = call("get_notes", track=TRACK, slot=1)["count"]
    call("add_notes", track=TRACK, slot=1, notes=[{"pitch": 72, "start_time": 0, "duration": 1}, {"pitch": 76, "start_time": 0, "duration": 1}])
    call("undo")
    after = call("get_notes", track=TRACK, slot=1)["count"]
    expect(after == before, "one undo should remove both notes; %d before, %d after" % (before, after))
    return {"before": before, "after": after}


def main():
    status = send("status")
    print(json.dumps(status))
    if status.get("result", {}).get("tools_error"):
        sys.exit("tools failed to load")

    check("get_session", lambda: {k: v for k, v in call("get_session").items() if k in ("tempo", "signature", "playing", "live_version")})
    if not check("create_track", lambda: call("create_track", kind="midi", name=TRACK)["name"]):
        sys.exit("can't continue without the track")
    try:
        kits = check("search_browser", lambda: call("search_browser", query="kit", category="drums", limit=5))
        if kits and kits["results"]:
            path = kits["results"][0]["path"]
            check("load_from_browser", lambda: call("load_from_browser", track=TRACK, path=path))
            check("list_devices", lambda: call("list_devices", track=TRACK))
            check("get_drum_kit", lambda: [(p["note"], p["name"], p["role"]) for p in call("get_drum_kit", track=TRACK)["pads"]])
            check("write_drum_pattern", beat_check)
            check("get_device_parameters", lambda: len(call("get_device_parameters", track=TRACK, device=0)["parameters"]))
            check("set_device_parameter", lambda: call("set_device_parameter", track=TRACK, device=0, parameter=1, value=call("get_device_parameters", track=TRACK, device=0)["parameters"][1]["value"]))
        check("create_clip", lambda: call("create_clip", track=TRACK, slot=1, bars=1, name="Notes"))
        check("add_notes", lambda: call("add_notes", track=TRACK, slot=1, notes=[{"pitch": 60, "start_time": 0, "duration": 1, "probability": 0.5}]))
        check("undo groups a tool call", undo_check)
        check("edit_notes", edit_check)
        check("quantize_clip", lambda: call("quantize_clip", track=TRACK, slot=1, grid="1/16"))
        check("clear_notes", lambda: call("clear_notes", track=TRACK, slot=1))
        check("set_clip", lambda: call("set_clip", track=TRACK, slot=1, name="Renamed", color="#3366cc", loop_end=8)["loop_end"])
        check("duplicate_clip", lambda: call("duplicate_clip", track=TRACK, slot=1, target_slot=2)["name"])
        check("set_clip_automation", lambda: call("set_clip_automation", track=TRACK, slot=1, device="mixer", parameter="volume", points=[{"time": 0, "value": 0.5}, {"time": 2, "value": 0.8}]))
        check("clear_clip_automation", lambda: call("clear_clip_automation", track=TRACK, slot=1, device="mixer", parameter="volume"))
        check("set_track", lambda: {k: v for k, v in call("set_track", track=TRACK, volume=0.7, pan=-0.2, color="#ff8800").items() if k in ("volume", "pan", "color")})
        check("get_track master", lambda: call("get_track", track="master")["kind"])
        check("browse", lambda: len(call("browse", path=["drums"])["items"]))
        check("copy_clip_to_arrangement", lambda: call("copy_clip_to_arrangement", track=TRACK, slot=1, beat=0, repeat=2))
        check("list_arrangement_clips", lambda: call("list_arrangement_clips", track=TRACK))
        check("fire_clip", lambda: call("fire_clip", track=TRACK, slot=1))
        check("stop_clips", lambda: call("stop_clips", track=TRACK))
        check("stop", lambda: call("stop", stop_clips=True))
    finally:
        check("delete_track", lambda: call("delete_track", track=TRACK, confirm=True))
    print("\n%d failed: %s" % (len(failures), ", ".join(failures)) if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
