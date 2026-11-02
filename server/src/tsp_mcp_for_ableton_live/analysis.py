"""Measurements from the TSP Spatial Max for Live device.

Each device sends "/tsp/analysis <track id> <track name> <11 floats>" as OSC over UDP ten
times a second. The floats are running measurements of its output, in MEASUREMENTS order.

Standard library only, so the tests can run this on Live's Python version.
"""

from __future__ import annotations

import math
import struct
import time
from typing import Any

ADDRESS = "/tsp/analysis"
MEASUREMENTS = ("peak_l", "peak_r", "ms_l", "ms_r", "ms_lr", "ms_mid", "ms_side", "ms_low", "ms_high", "k_ms", "samplerate")
# Older reports than this mean the device was removed, turned off or its track deleted.
STALE_SECONDS = 2.0
SILENT_LUFS = -70.0


def _string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\0", offset)
    return data[offset:end].decode("utf-8", "replace"), (end + 4) & ~3


def parse_osc(data: bytes) -> tuple[str, list[Any]]:
    """An OSC message's address and arguments. Raises ValueError for anything else."""
    try:
        address, offset = _string(data, 0)
        tags, offset = _string(data, offset)
        if not tags.startswith(","):
            raise ValueError("no type tags")
        arguments: list[Any] = []
        for tag in tags[1:]:
            if tag == "i":
                arguments.append(struct.unpack_from(">i", data, offset)[0])
                offset += 4
            elif tag == "f":
                arguments.append(struct.unpack_from(">f", data, offset)[0])
                offset += 4
            elif tag == "d":
                arguments.append(struct.unpack_from(">d", data, offset)[0])
                offset += 8
            elif tag == "h":
                arguments.append(struct.unpack_from(">q", data, offset)[0])
                offset += 8
            elif tag == "s":
                text, offset = _string(data, offset)
                arguments.append(text)
            else:
                raise ValueError("unsupported OSC type '%s'" % tag)
        return address, arguments
    except (struct.error, IndexError) as error:
        raise ValueError("truncated OSC message") from error


def _db(power: float) -> float | None:
    """Decibels from a mean square or squared peak, or None for silence."""
    return round(10 * math.log10(power), 1) if power > 1e-12 else None


def summarize(values: dict[str, float]) -> dict[str, Any]:
    """Readable figures from the raw measurements."""
    ms_l, ms_r, ms_mid, ms_side = values["ms_l"], values["ms_r"], values["ms_mid"], values["ms_side"]
    loudness = -0.691 + 10 * math.log10(values["k_ms"]) if values["k_ms"] > 1e-12 else None
    silent = loudness is None or loudness < SILENT_LUFS
    summary: dict[str, Any] = {
        "silent": silent,
        "loudness_lufs": None if loudness is None else round(loudness, 1),
        "peak_dbfs": {"left": _db(values["peak_l"] ** 2), "right": _db(values["peak_r"] ** 2)},
        "rms_dbfs": {"left": _db(ms_l), "right": _db(ms_r)},
    }
    if silent:
        return summary
    summary["balance_db"] = round(10 * math.log10(ms_l / ms_r), 1) if ms_l > 1e-12 and ms_r > 1e-12 else None
    summary["correlation"] = round(values["ms_lr"] / math.sqrt(ms_l * ms_r), 2) if ms_l > 1e-12 and ms_r > 1e-12 else None
    summary["side_to_mid_db"] = round(10 * math.log10(ms_side / ms_mid), 1) if ms_side > 1e-12 and ms_mid > 1e-12 else None
    if ms_mid > 1e-12:
        summary["bands_db"] = {"below_150_hz": _db(values["ms_low"] / ms_mid), "above_5_khz": _db(values["ms_high"] / ms_mid)}
    # The shares above barely move when a filter also lowers the rest of the signal, so report the levels too.
    summary["band_levels_dbfs"] = {"below_150_hz": _db(values["ms_low"]), "above_5_khz": _db(values["ms_high"])}
    return summary


class AnalysisStore:
    """The latest report from each device, keyed by its track's id."""

    def __init__(self) -> None:
        self.reports: dict[int, dict[str, Any]] = {}
        self.error: str | None = None

    def receive(self, data: bytes, now: float | None = None) -> None:
        try:
            address, arguments = parse_osc(data)
        except ValueError:
            return
        if address != ADDRESS or len(arguments) != 2 + len(MEASUREMENTS):
            return
        track_id, name, *numbers = arguments
        if not all(isinstance(n, (int, float)) for n in numbers):
            return
        name = str(name)
        if len(name) >= 2 and name[0] == name[-1] == '"':
            # live.observer quotes names that contain spaces.
            name = name[1:-1]
        self.reports[int(track_id)] = {
            "track": name,
            "received": time.monotonic() if now is None else now,
            "values": dict(zip(MEASUREMENTS, (float(n) for n in numbers))),
        }

    def snapshot(self, track: str | None = None, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        devices = []
        for report in self.reports.values():
            if track is not None and report["track"].lower() != track.lower():
                continue
            age = now - report["received"]
            entry: dict[str, Any] = {"track": report["track"], "age_seconds": round(age, 1)}
            if age > STALE_SECONDS:
                entry["stale"] = "no report for %.0f s: the device was removed, turned off or its track deleted" % age
            elif report["values"]["samplerate"] <= 0:
                entry["stale"] = "the device's gen~ code isn't running; open the device in Max to see the error"
            else:
                entry.update(summarize(report["values"]))
            devices.append(entry)
        result: dict[str, Any] = {"devices": sorted(devices, key=lambda d: d["track"].lower())}
        if self.error:
            result["error"] = self.error
        elif not devices:
            result["message"] = ("No TSP Spatial device has reported%s. Load it from User Library > TSP MCP > Devices "
                                 "onto the tracks to measure." % ("" if track is None else " from '%s'" % track))
        return result
