"""Arrangement View clips, locators and clip automation."""

from ableton_tools._registry import tool
from ableton_tools import _util as u

CLIP_REF = {"track": u.TRACK, "slot": u.SLOT}
TARGET = {
    "device": {"type": ["integer", "string"], "description": "Device index or name, or \"mixer\" for volume, pan and sends."},
    "parameter": {"type": ["integer", "string"], "description": "Parameter index or name. With device \"mixer\": volume, pan, or send A, B..."},
}
# Where create_locator found the playhead, by target beat, until the locator is added.
_return_to = {}
# Empty clips create_locator placed after the end of the arrangement, by target beat, as (track, clip).
# Live won't move the playhead past the arrangement's end, so these make room until the locator is added.
_padding = {}
PADDING_BEATS = 4.0


def _target_parameter(args):
    track = u.find_track(args["track"])
    if args["device"] == "mixer":
        return u.mixer_parameter(track, str(args["parameter"]))
    return u.find_parameter(u.find_device(track, args["device"]), args["parameter"])


def _arrangement_clip_info(index, clip):
    info = {"index": index, "name": clip.name, "start": clip.start_time, "end": clip.end_time, "midi": clip.is_midi_clip}
    if not clip.is_midi_clip:
        info["warping"] = clip.warping
    return info


def arrangement_clips(track):
    return [_arrangement_clip_info(index, clip) for index, clip in enumerate(track.arrangement_clips)]


def locators():
    return [{"name": cue.name, "beat": cue.time} for cue in sorted(u.song().cue_points, key=lambda cue: cue.time)]


@tool("list_arrangement_clips", "List the clips on a track in Arrangement View.", {"track": u.TRACK}, ("track",))
def list_arrangement_clips(args):
    track = u.find_track(args["track"])
    return {"track": track.name, "clips": arrangement_clips(track)}


def _find_arrangement_clip(args):
    track = u.find_track(args["track"])
    clips = list(track.arrangement_clips)
    if args["clip"] >= len(clips):
        raise ValueError("'%s' has no arrangement clip %d; it has %d" % (track.name, args["clip"], len(clips)))
    return track, clips[args["clip"]]


@tool("delete_arrangement_clip", "Delete a clip from Arrangement View. clip is its index from list_arrangement_clips; "
                                  "the clips after it move down one index.",
      {"track": u.TRACK,
       "clip": {"type": "integer", "minimum": 0},
       "confirm": u.CONFIRM},
      ("track", "clip", "confirm"), write=True)
def delete_arrangement_clip(args):
    u.require_confirm(args, "delete_arrangement_clip")
    track, clip = _find_arrangement_clip(args)
    deleted = {"deleted": clip.name, "start": clip.start_time, "end": clip.end_time}
    track.delete_clip(clip)
    return deleted


@tool("set_arrangement_clip", "Rename a clip in Arrangement View or turn its warping on or off. "
                               "clip is its index from list_arrangement_clips.",
      {"track": u.TRACK,
       "clip": {"type": "integer", "minimum": 0},
       "name": {"type": "string"},
       "warping": {"type": "boolean", "description": "Audio clips only. false plays the file at its own speed, "
                                                     "which keeps long sustained sounds free of time-stretching artifacts."}},
      ("track", "clip"), write=True)
def set_arrangement_clip(args):
    _track, clip = _find_arrangement_clip(args)
    if "warping" in args:
        u.set_warping(clip, args["warping"])
    if "name" in args:
        clip.name = args["name"]
    return _arrangement_clip_info(args["clip"], clip)


def _cue_at(beat):
    return next((cue for cue in u.song().cue_points if abs(cue.time - beat) < 1e-6), None)


def _add_padding(song, beat):
    """Put an empty MIDI clip from the arrangement's end to past beat, on the first MIDI track that takes one."""
    start = song.song_length
    for track in song.tracks:
        if not track.has_midi_input or track.is_frozen:
            continue
        clip = track.create_midi_clip(start, beat - start + PADDING_BEATS)
        _padding[beat] = (track, clip)
        return
    raise ValueError("beat %s is past the end of the arrangement (beat %s), and there's no MIDI track to put a "
                     "temporary clip on to make room; add a MIDI track or a clip that reaches that far" % (beat, start))


def _remove_padding(beat):
    """Delete the clip _add_padding placed for beat. Returns an error message, or None."""
    track, clip = _padding.pop(beat, (None, None))
    if track is None:
        return None
    try:
        track.delete_clip(clip)
    except Exception as error:  # noqa: BLE001 - reported to the user rather than losing the locator
        return "couldn't delete the temporary clip on '%s' at beat %s (%s); delete it in Arrangement View" % (
            track.name, clip.start_time, error)
    return None


@tool("create_locator", "Add a named locator (marker) to the arrangement at a beat, or rename the one already there. "
                        "Live only adds locators at the playhead, so this moves the playhead there and back, "
                        "and refuses while the transport is running. A beat past the end of the arrangement works too: "
                        "a temporary empty MIDI clip makes room and is deleted again. Live snaps a beat between grid "
                        "lines forward to the arrangement grid; the reply says where the locator landed. "
                        "A reply with retry_after means the playhead is still moving: call again with the same arguments.",
      {"beat": {"type": "number", "minimum": 0},
       "name": {"type": "string"}},
      ("beat",), write=True)
def create_locator(args):
    song = u.song()
    beat = float(args["beat"])
    cue = _cue_at(beat)
    if cue is None:
        if song.is_playing:
            raise ValueError("stop the transport first; adding a locator moves the playhead")
        if abs(song.current_song_time - beat) > 1e-6:
            if beat >= song.song_length and beat not in _padding:
                # Live extends the arrangement on its next tick, so the playhead moves on the next call.
                _add_padding(song, beat)
                return {"retry_after": 0.1, "message": "making room past the end of the arrangement; call again"}
            # Live moves the playhead on its next tick, so the locator is added by the next call.
            _return_to.setdefault(beat, song.current_song_time)
            song.current_song_time = beat
            return {"retry_after": 0.1, "message": "moving the playhead to beat %s; call again to add the locator" % beat}
        if song.is_cue_point_selected():
            _remove_padding(beat)
            raise ValueError("the playhead is on a locator; this would delete it")
        times = [cue.time for cue in song.cue_points]
        song.set_or_delete_cue()
        added = [cue for cue in song.cue_points if cue.time not in times]
        back = _return_to.pop(beat, None)
        if back is not None:
            song.current_song_time = back
        padding_error = _remove_padding(beat)
        if not added:
            raise ValueError("Live didn't add a locator; there may already be one where it snapped to")
        cue = added[0]
    else:
        padding_error = None
    if args.get("name"):
        cue.name = args["name"]
    result = {"name": cue.name, "beat": cue.time, "locators": locators()}
    if padding_error:
        # Not "warning": the Limiter check replaces that key.
        result["temporary_clip_left"] = padding_error
    if abs(cue.time - beat) > 1e-6:
        result["snapped"] = "Live put it on the arrangement grid at beat %s; zoom in for a finer grid" % cue.time
    return result


@tool("switch_to_arrangement_view", "Show Arrangement View in Live's main window.")
def switch_to_arrangement_view(_args):
    view = u.application().view
    view.show_view("Arranger")
    return {"view": view.focused_document_view}


@tool("copy_clip_to_arrangement", "Copy a Session View clip into Arrangement View on its own track, repeated back to back. "
                                  "Copies overwrite whatever is there. If the track is playing the arrangement clip, "
                                  "Live may need Back to Arrangement pressed.",
      dict(CLIP_REF,
           beat={"type": "number", "minimum": 0, "description": "Where the first copy starts."},
           repeat={"type": "integer", "minimum": 1, "maximum": 256, "description": "Copies to place (default 1)."}),
      ("track", "slot", "beat"), write=True)
def copy_clip_to_arrangement(args):
    track = u.find_track(args["track"])
    clip = u.find_clip(args)
    beat = args["beat"]
    repeat = args.get("repeat", 1)
    for index in range(repeat):
        track.duplicate_clip_to_arrangement(clip, beat + index * clip.length)
    return {"copies": repeat, "start": beat, "end": beat + repeat * clip.length}


@tool("set_clip_automation", "Write automation for a parameter into a Session View clip, as steps that hold their value. "
                             "Each point is {time, value, duration} in beats; duration defaults to reaching the next point.",
      dict(CLIP_REF,
           points={"type": "array", "minItems": 1, "items": {
               "type": "object",
               "properties": {"time": {"type": "number", "minimum": 0}, "value": {"type": "number"},
                              "duration": {"type": "number", "exclusiveMinimum": 0}},
               "additionalProperties": False, "required": ["time", "value"],
           }},
           **TARGET),
      ("track", "slot", "device", "parameter", "points"), write=True)
def set_clip_automation(args):
    clip = u.find_clip(args)
    parameter = _target_parameter(args)
    envelope = clip.automation_envelope(parameter)
    if envelope is None:
        envelope = clip.create_automation_envelope(parameter)
    if envelope is None:
        raise ValueError("'%s' can't be automated in this clip" % parameter.name)
    points = sorted(args["points"], key=lambda p: p["time"])
    for point, following in zip(points, points[1:] + [None]):
        if not parameter.min <= point["value"] <= parameter.max:
            raise ValueError("'%s' takes %s to %s, not %s" % (parameter.name, parameter.min, parameter.max, point["value"]))
        end = following["time"] if following else clip.length
        duration = point.get("duration", end - point["time"])
        if duration <= 0:
            raise ValueError("the point at beat %s has no length; give it a duration" % point["time"])
        envelope.insert_step(point["time"], duration, point["value"])
    return {"parameter": parameter.name, "points": len(points)}


@tool("clear_clip_automation", "Remove a parameter's automation from a Session View clip.",
      dict(CLIP_REF, **TARGET), ("track", "slot", "device", "parameter"), write=True)
def clear_clip_automation(args):
    clip = u.find_clip(args)
    parameter = _target_parameter(args)
    clip.clear_envelope(parameter)
    return {"cleared": parameter.name}
