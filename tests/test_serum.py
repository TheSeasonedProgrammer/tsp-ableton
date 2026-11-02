"""Serum 2 presets: the container, conversion to VST3 state, and editing. Uses made-up presets, never Xfer's."""

import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "src"))

from tsp_mcp_for_ableton_live import serum  # noqa: E402

HEADER = {"fileType": "SerumPreset", "presetAuthor": "Tester", "presetDescription": "Made up", "presetName": "Test Bass",
          "product": "Serum2", "productVersion": "2.1.4", "tags": ["Bass"], "url": "https://xferrecords.com/",
          "vendor": "Xfer Records", "version": 7.0}
SOUND = {
    "Env0": {"plainParams": {"kParamAttack": 0.01, "kParamDecay": 0.4}},
    "Env1": {"plainParams": "default"},
    "Macro0": {"name": "SPIKE", "plainParams": {"kParamValue": 20.0}},
    "Oscillator1": {"WTOsc1": {"plainParams": {"kParamWarp": 0.3}, "relativePathToWT": "S2 Tables/Default Shapes.wav"},
                    "plainParams": {"kParamUnison": 5.0}},
    "ModSlot0": {"destModuleParamName": "kParamFreq", "plainParams": {"kParamAmount": 25.0}, "source": [3, 0]},
}
UI = {"SerumGUI": {"kUIParamShowKeyboard": 1.0}, "Osc": [{"kUIParamZoomToStartEnd": 0.0}], "arpBankDisplayName": ""}


def make_preset(sound=SOUND, header=HEADER):
    payload = dict(sound, **UI, fileType="SerumPreset", presetName=header["presetName"],
                   presetAuthor=header["presetAuthor"], presetDescription=header["presetDescription"], tags=header["tags"])
    return serum.join(header, serum.encode(dict(sorted(payload.items()))))


def chunks(vstpreset):
    """The Comp and Cont chunks of a .vstpreset."""
    assert vstpreset[:4] == b"VST3" and vstpreset[8:40] == serum.CLASS_ID
    table = struct.unpack_from("<q", vstpreset, 40)[0]
    assert vstpreset[table:table + 4] == b"List"
    found = {}
    for index in range(struct.unpack_from("<i", vstpreset, table + 4)[0]):
        tag, offset, size = struct.unpack_from("<4sqq", vstpreset, table + 8 + index * 20)
        found[tag] = vstpreset[offset:offset + size]
    return found


class SerumTest(unittest.TestCase):
    def test_container_round_trip_and_hash(self):
        blob = make_preset()
        header, payload = serum.split(blob)
        self.assertEqual(header["presetName"], "Test Bass")
        frame = blob[blob.index(b"\x28\xb5\x2f\xfd"):]
        self.assertEqual(header["hash"], hashlib.md5(frame).hexdigest())
        self.assertEqual(serum.decode(payload)[0]["Env0"], SOUND["Env0"])
        with self.assertRaises(serum.SerumError):
            serum.split(b"not a preset")

    def test_cbor_round_trip(self):
        for value in (0, 23, 24, 255, 256, 70000, -1, -500, 1.5, -0.25, "", "Ä text", b"\x00\x01", [1, [2]], {"a": {"b": None}}, True, False):
            self.assertEqual(serum.decode(serum.encode(value))[0], value)
        self.assertEqual(serum.decode(b"\xf9\x3c\x00")[0], 1.0)  # half float
        self.assertEqual(serum.decode(b"\xfa\x3f\x80\x00\x00")[0], 1.0)  # single float

    def test_conversion_splits_sound_from_ui(self):
        found = chunks(serum.to_vstpreset(make_preset()))
        processor_header, processor_payload = serum.split(found[b"Comp"])
        processor = serum.decode(processor_payload)[0]
        self.assertEqual(processor_header["component"], "processor")
        self.assertEqual(processor["component"], "processor")
        self.assertEqual({k: processor[k] for k in SOUND}, SOUND)
        self.assertFalse(set(processor) & serum.UI_KEYS)
        # The sound keys' bytes are copied, not re-encoded.
        original = dict(serum.entries(serum.split(make_preset())[1]))
        self.assertEqual(dict(serum.entries(processor_payload))["Env0"], original["Env0"])

        controller_header, controller_payload = serum.split(found[b"Cont"])
        controller = serum.decode(controller_payload)[0]
        self.assertEqual((controller_header["presetName"], controller["presetName"], controller["component"]),
                         ("Test Bass", "Test Bass", "controller"))
        self.assertEqual(controller["SerumGUI"], UI["SerumGUI"])
        self.assertNotIn("Env0", controller)
        self.assertNotIn("fileType", controller)

    def test_describe(self):
        settings = serum.describe(make_preset())["settings"]
        self.assertEqual(settings["Env0.kParamAttack"], 0.01)
        self.assertEqual(settings["Oscillator1.kParamUnison"], 5.0)
        self.assertEqual(settings["Oscillator1.WTOsc1.kParamWarp"], 0.3)
        self.assertEqual(settings["Oscillator1.WTOsc1.relativePathToWT"], "S2 Tables/Default Shapes.wav")
        self.assertEqual(settings["Macro0.name"], "SPIKE")
        self.assertEqual(settings["ModSlot0.destModuleParamName"], "kParamFreq")
        self.assertFalse(any(key.startswith("Env1") for key in settings))
        self.assertFalse(any(key.startswith(("SerumGUI", "Osc.")) for key in settings))

    def test_create(self):
        base = make_preset()
        created = serum.create(base, "Soft Bass", {"Env0.kParamAttack": 0.5, "Env1.kParamSustain": 1,
                                                   "Oscillator1.WTOsc1.kParamWarpMenu": "kFilterHPF", "Macro0.name": "WOBBLE"},
                               description="Slower", tags=["Bass", "Soft"])
        header, payload = serum.split(created)
        data = serum.decode(payload)[0]
        self.assertEqual((header["presetName"], header["presetAuthor"], header["presetDescription"], header["tags"]),
                         ("Soft Bass", "Tester", "Slower", ["Bass", "Soft"]))
        self.assertEqual((data["presetName"], data["tags"]), ("Soft Bass", ["Bass", "Soft"]))
        self.assertEqual(data["Env0"]["plainParams"], {"kParamAttack": 0.5, "kParamDecay": 0.4})
        self.assertEqual(data["Env1"]["plainParams"], {"kParamSustain": 1.0})
        self.assertIsInstance(data["Env1"]["plainParams"]["kParamSustain"], float)
        self.assertEqual(data["Oscillator1"]["WTOsc1"]["plainParams"]["kParamWarpMenu"], "kFilterHPF")
        self.assertEqual(data["Macro0"]["name"], "WOBBLE")
        # Untouched modules keep their bytes.
        before, after = dict(serum.entries(serum.split(base)[1])), dict(serum.entries(payload))
        self.assertEqual(after["ModSlot0"], before["ModSlot0"])
        # The new preset converts like any other.
        self.assertIn(b"Comp", chunks(serum.to_vstpreset(created)))

        for bad, message in (({"Nope0.kParamX": 1}, "no 'Nope0'"), ({"kParamX": 1}, "needs a module"),
                             ({"SerumGUI.kUIParamShowKeyboard": 0}, "display setting"),
                             ({"Env0.decay": 1}, "only kParam")):
            with self.assertRaises(serum.SerumError) as raised:
                serum.create(base, "X", bad)
            self.assertIn(message, str(raised.exception))

    def test_catalog_search_and_find(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Packs" / "Pack").mkdir(parents=True)
            (root / "Packs" / "Pack" / "BA_Test.SerumPreset").write_bytes(make_preset())
            (root / "User").mkdir()
            (root / "User" / "Pad.SerumPreset").write_bytes(make_preset(header=dict(HEADER, presetName="Warm Pad", tags=["Pad"])))
            (root / "User" / "broken.SerumPreset").write_bytes(b"junk")
            presets = serum.catalog(root)
            self.assertEqual([p["name"] for p in presets], ["Test Bass", "Warm Pad"])
            self.assertEqual([p["name"] for p in serum.search(presets, "warm pad")], ["Warm Pad"])
            self.assertEqual(serum.find(presets, "ba_test")["path"], "Packs/Pack/BA_Test.SerumPreset")
            self.assertEqual(serum.find(presets, "user/pad.serumpreset")["name"], "Warm Pad")
            with self.assertRaises(serum.SerumError):
                serum.find(presets, "missing")
        with self.assertRaises(serum.SerumError):
            serum.catalog(Path(folder) / "gone")

    def test_raw_frames(self):
        data = bytes(range(256)) * 1100  # spans more than one 128 KiB block
        frame = serum._raw_frame(data)
        self.assertEqual(serum._read_simple_frame(frame), data)
        self.assertEqual(serum._read_simple_frame(serum._raw_frame(b"")), b"")
        self.assertEqual(serum.safe_name('a/b:c? '), "a_b_c_")


if __name__ == "__main__":
    unittest.main()
