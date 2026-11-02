"""A small stand-in for Live's API, enough to run the bridge and tools outside Live.

It checks the wiring and the tool logic. It proves nothing about how the real API
behaves, so changes to tools still need trying in Live.
"""

import sys
import types

_application = None


class Parameter:
    def __init__(self, name, value=0.0, low=0.0, high=1.0, items=()):
        self.name, self.value, self.min, self.max = name, value, low, high
        self.value_items = list(items)
        self.is_quantized = bool(items)
        self.is_enabled = True

    def str_for_value(self, value):
        return str(value)


class Mixer:
    def __init__(self):
        self.volume = Parameter("Track Volume", 0.85)
        self.panning = Parameter("Track Panning", 0.0, -1.0, 1.0)
        self.sends = [Parameter("Send A")]


class Pad:
    def __init__(self, note, name, filled):
        self.note, self.name = note, name
        self.chains = [object()] if filled else []
        self.mute = self.solo = False


class Device:
    can_have_chains = False
    is_active = True

    def __init__(self, name, pads=None, class_name="Reverb"):
        self.name = name
        self.class_name = "DrumGroupDevice" if pads is not None else class_name
        self.can_have_drum_pads = pads is not None
        filled = pads or {}
        self.drum_pads = [Pad(note, filled.get(note, "Empty"), note in filled) for note in range(128)] if pads is not None else []
        self.parameters = [Parameter("Device On", 1.0), Parameter("Decay", 0.5), Parameter("Mode", 0.0, 0.0, 1.0, ("Fast", "Slow"))]


class Note:
    _next_id = 1

    def __init__(self, spec):
        self.pitch = spec.pitch
        self.start_time = spec.start_time
        self.duration = spec.duration
        self.velocity = spec.velocity
        self.mute = spec.mute
        self.probability = spec.probability
        self.velocity_deviation = 0.0
        self.release_velocity = 64.0
        self.note_id = Note._next_id
        Note._next_id += 1


class MidiNoteSpecification:
    def __init__(self, pitch, start_time, duration, velocity=100.0, mute=False, probability=1.0, velocity_deviation=0.0, release_velocity=64.0):
        self.pitch, self.start_time, self.duration = pitch, start_time, duration
        self.velocity, self.mute, self.probability = velocity, mute, probability


class Envelope:
    def __init__(self):
        self.steps = []

    def insert_step(self, time, duration, value):
        self.steps.append((time, duration, value))


class Clip:
    is_midi_clip = True
    is_playing = False
    file_path = None

    def __init__(self, length, start_time=0.0):
        self.name = ""
        self.length = length
        self.start_time, self.end_time = start_time, start_time + length
        self.looping = True
        self.loop_start = self.start_marker = 0.0
        self.loop_end = self.end_marker = length
        self.color = 0x123456
        self.groove = None
        self.notes = []
        self.envelopes = {}

    @property
    def has_groove(self):
        return self.groove is not None

    def add_new_notes(self, specs):
        self.notes.extend(Note(spec) for spec in specs)

    def get_all_notes_extended(self):
        return list(self.notes)

    def apply_note_modifications(self, notes):
        self.notes = list(notes)

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        self.notes = [n for n in self.notes if not (
            from_pitch <= n.pitch < from_pitch + pitch_span and from_time <= n.start_time < from_time + time_span)]

    def automation_envelope(self, parameter):
        return self.envelopes.get(parameter.name)

    def create_automation_envelope(self, parameter):
        return self.envelopes.setdefault(parameter.name, Envelope())

    def clear_envelope(self, parameter):
        self.envelopes.pop(parameter.name, None)

    def quantize(self, grid, amount):
        self.quantized = (grid, amount)


class Slot:
    def __init__(self):
        self.clip = None
        self.fired = 0

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        self.clip = Clip(length)

    def create_audio_clip(self, path):
        self.clip = audio_clip(path)
        return self.clip

    def delete_clip(self):
        self.clip = None

    def fire(self):
        self.fired += 1

    def duplicate_clip_to(self, target):
        target.clip = Clip(self.clip.length)


def audio_clip(path, start_time=0.0):
    clip = Clip(4.0, start_time)
    clip.is_midi_clip = False
    clip.file_path = path
    return clip


class Track:
    is_foldable = False
    is_frozen = False
    color = 0xff0000
    mute = solo = arm = False

    def __init__(self, name, midi=True, slots=8):
        self.name = name
        self.has_midi_input = midi
        self.can_be_armed = True
        self.clip_slots = [Slot() for _ in range(slots)]
        self.devices = []
        self.mixer_device = Mixer()
        self.arrangement_clips = []

    def stop_all_clips(self):
        pass

    def delete_device(self, index):
        del self.devices[index]

    def duplicate_clip_to_arrangement(self, clip, time):
        self.arrangement_clips.append(Clip(clip.length, time))

    def create_audio_clip(self, path, time):
        self.arrangement_clips.append(audio_clip(path, time))
        return self.arrangement_clips[-1]


class Scene:
    color = 0

    def __init__(self, name):
        self.name = name
        self.fired = 0

    def fire(self):
        self.fired += 1


class Song:
    tempo = 120.0
    signature_numerator = 4
    signature_denominator = 4
    is_playing = False
    metronome = False
    groove_amount = 1.0
    loop = False
    loop_start = 0.0
    loop_length = 16.0
    can_undo = True
    can_redo = False
    back_to_arranger = True
    start_time = 0.0

    def __init__(self):
        self.tracks = [Track("Drums"), Track("Audio", midi=False)]
        self.return_tracks = [Track("A-Reverb", midi=False, slots=0)]
        self.master_track = Track("Master", midi=False, slots=0)
        self.master_track.can_be_armed = False
        self.master_track.devices.append(Device("Limiter", class_name="Limiter"))
        self.scenes = [Scene("Intro"), Scene("Verse")]
        self.groove_pool = types.SimpleNamespace(grooves=[])
        self.view = types.SimpleNamespace(selected_track=self.tracks[0])
        self.cue_points = []
        self.song_time = 0.0
        self.pending_song_time = None
        self.undo_depth = 0
        self.undo_steps = 0

    @property
    def current_song_time(self):
        return self.song_time

    @current_song_time.setter
    def current_song_time(self, value):
        # Like Live, a stopped playhead only moves on the next tick.
        if self.is_playing:
            self.song_time = value
        else:
            self.pending_song_time = value

    def tick(self):
        if self.pending_song_time is not None:
            self.song_time, self.pending_song_time = self.pending_song_time, None

    def is_cue_point_selected(self):
        return any(cue.time == self.song_time for cue in self.cue_points)

    def set_or_delete_cue(self):
        here = [cue for cue in self.cue_points if cue.time == self.song_time]
        if here:
            self.cue_points.remove(here[0])
        else:
            self.cue_points.append(types.SimpleNamespace(name=str(len(self.cue_points) + 1), time=self.song_time))

    def begin_undo_step(self):
        self.undo_depth += 1

    def end_undo_step(self):
        self.undo_depth -= 1
        self.undo_steps += 1

    def start_playing(self):
        self.is_playing = True

    def continue_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def stop_all_clips(self):
        pass

    def create_midi_track(self, index):
        track = Track("New MIDI")
        self.tracks.insert(len(self.tracks) if index < 0 else index, track)

    def create_audio_track(self, index):
        track = Track("New Audio", midi=False)
        self.tracks.insert(len(self.tracks) if index < 0 else index, track)

    def delete_track(self, index):
        del self.tracks[index]

    def create_scene(self, index):
        self.scenes.insert(len(self.scenes) if index < 0 else index, Scene(""))

    def delete_scene(self, index):
        del self.scenes[index]


class BrowserItem:
    def __init__(self, name, children=(), loadable=False):
        self.name, self.children, self.is_loadable = name, list(children), loadable
        self.is_folder = bool(children)


class Browser:
    def __init__(self, song):
        self.song = song
        kit = BrowserItem("808 Core Kit.adg", loadable=True)
        self.drums = BrowserItem("Drums", [BrowserItem("Kits", [kit, BrowserItem("Acoustic Kit.adg", loadable=True)])])
        for name in ("instruments", "sounds", "audio_effects", "midi_effects", "max_for_live", "plugins", "clips", "samples", "packs", "user_library", "current_project"):
            setattr(self, name, BrowserItem(name))

    def load_item(self, item):
        self.song.view.selected_track.devices.append(Device(item.name, {36: "Kick 808", 38: "Snare 808", 42: "Hihat Closed", 46: "Hihat Open"}))


class View:
    focused_document_view = "Session"

    def show_view(self, name):
        if name not in ("Session", "Arranger"):
            raise RuntimeError("unknown view")
        self.focused_document_view = name


class Application:
    def __init__(self, song):
        self.song = song
        self.browser = Browser(song)
        self.view = View()

    def get_document(self):
        return self.song

    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 4

    def get_bugfix_version(self):
        return 6


class ControlSurface:
    def __init__(self, c_instance):
        self.c_instance = c_instance
        self.messages = []

    def log_message(self, *message):
        self.messages.append(message)

    def show_message(self, message):
        self.messages.append(message)

    def schedule_message(self, delay_in_ticks, callback):
        callback()

    def update_display(self):
        _application.song.tick()

    def disconnect(self):
        pass


def install():
    """Put fake Live and _Framework modules in sys.modules. Returns the Application."""
    global _application
    application = _application = Application(Song())
    live = types.ModuleType("Live")
    live.Application = types.SimpleNamespace(get_application=lambda: application)
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=MidiNoteSpecification)
    live.Song = types.SimpleNamespace(RecordingQuantization=types.SimpleNamespace(
        rec_q_quarter=1, rec_q_eight=2, rec_q_eight_triplet=3, rec_q_sixtenth=5, rec_q_sixtenth_triplet=6, rec_q_thirtysecond=8))
    framework = types.ModuleType("_Framework")
    surface = types.ModuleType("_Framework.ControlSurface")
    surface.ControlSurface = ControlSurface
    sys.modules.update({"Live": live, "_Framework": framework, "_Framework.ControlSurface": surface})
    return application
