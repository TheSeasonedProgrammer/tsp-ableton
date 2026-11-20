"""Devices on a track and their parameters."""

from ableton_tools._registry import tool
from ableton_tools import _util as u

PARAMETER = {"type": ["integer", "string"], "description": "Parameter index or name."}


@tool("list_devices", "List the devices on a track.", {"track": u.TRACK}, ("track",))
def list_devices(args):
    track = u.find_track(args["track"])
    return {"track": track.name, "devices": [{
        "index": index,
        "name": device.name,
        "type": device.class_name,
        "drum_rack": device.can_have_drum_pads,
        "rack": device.can_have_chains,
        "parameters": len(list(device.parameters)),
    } for index, device in enumerate(track.devices)]}


@tool("get_device_parameters", "Read a device's parameters with their ranges and current values. "
                               "Third-party plugins only show parameters configured in Live's plugin panel.",
      {"track": u.TRACK, "device": u.DEVICE,
       "filter": {"type": "string", "description": "Only parameters whose name contains this text."}},
      ("track", "device"))
def get_device_parameters(args):
    device = u.find_device(u.find_track(args["track"]), args["device"])
    text = args.get("filter", "").lower()
    return {"device": device.name, "parameters": [
        u.parameter_info(parameter, index)
        for index, parameter in enumerate(device.parameters) if text in parameter.name.lower()
    ]}


@tool("set_device_parameter", "Set a device parameter, with a number within its range or an item label if it has items. "
                              "\"Device On\" (parameter 0) turns the device on or off.",
      {"track": u.TRACK, "device": u.DEVICE, "parameter": PARAMETER,
       "value": {"type": ["number", "string"]}},
      ("track", "device", "parameter", "value"), write=True)
def set_device_parameter(args):
    device = u.find_device(u.find_track(args["track"]), args["device"])
    parameter = u.find_parameter(device, args["parameter"])
    u.set_parameter(parameter, args["value"])
    return u.parameter_info(parameter)


@tool("delete_device", "Remove a device from a track.",
      {"track": u.TRACK, "device": u.DEVICE, "confirm": u.CONFIRM},
      ("track", "device", "confirm"), write=True)
def delete_device(args):
    u.require_confirm(args, "delete_device")
    track = u.find_track(args["track"])
    device = u.find_device(track, args["device"])
    name = device.name
    track.delete_device(list(track.devices).index(device))
    return {"deleted": name}
