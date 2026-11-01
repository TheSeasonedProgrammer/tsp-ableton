"""MIDI notes in a clip."""

import Live

from ableton_tools._registry import tool
from ableton_tools import _util as u

CLIP_REF = {"track": u.TRACK, "slot": u.SLOT}
NOTE_FIELDS = {
    "pitch": {"type": "integer", "minimum": 0, "maximum": 127, "description": "MIDI pitch; 60 is middle C, which Live calls C3."},
    "start_time": {"type": "number", "minimum": 0, "description": "Beats from the start of the clip."},
    "duration": {"type": "number", "exclusiveMinimum": 0, "description": "Beats."},
    "velocity": {"type": "number", "minimum": 0, "maximum": 127},
    "mute": {"type": "boolean"},
    "probability": {"type": "number", "minimum": 0, "maximum": 1, "description": "Chance the note plays; 1 is always."},
    "velocity_deviation": {"type": "number", "minimum": -127, "maximum": 127},
    "release_velocity": {"type": "number", "minimum": 0, "maximum": 127},
}
EDITABLE = ("pitch", "start_time", "duration", "velocity", "mute", "probability", "velocity_deviation", "release_velocity")
GRIDS = {
    "1/4": "rec_q_quarter",
    "1/8": "rec_q_eight",
    "1/8t": "rec_q_eight_triplet",
    "1/16": "rec_q_sixtenth",
    "1/16t": "rec_q_sixtenth_triplet",
    "1/32": "rec_q_thirtysecond",
}


def _note_info(note):
    return {
        "id": note.note_id,
        "pitch": note.pitch,
        "name": u.note_name(note.pitch),
        "start_time": round(note.start_time, 6),
        "duration": round(note.duration, 6),
        "velocity": round(note.velocity, 2),
        "mute": note.mute,
        "probability": note.probability,
    }


@tool("get_notes", "Read the notes in a clip, optionally only a pitch range or time range. Each note's id works with edit_notes.",
      dict(CLIP_REF,
           from_pitch={"type": "integer", "minimum": 0, "maximum": 127},
           to_pitch={"type": "integer", "minimum": 0, "maximum": 127, "description": "Inclusive."},
           from_time={"type": "number", "description": "Beats."},
           to_time={"type": "number", "description": "Beats; exclusive."}),
      ("track", "slot"))
def get_notes(args):
    clip = u.find_midi_clip(args)
    low, high = args.get("from_pitch", 0), args.get("to_pitch", 127)
    start, end = args.get("from_time", float("-inf")), args.get("to_time", float("inf"))
    notes = [_note_info(n) for n in clip.get_all_notes_extended() if low <= n.pitch <= high and start <= n.start_time < end]
    notes.sort(key=lambda n: (n["start_time"], n["pitch"]))
    return {"count": len(notes), "notes": notes}


@tool("add_notes", "Add MIDI notes to a clip. Use write_drum_pattern for beats on a grid.",
      dict(CLIP_REF, notes={
          "type": "array", "minItems": 1,
          "items": {"type": "object", "properties": NOTE_FIELDS, "additionalProperties": False,
                    "required": ["pitch", "start_time", "duration"]},
      }),
      ("track", "slot", "notes"), write=True)
def add_notes(args):
    clip = u.find_midi_clip(args)
    u.add_notes(clip, [dict({"velocity": 100.0}, **note) for note in args["notes"]])
    return {"added": len(args["notes"]), "total": len(clip.get_all_notes_extended())}


@tool("edit_notes", "Change existing notes by id (from get_notes). Give only the fields to change.",
      dict(CLIP_REF, notes={
          "type": "array", "minItems": 1,
          "items": {"type": "object", "properties": dict(NOTE_FIELDS, id={"type": "integer"}),
                    "additionalProperties": False, "required": ["id"]},
      }),
      ("track", "slot", "notes"), write=True)
def edit_notes(args):
    clip = u.find_midi_clip(args)
    notes = clip.get_all_notes_extended()
    by_id = {note.note_id: note for note in notes}
    for change in args["notes"]:
        note = by_id.get(change["id"])
        if note is None:
            raise ValueError("no note with id %d in this clip" % change["id"])
        for field in EDITABLE:
            if field in change:
                setattr(note, field, change[field])
    clip.apply_note_modifications(notes)
    return {"edited": len(args["notes"])}


@tool("clear_notes", "Remove notes from a clip: every note, or only a pitch range or time range.",
      dict(CLIP_REF,
           from_pitch={"type": "integer", "minimum": 0, "maximum": 127},
           to_pitch={"type": "integer", "minimum": 0, "maximum": 127, "description": "Inclusive."},
           from_time={"type": "number", "minimum": 0, "description": "Beats."},
           to_time={"type": "number", "description": "Beats; exclusive."}),
      ("track", "slot"), write=True)
def clear_notes(args):
    clip = u.find_midi_clip(args)
    low, high = args.get("from_pitch", 0), args.get("to_pitch", 127)
    start = args.get("from_time", 0.0)
    span = args.get("to_time", u.ALL_TIME) - start
    if high < low or span <= 0:
        raise ValueError("empty range")
    before = len(clip.get_all_notes_extended())
    clip.remove_notes_extended(low, high - low + 1, start, span)
    after = len(clip.get_all_notes_extended())
    return {"removed": before - after, "remaining": after}


@tool("quantize_clip", "Quantize a clip's notes to a grid.",
      dict(CLIP_REF,
           grid={"type": "string", "enum": list(GRIDS)},
           amount={"type": "number", "minimum": 0, "maximum": 1, "description": "1 moves notes fully onto the grid (default)."}),
      ("track", "slot", "grid"), write=True)
def quantize_clip(args):
    clip = u.find_midi_clip(args)
    clip.quantize(getattr(Live.Song.RecordingQuantization, GRIDS[args["grid"]]), args.get("amount", 1.0))
    return {"quantized": args["grid"]}
