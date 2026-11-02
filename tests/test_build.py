"""Checks the generated device without Max: the container, the patch wiring and the gen~ code's outlets."""

import json
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build  # noqa: E402


def read_amxd(data):
    assert data[:4] == b"ampf" and data[8:12] == build.AUDIO_EFFECT and data[12:16] == b"meta" and data[24:28] == b"ptch"
    length = struct.unpack("<I", data[28:32])[0]
    body = data[32:]
    assert len(body) == length and body.endswith(b"\0")
    return json.loads(body[:-1].decode("utf-8"))["patcher"]


def wiring_errors(patcher):
    boxes = {item["box"]["id"]: item["box"] for item in patcher["boxes"]}
    errors = []
    for item in patcher["lines"]:
        line = item["patchline"]
        (source, outlet), (destination, inlet) = line["source"], line["destination"]
        if source not in boxes or destination not in boxes:
            errors.append("cord to a missing box: %s" % line)
        elif outlet >= boxes[source]["numoutlets"] or inlet >= boxes[destination]["numinlets"]:
            errors.append("cord to a missing inlet or outlet: %s" % line)
    return errors


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            cls.patcher = read_amxd(build.build(Path(folder) / "device.amxd").read_bytes())
        cls.boxes = [item["box"] for item in cls.patcher["boxes"]]
        cls.gen = next(box for box in cls.boxes if box.get("text") == "gen~")["patcher"]

    def test_wiring(self):
        self.assertEqual(wiring_errors(self.patcher), [])
        self.assertEqual(wiring_errors(self.gen), [])

    def test_gen_code_has_an_outlet_for_every_measurement(self):
        code = build.GEN_SOURCE.read_text(encoding="utf-8")
        outlets = {int(n) for n in re.findall(r"^out(\d+)\s*=", code, re.MULTILINE)}
        self.assertEqual(outlets, set(range(1, 3 + len(build.MEASUREMENTS))))
        codebox = next(item["box"] for item in self.gen["boxes"] if item["box"]["maxclass"] == "codebox")
        self.assertEqual(codebox["numoutlets"], len(outlets))

    def test_dials_drive_gen_parameters(self):
        code = build.GEN_SOURCE.read_text(encoding="utf-8")
        declared = set(re.findall(r"^Param (\w+)\(", code, re.MULTILINE))
        dials = {box["varname"]: box for box in self.boxes if box["maxclass"] == "live.dial"}
        self.assertEqual(set(dials), {p[0] for p in build.PARAMETERS})
        for name, low, high, initial, _unit, gen_name in build.PARAMETERS:
            self.assertIn(gen_name, declared)
            values = dials[name]["saved_attribute_attributes"]["valueof"]
            self.assertEqual((values["parameter_mmin"], values["parameter_mmax"], values["parameter_initial"]), (low, high, [initial]))
            self.assertIn(r"Param %s(%g, min=%g, max=%g)" % (gen_name, initial, low, high), code)

    def test_settings_max_needs(self):
        # Max crashed on load when the project block was cut down.
        template = {"version", "creationdate", "modificationdate", "viewrect", "autoorganize", "hideprojectwindow",
                    "showdependencies", "autolocalize", "contents", "layout", "searchpath", "detailsvisible",
                    "amxdtype", "readonly", "devpathtype", "devpath", "sortmode", "viewmode"}
        self.assertLessEqual(template, set(self.patcher["project"]))
        dials = {box["id"]: box["varname"] for box in self.boxes if box["maxclass"] == "live.dial"}
        self.assertEqual({key: value[0] for key, value in self.patcher["parameters"].items()}, dials)

    def test_report_message(self):
        texts = [box.get("text", "") for box in self.boxes]
        self.assertIn("udpsend 127.0.0.1 %d" % build.PORT, texts)
        self.assertIn("pack" + " f" * len(build.MEASUREMENTS), texts)


if __name__ == "__main__":
    unittest.main()
