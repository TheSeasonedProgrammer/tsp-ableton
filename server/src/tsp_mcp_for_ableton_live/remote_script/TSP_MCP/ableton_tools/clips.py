"""Session View clips."""

from ableton_tools._registry import tool
from ableton_tools import _util as u

CLIP_REF = {"track": u.TRACK, "slot": u.SLOT}
LENGTH = {"bars": {"type": "number", "exclusiveMinimum": 0, "description": "Length in bars at the current time signature."},
          "length_beats": {"type": "number", "exclusiveMinimum": 0, "description": "Length in beats, instead of bars."}}


@tool("list_clips", "List the clips in a track's Session View slots.", {"track": u.TRACK}, ("track",))
def list_clips(args):
    track = u.find_track(args["track"])
    return {"track": track.name, "slots": len(list(track.clip_slots)), "clips": [
        dict(u.clip_info(slot.clip), slot=index)
        for index, slot in enumerate(track.clip_slots) if slot.has_clip
    ]}


@tool("get_clip", "Details of one clip: name, length, loop, markers and colour.", CLIP_REF, ("track", "slot"))
def get_clip(args):
    return u.clip_info(u.find_clip(args))


@tool("create_clip", "Create an empty MIDI clip in an empty slot. Give bars or length_beats (default 1 bar).",
      dict(CLIP_REF, name={"type": "string"}, **LENGTH), ("track", "slot"), write=True)
def create_clip(args):
    track = u.find_track(args["track"])
    u.require_midi_track(track)
    slot = u.find_slot(track, args["slot"])
    if slot.has_clip:
        raise ValueError("slot %d on '%s' already has a clip" % (args["slot"], track.name))
    slot.create_clip(u.clip_length(args) or u.beats_per_bar())
    if args.get("name"):
        slot.clip.name = args["name"]
    return u.clip_info(slot.clip)


@tool("set_clip", "Change a clip's name, colour, loop, markers or groove. Times are in beats. "
                  "groove is the name of a groove in the Groove Pool (see get_session). "
                  "Live's API can apply a groove but not remove one; that needs Clip View.",
      dict(CLIP_REF,
           name={"type": "string"},
           color=u.COLOR,
           looping={"type": "boolean"},
           loop_start={"type": "number"},
           loop_end={"type": "number"},
           start_marker={"type": "number"},
           end_marker={"type": "number"},
           groove={"type": "string", "minLength": 1}),
      ("track", "slot"), write=True)
def set_clip(args):
    clip = u.find_clip(args)
    if "name" in args:
        clip.name = args["name"]
    if "color" in args:
        clip.color = u.color_value(args["color"])
    if "looping" in args:
        clip.looping = args["looping"]
    # Live rejects a start past the current end, and the reverse, so order matters.
    pairs = (("loop_start", "loop_end"), ("start_marker", "end_marker"))
    for start, end in pairs:
        if start in args and args[start] >= getattr(clip, end):
            names = (end, start)
        else:
            names = (start, end)
        for name in names:
            if name in args:
                setattr(clip, name, args[name])
    if "groove" in args:
        grooves = [g for g in u.song().groove_pool.grooves if g.name.lower() == args["groove"].lower()]
        if not grooves:
            raise ValueError("no groove named '%s' in the Groove Pool" % args["groove"])
        clip.groove = grooves[0]
    return u.clip_info(clip)


@tool("delete_clip", "Delete a clip from its slot.",
      dict(CLIP_REF, confirm=u.CONFIRM), ("track", "slot", "confirm"), write=True)
def delete_clip(args):
    u.require_confirm(args, "delete_clip")
    clip = u.find_clip(args)
    name = clip.name
    u.find_slot(u.find_track(args["track"]), args["slot"]).delete_clip()
    return {"deleted": name}


@tool("duplicate_clip", "Copy a clip to another slot, on the same track or another, replacing anything there.",
      dict(CLIP_REF,
           target_track=u.TRACK,
           target_slot=u.SLOT),
      ("track", "slot", "target_slot"), write=True)
def duplicate_clip(args):
    source = u.find_slot(u.find_track(args["track"]), args["slot"])
    if not source.has_clip:
        raise ValueError("slot %d has no clip" % args["slot"])
    target_track = u.find_track(args.get("target_track", args["track"]))
    target = u.find_slot(target_track, args["target_slot"])
    source.duplicate_clip_to(target)
    return u.clip_info(target.clip)


@tool("fire_clip", "Launch a clip, starting the transport if it's stopped.", CLIP_REF, ("track", "slot"), warn=True)
def fire_clip(args):
    u.find_slot(u.find_track(args["track"]), args["slot"]).fire()
    return {"fired": True}


@tool("stop_clips", "Stop the clips playing on a track.", {"track": u.TRACK}, ("track",))
def stop_clips(args):
    u.find_track(args["track"]).stop_all_clips()
    return {"stopped": True}
