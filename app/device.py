"""device: the handheld's human name, read from the hardware at runtime.

    device_name()  -> "Retroid Pocket 5" on the RP5
    app_title()    -> "Retroid Pocket 5 Command Center"

Tries /proc/device-tree/model (ARM boards, a NUL-terminated string), then
/sys/class/dmi/id/product_name (x86 handhelds), then "Handheld".

The title only ever gets drawn inside the panel. The Wayland app_id and layer namespace stay
"rp5deck", and no window title carries it either, since ROCKNIX's sway rule
[title=".*(Secondary|Sub|Bottom|Screen 2|GamePad).*"] would grab a window whose title happened
to have one of those words.
"""
MODEL_PATH = "/proc/device-tree/model"
DMI_PATH = "/sys/class/dmi/id/product_name"
FALLBACK = "Handheld"

# placeholder strings firmware vendors leave in DMI, not a name
_DMI_JUNK = {"", "to be filled by o.e.m.", "default string", "system product name",
             "none", "not applicable", "unknown", "o.e.m."}


def _read_name(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(512)
    except OSError:
        return None
    # device-tree strings are NUL-terminated (a list property holds several NUL-separated strings,
    # and the first is the one that names it)
    text = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    name = " ".join(text.split())
    if name.lower() in _DMI_JUNK:
        return None
    return name


def device_name(model_path=MODEL_PATH, dmi_path=DMI_PATH, fallback=FALLBACK):
    for p in (model_path, dmi_path):
        name = _read_name(p)
        if name:
            return name
    return fallback


def app_title(name=None):
    return "%s Command Center" % (name or device_name())


if __name__ == "__main__":
    print(device_name())
    print(app_title())
