# TSP Spatial

A Max for Live audio effect that places a sound in the stereo field and measures the result, so an AI working through [TSP MCP for Ableton Live](../mcp) can check what it did. Nothing in Live's API can hear audio; this device sends numbers about the audio instead. This project is independent and isn't affiliated with or endorsed by Ableton or Cycling '74.

## Controls

| Parameter | Range | What it does |
|---|---|---|
| Azimuth | -90 to 90 | Constant-power balance, negative is left. The centre is unchanged. |
| Width | 0 to 200 % | Scales the side signal: 0 is mono, 100 unchanged, 200 twice as wide. |
| Distance | 0 to 100 % | Up to 12 dB quieter, with a low-pass that falls from 20 kHz to 2 kHz. |

They're ordinary Live parameters, so TSP MCP's `set_device_parameter` and automation work on them.

## Measurements

Ten times a second the device sends running measurements of its output, as OSC over UDP to 127.0.0.1:8769, where the TSP MCP server listens. Its `get_audio_analysis` tool turns them into:

- **Loudness:** momentary loudness in LUFS, using BS.1770 K-weighting. The 400 ms window is approximated with an exponential average, so treat it as close, not exact.
- **Peak and RMS level:** per side, in dBFS.
- **Stereo picture:** left/right balance, correlation (1 is mono, 0 wide, below 0 out of phase) and side-to-mid level.
- **Frequency balance:** how much energy sits below 150 Hz and above 5 kHz, relative to the mid signal.

Each figure averages roughly the last 0.3 s, so allow a second after a change before reading. The device only measures while audio passes through it, and each report carries the name of the track the device is on.

## Build and install

The device is generated from source, so the patch can be reviewed and rebuilt:

```
python build.py              # writes devices/TSP Spatial.amxd
python build.py --install    # also copies it to User Library/TSP MCP/Devices
```

`build.py` needs only Python's standard library. The audio code is in `src/tsp_spatial.genexpr`, a `gen~` codebox. Run the tests with `python -m unittest discover -s tests`.

If you edit the device in Max and save it, your changes are in the `.amxd` only, and the next build overwrites them. Make the change in `build.py` or the `.genexpr` file instead.

## Things worth knowing

- Max crashes, taking Live with it, when a device's `project` block is missing fields. `build.py` writes the full block from Live's own Max Audio Effect template, and a test checks it.
- To test a patch change without risking Live, open the `.amxd` in the standalone Max that ships with Live (`Resources\Max\Max.exe`). It crashes the same way, but only Max closes.
- `live.observer` quotes track names that contain spaces. The MCP server removes the quotes.

Tested with Live 12.4.16 Suite and its bundled Max 9 on Windows 11.
