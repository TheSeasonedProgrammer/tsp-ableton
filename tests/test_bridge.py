"""Runs the Remote Script against fake_live over a real socket, and through the MCP server.

    uv run --no-project --python 3.11 python -m unittest discover -s tests
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPTS = ROOT / "server" / "src" / "tsp_mcp_for_ableton_live" / "remote_script"
sys.path[:0] = [str(ROOT / "tests"), str(REMOTE_SCRIPTS), str(REMOTE_SCRIPTS / "TSP_MCP")]
PORT = "18768"
ANALYSIS_PORT = "18769"
os.environ["ABLETON_BRIDGE_PORT"] = PORT

import fake_live
import test_analysis
import test_serum

APPLICATION = fake_live.install()

import TSP_MCP
from ableton_tools import _drums as d


class DrumLogic(unittest.TestCase):
    PADS = [{"note": 36, "name": "Kick 808"}, {"note": 38, "name": "Snare 808"}, {"note": 42, "name": "Hihat Closed"},
            {"note": 46, "name": "Hihat Open"}, {"note": 41, "name": "Tom Low"}]

    def test_steps(self):
        self.assertEqual(d.parse_steps("x.X| o_-", 100), [100, None, 120, 45, None, None])
        self.assertEqual(d.parse_steps("9", 100), [127])
        with self.assertRaises(ValueError):
            d.parse_steps("xq", 100)
        with self.assertRaises(ValueError):
            d.parse_steps(" | ", 100)

    def test_pad_roles(self):
        for name, role in (("Kick 808", "kick"), ("BD Deep", "kick"), ("Snare Hip-Hop", "snare"), ("Clap 909", "clap"),
                           ("Hihat Closed", "closed_hat"), ("Hat", "closed_hat"), ("CH 808", "closed_hat"),
                           ("Hihat Open", "open_hat"), ("Open Hat", "open_hat"), ("OH 808", "open_hat"), ("hh_open", "open_hat"),
                           ("Tom Low", "tom_low"), ("Hi Tom", "tom_high"), ("Crash", "crash"), ("Cowbell", "cowbell"),
                           # Names from Live's 808 Core Kit.
                           ("Bass Drum", "kick"), ("Rim Shot", "rim"), ("Snare Drum", "snare"), ("Hand Clap", "clap"),
                           ("Closed Hi Hat", "closed_hat"), ("Open Hi Hat", "open_hat"), ("Mid Conga", "conga"),
                           ("Maracas", "shaker"), ("Cymbal", "crash"), ("Cow Bell", "cowbell"), ("Claves", "perc"),
                           ("Vocal Chop", None), ("Shirt", None), ("Empty", None)):
            self.assertEqual(d.pad_role(name), role, name)

    def test_resolve(self):
        self.assertEqual(d.resolve_voice("kick", self.PADS)["pitch"], 36)
        self.assertEqual(d.resolve_voice("Open Hat", self.PADS)["pitch"], 46)
        self.assertEqual(d.resolve_voice("hat", self.PADS)["pitch"], 42)
        self.assertEqual(d.resolve_voice("Tom Low", self.PADS)["matched_by"], "pad_name")
        self.assertEqual(d.resolve_voice("C1", self.PADS), {"pitch": 36, "pad": "Kick 808", "matched_by": "pitch"})
        self.assertEqual(d.resolve_voice("F#1", None)["pitch"], 42)
        self.assertEqual(d.resolve_voice("60", None)["pitch"], 60)
        self.assertEqual(d.resolve_voice("clap", None), {"pitch": 39, "pad": None, "matched_by": "general_midi"})

    def test_missing_pad_is_an_error_not_a_general_midi_guess(self):
        with self.assertRaisesRegex(ValueError, "Kick 808"):
            d.resolve_voice("clap", self.PADS)

    def test_repeat_and_timing(self):
        notes = d.build_notes({36: [100, None]}, 2.0)
        self.assertEqual([n["start_time"] for n in notes], [0.0, 0.5, 1.0, 1.5])

    def test_swing_delays_only_off_steps_and_never_overlaps(self):
        notes = d.build_notes({42: [100]}, 1.0, swing=75)
        self.assertEqual([n["start_time"] for n in notes], [0.0, 0.375, 0.5, 0.875])
        for first, second in zip(notes, notes[1:]):
            self.assertLessEqual(first["start_time"] + first["duration"], second["start_time"])

    def test_swing_needs_even_grid(self):
        with self.assertRaises(ValueError):
            d.build_notes({36: [100]}, 1.0, steps_per_beat=3, swing=60)

    def test_humanize_is_repeatable_and_stays_in_range(self):
        args = dict(humanize_ms=20, humanize_velocity=40, seed=7)
        first = d.build_notes({36: [120, 3]}, 4.0, **args)
        self.assertEqual(first, d.build_notes({36: [120, 3]}, 4.0, **args))
        self.assertTrue(all(1 <= n["velocity"] <= 127 and n["start_time"] >= 0 for n in first))


class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.surface = TSP_MCP.create_instance(None)
        cls.ticking = threading.Event()
        cls.main_thread = threading.Lock()

        def tick():
            while not cls.ticking.wait(0.01):
                with cls.main_thread:
                    cls.surface.update_display()

        cls.ticker = threading.Thread(target=tick, daemon=True)
        cls.ticker.start()

    @classmethod
    def tearDownClass(cls):
        cls.ticking.set()
        cls.ticker.join()
        cls.surface.disconnect()

    def send(self, method, params=None):
        with socket.create_connection(("127.0.0.1", int(PORT)), timeout=10) as connection:
            connection.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
            with connection.makefile(encoding="utf-8") as stream:
                return json.loads(stream.readline())

    def call(self, tool, /, **arguments):
        reply = self.send("call_tool", {"name": tool, "arguments": arguments})
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def fails(self, tool, /, **arguments):
        reply = self.send("call_tool", {"name": tool, "arguments": arguments})
        self.assertIn("error", reply, reply)
        return reply["error"]["message"]

    def setUp(self):
        song = APPLICATION.song
        song.tracks[:] = [fake_live.Track("Drums"), fake_live.Track("Audio", midi=False)]
        song.scenes[:] = [fake_live.Scene("Intro"), fake_live.Scene("Verse")]
        song.cue_points[:] = []
        song.undo_steps = 0

    def test_status_and_tools(self):
        status = self.send("status")["result"]
        self.assertEqual(status["live"], "12.4.6")
        self.assertIsNone(status["tools_error"])
        tools = self.send("list_tools")["result"]["tools"]
        self.assertEqual(status["tools"], len(tools))
        self.assertIn("write_drum_pattern", {t["name"] for t in tools})

    def test_session(self):
        session = self.call("get_session")
        self.assertEqual(session["signature"], "4/4")
        self.assertEqual([t["kind"] for t in session["tracks"]], ["midi", "audio"])
        self.assertEqual(self.call("set_session", tempo=92)["tempo"], 92)
        self.assertEqual(APPLICATION.song.undo_steps, 1)
        self.assertEqual(APPLICATION.song.undo_depth, 0)

    def test_undo_step_closes_when_a_tool_fails(self):
        self.assertIn("confirm", self.fails("delete_track", track=0))
        self.assertEqual(APPLICATION.song.undo_depth, 0)

    def test_tracks(self):
        created = self.call("create_track", kind="midi", name="Bass")
        self.assertEqual((created["index"], created["name"]), (2, "Bass"))
        self.assertEqual(self.call("get_track", track="bass")["index"], 2)
        self.assertEqual(self.call("get_track", track="master")["kind"], "master")
        self.assertEqual(self.call("get_track", track="A-Reverb")["kind"], "return")
        changed = self.call("set_track", track="Bass", volume=0.5, pan=-0.25, mute=True, color="#00ff00", sends={"A": 0.3})
        self.assertEqual((changed["volume"]["value"], changed["pan"]["value"], changed["mute"]), (0.5, -0.25, True))
        self.assertEqual(changed["sends"][0]["value"], 0.3)
        self.assertEqual(self.call("delete_track", track="Bass", confirm=True), {"deleted": "Bass"})
        self.assertIn("no track", self.fails("get_track", track=9))

    def test_limiter_warning(self):
        master = APPLICATION.song.master_track
        limiter = master.devices[0]
        self.addCleanup(setattr, master, "devices", [limiter])
        warning = TSP_MCP.sys.modules["ableton_tools"].limiter_warning

        self.assertIsNone(warning())
        self.assertNotIn("warning", self.call("set_session", tempo=125))

        master.devices = []
        for reply in (self.call("set_session", tempo=126), self.call("get_session"), self.call("play"),
                      self.call("fire_scene", scene=0), self.call("create_track", kind="midi")):
            self.assertIn("Limiter", reply["warning"])
        self.assertNotIn("warning", self.call("stop"))
        self.assertNotIn("warning", self.call("list_clips", track="Drums"))
        self.assertIn(warning(), self.surface.messages)

        # Opening a Set shows it too.
        self.surface.messages.clear()
        self.surface._check_limiter()
        self.assertEqual(self.surface.messages, [warning()])

        # Only an active Limiter counts, and it can sit in a rack.
        off = fake_live.Device("Limiter", class_name="Limiter")
        off.is_active = False
        master.devices = [off, fake_live.Device("Color Limiter", class_name="MxDeviceAudioEffect")]
        self.assertIsNotNone(warning())
        rack = fake_live.Device("Rack", class_name="AudioEffectGroupDevice")
        rack.can_have_chains = True
        rack.chains = [types.SimpleNamespace(devices=[limiter])]
        master.devices = [rack]
        self.assertIsNone(warning())

    def test_pigments_presets(self):
        state = b"22 serialization::archive 10 0 7 0 7 9 Test Bass 7 Factory 22 11 Test Author 4 Bass 0 0 2 0 3 80s 6 Analog 1 0 9 Deep bass 17 tail"
        with tempfile.TemporaryDirectory() as folder:
            root, library = Path(folder) / "Presets", Path(folder) / "Library"
            for relative in ("Factory/Factory/Test Bass", "Factory/Pigments 2.0/Test Bass", "Tutorials/Step1"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes(state)
            (root / "presets.featured.xml").write_bytes(b"<rootnode/>")

            module = sys.modules["ableton_tools.pigments"]
            with mock.patch.object(module, "PRESET_ROOT", str(root)), mock.patch.object(module, "_user_library", lambda: str(library)):
                found = self.call("list_pigments_presets", query="deep analog", type="bass")
                self.assertEqual((found["total"], found["matched"]), (2, 2))
                self.assertEqual(found["presets"][0]["author"], "Test Author")
                self.assertEqual(found["presets"][0]["tags"], ["80s", "Analog"])
                self.assertEqual(self.call("list_pigments_presets", type="lead")["matched"], 0)

                self.assertIn("several", self.fails("load_pigments_preset", track="Drums", preset="Test Bass"))
                self.assertIn("no Pigments preset", self.fails("load_pigments_preset", track="Drums", preset="Nope"))

                # The browser hasn't seen the file yet, then it has.
                path = "Factory/Factory/Test Bass"
                self.assertFalse(self.call("load_pigments_preset", track="Drums", preset=path)["loaded"])
                name = "Factory - Factory - Test Bass.vstpreset"
                written = (library / "TSP MCP" / "Pigments" / name).read_bytes()
                self.assertEqual(written[:8], b"VST3" + struct.pack("<i", 1))
                self.assertEqual(written[8:40], b"41727475415649534B61743150726F63")
                table = struct.unpack("<q", written[40:48])[0]
                self.assertEqual(written[48:table], state)
                self.assertEqual(written[table:table + 8], b"List" + struct.pack("<i", 2))
                self.assertEqual(written[table + 8:table + 28], b"Comp" + struct.pack("<qq", 48, len(state)))

                item = fake_live.BrowserItem(name, loadable=True)
                folders = fake_live.BrowserItem("TSP MCP", [fake_live.BrowserItem("Pigments", [item])])
                with mock.patch.object(APPLICATION.browser.user_library, "children", [folders]):
                    loaded = self.call("load_pigments_preset", track="Drums", preset=path)
                self.assertEqual((loaded["loaded"], loaded["devices"]), (True, [name]))

    def test_drum_beat_on_a_loaded_kit(self):
        found = self.call("search_browser", query="808 kit", category="drums")
        self.assertEqual(found["results"][0]["path"], ["drums", "Kits", "808 Core Kit.adg"])
        loaded = self.call("load_from_browser", track="Drums", path=found["results"][0]["path"])
        self.assertEqual(loaded["devices"], ["808 Core Kit.adg"])
        kit = self.call("get_drum_kit", track="Drums")
        self.assertEqual({p["role"]: p["note"] for p in kit["pads"]}, {"kick": 36, "snare": 38, "closed_hat": 42, "open_hat": 46})

        written = self.call("write_drum_pattern", track="Drums", slot=0, bars=2, name="Beat", fire=True, pattern={
            "kick": "x...|..x.|x...|....", "snare": "....|x...|....|x..o", "hat": "x.x.|x.x.|x.x.|x.X."})
        self.assertEqual(written["mapping"], "808 Core Kit.adg")
        self.assertEqual(written["length_beats"], 8.0)
        self.assertEqual(written["notes_written"], (3 + 3 + 8) * 2)
        slot = APPLICATION.song.tracks[0].clip_slots[0]
        self.assertEqual((slot.clip.name, slot.fired), ("Beat", 1))

        # Rewriting one voice leaves the others alone.
        self.call("write_drum_pattern", track="Drums", slot=0, pattern={"hat": "xxxx"})
        notes = self.call("get_notes", track="Drums", slot=0)["notes"]
        self.assertEqual(sum(n["pitch"] == 42 for n in notes), 32)
        self.assertEqual(sum(n["pitch"] == 36 for n in notes), 6)
        self.assertIn("no pad", self.fails("write_drum_pattern", track="Drums", slot=0, pattern={"clap": "x"}))
        self.assertIn("already has a clip", self.fails("write_drum_pattern", track="Drums", slot=0, bars=4, pattern={"kick": "x"}))

    def test_general_midi_without_a_rack(self):
        written = self.call("write_drum_pattern", track=0, slot=1, pattern={"clap": "x"})
        self.assertEqual(written["mapping"], "general_midi")
        self.assertEqual(written["voices"]["clap"]["pitch"], 39)
        self.assertIn("not a MIDI track", self.fails("write_drum_pattern", track="Audio", slot=0, pattern={"kick": "x"}))

    def test_notes(self):
        self.call("create_clip", track=0, slot=0, bars=1)
        added = self.call("add_notes", track=0, slot=0, notes=[{"pitch": 60, "start_time": 0, "duration": 1}, {"pitch": 64, "start_time": 1, "duration": 1, "velocity": 80}])
        self.assertEqual(added, {"added": 2, "total": 2})
        notes = self.call("get_notes", track=0, slot=0)["notes"]
        self.assertEqual((notes[0]["name"], notes[0]["velocity"]), ("C3", 100.0))
        self.call("edit_notes", track=0, slot=0, notes=[{"id": notes[0]["id"], "pitch": 62, "velocity": 50}])
        self.assertEqual(self.call("get_notes", track=0, slot=0, to_pitch=62)["notes"][0]["pitch"], 62)
        self.assertIn("no note", self.fails("edit_notes", track=0, slot=0, notes=[{"id": -1}]))
        self.assertEqual(self.call("clear_notes", track=0, slot=0, from_pitch=64), {"removed": 1, "remaining": 1})
        self.call("quantize_clip", track=0, slot=0, grid="1/16")

    def test_clips(self):
        self.call("create_clip", track=0, slot=0, length_beats=4, name="A")
        self.assertIn("already has", self.fails("create_clip", track=0, slot=0))
        # A loop moved fully past the old end only works if the end moves first.
        moved = self.call("set_clip", track=0, slot=0, loop_start=8, loop_end=12)
        self.assertEqual((moved["loop_start"], moved["loop_end"]), (8, 12))
        APPLICATION.song.groove_pool.grooves[:] = [fake_live.Scene("Swing 16ths 66")]
        self.assertEqual(self.call("set_clip", track=0, slot=0, groove="swing 16ths 66")["groove"], "Swing 16ths 66")
        self.call("duplicate_clip", track=0, slot=0, target_slot=2)
        self.assertEqual(len(self.call("list_clips", track=0)["clips"]), 2)
        self.call("fire_clip", track=0, slot=0)
        self.assertEqual(self.call("delete_clip", track=0, slot=2, confirm=True), {"deleted": ""})

    def test_devices(self):
        APPLICATION.song.tracks[0].devices.append(fake_live.Device("Reverb"))
        self.assertEqual(self.call("list_devices", track=0)["devices"][0]["name"], "Reverb")
        self.assertEqual(self.call("set_device_parameter", track=0, device="Reverb", parameter="Decay", value=0.25)["value"], 0.25)
        self.assertEqual(self.call("set_device_parameter", track=0, device=0, parameter="Mode", value="Slow")["value"], 1)
        self.assertIn("takes", self.fails("set_device_parameter", track=0, device=0, parameter=1, value=9))
        self.assertIn("no item", self.fails("set_device_parameter", track=0, device=0, parameter=2, value="Nope"))
        self.assertEqual(len(self.call("get_device_parameters", track=0, device=0, filter="dec")["parameters"]), 1)
        self.call("delete_device", track=0, device=0, confirm=True)

    def test_scenes_arrangement_and_automation(self):
        self.assertEqual(self.call("create_scene", name="Chorus")["index"], 2)
        self.call("fire_scene", scene=0)
        self.assertEqual(self.call("delete_scene", scene=2, confirm=True), {"deleted": "Chorus"})
        self.call("create_clip", track=0, slot=0, bars=1)
        self.assertEqual(self.call("copy_clip_to_arrangement", track=0, slot=0, beat=8, repeat=4)["end"], 24)
        self.call("set_clip_automation", track=0, slot=0, device="mixer", parameter="volume",
                  points=[{"time": 2, "value": 0.5}, {"time": 0, "value": 0.8}])
        clip = APPLICATION.song.tracks[0].clip_slots[0].clip
        self.assertEqual(clip.envelopes["Track Volume"].steps, [(0, 2, 0.8), (2, 2.0, 0.5)])
        self.call("clear_clip_automation", track=0, slot=0, device="mixer", parameter="volume")
        self.assertFalse(clip.envelopes)

    def test_audio_clips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "loop.wav")
            Path(path).write_bytes(b"RIFF")
            clip = self.call("create_audio_clip", track="Audio", slot=0, file_path=path, name="Loop")
            self.assertEqual((clip["midi"], clip["name"], clip["file_path"], clip["slot"]), (False, "Loop", path, 0))
            self.assertIn("already has", self.fails("create_audio_clip", track="Audio", slot=0, file_path=path))
            placed = self.call("create_audio_clip", track="Audio", beat=16, file_path=path)
            self.assertEqual((placed["start"], placed["end"]), (16, 20))
            self.assertIn("not an audio track", self.fails("create_audio_clip", track="Drums", slot=1, file_path=path))
            self.assertIn("not both", self.fails("create_audio_clip", track="Audio", slot=1, beat=0, file_path=path))
            self.assertIn("not both", self.fails("create_audio_clip", track="Audio", file_path=path))
            self.assertIn("absolute", self.fails("create_audio_clip", track="Audio", slot=1, file_path="loop.wav"))
            self.assertIn("no file", self.fails("create_audio_clip", track="Audio", slot=1, file_path=path + ".missing"))

        self.assertEqual(self.call("list_arrangement_clips", track="Audio")["clips"][0]["index"], 0)
        renamed = self.call("set_arrangement_clip_name", track="Audio", clip=0, name="Break")
        self.assertEqual((renamed["name"], renamed["start"]), ("Break", 16))
        self.assertIn("no arrangement clip 1", self.fails("set_arrangement_clip_name", track="Audio", clip=1, name="X"))

    def call_until_done(self, tool, /, **arguments):
        """What the MCP server does with retry_after."""
        for _attempt in range(50):
            result = self.call(tool, **arguments)
            if "retry_after" not in result:
                return result
            time.sleep(result["retry_after"])
        self.fail("%s kept asking to be called again" % tool)

    def test_locators_and_view(self):
        song = APPLICATION.song
        song.song_time = 4.0
        # Live only moves a stopped playhead on its next tick, so the first call asks for another.
        self.assertIn("retry_after", self.call("create_locator", beat=32, name="Drop"))
        self.assertEqual(self.call_until_done("create_locator", beat=32, name="Drop")["beat"], 32)
        made = self.call_until_done("create_locator", beat=8)
        self.assertEqual(made["locators"], [{"name": made["name"], "beat": 8}, {"name": "Drop", "beat": 32}])
        time.sleep(0.1)
        self.assertEqual(song.current_song_time, 4.0)
        # One already there is renamed, not toggled off by set_or_delete_cue.
        self.assertEqual(self.call("create_locator", beat=32, name="Build")["locators"][1], {"name": "Build", "beat": 32})
        self.assertEqual(len(self.call("get_session")["locators"]), 2)
        song.is_playing = True
        try:
            self.assertIn("stop the transport", self.fails("create_locator", beat=64))
        finally:
            song.is_playing = False

        # Past the end of the arrangement, a temporary clip makes room and is gone afterwards.
        drums = song.tracks[0]
        clips_before = list(drums.arrangement_clips)
        self.assertLess(song.song_length, 5400)
        far = self.call_until_done("create_locator", beat=5400, name="End")
        self.assertEqual((far["name"], far["beat"]), ("End", 5400))
        self.assertNotIn("temporary_clip_left", far)
        self.assertEqual(drums.arrangement_clips, clips_before)
        time.sleep(0.1)
        self.assertEqual(song.current_song_time, 4.0)

        self.assertEqual(self.call("switch_to_arrangement_view"), {"view": "Arranger"})
        self.assertEqual(self.call("get_session")["view"], "Arranger")

    def test_load_drum_kit(self):
        loaded = self.call("load_drum_kit", track="Drums", kit="808 core kit")
        self.assertEqual((loaded["loaded"], loaded["path"]), ("808 Core Kit.adg", ["drums", "Kits", "808 Core Kit.adg"]))
        self.assertEqual({p["role"] for p in loaded["pads"]}, {"kick", "snare", "closed_hat", "open_hat"})
        self.assertEqual(self.call("load_drum_kit", track="Drums", kit="kit")["other_matches"], ["Acoustic Kit.adg"])
        self.assertIn("no drum kit", self.fails("load_drum_kit", track="Drums", kit="909"))
        self.assertIn("not a MIDI track", self.fails("load_drum_kit", track="Audio", kit="808"))

    def test_session_snapshot(self):
        self.call("create_clip", track=0, slot=1, bars=2, name="Bass")
        self.call("copy_clip_to_arrangement", track=0, slot=1, beat=0)
        APPLICATION.song.tracks[0].devices.append(fake_live.Device("Reverb"))
        snapshot = self.call("get_session_snapshot")
        drums = snapshot["tracks"][0]
        self.assertEqual((drums["clips"][0]["slot"], drums["clips"][0]["length_beats"]), (1, 8.0))
        self.assertEqual(drums["arrangement_clips"][0]["end"], 8.0)
        self.assertNotIn("parameters", drums["devices"][0])
        self.assertEqual((snapshot["master"]["kind"], snapshot["return_tracks"][0]["name"]), ("master", "A-Reverb"))
        detailed = self.call("get_session_snapshot", parameters=True)
        self.assertEqual(detailed["tracks"][0]["devices"][0]["parameters"][1]["name"], "Decay")

    def test_play_can_return_to_the_arrangement(self):
        self.call("play")
        self.assertTrue(APPLICATION.song.back_to_arranger)
        self.call("play", arrangement=True)
        self.assertFalse(APPLICATION.song.back_to_arranger)

    def test_play_from_a_beat_uses_the_marker_when_stopped(self):
        self.call("stop")
        self.call("play", from_beat=8)
        self.assertEqual(APPLICATION.song.start_time, 8)
        self.call("play", from_beat=16)
        self.assertEqual(APPLICATION.song.current_song_time, 16)
        self.assertEqual(APPLICATION.song.start_time, 8)
        self.call("stop")

    def test_port_rebinds_straight_away(self):
        """Live disconnects the script and creates a new one whenever a Set opens."""
        self.send("status")
        with self.main_thread:
            TSP_MCP.stop_server()
            TSP_MCP.start_server()
        self.assertEqual(self.send("status")["result"]["bridge"], "TSP MCP")

    def test_replies_larger_than_the_socket_buffer(self):
        """Non-blocking sends can't take a big reply in one go."""
        self.call("create_clip", track=0, slot=0, bars=512)
        self.call("add_notes", track=0, slot=0, notes=[{"pitch": 60, "start_time": i * 0.01, "duration": 0.01} for i in range(20000)])
        self.assertEqual(self.call("get_notes", track=0, slot=0)["count"], 20000)

    def test_several_requests_on_one_connection(self):
        with socket.create_connection(("127.0.0.1", int(PORT)), timeout=10) as connection:
            connection.sendall(b'{"id": 1, "method": "status"}\n{"id": 2, "method": "status"}\n')
            with connection.makefile(encoding="utf-8") as stream:
                self.assertEqual([json.loads(stream.readline())["id"] for _ in range(2)], [1, 2])

    def test_malformed_request_gets_an_error_and_the_bridge_survives(self):
        with socket.create_connection(("127.0.0.1", int(PORT)), timeout=10) as connection:
            connection.sendall(b"not json\n")
            with connection.makefile(encoding="utf-8") as stream:
                self.assertIn("error", json.loads(stream.readline()))
        self.assertEqual(self.send("status")["result"]["bridge"], "TSP MCP")

    def test_reload_tools(self):
        self.assertIsNone(self.send("reload_tools")["result"]["error"])
        self.assertEqual(self.call("get_session")["tempo"], APPLICATION.song.tempo)

    def test_mcp_server_end_to_end(self):
        """Drive the real MCP server over stdio, as Claude Code does."""
        serum_folder = tempfile.TemporaryDirectory()
        self.addCleanup(serum_folder.cleanup)
        Path(serum_folder.name, "Pack").mkdir()
        Path(serum_folder.name, "Pack", "Test.SerumPreset").write_bytes(test_serum.make_preset())
        env = dict(os.environ, ABLETON_BRIDGE_PORT=PORT, ABLETON_ANALYSIS_PORT=ANALYSIS_PORT, SERUM2_PRESETS=serum_folder.name,
                   LOCALAPPDATA=str(ROOT / "tests" / ".cache"))
        server = subprocess.Popen(["uv", "run", "--quiet", "--directory", str(ROOT / "server"), "tsp-mcp-for-ableton-live"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
        try:
            def rpc(request_id, method, params=None):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n")
                server.stdin.flush()
                while True:
                    reply = json.loads(server.stdout.readline())
                    if reply.get("id") == request_id:
                        return reply

            rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            server.stdin.flush()
            names = {tool["name"] for tool in rpc(2, "tools/list")["result"]["tools"]}
            self.assertLessEqual({"ableton_bridge_status", "ableton_reload_tools", "write_drum_pattern"}, names)

            result = rpc(3, "tools/call", {"name": "ableton_bridge_status", "arguments": {}})["result"]
            self.assertIn("12.4.6", result["content"][0]["text"])

            result = rpc(4, "tools/call", {"name": "write_drum_pattern", "arguments": {"track": 0, "slot": 3, "pattern": {"kick": "x..."}}})["result"]
            self.assertIn('"notes_written": 4', result["content"][0]["text"])

            # The server repeats a call that replies with retry_after, so the client sees one call.
            result = rpc(6, "tools/call", {"name": "create_locator", "arguments": {"beat": 48, "name": "Outro"}})["result"]
            self.assertEqual(json.loads(result["content"][0]["text"])["name"], "Outro")

            # TSP Spatial devices report over UDP straight to the server.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.sendto(test_analysis.report(4, "Pad"), ("127.0.0.1", int(ANALYSIS_PORT)))
            for request_id in range(100, 150):
                result = rpc(request_id, "tools/call", {"name": "get_audio_analysis", "arguments": {"track": "pad"}})["result"]
                devices = json.loads(result["content"][0]["text"])["devices"]
                if devices:
                    break
                time.sleep(0.05)
            self.assertEqual((devices[0]["track"], devices[0]["correlation"]), ("Pad", 0.8))

            # Serum 2 presets are handled in the server, with real zstd.
            def tool(request_id, tool_name, /, **arguments):
                reply = rpc(request_id, "tools/call", {"name": tool_name, "arguments": arguments})["result"]
                self.assertFalse(reply.get("isError"), reply)
                return json.loads(reply["content"][0]["text"])

            self.assertEqual(tool(200, "list_serum_presets", query="bass")["presets"][0]["path"], "Pack/Test.SerumPreset")
            self.assertEqual(tool(201, "get_serum_preset", preset="Test Bass")["settings"]["Env0.kParamAttack"], 0.01)
            created = tool(202, "create_serum_preset", base="Test Bass", name="Slow Bass", settings={"Env0.kParamAttack": 0.6, "Env0.kParamAtack": 1})
            self.assertEqual((created["created"], created["unrecognized"]), ("User/TSP MCP/Slow Bass.SerumPreset", ["Env0.kParamAtack"]))
            self.assertEqual(tool(203, "get_serum_preset", preset="Slow Bass")["settings"]["Env0.kParamAttack"], 0.6)
            reply = rpc(204, "tools/call", {"name": "create_serum_preset", "arguments": {"base": "Test Bass", "name": "Slow Bass", "settings": {}}})["result"]
            self.assertTrue(reply.get("isError"))
            self.assertIn("overwrite", reply["content"][0]["text"])

            # The schema is enforced before anything reaches Live.
            result = rpc(5, "tools/call", {"name": "write_drum_pattern", "arguments": {"track": 0, "slot": -1, "pattern": {"kick": "x"}}})["result"]
            self.assertTrue(result.get("isError"))
            self.assertIn("invalid arguments", result["content"][0]["text"])
        finally:
            server.stdin.close()
            server.terminate()
            server.wait(10)


if __name__ == "__main__":
    unittest.main()
