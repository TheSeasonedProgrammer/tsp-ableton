"""Live's browser: finding and loading instruments, effects, kits and samples."""

import collections
import time

from ableton_tools._registry import tool
from ableton_tools import _util as u

CATEGORIES = ("instruments", "drums", "sounds", "audio_effects", "midi_effects", "max_for_live", "plugins",
              "clips", "samples", "packs", "user_library", "current_project")
PATH = {"type": "array", "items": {"type": "string"},
        "description": "Category then folder and item names, as returned by browse or search_browser, such as [\"drums\", \"Kits\", \"808 Core Kit.adg\"]."}
# The browser is walked on Live's main thread, so a search must stay short.
SEARCH_SECONDS = 2.0
SEARCH_ITEMS = 50000


def _category(name):
    if name not in CATEGORIES:
        raise ValueError("no browser category '%s'; use one of %s" % (name, ", ".join(CATEGORIES)))
    return getattr(u.application().browser, name)


def _item_at(path):
    if not path:
        raise ValueError("path is empty")
    item = _category(path[0])
    for name in path[1:]:
        children = list(item.children)
        match = next((child for child in children if child.name.lower() == name.lower()), None)
        if match is None:
            raise ValueError("'%s' has no item '%s'" % (item.name, name))
        item = match
    return item


def _item_info(item, path):
    return {"name": item.name, "folder": item.is_folder, "loadable": item.is_loadable, "path": path}


@tool("browse", "List one level of Live's browser. With no path, lists the categories.",
      {"path": PATH})
def browse(args):
    path = args.get("path") or []
    if not path:
        return {"categories": list(CATEGORIES)}
    return {"items": [_item_info(child, path + [child.name]) for child in _item_at(path).children]}


@tool("search_browser", "Find browser items by name, nearest the top of the category first. Every word must appear in the name. "
                        "Stops after 2 seconds and says so in truncated; narrow the search with path if it does.",
      {"query": {"type": "string", "minLength": 1},
       "category": {"type": "string", "enum": list(CATEGORIES), "description": "Where to search, such as drums for kits."},
       "path": PATH,
       "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Default 30."}},
      ("query",))
def search_browser(args):
    if "path" in args:
        start = args["path"]
    elif "category" in args:
        start = [args["category"]]
    else:
        raise ValueError("give category or path")
    words = args["query"].lower().split()
    limit = args.get("limit", 30)
    deadline = time.monotonic() + SEARCH_SECONDS
    pending = collections.deque([(_item_at(start), start)])
    visited = 0
    results = []
    while pending and len(results) < limit:
        if visited >= SEARCH_ITEMS or time.monotonic() > deadline:
            break
        item, path = pending.popleft()
        for child in item.children:
            visited += 1
            child_path = path + [child.name]
            name = child.name.lower()
            if child.is_loadable and all(word in name for word in words):
                results.append(_item_info(child, child_path))
                if len(results) >= limit:
                    break
            if child.is_folder:
                pending.append((child, child_path))
    return {"results": results, "visited": visited, "truncated": bool(pending) and len(results) < limit}


@tool("load_from_browser", "Load a browser item onto a track: an instrument, effect, Drum Rack kit, plugin or sample. "
                           "Loading an instrument replaces the track's instrument. This selects the track.",
      {"track": u.TRACK, "path": PATH}, ("track", "path"), write=True)
def load_from_browser(args):
    track = u.find_track(args["track"])
    item = _item_at(args["path"])
    if not item.is_loadable:
        raise ValueError("'%s' can't be loaded; browse inside it" % item.name)
    u.song().view.selected_track = track
    u.application().browser.load_item(item)
    return {"loaded": item.name, "track": track.name, "devices": [device.name for device in track.devices]}
