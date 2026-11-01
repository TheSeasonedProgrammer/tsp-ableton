"""Shared lookups, schema fragments and conversions."""

import Live

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
# Upper bound for "the whole clip" note ranges, in beats.
ALL_TIME = 1000000.0

TRACK = {"type": ["integer", "string"],
         "description": "Track index (0-based) or name. Names also find return tracks, and \"master\" is the master track."}
SLOT = {"type": "integer", "minimum": 0, "description": "Clip slot index, the same as the scene index (0-based)."}
DEVICE = {"type": ["integer", "string"], "description": "Device index on the track (0-based) or name."}
COLOR = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$",
         "description": "\"#rrggbb\". Live snaps it to the nearest colour in its palette."}
CONFIRM = {"type": "boolean", "description": "Must be true; this can only be reversed with undo."}


def application():
    return Live.Application.get_application()


def song():
    return application().get_document()


def require_confirm(args, what):
    if args.get("confirm") is not True:
        raise ValueError("%s needs confirm: true" % what)


def note_name(pitch):
    """Live's naming: middle C (60) is C3, so a Drum Rack's first pad (36) is C1."""
    return "%s%d" % (_NOTE_NAMES[pitch % 12], pitch // 12 - 2)


def beats_per_bar():
    current = song()
    return current.signature_numerator * 4.0 / current.signature_denominator


def color_value(text):
    return int(text[1:], 16)


def color_text(value):
    return "#%06x" % value


def find_track(ref):
    current = song()
    tracks = list(current.tracks)
    if isinstance(ref, int):
        if 0 <= ref < len(tracks):
            return tracks[ref]
        raise ValueError("no track %d; the set has %d tracks" % (ref, len(tracks)))
    if ref.lower() == "master":
        return current.master_track
    matches = [t for t in tracks + list(current.return_tracks) if t.name.lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError("%d tracks are named '%s'; use the track index" % (len(matches), ref))
    raise ValueError("no track named '%s'" % ref)


def track_index(track):
    """Index in song.tracks, or None for return tracks and the master."""
    tracks = list(song().tracks)
    return tracks.index(track) if track in tracks else None


def track_kind(track):
    current = song()
    if track == current.master_track:
        return "master"
    if track in list(current.return_tracks):
        return "return"
    if track.is_foldable:
        return "group"
    return "midi" if track.has_midi_input else "audio"


def require_midi_track(track):
    if not track.has_midi_input:
        raise ValueError("'%s' is not a MIDI track" % track.name)


def find_slot(track, index):
    slots = list(track.clip_slots)
    if index < len(slots):
        return slots[index]
    raise ValueError("'%s' has no slot %d; it has %d" % (track.name, index, len(slots)))


def find_clip(args):
    track = find_track(args["track"])
    slot = find_slot(track, args["slot"])
    if not slot.has_clip:
        raise ValueError("slot %d on '%s' has no clip" % (args["slot"], track.name))
    return slot.clip


def find_midi_clip(args):
    clip = find_clip(args)
    if not clip.is_midi_clip:
        raise ValueError("'%s' is an audio clip" % clip.name)
    return clip


def clip_length(args):
    """Length in beats from the bars or length_beats argument, or None if neither is given."""
    if args.get("length_beats") is not None:
        return float(args["length_beats"])
    if args.get("bars") is not None:
        return args["bars"] * beats_per_bar()
    return None


def clip_info(clip):
    return {
        "name": clip.name,
        "midi": clip.is_midi_clip,
        "length_beats": clip.length,
        "looping": clip.looping,
        "loop_start": clip.loop_start,
        "loop_end": clip.loop_end,
        "start_marker": clip.start_marker,
        "end_marker": clip.end_marker,
        "color": color_text(clip.color),
        "groove": clip.groove.name if clip.has_groove else None,
        "playing": clip.is_playing,
    }


def add_notes(clip, notes):
    """Write note dicts whose keys are MidiNoteSpecification's keyword arguments."""
    if notes:
        clip.add_new_notes(tuple(Live.Clip.MidiNoteSpecification(**note) for note in notes))


def find_device(track, ref):
    devices = list(track.devices)
    if isinstance(ref, int):
        if 0 <= ref < len(devices):
            return devices[ref]
        raise ValueError("'%s' has no device %d; it has %d" % (track.name, ref, len(devices)))
    matches = [d for d in devices if d.name.lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError("%d devices are named '%s'; use the device index" % (len(matches), ref))
    raise ValueError("no device named '%s' on '%s'; it has: %s" % (ref, track.name, ", ".join(d.name for d in devices) or "none"))


def find_parameter(device, ref):
    parameters = list(device.parameters)
    if isinstance(ref, int):
        if 0 <= ref < len(parameters):
            return parameters[ref]
        raise ValueError("'%s' has no parameter %d; it has %d" % (device.name, ref, len(parameters)))
    matches = [p for p in parameters if p.name.lower() == ref.lower()]
    if matches:
        return matches[0]
    raise ValueError("no parameter named '%s' on '%s'" % (ref, device.name))


def mixer_parameter(track, name):
    """A mixer parameter by name: "volume", "pan", or "send A", "send B" and so on."""
    mixer = track.mixer_device
    lowered = name.lower()
    if lowered == "volume":
        return mixer.volume
    if lowered == "pan":
        return mixer.panning
    if lowered.startswith("send ") and len(lowered) == 6:
        index = ord(lowered[5]) - ord("a")
        sends = list(mixer.sends)
        if 0 <= index < len(sends):
            return sends[index]
    raise ValueError("no mixer parameter '%s'; use volume, pan or send A, B..." % name)


def parameter_info(parameter, index=None):
    info = {
        "name": parameter.name,
        "value": parameter.value,
        "min": parameter.min,
        "max": parameter.max,
        "display": parameter.str_for_value(parameter.value),
        "enabled": parameter.is_enabled,
    }
    if index is not None:
        info["index"] = index
    if parameter.is_quantized:
        info["items"] = list(parameter.value_items)
    return info


def set_parameter(parameter, value):
    """Set from a number, or from an item label on a quantized parameter."""
    if not parameter.is_enabled:
        raise ValueError("'%s' is disabled, probably mapped to a Macro or controlled by a clip" % parameter.name)
    if isinstance(value, str):
        items = [str(item).lower() for item in parameter.value_items]
        if value.lower() not in items:
            raise ValueError("'%s' has no item '%s'; it has: %s" % (parameter.name, value, ", ".join(parameter.value_items) or "none"))
        value = items.index(value.lower())
    if not parameter.min <= value <= parameter.max:
        raise ValueError("'%s' takes %s to %s, not %s" % (parameter.name, parameter.min, parameter.max, value))
    parameter.value = value


def find_drum_rack(devices):
    """The first Drum Rack in a device list, looking inside other racks."""
    for device in devices:
        if device.can_have_drum_pads:
            return device
        if device.can_have_chains:
            for chain in device.chains:
                found = find_drum_rack(chain.devices)
                if found is not None:
                    return found
    return None


def filled_pads(rack):
    return [pad for pad in rack.drum_pads if len(pad.chains) > 0]
