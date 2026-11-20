"""Drum pattern logic: step strings to notes, and drum names to pitches. No Live API here."""

import random
import re

ACCENT_VELOCITY = 120
GHOST_VELOCITY = 45
# Below a full step so that swing and timing variation can't make a hit overlap the next one.
GATE = 0.4

# role: (General MIDI pitch, pattern a pad name must match, pattern it must not match)
_HAT = r"hi[- ]?hat|\bhat|\bhh\b|\bchh?\b|\bohh?\b"
ROLES = {
    "kick": (36, r"\bkick|\bkik|\bbd\b|bass ?drum", None),
    "snare": (38, r"\bsnare|\bsnr\b|\bsd\b", None),
    "clap": (39, r"\bclap", None),
    "rim": (37, r"\brim|side ?stick", None),
    "closed_hat": (42, _HAT, r"\bopen|\bohh?\b"),
    "open_hat": (46, r"(%s).*\bopen|\bopen.*(%s)|\bohh?\b" % (_HAT, _HAT), None),
    "crash": (49, r"\bcrash|\bcymbal", None),
    "ride": (51, r"\bride", None),
    "tom_low": (45, r"\btom.*(low|\blo\b|floor)|(low|\blo\b|floor).*\btom", None),
    "tom_mid": (47, r"\btom.*\bmid|\bmid.*\btom", None),
    "tom_high": (50, r"\btom.*(high|\bhi\b)|(high|\bhi\b).*\btom", None),
    "tambourine": (54, r"\btamb", None),
    "cowbell": (56, r"cow ?bell", None),
    "conga": (63, r"\bconga", None),
    "shaker": (70, r"\bshak|\bmaraca", None),
    "perc": (75, r"\bperc|\bclave", None),
}
ALIASES = {
    "bd": "kick", "sd": "snare", "cp": "clap", "rimshot": "rim",
    "hat": "closed_hat", "hihat": "closed_hat", "hi_hat": "closed_hat", "hh": "closed_hat", "ch": "closed_hat", "chh": "closed_hat",
    "oh": "open_hat", "ohh": "open_hat",
    "tom": "tom_mid", "tamb": "tambourine", "percussion": "perc",
}


def role_of(voice):
    """The role a voice name asks for, or None."""
    key = re.sub(r"[ -]+", "_", voice.strip().lower())
    key = ALIASES.get(key, key)
    return key if key in ROLES else None


def pad_role(pad_name):
    """The role a Drum Rack pad's name suggests, or None."""
    name = pad_name.lower().replace("_", " ")
    # open_hat comes before closed_hat here so a pad never matches the broader one first.
    for role in sorted(ROLES, key=lambda r: r != "open_hat"):
        _pitch, include, exclude = ROLES[role]
        if re.search(include, name) and not (exclude and re.search(exclude, name)):
            return role
    return None


def _as_pitch(voice):
    """A MIDI number or a note name such as C1 or F#1, or None."""
    text = voice.strip()
    if text.isdigit():
        pitch = int(text)
    else:
        match = re.fullmatch(r"([A-Ga-g])(#?)(-?\d)", text)
        if not match:
            return None
        base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[match.group(1).upper()]
        pitch = base + (1 if match.group(2) else 0) + (int(match.group(3)) + 2) * 12
    if not 0 <= pitch <= 127:
        raise ValueError("'%s' is outside MIDI pitches 0-127" % voice)
    return pitch


def resolve_voice(voice, pads):
    """Find the pitch for a voice name.

    voice: a role ("kick"), a pad's exact name, a MIDI number or a note name.
    pads: [{"note", "name"}] from the track's Drum Rack, or None without one, which
    falls back to General MIDI. A voice the rack can't play is an error: writing it
    to the General MIDI pitch would land on a silent pad or the wrong sound.

    Returns {"pitch", "pad", "matched_by"}.
    """
    pitch = _as_pitch(voice)
    if pitch is not None:
        pad = next((p["name"] for p in pads or () if p["note"] == pitch), None)
        return {"pitch": pitch, "pad": pad, "matched_by": "pitch"}
    role = role_of(voice)
    if pads is None:
        if role is None:
            raise ValueError("'%s' isn't a known drum; use one of %s, or a MIDI number" % (voice, ", ".join(ROLES)))
        return {"pitch": ROLES[role][0], "pad": None, "matched_by": "general_midi"}
    for pad in pads:
        if pad["name"].lower() == voice.strip().lower():
            return {"pitch": pad["note"], "pad": pad["name"], "matched_by": "pad_name"}
    if role is not None:
        for pad in pads:
            if pad_role(pad["name"]) == role:
                return {"pitch": pad["note"], "pad": pad["name"], "matched_by": "role"}
    raise ValueError("no pad for '%s'; the rack has: %s" % (voice, ", ".join(p["name"] for p in pads) or "no filled pads"))


def parse_steps(text, velocity):
    """A step string to one velocity (or None for a rest) per step.

    x hit, X accent, o ghost, 1-9 explicit level, . - _ rest. Spaces and | are ignored.
    """
    steps = []
    for char in text:
        if char in " |":
            continue
        if char in ".-_":
            steps.append(None)
        elif char == "x":
            steps.append(velocity)
        elif char == "X":
            steps.append(ACCENT_VELOCITY)
        elif char == "o":
            steps.append(GHOST_VELOCITY)
        elif char in "123456789":
            steps.append(round(int(char) * 127 / 9))
        else:
            raise ValueError("'%s' isn't a step; use x X o 1-9 for hits and . - _ for rests" % char)
    if not steps:
        raise ValueError("empty step pattern")
    return steps


def build_notes(voices, length, steps_per_beat=4, start=0.0, swing=50, tempo=120.0,
                humanize_ms=0, humanize_velocity=0, seed=None):
    """Turn {pitch: steps} into note dicts, repeating each pattern across length beats.

    swing: 50 is straight, 66 a triplet feel, as on an MPC. It delays every second step.
    humanize_ms, humanize_velocity: maximum random offset, either way, per hit.
    """
    if swing != 50 and steps_per_beat % 2:
        raise ValueError("swing needs an even steps_per_beat; %d has no off-steps to delay" % steps_per_beat)
    rng = random.Random(seed)
    step_length = 1.0 / steps_per_beat
    delay = (swing - 50) / 50.0 * step_length
    jitter = humanize_ms / 1000.0 * tempo / 60.0
    total = int(round(length * steps_per_beat))
    notes = []
    for pitch, steps in voices.items():
        for index in range(total):
            velocity = steps[index % len(steps)]
            if velocity is None:
                continue
            time = start + index * step_length
            if index % 2:
                time += delay
            if jitter:
                time = max(start, time + rng.uniform(-jitter, jitter))
            if humanize_velocity:
                velocity += rng.randint(-humanize_velocity, humanize_velocity)
            notes.append({
                "pitch": pitch,
                "start_time": time,
                "duration": step_length * GATE,
                "velocity": float(max(1, min(127, velocity))),
                "mute": False,
            })
    return notes
