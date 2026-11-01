"""Tracks and their mixer settings."""

from ableton_tools._registry import tool
from ableton_tools import _util as u


def _track_info(track):
    kind = u.track_kind(track)
    mixer = track.mixer_device
    info = {
        "index": u.track_index(track),
        "name": track.name,
        "kind": kind,
        "color": u.color_text(track.color),
        "volume": u.parameter_info(mixer.volume),
        "pan": u.parameter_info(mixer.panning),
        "sends": [u.parameter_info(send) for send in mixer.sends],
        "devices": [{"index": index, "name": device.name, "type": device.class_name} for index, device in enumerate(track.devices)],
    }
    if kind != "master":
        info["mute"] = track.mute
        info["solo"] = track.solo
        info["clips"] = [{"slot": index, "name": slot.clip.name} for index, slot in enumerate(track.clip_slots) if slot.has_clip]
    if track.can_be_armed:
        info["arm"] = track.arm
    return info


@tool("get_track", "Details of one track: mixer, devices and which slots hold clips.", {"track": u.TRACK}, ("track",))
def get_track(args):
    return _track_info(u.find_track(args["track"]))


@tool("create_track", "Create a MIDI, audio or return track.",
      {"kind": {"type": "string", "enum": ["midi", "audio", "return"]},
       "index": {"type": "integer", "minimum": -1, "description": "Position among the tracks; -1 is the end (default). Ignored for return tracks."},
       "name": {"type": "string"}},
      ("kind",), write=True)
def create_track(args):
    song = u.song()
    index = args.get("index", -1)
    if args["kind"] == "return":
        song.create_return_track()
        track = list(song.return_tracks)[-1]
    else:
        (song.create_midi_track if args["kind"] == "midi" else song.create_audio_track)(index)
        tracks = list(song.tracks)
        track = tracks[index]
    if args.get("name"):
        track.name = args["name"]
    return _track_info(track)


@tool("duplicate_track", "Duplicate a track, with its clips and devices, right after itself.",
      {"track": u.TRACK}, ("track",), write=True)
def duplicate_track(args):
    track = u.find_track(args["track"])
    index = u.track_index(track)
    if index is None:
        raise ValueError("only regular tracks can be duplicated")
    u.song().duplicate_track(index)
    return _track_info(list(u.song().tracks)[index + 1])


@tool("delete_track", "Delete a track and everything on it.",
      {"track": u.TRACK, "confirm": u.CONFIRM}, ("track", "confirm"), write=True)
def delete_track(args):
    u.require_confirm(args, "delete_track")
    song = u.song()
    track = u.find_track(args["track"])
    name = track.name
    kind = u.track_kind(track)
    if kind == "master":
        raise ValueError("the master track can't be deleted")
    if kind == "return":
        song.delete_return_track(list(song.return_tracks).index(track))
    else:
        song.delete_track(u.track_index(track))
    return {"deleted": name}


@tool("set_track", "Change a track's name, colour, mute, solo, arm, volume, pan or sends. "
                   "Volume, pan and sends use Live's raw values; volume 0.85 is 0 dB and pan runs from -1 to 1. "
                   "The reply shows each as dB or L/R text.",
      {"track": u.TRACK,
       "name": {"type": "string"},
       "color": u.COLOR,
       "mute": {"type": "boolean"},
       "solo": {"type": "boolean"},
       "arm": {"type": "boolean"},
       "volume": {"type": "number", "minimum": 0, "maximum": 1},
       "pan": {"type": "number", "minimum": -1, "maximum": 1},
       "sends": {"type": "object", "description": "Send letter to value, such as {\"A\": 0.4}.", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}}},
      ("track",), write=True)
def set_track(args):
    track = u.find_track(args["track"])
    if "name" in args:
        track.name = args["name"]
    if "color" in args:
        track.color = u.color_value(args["color"])
    for flag in ("mute", "solo"):
        if flag in args:
            setattr(track, flag, args[flag])
    if "arm" in args:
        if not track.can_be_armed:
            raise ValueError("'%s' can't be armed" % track.name)
        track.arm = args["arm"]
    if "volume" in args:
        u.set_parameter(track.mixer_device.volume, args["volume"])
    if "pan" in args:
        u.set_parameter(track.mixer_device.panning, args["pan"])
    for letter, value in args.get("sends", {}).items():
        u.set_parameter(u.mixer_parameter(track, "send " + letter), value)
    return _track_info(track)
