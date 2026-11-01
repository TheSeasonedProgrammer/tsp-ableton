"""Drum Racks and step-pattern beats."""

from ableton_tools._registry import tool
from ableton_tools import _drums as d
from ableton_tools import _util as u


@tool("get_drum_kit", "List the filled pads of the track's Drum Rack with their pitch and the role each name suggests.",
      {"track": u.TRACK}, ("track",))
def get_drum_kit(args):
    track = u.find_track(args["track"])
    rack = u.find_drum_rack(track.devices)
    if rack is None:
        return {"rack": None, "pads": []}
    return {"rack": rack.name, "pads": [
        {"note": pad.note, "note_name": u.note_name(pad.note), "name": pad.name, "role": d.pad_role(pad.name), "mute": pad.mute, "solo": pad.solo}
        for pad in u.filled_pads(rack)
    ]}


@tool("write_drum_pattern",
      "Write a beat from step strings into a clip, creating the clip if the slot is empty. "
      "Each key of pattern is a drum and each value its steps, which repeat to fill the clip. "
      "Drums are roles (kick, snare, clap, rim, closed_hat, open_hat, crash, ride, tom_low, tom_mid, tom_high, tambourine, cowbell, conga, shaker, perc), "
      "exact pad names, MIDI numbers or note names. They resolve against the track's Drum Rack, or General MIDI without one. "
      "Steps: x hit, X accent, o ghost, 1-9 explicit level, . - _ rest; spaces and | are ignored. "
      "Example with 16 steps to the bar: {\"kick\": \"x...|....|x.x.|....\", \"snare\": \"....|x...|....|x..o\", \"closed_hat\": \"x.x.|x.x.|x.x.|x.X.\"}.",
      {"track": u.TRACK,
       "slot": u.SLOT,
       "pattern": {"type": "object", "minProperties": 1, "additionalProperties": {"type": "string"}},
       "bars": {"type": "number", "exclusiveMinimum": 0, "description": "Length of a new clip (default 1). Existing clips keep their loop length."},
       "steps_per_beat": {"type": "integer", "minimum": 1, "maximum": 12, "description": "4 is sixteenths (default), 2 eighths, 3 eighth triplets, 6 sixteenth triplets."},
       "velocity": {"type": "integer", "minimum": 1, "maximum": 127, "description": "Velocity of an x (default 100)."},
       "swing": {"type": "number", "minimum": 50, "maximum": 75, "description": "50 straight (default), 54-58 subtle, 62 heavy, 66 triplet feel."},
       "humanize_ms": {"type": "number", "minimum": 0, "maximum": 30, "description": "Random timing offset per hit, up to this many ms either way."},
       "humanize_velocity": {"type": "integer", "minimum": 0, "maximum": 40, "description": "Random velocity offset per hit."},
       "seed": {"type": "integer", "description": "Repeat the same random variation."},
       "clear": {"type": "string", "enum": ["voices", "all", "none"], "description": "Notes to remove first: only the pitches being written (default), the whole clip, or none."},
       "name": {"type": "string"},
       "fire": {"type": "boolean", "description": "Launch the clip afterwards."}},
      ("track", "slot", "pattern"), write=True)
def write_drum_pattern(args):
    track = u.find_track(args["track"])
    u.require_midi_track(track)
    slot = u.find_slot(track, args["slot"])

    rack = u.find_drum_rack(track.devices)
    pads = None if rack is None else [{"note": pad.note, "name": pad.name} for pad in u.filled_pads(rack)]
    velocity = args.get("velocity", 100)
    voices, resolved = {}, {}
    for voice, text in args["pattern"].items():
        match = d.resolve_voice(voice, pads)
        if match["pitch"] in voices:
            raise ValueError("'%s' is the same pad as another voice in this pattern" % voice)
        voices[match["pitch"]] = d.parse_steps(text, velocity)
        resolved[voice] = match

    if slot.has_clip:
        clip = slot.clip
        if not clip.is_midi_clip:
            raise ValueError("slot %d on '%s' holds an audio clip" % (args["slot"], track.name))
        if args.get("bars") is not None:
            raise ValueError("slot %d already has a clip; leave out bars, or change its length with set_clip" % args["slot"])
    else:
        slot.create_clip(args.get("bars", 1) * u.beats_per_bar())
        clip = slot.clip
    start, length = clip.loop_start, clip.loop_end - clip.loop_start

    notes = d.build_notes(
        voices, length,
        steps_per_beat=args.get("steps_per_beat", 4),
        start=start,
        swing=args.get("swing", 50),
        tempo=u.song().tempo,
        humanize_ms=args.get("humanize_ms", 0),
        humanize_velocity=args.get("humanize_velocity", 0),
        seed=args.get("seed"),
    )
    clear = args.get("clear", "voices")
    if clear == "all":
        clip.remove_notes_extended(0, 128, 0.0, u.ALL_TIME)
    elif clear == "voices":
        for pitch in voices:
            clip.remove_notes_extended(pitch, 1, 0.0, u.ALL_TIME)
    u.add_notes(clip, notes)
    if args.get("name"):
        clip.name = args["name"]
    if args.get("fire"):
        slot.fire()
    return {
        "track": track.name,
        "slot": args["slot"],
        "length_beats": length,
        "notes_written": len(notes),
        "mapping": "general_midi" if rack is None else rack.name,
        "voices": resolved,
    }
