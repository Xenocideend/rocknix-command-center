#!/usr/bin/env python3
"""CC4's system-carousel background colour, against the REAL merged
companion.py / config.py (I2, 24 Sep: patches/CC4-*.patch are applied; this
replaces tests/test_cc4_patches.py, which applied them to a temp copy and
ran the same checks in a subprocess). Coexistence with CC23 is now simply
"both are in the file": the CC23 keys are checked next to CC4's.

The behavioural part drives a real CompanionController through
system-selected / game-start events for the "theme" and "sample" sources,
including the 400 ms settle debounce and the never-during-a-game gate."""
import os
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import companion  # noqa: E402
import config  # noqa: E402
import esevents  # noqa: E402
import settings_view  # noqa: E402
import theme_colour  # noqa: E402
import ui  # noqa: E402

KEY = ("companion", "system_bg_source")
THEME_BG = ((0.2, 0.3, 0.4, 1.0), (0.9, 0.9, 0.9, 1.0))


class FakeTimers:
    def __init__(self):
        self.now = 0.0
        self.items = []

    def call_later(self, delay, fn):
        h = [self.now + delay, fn, False]
        self.items.append(h)
        return h

    @staticmethod
    def cancel(h):
        if h is not None:
            h[2] = True

    def advance(self, dt):
        self.now += dt
        due = [h for h in self.items if not h[2] and h[0] <= self.now]
        self.items = [h for h in self.items if h not in due]
        for h in due:
            h[1]()


def sync_submit(fn, *args, done=None):
    res = fn(*args)
    if done:
        done(res)


class FakeVideo:
    def play(self, *a, **k):
        pass

    def stop(self):
        pass


class Handlers:
    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeResolver:
    def resolve(self, t):
        return {"kind": t.kind, "system": t.system, "title": t.system, "media": {},
                "manual": None, "logo": "/theme/logos/gba.png", "running": t.running}

    def system_bg_color_theme(self, system, logo_path):
        return THEME_BG


def make_png():
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    w = h = 8
    raw = bytearray()
    for _ in range(h):
        raw.append(0)
        for _ in range(w):
            raw += bytes([10, 20, 220, 255])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (theme_colour._PNG_SIG + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def sel(system, seq):
    return esevents.EsEvent(esevents.SYSTEM_SELECTED, system, "", "", (), "hook", 0.0, None, seq)


class TestConfig(unittest.TestCase):
    def test_enum_default_wired_and_a_control_with_labels(self):
        # "sample" since test day: PiStation-X's default colorset colour is a
        # tint (ffffff) over a dark image; the DP-1 sample matches what ES shows
        self.assertEqual(config.get_value(config.defaults(), KEY), "sample")
        self.assertIn(KEY, config.WIRED)
        self.assertEqual(sorted(config.field_for(KEY)["values"]), ["off", "sample", "theme"])
        sheet = settings_view.SettingsSheet(config.defaults(), on_change=lambda *a: None,
                                            on_close=lambda: None)
        self.assertEqual(sorted(".".join(k) for k in sheet.controls),
                         sorted(".".join(k) for k in config.WIRED))
        c = sheet.controls[KEY]
        for v in config.field_for(KEY)["values"]:
            c.set_value(v)
            self.assertTrue(isinstance(c.text, str) and c.text, v)

    def test_cc23_keys_survive_next_to_cc4(self):
        self.assertEqual(config.get_value(config.defaults(), ("companion", "in_game_display")),
                         "art")
        self.assertIn("cartridge", config.MEDIA_VALUES)

    def test_companion_source_no_longer_hardcodes_black(self):
        with open(os.path.join(APP, "companion.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("g.fill_rect(self.rect, self.bg_color)", text)
        self.assertNotIn("g.fill_rect(self.rect, BLACK)", text)


class TestControllerBehaviour(unittest.TestCase):
    def make(self, source):
        cfg = config.defaults()
        cfg.setdefault("companion", {})["system_bg_source"] = source
        view = companion.CompanionView(Handlers())
        root = ui.Root(400, 300)
        root.add(view)
        view.layout((0, 0, 400, 300))
        timers = FakeTimers()
        c = companion.CompanionController(view, sync_submit, timers, FakeVideo(), lambda: cfg,
                                          resolver=FakeResolver(), clock=lambda: timers.now)
        if source == "sample":
            c.bg_sample = theme_colour.SampleCache(capture=make_png)
        return c, view, timers

    def test_theme_source_resolves_inline(self):
        c, view, _ = self.make("theme")
        c.on_es_event(sel("gba", 1))
        self.assertEqual(tuple(view.bg_color), THEME_BG[0])
        self.assertEqual(tuple(view.fg_color), THEME_BG[1])
        self.assertIsNone(c.bg_timer)

    def test_sample_source_waits_for_the_settle_then_captures_once(self):
        c, view, timers = self.make("sample")
        before = list(view.bg_color)
        c.on_es_event(sel("gba", 1))
        self.assertEqual(list(view.bg_color), before)
        self.assertEqual(c.bg_sample.captures, 0)
        timers.advance(0.39)
        self.assertEqual(list(view.bg_color), before)
        timers.advance(0.02)
        self.assertEqual(c.bg_sample.captures, 1)
        self.assertAlmostEqual(view.bg_color[2], 220 / 255.0, places=2)

    def test_rapid_reselect_restarts_the_debounce(self):
        c, _, timers = self.make("sample")
        c.on_es_event(sel("gba", 1))
        timers.advance(0.2)
        c.on_es_event(sel("snes", 2))
        timers.advance(0.2)
        self.assertEqual(c.bg_sample.captures, 0)
        timers.advance(0.2)
        self.assertEqual(c.bg_sample.captures, 1)

    def test_game_start_cancels_a_pending_sample(self):
        c, _, timers = self.make("sample")
        c.on_es_event(sel("gba", 1))
        c.on_es_event(esevents.EsEvent(esevents.GAME_START, "gba", "/roms/gba/g.gba", "G", (),
                                       "hook", 0.0, None, 2))
        timers.advance(1.0)
        self.assertEqual(c.bg_sample.captures, 0)


if __name__ == "__main__":
    unittest.main()
