# TSP MCP for Ableton Live

MCP server for Ableton Live 12, split in two so the MCP side can use the official SDK while the Live side stays a plain Remote Script. This project is independent and isn't affiliated with or endorsed by Ableton or Arturia. All product names are trademarks of their owners.

```
Claude Code ──stdio──> MCP server  (Python 3.12+, official mcp SDK, run by uv)
                         │  JSON lines over TCP 127.0.0.1:8768
                         ▼
                       TSP_MCP Remote Script  (inside Live, polled on the main thread)
```

The Remote Script ships inside the Python package, at `server/src/tsp_mcp_for_ableton_live/remote_script/TSP_MCP/`. Its 44 tools live in `ableton_tools/` there:

| Area | Tools |
|---|---|
| Session | `get_session`, `set_session`, `play`, `stop`, `undo`, `redo` |
| Tracks | `get_track`, `create_track`, `duplicate_track`, `delete_track`, `set_track` |
| Scenes | `create_scene`, `set_scene`, `duplicate_scene`, `delete_scene`, `fire_scene` |
| Clips | `list_clips`, `get_clip`, `create_clip`, `set_clip`, `delete_clip`, `duplicate_clip`, `fire_clip`, `stop_clips` |
| Notes | `get_notes`, `add_notes`, `edit_notes`, `clear_notes`, `quantize_clip` |
| Drums | `get_drum_kit`, `write_drum_pattern` |
| Devices | `list_devices`, `get_device_parameters`, `set_device_parameter`, `delete_device` |
| Browser | `browse`, `search_browser`, `load_from_browser` |
| Pigments | `list_pigments_presets`, `load_pigments_preset` |
| Arrangement | `list_arrangement_clips`, `copy_clip_to_arrangement`, `set_clip_automation`, `clear_clip_automation` |

The MCP server adds `ableton_bridge_status` and `ableton_reload_tools`.

Tested with Live 12.4.16 Suite on Windows 11, mcp SDK 2.3, uv 0.12. Opening a different Set makes Live restart the script; that restart has only been tested outside Live.

## Setup

You need [uv](https://docs.astral.sh/uv/getting-started/installation/).

1. **Install it.** This copies the Remote Script into your User Library, which it finds from Live's preferences, and registers the server with Claude Code as `tsp-ableton`:

   ```
   uvx tsp-mcp-for-ableton-live install
   ```

   Without Claude Code, it prints the config to add to your MCP client. `--user-library <folder>` skips the detection and `--no-register` skips the registration.

2. **Enable it in Live.** Restart Live. Under Settings > Link, Tempo & MIDI, pick **TSP MCP** in an empty Control Surface slot and leave Input and Output on None.

3. **Check it.** Start a new Claude Code session and ask it to run `ableton_bridge_status`.

To update, run `uvx tsp-mcp-for-ableton-live@latest install` and restart Live. To remove it, run `uvx tsp-mcp-for-ableton-live uninstall`.

### From a checkout

```
uv run --directory server tsp-mcp-for-ableton-live install --dev
```

`--dev` loads `ableton_tools` from the checkout, so tool edits only need `ableton_reload_tools`, and enables the "inspect" method. Changes to `TSP_MCP/__init__.py` always need a reinstall and a Live restart.

## Conventions

- Time is in beats, meaning quarter notes, whatever the time signature.
- Tracks are an index or a name. Names also find return tracks, and `"master"` is the master track. Live prefixes return tracks with a letter, so a return created as `Verb` is called `C-Verb`.
- Slots and scenes are 0-based. Live calls middle C (60) C3, so a Drum Rack's first pad (36) is C1.
- Each tool that changes the Set is one undo step, however many edits it makes.
- Destructive tools (`delete_track`, `delete_scene`, `delete_clip`, `delete_device`) refuse unless called with `confirm: true`.
- Arguments are validated against each tool's JSON Schema in the MCP server before anything reaches Live.

## Drum beats

`write_drum_pattern` takes one step string per drum and repeats each to fill the clip:

```json
{"track": "Drums", "slot": 0, "bars": 2, "swing": 56, "pattern": {
  "kick":       "x...|..x.|x...|....",
  "snare":      "....|x...|....|x..o",
  "closed_hat": "x.x.|x.x.|x.x.|x.X."
}}
```

`x` is a hit, `X` an accent, `o` a ghost note and `1`-`9` an explicit level. `.`, `-` and `_` are rests. Spaces and `|` are ignored.

Drums resolve against the pad names of the track's Drum Rack, including one nested inside another rack, so `kick` finds `Bass Drum` or `Kick 808` wherever it sits. If the rack has no pad for a drum, the call fails and lists the pads, because writing the General MIDI pitch would land on a silent or wrong pad. General MIDI is only used when the track has no Drum Rack, as with third-party drum plugins, and the reply reports `mapping: general_midi`. Check that mapping against the plugin.

## Pigments presets

`list_pigments_presets` searches Arturia Pigments' factory and user presets, and `load_pigments_preset` loads one onto a track:

```json
{"track": "Lead", "preset": "Bells Of Varanasi"}
```

Pigments only reports `Default` to Live's API, so the tool wraps the preset file in a `.vstpreset`, saves it under `TSP MCP/Pigments` in your User Library and loads it from there. Live's browser takes a few seconds to find a new file. Until it does, the call returns `loaded: false`, so call it again. Later loads of that preset are immediate.

Each load is a fresh Pigments instance. It replaces the track's instrument and any parameters configured in Live's plugin panel, and keeps the clips. It relies on Arturia's preset file format, which is undocumented, and has been tested with Pigments 6.0.4 and 7.0.1 on Windows.

## Limits of Live's API

- **No audio.** Nothing here can hear the result, render or export audio.
- **No saving.** The API can't save the Set, so save it in Live.
- **Grooves can be applied, not removed.** `Clip.groove` rejects `None` and `has_groove` is read-only, so removing one needs Clip View.
- **Third-party plugins** only show parameters configured in Live's plugin panel.
- **Device tools see top-level devices.** Devices inside racks and pads aren't reachable yet, apart from pad names.
- **`notes` and automation tools work on Session View clips.** To use them in the arrangement, edit the Session clip and copy it across.

## Live behaviour worth knowing

Found while testing against Live 12.4.7 and 12.4.16:

- Python threads barely run inside Live. A socket thread took 200-430 ms per call even for requests that never touched the Live API, so the bridge has no threads and `update_display` polls non-blocking sockets. That also makes a call's speed depend on Live's display tick.
- While a modal dialog is open Live stops calling `update_display`, so calls wait. The MCP server reports a timeout after 60 s, and the call can still run once the dialog closes.
- `load_item` loads onto the selected track, so `load_from_browser` selects the target track first.
- Colours snap to Live's palette: `#00aa44` came back as `#0a9c8e`. The reply has the real value.
- `stop` leaves the arrangement position where it stopped.
- Live ignores `current_song_time` while stopped, so `play` with `from_beat` sets the start marker instead when the transport isn't running.
- After Session clips play, tracks ignore the arrangement until Back to Arrangement is pressed. `play` with `arrangement: true` does that.
- A Session clip's mixer automation carries over with `copy_clip_to_arrangement`, then holds its last value. End each envelope on the level the track should stay at, or the next section stays at the envelope's last value.

## Tests

```
uv run --no-project --python 3.11 python -m unittest discover -s tests
```

`tests/test_bridge.py` runs the Remote Script against `tests/fake_live.py` over a real socket, on Live's Python version, and drives the real MCP server over stdio. The fake proves the wiring, not how Live behaves, so check tool changes in Live too:

```
uv run --no-project --python 3.11 python tests/live_check.py
```

That needs a running Live with the script enabled. It adds a temporary track and removes it afterwards, so use a Set you don't mind changing.
