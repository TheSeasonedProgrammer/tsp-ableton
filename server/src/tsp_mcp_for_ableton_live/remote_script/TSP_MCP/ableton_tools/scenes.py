"""Scenes."""

from ableton_tools._registry import tool
from ableton_tools import _util as u

SCENE = {"type": "integer", "minimum": 0, "description": "Scene index (0-based)."}


def _find_scene(index):
    scenes = list(u.song().scenes)
    if index < len(scenes):
        return scenes[index]
    raise ValueError("no scene %d; the set has %d" % (index, len(scenes)))


@tool("create_scene", "Create an empty scene.",
      {"index": {"type": "integer", "minimum": -1, "description": "Position; -1 is the end (default)."},
       "name": {"type": "string"}},
      write=True)
def create_scene(args):
    song = u.song()
    index = args.get("index", -1)
    song.create_scene(index)
    position = index if index >= 0 else len(list(song.scenes)) - 1
    if args.get("name"):
        list(song.scenes)[position].name = args["name"]
    return {"index": position, "name": list(song.scenes)[position].name}


@tool("set_scene", "Rename or recolour a scene.",
      {"scene": SCENE, "name": {"type": "string"}, "color": u.COLOR}, ("scene",), write=True)
def set_scene(args):
    scene = _find_scene(args["scene"])
    if "name" in args:
        scene.name = args["name"]
    if "color" in args:
        scene.color = u.color_value(args["color"])
    return {"index": args["scene"], "name": scene.name, "color": u.color_text(scene.color)}


@tool("duplicate_scene", "Duplicate a scene and its clips, right after itself.",
      {"scene": SCENE}, ("scene",), write=True)
def duplicate_scene(args):
    _find_scene(args["scene"])
    u.song().duplicate_scene(args["scene"])
    return {"index": args["scene"] + 1, "name": list(u.song().scenes)[args["scene"] + 1].name}


@tool("delete_scene", "Delete a scene and its clips.",
      {"scene": SCENE, "confirm": u.CONFIRM}, ("scene", "confirm"), write=True)
def delete_scene(args):
    u.require_confirm(args, "delete_scene")
    scene = _find_scene(args["scene"])
    name = scene.name
    u.song().delete_scene(args["scene"])
    return {"deleted": name}


@tool("fire_scene", "Launch a scene.", {"scene": SCENE}, ("scene",), warn=True)
def fire_scene(args):
    _find_scene(args["scene"]).fire()
    return {"fired": args["scene"]}
