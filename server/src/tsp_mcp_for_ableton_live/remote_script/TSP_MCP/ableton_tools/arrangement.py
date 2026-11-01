"""Arrangement View clips and clip automation."""

from ableton_tools._registry import tool
from ableton_tools import _util as u

CLIP_REF = {"track": u.TRACK, "slot": u.SLOT}
TARGET = {
    "device": {"type": ["integer", "string"], "description": "Device index or name, or \"mixer\" for volume, pan and sends."},
    "parameter": {"type": ["integer", "string"], "description": "Parameter index or name. With device \"mixer\": volume, pan, or send A, B..."},
}


def _target_parameter(args):
    track = u.find_track(args["track"])
    if args["device"] == "mixer":
        return u.mixer_parameter(track, str(args["parameter"]))
    return u.find_parameter(u.find_device(track, args["device"]), args["parameter"])


@tool("list_arrangement_clips", "List the clips on a track in Arrangement View.", {"track": u.TRACK}, ("track",))
def list_arrangement_clips(args):
    track = u.find_track(args["track"])
    return {"track": track.name, "clips": [
        {"name": clip.name, "start": clip.start_time, "end": clip.end_time, "midi": clip.is_midi_clip}
        for clip in track.arrangement_clips
    ]}


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
