"""The Live Set: tempo, time signature, transport and undo."""

from ableton_tools._registry import tool
from ableton_tools import _util as u
from ableton_tools import arrangement
from ableton_tools import tracks


def _track_summary(track, index=None):
    info = {"name": track.name, "kind": u.track_kind(track)}
    if index is not None:
        info["index"] = index
    return info


def _state():
    song = u.song()
    application = u.application()
    return {
        "live_version": "%d.%d.%d" % (application.get_major_version(), application.get_minor_version(), application.get_bugfix_version()),
        "tempo": song.tempo,
        "signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
        "playing": song.is_playing,
        "song_time": song.current_song_time,
        "metronome": song.metronome,
        "groove_amount": song.groove_amount,
        "grooves": [groove.name for groove in song.groove_pool.grooves],
        "loop": {"on": song.loop, "start": song.loop_start, "length": song.loop_length},
        "can_undo": song.can_undo,
        "can_redo": song.can_redo,
        "selected_track": song.view.selected_track.name,
        "view": application.view.focused_document_view,
        "tracks": [_track_summary(track, index) for index, track in enumerate(song.tracks)],
        "return_tracks": [_track_summary(track) for track in song.return_tracks],
        "scenes": [{"index": index, "name": scene.name} for index, scene in enumerate(song.scenes)],
        "locators": arrangement.locators(),
    }


def _snapshot_track(track, parameters):
    info = tracks.track_info(track)
    if info["kind"] != "master":
        info["clips"] = [dict(u.clip_info(slot.clip), slot=index) for index, slot in enumerate(track.clip_slots) if slot.has_clip]
    # Live raises for the arrangement clips of group, return and master tracks.
    if info["kind"] in ("midi", "audio"):
        info["arrangement_clips"] = arrangement.arrangement_clips(track)
    if parameters:
        for entry, device in zip(info["devices"], track.devices):
            entry["parameters"] = [u.parameter_info(parameter, index) for index, parameter in enumerate(device.parameters)]
    return info


@tool("get_session", "Overview of the Live Set: tempo, signature, transport, loop, tracks, return tracks, scenes and locators.", warn=True)
def get_session(_args):
    return _state()


@tool("get_session_snapshot", "Everything get_session returns, with each track's mixer, devices, Session clips and Arrangement clips, "
                              "and the master track. parameters adds every device's parameters, which makes the reply large.",
      {"parameters": {"type": "boolean"}}, warn=True)
def get_session_snapshot(args):
    song = u.song()
    parameters = args.get("parameters", False)
    return dict(_state(),
                tracks=[_snapshot_track(track, parameters) for track in song.tracks],
                return_tracks=[_snapshot_track(track, parameters) for track in song.return_tracks],
                master=_snapshot_track(song.master_track, parameters))


@tool("set_session", "Change the tempo, time signature, metronome, global groove amount or arrangement loop.",
      {"tempo": {"type": "number", "minimum": 20, "maximum": 999},
       "signature_numerator": {"type": "integer", "minimum": 1, "maximum": 99},
       "signature_denominator": {"type": "integer", "enum": [1, 2, 4, 8, 16]},
       "metronome": {"type": "boolean"},
       "groove_amount": {"type": "number", "minimum": 0, "maximum": 1.31, "description": "Global Groove Amount; 1 is 100%."},
       "loop": {"type": "boolean"},
       "loop_start": {"type": "number", "minimum": 0, "description": "Beats."},
       "loop_length": {"type": "number", "exclusiveMinimum": 0, "description": "Beats."}},
      write=True)
def set_session(args):
    if not args:
        raise ValueError("give at least one setting")
    song = u.song()
    for name in ("tempo", "signature_numerator", "signature_denominator", "metronome", "groove_amount", "loop", "loop_start", "loop_length"):
        if name in args:
            setattr(song, name, args[name])
    return _state()


@tool("play", "Start the transport, from from_beat if given, otherwise from the start marker. "
              "arrangement presses Back to Arrangement first, which tracks need after playing Session clips.",
      {"from_beat": {"type": "number", "minimum": 0},
       "arrangement": {"type": "boolean"}}, warn=True)
def play(args):
    song = u.song()
    if args.get("arrangement"):
        song.back_to_arranger = False
    if "from_beat" in args and song.is_playing:
        song.current_song_time = args["from_beat"]
    else:
        # Live ignores current_song_time while stopped, so a stopped Set starts from the marker.
        if "from_beat" in args:
            song.start_time = args["from_beat"]
        song.start_playing()
    return {"playing": True, "song_time": song.current_song_time}


@tool("stop", "Stop the transport. stop_clips also stops every Session View clip.",
      {"stop_clips": {"type": "boolean"}})
def stop(args):
    song = u.song()
    song.stop_playing()
    if args.get("stop_clips"):
        song.stop_all_clips()
    return {"playing": False}


@tool("undo", "Undo the last change in Live.")
def undo(_args):
    song = u.song()
    if not song.can_undo:
        raise ValueError("nothing to undo")
    song.undo()
    return {"can_undo": song.can_undo, "can_redo": song.can_redo}


@tool("redo", "Redo the last undone change in Live.")
def redo(_args):
    song = u.song()
    if not song.can_redo:
        raise ValueError("nothing to redo")
    song.redo()
    return {"can_undo": song.can_undo, "can_redo": song.can_redo}
