#!/usr/bin/env python3
"""Widget hit-testing, slider value maths, touch routing, damage, layout,
the volume throttle, and the dry-run audio switch. Offline, stdlib only
(ui.py, screens.py and audioctl.py import nothing native)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio  # noqa: E402
import audioctl  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402


class TestHitTesting(unittest.TestCase):
    def setUp(self):
        self.root = ui.Root(1920, 1080)
        self.a = self.root.add(ui.Button("A", rect=(100, 100, 200, 150), name="a"))
        self.b = self.root.add(ui.Button("B", rect=(250, 100, 200, 150), name="b"))  # overlaps a
        self.label = self.root.add(ui.Label("x", rect=(600, 100, 200, 150), name="label"))

    def test_inside_and_outside(self):
        self.assertIs(self.root.hit(150, 150), self.a)
        self.assertIsNone(self.root.hit(99, 150))
        self.assertIsNone(self.root.hit(150, 250))      # y + h is exclusive

    def test_edges(self):
        self.assertIs(self.root.hit(100, 100), self.a)   # top-left inclusive
        self.assertIs(self.root.hit(249, 249), self.a)
        self.assertIsNone(self.root.hit(100, 250))

    def test_topmost_wins_on_overlap(self):
        self.assertIs(self.root.hit(275, 150), self.b)

    def test_labels_are_not_targets(self):
        self.assertIsNone(self.root.hit(650, 150))

    def test_hidden_and_disabled_do_not_hit(self):
        self.b.set_visible(False)
        self.assertIs(self.root.hit(275, 150), self.a)
        self.a.set_enabled(False)
        self.assertIsNone(self.root.hit(275, 150))

    def test_hidden_container_hides_children(self):
        c = self.root.add(ui.Container(rect=(0, 500, 500, 500)))
        btn = c.add(ui.Button("C", rect=(10, 510, 150, 150)))
        self.assertIs(self.root.hit(20, 520), btn)
        c.set_visible(False)
        self.assertIsNone(self.root.hit(20, 520))

    def test_hit_pad_grows_target(self):
        self.a.hit_pad = 20
        self.assertIs(self.root.hit(85, 150), self.a)
        self.assertIsNone(self.root.hit(79, 150))


class TestSliderMath(unittest.TestCase):
    def setUp(self):
        self.changes, self.releases = [], []
        # rect x 100..1100, knob_r 50 -> track 150..1050 (900 px)
        self.s = ui.Slider(rect=(100, 0, 1000, 120), knob_r=50, step=0.01,
                           on_change=self.changes.append, on_release=self.releases.append)

    def test_track_and_value_at(self):
        self.assertEqual(self.s.track(), (150, 1050))
        self.assertEqual(self.s.value_at(150), 0.0)
        self.assertEqual(self.s.value_at(1050), 1.0)
        self.assertEqual(self.s.value_at(600), 0.5)
        self.assertEqual(self.s.value_at(150 + 900 * 0.37), 0.37)

    def test_clamping(self):
        self.assertEqual(self.s.value_at(-500), 0.0)
        self.assertEqual(self.s.value_at(100), 0.0)      # inside rect, left of track
        self.assertEqual(self.s.value_at(5000), 1.0)
        self.assertEqual(self.s.value_at(1099), 1.0)

    def test_clamping_on_a_continuous_slider(self):
        # step=0: no rounding stage, so the clamp in value_at is the only one
        s = ui.Slider(rect=(100, 0, 1000, 120), knob_r=50, step=0)
        self.assertEqual(s.value_at(-500), 0.0)
        self.assertEqual(s.value_at(5000), 1.0)
        self.assertAlmostEqual(s.value_at(150 + 900 * 0.123), 0.123, places=6)

    def test_step_rounding(self):
        self.assertEqual(self.s.value_at(150 + 900 * 0.374), 0.37)
        self.assertEqual(self.s.value_at(150 + 900 * 0.376), 0.38)

    def test_x_for_inverts_value_at(self):
        for v in (0.0, 0.05, 0.42, 0.5, 0.99, 1.0):
            self.assertEqual(self.s.value_at(self.s.x_for(v)), v)

    def test_drag_reports_changes_and_one_release(self):
        s = self.s
        s.set_value(0.30)
        s.on_press("p", s.x_for(0.30), 60)          # touch-down never changes the value
        for v in (0.35, 0.40, 0.40, 0.50, 0.60):
            s.on_move("p", s.x_for(v), 60)
        s.on_release("p", s.x_for(0.60), 60)
        self.assertEqual(self.changes, [0.35, 0.40, 0.50, 0.60])   # no duplicate 0.40
        self.assertEqual(self.releases, [0.60])
        self.assertFalse(s.dragging)

    def test_drag_beyond_the_ends_clamps(self):
        s = self.s
        s.on_press("p", s.x_for(0.9), 60)
        s.on_move("p", 3000, 60)
        s.on_release("p", 3000, 60)
        self.assertEqual(self.releases, [1.0])
        s.on_press("p", 500, 60)
        s.on_move("p", -3000, 60)
        s.on_release("p", -3000, 60)
        self.assertEqual(self.releases, [1.0, 0.0])

    def test_release_without_change_commits_nothing(self):
        # (was: release fires even without change) A press that changed
        # nothing must not re-commit a value the hardware keys may have
        # changed meanwhile (RV1-M1: only a release that changed it commits).
        self.s.set_value(0.5)
        self.s.on_press("p", self.s.x_for(0.5), 60)
        self.s.on_release("p", self.s.x_for(0.5), 60)
        self.assertEqual(self.changes, [])
        self.assertEqual(self.releases, [])

    def test_cancel_restores_and_commits_nothing(self):
        # (was: cancel commits the last value) RV1-M1: a cancel is not a release.
        self.s.set_value(0.2)
        self.s.on_press("p", self.s.x_for(0.2), 60)
        self.s.on_move("p", self.s.x_for(0.3), 60)
        self.assertEqual(self.s.value, 0.3)
        self.s.on_cancel("p")
        self.s.on_cancel("p")
        self.assertEqual(self.releases, [])
        self.assertEqual(self.s.value, 0.2)

    def test_other_pointer_is_ignored(self):
        self.s.set_value(0.2)
        self.s.on_press("p", self.s.x_for(0.2), 60)
        self.s.on_press("q", self.s.x_for(0.9), 60)
        self.s.on_move("q", self.s.x_for(0.8), 60)
        self.s.on_release("q", self.s.x_for(0.8), 60)
        self.assertEqual(self.releases, [])
        self.assertEqual(self.s.value, 0.2)

    def test_external_value_ignored_while_dragging(self):
        self.s.set_value(0.2)
        self.s.on_press("p", self.s.x_for(0.2), 60)
        self.assertFalse(self.s.set_value(0.9))
        self.assertEqual(self.s.value, 0.2)
        self.s.on_release("p", self.s.x_for(0.2), 60)
        self.assertTrue(self.s.set_value(0.9))
        self.assertEqual(self.s.value, 0.9)
        self.assertTrue(self.s.set_value(7))
        self.assertEqual(self.s.value, 1.0)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.root = ui.Root(1920, 1080)
        self.clicks = []
        self.btn = self.root.add(ui.Button("B", rect=(0, 0, 200, 200),
                                           on_click=lambda: self.clicks.append(1)))
        self.rel = []
        self.sl = self.root.add(ui.Slider(rect=(300, 0, 1000, 120), knob_r=50,
                                          on_release=self.rel.append))
        self.r = ui.TouchRouter(self.root)

    def test_tap_clicks(self):
        self.r.down(("f", 1), 50, 50)
        self.assertTrue(self.btn.pressed)            # press feedback on touch-down
        self.r.up(("f", 1), 50, 50)
        self.assertEqual(self.clicks, [1])
        self.assertFalse(self.btn.pressed)

    def test_release_outside_does_not_click(self):
        self.r.down(("f", 1), 50, 50)
        self.r.move(("f", 1), 250, 50)
        self.assertFalse(self.btn.pressed)
        self.r.up(("f", 1), 250, 50)
        self.assertEqual(self.clicks, [])

    def test_capture_keeps_slider_after_leaving_it(self):
        self.r.down(("m", 0), 800, 60)
        self.r.move(("m", 0), 900, 60)               # sideways: the drag engages
        self.r.move(("m", 0), 5000, 900)             # far off the slider
        self.r.up(("m", 0), 5000, 900)
        self.assertEqual(self.rel, [1.0])
        self.assertEqual(self.clicks, [])

    def test_two_fingers_independent(self):
        self.r.down(("f", 1), 50, 50)
        self.r.down(("f", 2), 800, 60)
        self.r.up(("f", 2), 800, 60)
        self.r.up(("f", 1), 50, 50)
        self.assertEqual(self.clicks, [1])
        self.assertEqual(len(self.rel), 1)

    def test_cancel_all_commits_nothing(self):
        # (was: cancel_all releases the slider once) RV1-M1
        self.r.down(("f", 3), 800, 60)
        self.r.move(("f", 3), 900, 60)
        self.assertGreater(self.sl.value, 0.0)
        self.r.cancel_all()
        self.r.cancel_all()
        self.assertEqual(self.rel, [])
        self.assertEqual(self.sl.value, 0.0)
        self.assertEqual(self.r.captures, {})


class TestSwipes(unittest.TestCase):
    def setUp(self):
        self.root = ui.Root(1920, 1080)
        self.clicks, self.rel, self.got = [], [], []
        self.btn = self.root.add(ui.Button("B", rect=(0, 0, 300, 200),
                                           on_click=lambda: self.clicks.append(1)))
        self.sl = self.root.add(ui.Slider(rect=(400, 900, 1000, 120), knob_r=50,
                                          on_release=self.rel.append))
        self.g = ui.SwipeRecognizer(lambda: 1080, edge=60, distance=120,
                                    wants=lambda n: n in ("swipe_down_from_top", "swipe_up"),
                                    on_gesture=self.got.append)
        self.r = ui.TouchRouter(self.root, gestures=self.g)

    def test_swipe_down_from_top_edge_is_claimed(self):
        self.r.down(("f", 1), 100, 20)            # on the button, inside the top edge
        self.r.move(("f", 1), 105, 80)
        self.assertEqual(self.got, [])
        self.r.move(("f", 1), 110, 200)
        self.assertEqual(self.got, ["swipe_down_from_top"])
        self.assertFalse(self.btn.pressed)         # the button was cancelled
        self.r.move(("f", 1), 110, 400)
        self.r.up(("f", 1), 110, 400)
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.got, ["swipe_down_from_top"])   # reported once

    def test_tap_is_still_a_tap(self):
        self.r.down(("f", 1), 100, 20)
        self.r.move(("f", 1), 104, 30)
        self.r.up(("f", 1), 104, 30)
        self.assertEqual(self.clicks, [1])
        self.assertEqual(self.got, [])

    def test_swipe_down_mid_screen_not_wanted_keeps_widget(self):
        self.r.down(("f", 1), 100, 100)            # below the edge zone
        self.r.move(("f", 1), 100, 190)
        self.r.move(("f", 1), 100, 199)
        self.assertEqual(self.got, [])
        self.assertIn(("f", 1), self.r.captures)

    def test_horizontal_slider_drag_is_never_a_swipe(self):
        self.r.down(("f", 2), 500, 960)
        self.r.move(("f", 2), 800, 965)            # sideways first
        self.r.move(("f", 2), 820, 700)            # then a big vertical wobble
        self.r.up(("f", 2), 820, 700)
        self.assertEqual(self.got, [])
        self.assertEqual(len(self.rel), 1)

    def test_swipe_up_cancels_slider_and_commits_nothing(self):
        # (was: ...and commits once) RV1-M1
        self.r.down(("f", 3), 900, 960)
        self.r.move(("f", 3), 905, 800)
        self.assertEqual(self.got, ["swipe_up"])
        self.assertEqual(self.rel, [])              # slider got on_cancel: no release
        self.r.up(("f", 3), 905, 700)
        self.assertEqual(self.rel, [])
        self.assertEqual(self.sl.value, 0.0)

    def test_classify_names(self):
        g = ui.SwipeRecognizer(lambda: 1080)
        self.assertEqual(g.classify(0, 10, 0, 300), "swipe_down_from_top")
        self.assertEqual(g.classify(0, 500, 0, 800), "swipe_down")
        self.assertEqual(g.classify(0, 1070, 0, 800), "swipe_up_from_bottom")
        self.assertEqual(g.classify(0, 500, 0, 300), "swipe_up")
        self.assertFalse(g.classify(0, 500, 300, 520))
        self.assertIsNone(g.classify(0, 500, 10, 560))


class TestCommandCenterSeam(unittest.TestCase):
    def test_command_center_can_be_hidden_as_one_view(self):
        class H:
            def __getattr__(self, name):
                return lambda *a, **k: None
        d = screens.DeckUI(H(), 1920, 1080, "T")
        self.assertIsInstance(d.cc, screens.CommandCenter)
        self.assertIn("bar.slider", d.targets())
        d.show_command_center(False)
        self.assertEqual(d.targets(), {})
        self.assertIsNone(d.root.hit(960, 1010))     # the strip is part of it
        self.assertIsNone(d.root.hit(300, 300))
        d.show_command_center(True)
        self.assertIn("home.mixer", d.targets())     # batch 1: HUD is a tab, not a tile


class TestNavigationV2Views(unittest.TestCase):
    """I1: DeckUI with a companion view under the Command Center."""

    def make(self):
        class H:
            def __getattr__(self, name):
                return lambda *a, **k: None
        comp = ui.Container(name="companion")
        comp.add(ui.Button("tab", name="companion.tab"))
        comp.layout = lambda rect: (comp.set_rect(rect), comp.children[0].set_rect(
            (rect[0] + 700, rect[1], 480, 120)))
        return screens.DeckUI(H(), 1920, 1080, "T", companion=comp), comp

    def test_views_cover_the_panel_and_exclude_each_other(self):
        d, comp = self.make()
        self.assertEqual(d.showing, "cc")                 # without set_view: B1's shape
        d.set_view("companion")
        self.assertEqual(d.showing, "companion")
        self.assertEqual(comp.rect, (0, 0, 1920, 1080))
        self.assertEqual(set(d.targets()), {"companion.tab"})
        d.set_view("cc")
        self.assertNotIn("companion.tab", d.targets())
        self.assertIn("bar.slider", d.targets())
        self.assertEqual(d.cc.rect, (0, 0, 1920, 1080))

    def test_bar_mode_is_always_the_strip(self):
        d, comp = self.make()
        d.set_view("companion")
        d.set_size(1920, screens.BAR_H)
        self.assertEqual(d.showing, "bar")
        self.assertFalse(comp.visible)
        self.assertIn("bar.slider", d.targets())
        d.set_size(1920, 1080)
        self.assertEqual(d.showing, "companion")

    def test_mixer_pages_five_rows_at_a_time(self):
        d, comp = self.make()
        d.open("mixer")
        d.mixer.set_streams([{"id": i, "display_name": str(i), "volume": 0.5, "muted": False}
                             for i in range(12)])
        self.assertEqual(sorted(d.mixer.rows), [0, 1, 2, 3, 4])
        d.mixer.turn(1)
        d.mixer.turn(1)
        self.assertEqual(sorted(d.mixer.rows), [10, 11])
        d.mixer.turn(1)                                   # clamped at the last page
        self.assertEqual(sorted(d.mixer.rows), [10, 11])
        self.assertFalse(d.mixer.next.enabled)


class TestDamageAndLayout(unittest.TestCase):
    def test_idle_means_no_damage(self):
        root = ui.Root(1920, 1080)
        lbl = root.add(ui.Label("a", rect=(10, 10, 100, 50)))
        root.take_damage()
        self.assertIsNone(root.take_damage())
        lbl.set_text("a")                              # unchanged: no redraw
        self.assertIsNone(root.take_damage())
        lbl.set_text("b")
        d = root.take_damage()
        self.assertIsNotNone(d)
        self.assertTrue(d[0] <= 10 and d[1] <= 10 and d[0] + d[2] >= 110 and d[1] + d[3] >= 60)
        self.assertLess(d[2] * d[3], 1920 * 1080 // 10)   # a partial redraw

    def test_invisible_widget_changes_cause_no_damage(self):
        root = ui.Root(1920, 1080)
        c = root.add(ui.Container(rect=(0, 0, 500, 500)))
        lbl = c.add(ui.Label("a", rect=(10, 10, 100, 50)))
        c.set_visible(False)
        root.take_damage()
        lbl.set_text("zzz")
        self.assertIsNone(root.take_damage())

    def test_hsplit_and_grid(self):
        r = ui.hsplit((10, 20, 1000, 100), [100, None, 200, None], gap=10)
        self.assertEqual(r[0], (10, 20, 100, 100))
        self.assertEqual(r[1][2], r[3][2])               # flex cells share equally
        self.assertEqual(r[3][0] + r[3][2], 10 + 1000)    # fills to the edge
        cells = ui.grid((0, 0, 1000, 600), 4, 2, 20)
        self.assertEqual(len(cells), 8)
        self.assertEqual(cells[4][1], cells[0][1] + cells[0][3] + 20)

    def test_deck_ui_touch_targets_are_big_enough(self):
        class H:
            def __getattr__(self, name):
                return lambda *a, **k: None
        d = screens.DeckUI(H(), 1920, 1080, "Retroid Pocket 5 Command Center")
        self.assertEqual(d.home.title.text, "Retroid Pocket 5 Command Center")
        tx, ty, tw, th = d.home.title.rect
        sx = d.home.sub.rect[0]
        self.assertLessEqual(tx + tw, sx)               # title and sink label do not overlap
        self.assertGreaterEqual(tw, 31 * 30)             # room for the full title at 52 px
        t = d.targets()
        for name in ("bar.mute", "bar.slider", "bar.battery", "home.mixer", "home.hotkeys",
                     "home.lights", "home.topscreen", "home.perf"):
            self.assertIn(name, t)
            x, y, w, h = t[name]
            self.assertGreaterEqual(min(w, h), 110, name)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y + h, 1080)
        bx, by, bw, bh = t["bar.slider"]
        # Navigation v2 (I1): in FULL mode the volume strip is the FIRST row of
        # the pulled-down Command Center, on the top edge (B1 had it on the
        # bottom edge; DESIGN.md "master volume at the top").
        self.assertEqual(by, 10)
        self.assertEqual(d.cc.bar.rect, (0, 0, 1920, screens.BAR_H))
        # BAR mode: only the strip exists and it fills the 140 px surface
        d.set_size(1920, screens.BAR_H)
        t = d.targets()
        self.assertNotIn("home.hud", t)
        self.assertIn("bar.slider", t)
        self.assertTrue(all(v[1] >= 0 and v[1] + v[3] <= screens.BAR_H
                            for k, v in t.items() if not k.endswith(".track")))


class TestMixerStates(unittest.TestCase):
    def setUp(self):
        class H:
            def __getattr__(self, name):
                return lambda *a, **k: None
        self.d = screens.DeckUI(H(), 1920, 1080, "T")
        self.d.open("mixer")

    def test_empty_state_says_nothing_is_playing(self):
        self.d.mixer.set_streams([])
        self.assertEqual(self.d.mixer.msg.text, "No app is playing audio right now")
        self.assertEqual(self.d.mixer.rows, {})

    def test_failure_is_not_shown_as_empty(self):
        self.d.mixer.set_streams(None)
        self.assertIn("Could not read", self.d.mixer.msg.text)

    def test_rows_and_vanishing_stream(self):
        s1 = {"id": 62, "display_name": "emulationstation", "volume": 1.0, "muted": False}
        s2 = {"id": 201, "display_name": "retroarch", "volume": None, "muted": None}
        self.d.mixer.set_streams([s1, s2])
        self.assertEqual(sorted(self.d.mixer.rows), [62, 201])
        self.assertFalse(self.d.mixer.rows[201].slider.enabled)   # unknown volume: no fake 0
        self.assertIn("mixer.slider.62", self.d.targets())
        self.d.mixer.set_streams([s2])
        self.assertEqual(sorted(self.d.mixer.rows), [201])
        self.assertNotIn("mixer.slider.62", self.d.targets())


class TestFormatting(unittest.TestCase):
    def test_unknown_values_are_dashes_not_zero(self):
        cols = screens.hud_rows({"battery_percent": None, "fan_rpm": None, "fan_pwm": 0})
        flat = dict(r for _, rows in cols for r in rows)
        self.assertEqual(flat["Battery"], screens.NA)
        self.assertEqual(flat["Fan"], "%s rpm  PWM 0" % screens.NA)

    def test_every_hud_field_has_a_place(self):
        cols = screens.hud_rows({})
        self.assertEqual(sum(len(r) for _, r in cols), 21)

    def test_volume_readout_states(self):
        self.assertEqual(screens.volume_readout({"state": "ok", "volume": 0.42}), "42%")
        self.assertEqual(screens.volume_readout({"state": "restoring", "volume": None}), "Restoring")
        self.assertEqual(screens.volume_readout({"state": "error", "volume": None}), "Audio error")


class TestThrottle(unittest.TestCase):
    def test_at_most_one_send_per_interval_latest_wins(self):
        sent = []
        th = audioctl.Throttle(0.05, sent.append)
        t = 100.0
        th.push(0.10, t)                    # first value goes at once
        due = None
        for i, v in enumerate((0.11, 0.12, 0.13)):
            due = th.push(v, t + 0.01 * (i + 1))
        self.assertEqual(sent, [0.10])
        self.assertAlmostEqual(due, t + 0.05)
        self.assertIsNone(th.due(t + 0.05))
        self.assertEqual(sent, [0.10, 0.13])  # intermediate values dropped
        th.push(0.20, t + 0.06)
        th.finish()                          # release: pending is dropped
        self.assertIsNone(th.due(t + 1.0))
        self.assertEqual(sent, [0.10, 0.13])

    def test_rate_over_a_long_drag(self):
        sent = []
        th = audioctl.Throttle(0.05, sent.append)
        t, due = 0.0, None
        for i in range(1, 121):             # 120 moves at 120 Hz = 1 s
            now = i / 120.0
            if due is not None and now >= due:
                due = th.due(now)
            d = th.push(i / 120.0, now)
            due = d if d is not None else due
        self.assertLessEqual(len(sent), 21)
        self.assertGreaterEqual(len(sent), 15)


class TestDryRun(unittest.TestCase):
    def test_env_switch(self):
        self.assertTrue(audioctl.dryrun_from_env({"RP5DECK_AUDIO_DRYRUN": "1"}))
        self.assertFalse(audioctl.dryrun_from_env({}))
        self.assertFalse(audioctl.dryrun_from_env({"RP5DECK_AUDIO_DRYRUN": "0"}))

    def test_setters_never_run_anything(self):
        b = audioctl.Backend(dryrun=True)
        with mock.patch.object(audio, "_run", side_effect=AssertionError("ran a command")), \
                mock.patch.object(audio.subprocess, "run", side_effect=AssertionError("subprocess")):
            with self.assertLogs("rp5deck.audio", "INFO") as cm:
                self.assertTrue(b.set_master(0.4242))
                self.assertTrue(b.commit_master(1.7))
                self.assertTrue(b.toggle_master_mute())
                self.assertTrue(b.set_stream_volume(62, 0.5))
                self.assertTrue(b.toggle_stream_mute(62))
        text = "\n".join(cm.output)
        self.assertIn("wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.4242", text)
        self.assertIn("/usr/bin/volume 100", text)        # clamped like audio.commit_master
        self.assertIn("wpctl set-mute 62 toggle", text)
        self.assertEqual([c[0] for c in b.calls],
                         ["set_master", "commit_master", "toggle_master_mute",
                          "set_stream_volume", "toggle_stream_mute"])

    def test_readers_are_real_in_dryrun(self):
        b = audioctl.Backend(dryrun=True)
        with mock.patch.object(audio, "get_master", return_value={"state": "ok"}) as g:
            self.assertEqual(b.get_master(), {"state": "ok"})
            g.assert_called_once()

    def test_live_mode_calls_audio(self):
        b = audioctl.Backend(dryrun=False)
        with mock.patch.object(audio, "set_master", return_value=True) as s:
            b.set_master(0.3)
            s.assert_called_once_with(0.3)


# ---------------------------------------------------------------------------
# HF1: text entry, the app-aware strip, the launch and YouTube sheets
# ---------------------------------------------------------------------------
class _H:
    def __getattr__(self, name):
        return lambda *a, **k: None


class Recorder:
    """A handlers object that records the HF1 callbacks it receives."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith(("on_app_action", "on_yt_")):
            return lambda *a: self.calls.append((name,) + a)
        return lambda *a, **k: None


class TestKeyboard(unittest.TestCase):
    def make(self):
        typed = []
        kb = ui.Keyboard(on_text=typed.append, on_backspace=lambda: typed.append("<BS>"),
                         on_enter=lambda: typed.append("<ENTER>"),
                         on_clear=lambda: typed.append("<CLEAR>"), name="kb")
        kb.layout((0, 400, 1920, 600))
        return kb, typed

    def test_letters_space_backspace_enter(self):
        kb, typed = self.make()
        for k in ("h", "i", "space", "bksp", "enter"):
            kb.press(k)
        self.assertEqual(typed, ["h", "i", " ", "<BS>", "<ENTER>"])

    def test_shift_is_one_shot(self):
        kb, typed = self.make()
        kb.press("shift")
        self.assertTrue(kb.keys["shift"].lit)
        self.assertEqual(kb.keys["a"].text, "A")
        kb.press("a")
        kb.press("b")
        self.assertEqual(typed, ["A", "b"])
        self.assertFalse(kb.keys["shift"].lit)
        self.assertEqual(kb.keys["a"].text, "a")

    def test_symbol_layer_and_back(self):
        kb, typed = self.make()
        kb.press("sym")
        self.assertIn("5", kb.keys)
        self.assertNotIn("q", kb.keys)
        kb.press("5")
        kb.press("clear")
        kb.press("letters")
        self.assertIn("q", kb.keys)
        self.assertEqual(typed, ["5", "<CLEAR>"])

    def test_every_row_fills_the_width_without_overlap_and_keys_are_big(self):
        for layer in ui.KB_LAYERS:
            kb, _ = self.make()
            if layer != "letters":
                kb.press(layer)
            x0, y0, w, h = kb.rect
            rows = {}
            for kid, b in kb.keys.items():
                rows.setdefault(b.rect[1], []).append(b.rect)
                self.assertGreaterEqual(min(b.rect[2], b.rect[3]), 110, (layer, kid))
            self.assertEqual(len(rows), 4, layer)
            for y, rects in rows.items():
                rects.sort()
                self.assertLessEqual(abs(rects[0][0] - (x0 + kb.GAP)), 1, layer)
                right = rects[-1][0] + rects[-1][2]
                self.assertLessEqual(abs(right - (x0 + w - kb.GAP)), 2, (layer, y))
                for a, b in zip(rects, rects[1:]):
                    self.assertLessEqual(a[0] + a[2], b[0], (layer, a, b))

    def test_key_ids_are_unique_per_layer(self):
        for layer, rows in ui.KB_LAYERS.items():
            ids = [ui.Keyboard._spec(k)[0] for row in rows for k in row]
            self.assertEqual(len(ids), len(set(ids)), layer)
            for row in rows:
                self.assertAlmostEqual(sum(ui.Keyboard._spec(k)[1] for k in row), 10, msg=layer)

    def test_taps_reach_keys_through_the_router(self):
        root = ui.Root(1920, 1080)
        typed = []
        kb = root.add(ui.Keyboard(on_text=typed.append, name="kb"))
        kb.layout((0, 400, 1920, 600))
        r = ui.TouchRouter(root)
        x, y, w, h = kb.keys["q"].rect
        r.down(("f", 1), x + w / 2, y + h / 2)
        r.up(("f", 1), x + w / 2, y + h / 2)
        self.assertEqual(typed, ["q"])


class TestTextField(unittest.TestCase):
    def test_editing(self):
        f = ui.TextField("Search", max_len=5)
        f.insert("abc")
        f.insert("def")
        self.assertEqual(f.text, "abcde")          # capped
        f.backspace()
        self.assertEqual(f.text, "abcd")
        f.clear()
        f.backspace()                               # harmless when empty
        self.assertEqual(f.text, "")

    def test_tap_asks_for_focus(self):
        hits = []
        f = ui.TextField("Search", on_focus=lambda: hits.append(1), name="f")
        f.set_rect((0, 0, 400, 120))
        f.on_press(1, 10, 10)
        f.on_release(1, 10, 10)
        self.assertEqual(hits, [1])


class TestLitButton(unittest.TestCase):
    def test_lit_uses_the_accent(self):
        b = ui.LitButton("K")
        self.assertEqual(b.colors()[0], ui.THEME["tile"])
        b.set_lit(True)
        self.assertEqual(b.colors()[0], ui.THEME["accent"])
        b.pressed = True
        self.assertEqual(b.colors()[0], ui.THEME["tile_pressed"])


class TestAppStrip(unittest.TestCase):
    def deck(self, h=None):
        d = screens.DeckUI(h or _H(), 1920, 1080, "T")
        return d

    def test_full_mode_never_shows_app_controls(self):
        d = self.deck()
        d.cc.set_bar_app("web")
        t = d.targets()
        self.assertIn("bar.battery", t)
        self.assertFalse([k for k in t if k.startswith(("bar.web.", "bar.tv."))])

    def test_web_and_tv_strips(self):
        d = self.deck()
        d.set_size(1920, screens.BAR_H)
        for app, want, absent in (
                ("web", ["bar.web.back", "bar.web.reload", "bar.web.home", "bar.web.keys",
                         "bar.web.close"], ["bar.tv.left", "bar.battery", "bar.clock"]),
                ("tv", ["bar.tv.left", "bar.tv.up", "bar.tv.down", "bar.tv.right", "bar.tv.ok",
                        "bar.tv.back", "bar.tv.home", "bar.tv.close"],
                 ["bar.web.back", "bar.battery", "bar.tabs"])):
            d.cc.set_bar_app(app)
            t = d.targets()
            for name in want + ["bar.mute", "bar.slider"]:
                self.assertIn(name, t, app)
                x, y, w, h = t[name]
                self.assertGreaterEqual(min(w, h), 110, name)
                self.assertLessEqual(x + w, 1920, name)
                self.assertLessEqual(y + h, screens.BAR_H, name)
            for name in absent:
                self.assertNotIn(name, t, app)
        d.cc.set_bar_app(None)
        self.assertIn("bar.battery", d.targets())

    def test_tv_strip_drops_the_percent_readout(self):
        """YT3: the tv row trades the % label for width (Close/Home alongside
        the 6-button D-pad) - see Bar.layout()'s own note."""
        d = self.deck()
        d.set_size(1920, screens.BAR_H)
        d.cc.set_bar_app("web")
        self.assertTrue(d.bar.readout.visible)
        d.cc.set_bar_app("tv")
        self.assertFalse(d.bar.readout.visible)

    def test_keyboard_button_can_be_withheld(self):
        d = self.deck()
        d.set_size(1920, screens.BAR_H)
        d.cc.set_bar_app("web", keys_offered=False)
        t = d.targets()
        self.assertNotIn("bar.web.keys", t)
        self.assertIn("bar.web.close", t)

    def test_app_controls_call_the_handler(self):
        h = Recorder()
        d = self.deck(h)
        d.set_size(1920, screens.BAR_H)
        d.cc.set_bar_app("tv")
        r = ui.TouchRouter(d.root)
        x, y, w, hh = d.targets()["bar.tv.ok"]
        r.down(("f", 1), x + 5, y + 5)
        r.up(("f", 1), x + 5, y + 5)
        self.assertEqual(h.calls, [("on_app_action", "tv.ok")])

    def test_tv_close_and_home_call_the_handler(self):
        """YT3: dedicated Close/Home buttons, not the generic Tabs picker
        (which is hidden entirely for the tv app - see test_web_and_tv_strips'
        own "bar.tabs" absence check)."""
        h = Recorder()
        d = self.deck(h)
        d.set_size(1920, screens.BAR_H)
        d.cc.set_bar_app("tv")
        r = ui.TouchRouter(d.root)
        for name, action in (("bar.tv.close", "tv.close"), ("bar.tv.home", "tv.home")):
            x, y, w, hh = d.targets()[name]
            r.down(("f", 1), x + 5, y + 5)
            r.up(("f", 1), x + 5, y + 5)
        self.assertEqual(h.calls, [("on_app_action", "tv.close"), ("on_app_action", "tv.home")])

    def test_hint_covers_the_slider_but_never_mid_drag(self):
        d = self.deck()
        d.set_size(1920, screens.BAR_H)
        d.cc.set_bar_app("web")
        self.assertTrue(d.bar.show_hint("Tap the page first"))
        self.assertNotIn("bar.slider", d.targets())
        d.bar.clear_hint()
        self.assertIn("bar.slider", d.targets())
        d.bar.slider.dragging = True
        self.assertFalse(d.bar.show_hint("x"))
        self.assertFalse(d.bar.hinting)


class TestLaunchSheet(unittest.TestCase):
    def test_show_and_action(self):
        h = Recorder()
        d = screens.DeckUI(h, 1920, 1080, "T")
        d.open("launch")
        d.launch.show("Browser", "Opening Browser…", "small", action="Cancel")
        t = d.targets()
        self.assertIn("launch.action", t)
        x, y, w, hh = t["launch.action"]
        r = ui.TouchRouter(d.root)
        r.down(("f", 1), x + 5, y + 5)
        r.up(("f", 1), x + 5, y + 5)
        self.assertEqual(h.calls, [("on_app_action", "launch.action")])
        d.launch.show("Browser", "done")
        self.assertNotIn("launch.action", d.targets())

    def test_unknown_sheet_is_refused(self):
        d = screens.DeckUI(_H(), 1920, 1080, "T")
        with self.assertRaises(ValueError):
            d.open("soon")


class TestLiveTheming(unittest.TestCase):
    """Appearance (palettes.py) needs an ALREADY-BUILT Container/Label to
    pick up a runtime THEME change with no restart. Container.bg/Label.color
    accept either a THEME key string (resolved live by bg_color()/
    color_value() on every draw) or an already-resolved rgba tuple (frozen
    forever, unchanged pre-existing behaviour) - both forms are tested here
    so a future edit cannot silently drop either."""

    def tearDown(self):
        # A couple of these tests mutate the real ui.THEME directly.
        import config
        import palettes
        palettes.apply_theme(config.defaults())

    def test_container_bg_string_key_resolves_live(self):
        c = ui.Container(bg="bar")
        self.assertEqual(c.bg_color(), ui.THEME["bar"])
        old = dict(ui.THEME)
        try:
            ui.THEME["bar"] = (1.0, 0.0, 0.0, 1.0)
            self.assertEqual(c.bg_color(), (1.0, 0.0, 0.0, 1.0))
        finally:
            ui.THEME.clear()
            ui.THEME.update(old)

    def test_container_bg_literal_tuple_stays_frozen(self):
        literal = (0.1, 0.2, 0.3, 1.0)
        c = ui.Container(bg=literal)
        old = ui.THEME["bar"]
        try:
            ui.THEME["bar"] = (1.0, 0.0, 0.0, 1.0)
            self.assertEqual(c.bg_color(), literal)   # unaffected - not a THEME key
        finally:
            ui.THEME["bar"] = old

    def test_container_bg_none_paints_nothing(self):
        c = ui.Container()
        self.assertIsNone(c.bg_color())

    def test_label_default_color_resolves_live(self):
        lbl = ui.Label("hi")   # color=None -> the live "text" key
        self.assertEqual(lbl.color_value(), ui.THEME["text"])
        old = dict(ui.THEME)
        try:
            ui.THEME["text"] = (0.0, 1.0, 0.0, 1.0)
            self.assertEqual(lbl.color_value(), (0.0, 1.0, 0.0, 1.0))
        finally:
            ui.THEME.clear()
            ui.THEME.update(old)

    def test_label_string_key_resolves_live(self):
        lbl = ui.Label("hi", color="warn")
        self.assertEqual(lbl.color_value(), ui.THEME["warn"])
        old = ui.THEME["warn"]
        try:
            ui.THEME["warn"] = (0.0, 0.0, 1.0, 1.0)
            self.assertEqual(lbl.color_value(), (0.0, 0.0, 1.0, 1.0))
        finally:
            ui.THEME["warn"] = old

    def test_label_literal_tuple_color_stays_frozen(self):
        literal = (0.4, 0.4, 0.4, 1.0)
        lbl = ui.Label("hi", color=literal)
        old = ui.THEME["text"]
        try:
            ui.THEME["text"] = (0.0, 1.0, 0.0, 1.0)
            self.assertEqual(lbl.color_value(), literal)
        finally:
            ui.THEME["text"] = old

    def test_set_text_with_explicit_tuple_still_works(self):
        lbl = ui.Label("hi")
        lbl.set_text("bye", (0.5, 0.5, 0.5, 1.0))
        self.assertEqual(lbl.color_value(), (0.5, 0.5, 0.5, 1.0))

    def test_a_full_screen_already_built_recolors_with_no_reconstruction(self):
        """End-to-end proof: build a DeckUI (like main.py does once at
        startup), mutate ui.THEME the way palettes.apply_theme() does, and
        confirm the ALREADY-BUILT root's own background (captured as a "bg"
        key at construction - screens.py's `ui.Root(w, h, bg="bg")`) reads
        the new colour with no rebuild at all."""
        d = screens.DeckUI(_H(), 1920, 1080, "T")
        original_bg = d.root.bg_color()
        ui.THEME["bg"] = (0.9, 0.1, 0.1, 1.0)
        self.assertEqual(d.root.bg_color(), (0.9, 0.1, 0.1, 1.0))
        self.assertNotEqual(d.root.bg_color(), original_bg)

    def test_volume_readout_follows_a_theme_change_without_a_new_volume(self):
        """Device field test 25 Sep: the volume % readout stayed white on
        every preset (unreadable on RP5 White's light bar). Root cause was
        screens.py's Bar.set_master()/show_percent() passing an already-
        RESOLVED THEME["text"]/THEME["warn"] tuple to Label.set_text(),
        which freezes it (ui.Label's own docstring) - correct at the
        instant of that one call, but never refreshed again until the next
        volume change, which may never come before/after a theme switch.
        Fixed to pass the bare key ("text"/"warn") instead, so it now
        follows ui.THEME live like the rest of the app - proven here by
        changing THEME with NO further set_master() call at all."""
        d = screens.DeckUI(_H(), 1920, 1080, "T")
        d.bar.set_master({"state": "ok", "volume": 0.42})
        self.assertEqual(d.bar.readout.color_value(), ui.THEME["text"])
        old = ui.THEME["text"]
        try:
            ui.THEME["text"] = (0.2, 0.2, 0.2, 1.0)
            self.assertEqual(d.bar.readout.color_value(), (0.2, 0.2, 0.2, 1.0))
        finally:
            ui.THEME["text"] = old

    def test_volume_readout_warn_colour_also_follows_live(self):
        d = screens.DeckUI(_H(), 1920, 1080, "T")
        d.bar.set_master({"state": "error"})   # not ok -> the "warn" branch
        self.assertEqual(d.bar.readout.color_value(), ui.THEME["warn"])
        old = ui.THEME["warn"]
        try:
            ui.THEME["warn"] = (0.3, 0.3, 0.0, 1.0)
            self.assertEqual(d.bar.readout.color_value(), (0.3, 0.3, 0.0, 1.0))
        finally:
            ui.THEME["warn"] = old


if __name__ == "__main__":
    unittest.main()
