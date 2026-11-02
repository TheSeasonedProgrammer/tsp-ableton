"""TSP Spatial reports: OSC parsing and the figures get_audio_analysis returns."""

import math
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "src"))

from tsp_mcp_for_ableton_live import analysis  # noqa: E402


def _padded(text):
    data = text.encode() + b"\0"
    return data + b"\0" * (-len(data) % 4)


def osc(address, *arguments):
    """An OSC message as Max's udpsend writes it: ints, 32-bit floats and strings."""
    tags, payload = ",", b""
    for value in arguments:
        if isinstance(value, int):
            tags, payload = tags + "i", payload + struct.pack(">i", value)
        elif isinstance(value, float):
            tags, payload = tags + "f", payload + struct.pack(">f", value)
        else:
            tags, payload = tags + "s", payload + _padded(value)
    return _padded(address) + _padded(tags) + payload


def report(track_id, name, **values):
    """A report of a full-scale-ish stereo signal unless values say otherwise."""
    measured = dict(peak_l=0.5, peak_r=0.5, ms_l=0.1, ms_r=0.1, ms_lr=0.08, ms_mid=0.09, ms_side=0.01,
                    ms_low=0.045, ms_high=0.009, k_ms=0.2, samplerate=48000.0)
    measured.update(values)
    return osc("/tsp/analysis", track_id, name, *(float(measured[key]) for key in analysis.MEASUREMENTS))


class AnalysisTest(unittest.TestCase):
    def test_parse_osc(self):
        self.assertEqual(analysis.parse_osc(osc("/a/b", 3, "My Bass", 0.5)), ("/a/b", [3, "My Bass", 0.5]))
        with self.assertRaises(ValueError):
            analysis.parse_osc(osc("/a", 1)[:-2])
        with self.assertRaises(ValueError):
            analysis.parse_osc(b"not osc")

    def test_figures(self):
        store = analysis.AnalysisStore()
        store.receive(report(5, "Pad"), now=10.0)
        device = store.snapshot(now=10.5)["devices"][0]
        self.assertEqual((device["track"], device["age_seconds"], device["silent"]), ("Pad", 0.5, False))
        self.assertEqual(device["loudness_lufs"], round(-0.691 + 10 * math.log10(0.2), 1))
        self.assertEqual(device["peak_dbfs"], {"left": -6.0, "right": -6.0})
        self.assertEqual((device["balance_db"], device["correlation"], device["side_to_mid_db"]), (0.0, 0.8, -9.5))
        self.assertEqual(device["bands_db"], {"below_150_hz": -3.0, "above_5_khz": -10.0})
        self.assertEqual(device["band_levels_dbfs"], {"below_150_hz": -13.5, "above_5_khz": -20.5})

    def test_silence_and_stale_reports(self):
        store = analysis.AnalysisStore()
        store.receive(report(1, "Quiet", peak_l=0.0, peak_r=0.0, ms_l=0.0, ms_r=0.0, ms_lr=0.0, ms_mid=0.0, ms_side=0.0, k_ms=0.0), now=0.0)
        store.receive(report(2, "Gone"), now=0.0)
        store.receive(report(3, "Broken", samplerate=0.0), now=4.0)
        devices = {d["track"]: d for d in store.snapshot(now=4.5)["devices"]}
        self.assertTrue(devices["Quiet"]["stale"].startswith("no report"))
        self.assertIn("gen~", devices["Broken"]["stale"])
        store.receive(report(1, "Quiet", peak_l=0.0, peak_r=0.0, ms_l=0.0, ms_r=0.0, ms_lr=0.0, ms_mid=0.0, ms_side=0.0, k_ms=0.0), now=4.5)
        quiet = store.snapshot(track="quiet", now=4.5)["devices"]
        self.assertEqual((len(quiet), quiet[0]["silent"], quiet[0]["loudness_lufs"]), (1, True, None))
        self.assertNotIn("correlation", quiet[0])
        self.assertNotIn("band_levels_dbfs", quiet[0])

    def test_renamed_track_replaces_its_report_and_junk_is_ignored(self):
        store = analysis.AnalysisStore()
        store.receive(report(7, "Old"), now=0.0)
        store.receive(report(7, "New"), now=0.1)
        store.receive(osc("/tsp/analysis", 7, "Short", 1.0), now=0.2)
        store.receive(osc("/other", 7), now=0.2)
        store.receive(b"\xff\xfe", now=0.2)
        self.assertEqual([d["track"] for d in store.snapshot(now=0.3)["devices"]], ["New"])
        store.receive(report(8, '"Lead Synth"'), now=0.3)
        self.assertEqual(store.snapshot(track="lead synth", now=0.3)["devices"][0]["track"], "Lead Synth")
        self.assertIn("No TSP Spatial device", store.snapshot(track="Drums", now=0.3)["message"])


if __name__ == "__main__":
    unittest.main()
