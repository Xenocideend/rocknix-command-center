"""user_text_scan - every string the Command Center can show, checked for developer wording.

Builds the real App (no SDL, like the tests), opens each sheet it can, walks every widget
and collects text/subtitle/label, plus every Settings row label and every value each
setting can display. Prints the strings that look like developer wording (task codes such
as CC5/SW1/HF1, daemon numbers, raw key names, "x/y" pairs) so a person can read them.

    python tools/user_text_scan.py [--all]      --all prints every string
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "tests"))

DEV = re.compile(
    r"\b(CC\d+|SW\d+|HF\d+|YT\d+|FX-[A-Z]|R\d{1,2}|I\d|W\d[a-z]?|DS\d|TD\d|AH|RG\d?|"
    r"09\d|batch ?\d|TODO|FIXME|XXX|debug|dry.?run|stub|wip)\b"
    r"|\b\d+/\d+\b(?! ?(fps|FPS))"                  # "85/80" style pairs
    r"|[a-z]+_[a-z_]+"                               # raw_key_names
    r"|\b[a-z]+\.[a-z_]+\.[a-z_]+\b",                # dotted.config.paths
    re.I)
OK_PAIRS = re.compile(r"^\d+ of \d+$")


def walk(w, out, seen):
    if id(w) in seen:
        return
    seen.add(id(w))
    for attr in ("text", "subtitle", "label", "title", "hint_text"):
        v = getattr(w, attr, None)
        if isinstance(v, str) and v.strip():
            out.add((type(w).__name__, getattr(w, "name", None) or "", v))
    for c in list(getattr(w, "children", []) or []):
        walk(c, out, seen)


def collect():
    import unittest
    import config
    import settings_view as sv
    from test_companion import AppCase

    class T(AppCase):
        def runTest(self):
            pass
    t = T()
    t.setUp()
    app = t.make_app()
    out, seen = set(), set()
    # open what can be opened, walking after each
    openers = ["open_settings", "open_power", "open_notes", "open_hud", "open_sleep",
               "open_hotkeys", "open_mixer", "open_clean", "open_lights", "ask_quit_game",
               "ask_swap_screens", "top_screen_ask"]
    walk(app.ui.root, out, seen)
    for name in openers:
        fn = getattr(app, name, None)
        if fn is None:
            continue
        try:
            fn()
            app.post.drain()
        except Exception as e:          # noqa: BLE001 - a scan, not a test
            out.add(("error", name, "could not open: %s" % e))
        seen.clear()
        walk(app.ui.root, out, seen)
        try:
            app.close_sheet()
        except Exception:               # noqa: BLE001
            pass
    # every value every enum/int setting can display
    for f in config.SCHEMA:
        if f["key_path"] not in config.WIRED:
            continue
        out.add(("setting", ".".join(f["key_path"]), f["label"]))
        disp = sv._ENUM_DISPLAY_OVERRIDES.get(f["key_path"], sv._display)
        if f["type"] == "enum":
            for v in f["values"]:
                out.add(("value", ".".join(f["key_path"]), str(disp(v))))
    t.doCleanups()
    return out


def main():
    rows = sorted(collect(), key=lambda r: (r[0], r[1], r[2]))
    show_all = "--all" in sys.argv
    flagged = 0
    for kind, name, text in rows:
        bad = DEV.search(text) and not OK_PAIRS.match(text)
        if bad or show_all:
            flagged += bool(bad)
            print("%s %-10s %-44s %s" % ("!!" if bad else "  ", kind, name[:44], text.replace("\n", " | ")[:110]))
    print("%d strings, %d flagged" % (len(rows), flagged))


if __name__ == "__main__":
    main()
