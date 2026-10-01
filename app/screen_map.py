"""screen_map: which sway output is which screen on this handheld, and what hardware the device has.

`bottom` is the Command Center's own panel, `top` is where ES and the games live. On the RP5 the
top screen is the Dual Screen add-on, which can be unplugged (undocked) and has no backlight the
handheld can reach. On the handhelds with two built-in panels ROCKNIX keeps ES on the first panel
and only uses the second one for a game's second-screen window, so there is nothing to swap.

A profile also lists the optional hardware features the app can offer (see FEATURES). A tile or
setting that needs a feature the device does not have is not shown.

RP5DECK_SCREENS=BOTTOM,TOP (like DSI-2,DSI-1) overrides the outputs, for a new device or when a
kernel names its outputs differently. RP5DECK_DEVICE=<model name> picks the profile of another model.
"""
import os
import posixpath
import re
from collections import namedtuple

import device

# swap: ES can move between the panels. addon: a detachable top screen with its own power (the Retroid Dual
# Screen add-on). rgb_leds: stick lights. perf_profile: the GPU/CPU mode switch. charge_limit and charge_stuck:
# the RP5's PMIC charger controls.
FEATURES = frozenset(("swap", "addon", "rgb_leds", "perf_profile", "charge_limit", "charge_stuck"))

# Home tile name -> the feature it needs
TILE_NEEDS = {"home.swap": "swap", "home.topscreen": "addon", "home.lights": "rgb_leds", "home.perf": "perf_profile"}

# Settings group -> the feature it needs (a group with no feature is always there)
GROUP_NEEDS = {"lights": "rgb_leds", "battery": "charge_limit", "dualscreen": "addon"}

RP5_BACKLIGHT = "/sys/class/backlight/ae94000.dsi.0"
BACKLIGHT_DIR = "/sys/class/backlight"


class Screens(namedtuple("Screens", "bottom top top_is_addon backlight features verified touch_top touch_bottom "
                                    "audio_card touch_seat backlight_top")):
    """bottom, top: the output names. top_is_addon: the top screen can be unplugged. backlight: the bottom panel's
    sysfs folder, or None to find the first one. features: a set of names from FEATURES. verified: run on
    the real device (False means built from ROCKNIX's source only). touch_top, touch_bottom: sway input
    identifiers of the two touchscreens (None when not known). audio_card: the sound card's name. touch_seat:
    "seat0" to use only the first seat's touch device when sway has two (the RP5 sees every finger twice),
    "any" to use every seat and drop the twin of a finger instead (touch_seats.TwinFilter). backlight_top: the top
    panel's sysfs folder when it has a real backlight (None: dim it in software, as for the add-on)."""
    __slots__ = ()

    def has(self, feature):
        return feature in self.features

    def unsupported_groups(self):
        """Settings groups this device cannot use."""
        return sorted(g for g, f in GROUP_NEEDS.items() if f not in self.features)

    def unsupported_tiles(self):
        """Home tiles this device cannot use."""
        return sorted(t for t, f in TILE_NEEDS.items() if f not in self.features)


def _screens(bottom, top, top_is_addon, backlight=None, features=frozenset(), verified=False, touch_top=None,
             touch_bottom=None, audio_card=None, touch_seat="seat0", backlight_top=None):
    return Screens(bottom, top, top_is_addon, backlight, frozenset(features), verified, touch_top, touch_bottom,
                   audio_card, touch_seat, backlight_top)


RP5 = _screens("DSI-1", "DP-1", True, RP5_BACKLIGHT, FEATURES, True, "8746:1:RetroidPocket_RDS_Touchscreen",
               "0:0:generic_ft5x06_(a0)", "Built-in Audio")
# The handhelds with two built-in panels, from ROCKNIX's 111-sway-init and vertical-check. ES lives on the first
# output sway names `con` (the last connected connector, DSI-2, unless the script sets it: DSI-1 on the Thor Lite and
# the Pocket DS) and the other one, `second_con`, holds a game's second-screen window and ROCKNIX's own bottom-screen UI
# (lowerdeck). That second panel is where the Command Center goes, so it is our "bottom". None of these has been run
# here (verified=False). Their touchscreens sit on seat1 as well as seat0, so a finger may come through either seat:
# touch_seat "any".
THOR = _screens("DSI-1", "DSI-2", False, touch_top="0:0:top_touchscreen", touch_bottom="0:0:bottom_touchscreen",
                touch_seat="any")
THOR_LITE = _screens("DSI-2", "DSI-1", False, touch_top="0:0:top_touchscreen", touch_bottom="0:0:bottom_touchscreen",
                     touch_seat="any")
POCKET_DS = _screens("DSI-2", "DSI-1", False, touch_top="0:0:generic_ft5x06_(44)",
                     touch_bottom="1046:967:Goodix_Capacitive_TouchScreen", touch_seat="any")
# the one Goodix touchscreen ROCKNIX names for these is not tied to a panel there, so the touch names stay unknown
RG_DS = _screens("DSI-1", "DSI-2", False, touch_seat="any")
# a built-in dual-screen device nothing above matches: the Pocket Duo's guess, until its device tree exists (top on the
# first DSI link, bottom on the second; check with `swaymsg -t get_outputs` on the device)
BUILTIN_DUAL = _screens("DSI-2", "DSI-1", False, touch_seat="any")
DUO = BUILTIN_DUAL

# matched in order against the device-tree model, the first hit wins
PROFILES = (("Pocket Duo Lite", None), ("Pocket Duo", DUO), ("Retroid Pocket 5", RP5),
            ("AYN Thor Lite", THOR_LITE), ("AYN Thor", THOR), ("AYANEO Pocket DS", POCKET_DS),
            ("Anbernic RG DS Plus", RG_DS), ("Anbernic RG DS", RG_DS))
DEFAULT = RP5


def for_device(name=None, env=None):
    """The screens for a model name (default: this device's), with RP5DECK_SCREENS on top."""
    env = os.environ if env is None else env
    raw = env.get("RP5DECK_SCREENS", "").strip()
    name = (env.get("RP5DECK_DEVICE", "").strip() or device.device_name()) if name is None else name
    base = DEFAULT
    for key, screens in PROFILES:
        if key.lower() in name.lower():
            base = screens or DEFAULT
            break
    if raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) == 2 and all(parts) and parts[0] != parts[1]:
            addon = parts[1].startswith(("DP-", "HDMI-"))
            return base._replace(bottom=parts[0], top=parts[1], top_is_addon=addon,
                                 features=base.features if addon else base.features - {"swap", "addon"},
                                 verified=False)
    return base


def known_kind(name=None, env=None):
    """"addon" or "builtin" when a profile names this model, None for a model no profile knows (for_device would
    fall back to the RP5's profile for it)."""
    env = os.environ if env is None else env
    name = (env.get("RP5DECK_DEVICE", "").strip() or device.device_name()) if name is None else name
    for key, screens in PROFILES:
        if key.lower() in name.lower():
            return kind(screens) if screens else None
    return None


def find_backlight(listdir=os.listdir, base=BACKLIGHT_DIR):
    """The first backlight folder the kernel lists, or None."""
    try:
        names = sorted(listdir(base))
    except OSError:
        return None
    return posixpath.join(base, names[0]) if names else None


def _dsi_number(output):
    m = re.match(r"^DSI-(\d+)$", output or "")
    return int(m.group(1)) if m else None


def backlight_paths(screens=None, listdir=os.listdir, env=None):
    """(bottom, top) backlight folders. The bottom one is always a path (the profile's, else found, else the RP5's). The
    top one is None unless the device has two built-in panels and a real backlight can be named for it.
    Order of trust: RP5DECK_BACKLIGHTS=bottom,top, the profile, then a guess for two panels and two backlights: DSI-N
    takes the Nth backlight in name order (the first DSI host registers DSI-1). The guess is logged by the app."""
    s = CURRENT if screens is None else screens
    env = os.environ if env is None else env
    raw = [p.strip() for p in env.get("RP5DECK_BACKLIGHTS", "").split(",")]
    if len(raw) == 2 and all(raw):
        return raw[0], raw[1]
    try:
        names = sorted(listdir(BACKLIGHT_DIR))
    except OSError:
        names = []
    paths = [posixpath.join(BACKLIGHT_DIR, n) for n in names]
    bottom, top = s.backlight, s.backlight_top
    if not s.top_is_addon and len(paths) == 2:
        by_dsi = {1: paths[0], 2: paths[1]}
        nb, nt = _dsi_number(s.bottom), _dsi_number(s.top)
        if nb in by_dsi and nt in by_dsi and nb != nt:
            bottom, top = bottom or by_dsi[nb], top or by_dsi[nt]
        else:
            bottom, top = bottom or paths[0], top or paths[1]
    return bottom or (paths[0] if paths else None) or RP5_BACKLIGHT, top


def backlight_path(screens=None, listdir=os.listdir, env=None):
    """The bottom panel's backlight folder, see backlight_paths."""
    return backlight_paths(screens, listdir, env)[0]


def top_backlight_path(screens=None, listdir=os.listdir, env=None):
    """The top panel's backlight folder, or None when it has none we can name."""
    return backlight_paths(screens, listdir, env)[1]


def kind(screens=None):
    """Which layout daemon the device needs: "addon" (the Retroid Dual Screen add-on, dual-screen-layout-and-power) or
    "builtin" (two built-in panels, dual-screen-builtin-layout)."""
    s = CURRENT if screens is None else screens
    return "addon" if s.top_is_addon else "builtin"


def env_lines(screens=None):
    """The device-profile.env text the layout daemon reads (NAME='value' lines), for what the profile knows.
    The add-on daemon takes INTERNAL (the Command Center's panel), EXTERNAL (the top one), the two touch names and the
    sound card. The built-in-panels daemon takes BOTTOM_OUTPUT, TOP_OUTPUT, TOUCH_BOTTOM and TOUCH_TOP."""
    s = CURRENT if screens is None else screens
    if kind(s) == "addon":
        pairs = (("INTERNAL", s.bottom), ("EXTERNAL", s.top), ("TOUCH_EXT", s.touch_top), ("TOUCH_INT", s.touch_bottom),
                 ("AUDIO_CARD_NAME", s.audio_card))
    else:
        pairs = (("BOTTOM_OUTPUT", s.bottom), ("TOP_OUTPUT", s.top), ("TOUCH_BOTTOM", s.touch_bottom),
                 ("TOUCH_TOP", s.touch_top))
    return "".join("%s='%s'\n" % (k, v) for k, v in pairs if v)


CURRENT = for_device()


if __name__ == "__main__":
    import sys
    if "--env" in sys.argv[1:]:
        sys.stdout.write(env_lines())
    elif "--kind" in sys.argv[1:]:
        print(kind())
    elif "--known" in sys.argv[1:]:
        print(known_kind() or "unknown")
    elif "--backlights" in sys.argv[1:]:
        _b, _t = backlight_paths()
        print("bottom backlight: %s" % _b)
        print("top backlight: %s" % (_t or "none (dimmed in software)"))
    else:
        print(CURRENT)
