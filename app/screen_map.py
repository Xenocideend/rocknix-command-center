"""screen_map: which sway output is which screen on this handheld.

`bottom` is the Command Center's own panel, `top` is where ES and the games live. On the RP5 the
top screen is the Dual Screen add-on, which can be unplugged (undocked) and has no backlight the
handheld can reach. On the Pocket Duo both are built-in panels.

RP5DECK_SCREENS=BOTTOM,TOP (like DSI-2,DSI-1) overrides the profile, for a new device or when a
kernel names its outputs differently.
"""
import os
from collections import namedtuple

import device

Screens = namedtuple("Screens", "bottom top top_is_addon")

RP5 = Screens("DSI-1", "DP-1", True)
# the Duo's names are a guess until the device tree exists: top on the first DSI link, bottom on
# the second. Check with `swaymsg -t get_outputs` on the device.
DUO = Screens("DSI-2", "DSI-1", False)

# matched in order against the device-tree model, the first hit wins
PROFILES = (("Pocket Duo Lite", None), ("Pocket Duo", DUO), ("Retroid Pocket 5", RP5))
DEFAULT = RP5


def for_device(name=None, env=None):
    """The screens for a model name (default: this device's), with RP5DECK_SCREENS on top."""
    env = os.environ if env is None else env
    raw = env.get("RP5DECK_SCREENS", "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) == 2 and all(parts) and parts[0] != parts[1]:
            return Screens(parts[0], parts[1], parts[1].startswith(("DP-", "HDMI-")))
    name = device.device_name() if name is None else name
    for key, screens in PROFILES:
        if key.lower() in name.lower():
            return screens or DEFAULT
    return DEFAULT


CURRENT = for_device()
