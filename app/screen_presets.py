"""screen_presets: lays the Command Center out for the device's panel, then scales it.

The UI was drawn for the RP5's 1920x1080 panel with fixed pixel fonts and tiles, so on a
640x480 panel it wouldnt fit. So the UI is laid out on a layout canvas and SDL stretches that
canvas to the real surface (main.App._resize/_render already draw the UI as one texture). The
layout canvas is never smaller than MIN_LAYOUT (1280x720, the smallest size the screens were
checked at), so a small panel gets the whole UI shrunk, not cut off. It keeps the surface's
aspect ratio so nothing gets stretched out of shape.

"auto" uses the surface's own size. A preset stands for a panel resolution ROCKNIX devices
ship with (research/ROCKNIX-DEVICES-AND-RESOLUTIONS.md) and scales as if the panel were that
size, handy when a panel reports the wrong size, or to make the UI bigger (pick a smaller
panel) or smaller (a bigger one).

On the RP5 (1920x1080, "auto") the layout is exactly the surface and nothing changes. Only the
RP5 has been tested, every other device here hasnt.
"""

AUTO = "auto"
MIN_LAYOUT = (1280, 720)

# key -> (label, panel size, example devices). Sizes are the ones confirmed from ROCKNIX's device
# trees or wiki, in the Settings cycle order.
PRESETS = {
    "1920x1080": ("1920x1080", (1920, 1080), "Retroid Pocket 5 (tested), Pocket Flip 2"),
    "1280x720": ("1280x720", (1280, 720), "Powkiddy X55"),
    "1024x768": ("1024x768", (1024, 768), "Anbernic RG DS Plus"),
    "720x720": ("720x720", (720, 720), "Anbernic RG CubeXX"),
    "640x480": ("640x480", (640, 480), "Anbernic RG353, RG ARC, RG DS"),
}
VALUES = (AUTO,) + tuple(PRESETS)            # config.py's enum (kept equal by a test)


def label(key):
    if key == AUTO:
        return "Auto (this screen)"
    return PRESETS[key][0] if key in PRESETS else key


def layout_size(surface_w, surface_h, preset=AUTO):
    """The canvas the UI is laid out on for a surface of surface_w x surface_h."""
    if surface_w <= 0 or surface_h <= 0:
        return surface_w, surface_h
    if preset in PRESETS:
        pw, ph = PRESETS[preset][1]
        if (pw < ph) != (surface_w < surface_h):  # the panel is rotated, match it
            pw, ph = ph, pw
    else:
        pw, ph = surface_w, surface_h
    lo_w, lo_h = MIN_LAYOUT if pw >= ph else MIN_LAYOUT[::-1]
    if surface_w >= lo_w and surface_h < lo_h:
        # A full-width strip shorter than the minimum layout (the BAR's 140 px row and its 24 px
        # auto-hide handle) is laid out 1:1. Forcing the MIN_LAYOUT height would stretch the canvas
        # to e.g. 57600x720, so the strip's controls scaled down to a sliver and it flickered on
        # every reconfigure. A small panel (narrow too) still gets the whole UI shrunk to fit.
        return surface_w, surface_h
    scale = min(pw / float(lo_w), ph / float(lo_h), 1.0)  # 1.0, never lay out bigger
    lh = ph / scale
    lw = lh * surface_w / float(surface_h)                 # the surface's own shape
    return int(round(lw)), int(round(lh))
