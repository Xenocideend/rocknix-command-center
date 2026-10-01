"""Batch 2: button colours - ES's A/B/X/Y prompt icons, set apart from the screen theme."""
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import button_colours as bc  # noqa: E402
import config  # noqa: E402
from test_companion import AppCase  # noqa: E402


def device_block(name, codes):
    """A /proc/bus/input/devices block whose KEY bitmap has exactly `codes`."""
    bits = 0
    for c in codes:
        bits |= 1 << c
    words = []
    while bits:
        words.append("%x" % (bits & (2 ** 64 - 1)))
        bits >>= 64
    return 'I: Bus=0019\nN: Name="%s"\nB: EV=b\nB: KEY=%s\n' % (name, " ".join(reversed(words)))


SOUTH, EAST, C, NORTH, WEST, Z = bc.FACE_CODES


def hexes(svg):
    return set(h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}", svg))


class Schemes(unittest.TestCase):
    def test_config_enum_matches_the_module(self):
        self.assertEqual(config.BUTTON_COLOUR_VALUES, bc.VALUES)

    def test_every_scheme_renders_eight_files_in_its_colours(self):
        for name, (_label, colours, _src) in bc.SCHEMES.items():
            files = bc.render(name)
            self.assertEqual(len(files), 8, name)
            for stem, pos in bc.FILE_POS.items():
                svg = files[stem + ".svg"]
                self.assertIn(colours[pos].upper(), hexes(svg), (name, stem))
                if colours[pos].upper() != bc.TEMPLATE_HEX[pos].upper():
                    self.assertNotIn(bc.TEMPLATE_HEX[pos].upper(), hexes(svg), (name, stem))

    def test_positions_follow_the_controller(self):
        # Xbox's green A is the BOTTOM button: on the RP5 that is B's icon (south)
        files = bc.render("xbox")
        self.assertIn("#5DBB46", hexes(files["button_b.svg"]))
        self.assertIn("#E23B2E", hexes(files["button_a.svg"]))

    def test_super_famicom_keeps_white_letters_like_the_real_caps(self):
        for svg in bc.render("super_famicom").values():
            self.assertNotIn(bc.DARK_LETTER.upper(), hexes(svg))

    def test_letter_is_dark_on_a_light_cap_and_white_on_a_dark_one(self):
        white = bc.render("rp5_white")["button_a.svg"]
        self.assertIn(bc.DARK_LETTER.upper(), hexes(white))
        black = bc.render("rp5_black")["button_a.svg"]
        self.assertIn("#FFFFFF", hexes(black))
        self.assertNotIn(bc.DARK_LETTER.upper(), hexes(black))

    def test_follow_theme(self):
        self.assertEqual(bc.resolve("follow_theme", "rp5_gc"), "rp5_gc")
        self.assertEqual(bc.resolve("follow_theme", "playstation"), "playstation")
        self.assertEqual(bc.resolve("follow_theme", "default"), bc.DEFAULT_SCHEME)
        self.assertEqual(bc.resolve("xbox", "rp5_gc"), "xbox")      # the button choice wins

    def test_labels(self):
        self.assertEqual(bc.label("follow_theme"), "Match screen theme")
        self.assertEqual(bc.label("snes_us"), "SNES (US)")


class FaceCount(unittest.TestCase):
    def test_rp5_pad_has_four(self):
        text = "\n".join([device_block("InputPlumber Keyboard", [59]),
                          device_block("Retroid Pocket Gamepad", [SOUTH, EAST, NORTH, WEST, 0x13a]),
                          device_block("Sony Interactive Entertainment DualSense Wireless Controller",
                                       [SOUTH, EAST, C, NORTH, WEST, Z])])
        self.assertEqual(bc.face_button_count(text), 4)

    def test_emulated_pad_is_ignored(self):
        text = device_block("Sony Interactive Entertainment DualSense Wireless Controller",
                            [SOUTH, EAST, NORTH, WEST])
        self.assertIsNone(bc.face_button_count(text))

    def test_two_and_six_button_devices(self):
        self.assertEqual(bc.face_button_count(device_block("Handheld pad", [SOUTH, EAST])), 2)
        self.assertEqual(bc.face_button_count(device_block("Handheld pad",
                                                           [SOUTH, EAST, C, NORTH, WEST, Z])), 6)

    def test_supported_only_on_four(self):
        self.assertTrue(bc.supported(4))
        self.assertTrue(bc.supported(None))
        for n in (2, 3, 6):
            self.assertFalse(bc.supported(n), n)


class Apply(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="bc-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_writes_the_eight_icons(self):
        scheme, msg = bc.apply("dreamcast", "default", help_dir=self.d, count=4)
        self.assertEqual(scheme, "dreamcast")
        self.assertEqual(sorted(os.listdir(self.d)), sorted(s + ".svg" for s in bc.FILE_POS))
        self.assertIn("restart", msg)

    def test_coming_soon_writes_nothing(self):
        scheme, msg = bc.apply("xbox", "default", help_dir=self.d, count=6)
        self.assertIsNone(scheme)
        self.assertEqual(msg, bc.COMING_SOON)
        self.assertEqual(os.listdir(self.d), [])


class InTheApp(AppCase):
    def setUp(self):
        AppCase.setUp(self)
        self.help = os.path.join(self.tmp, "help")
        orig = bc.ES_HELP_DIR
        bc.ES_HELP_DIR = self.help
        self.addCleanup(setattr, bc, "ES_HELP_DIR", orig)

    def icon(self, fn):
        with open(os.path.join(self.help, fn), encoding="utf-8") as f:
            return f.read()

    def settings_control(self, app):
        app.open_settings()
        for w in (getattr(app.ui, n, None) for n in dir(app.ui)):
            ctl = getattr(w, "controls", None)
            if isinstance(ctl, dict) and ("appearance", "button_colours") in ctl:
                return ctl[("appearance", "button_colours")]
        self.fail("no Button colours row in Settings")

    def test_choosing_a_scheme_writes_the_icons(self):
        app = self.make_app()
        config.set_value(app.cfg, ("appearance", "button_colours"), "xbox")
        app.on_setting(("appearance", "button_colours"), "xbox")
        self.assertIn("#5DBB46", hexes(self.icon("button_b.svg")))

    def test_screen_theme_change_moves_follow_theme_buttons(self):
        app = self.make_app()
        config.set_value(app.cfg, ("appearance", "theme_preset"), "rp5_white")
        app.on_setting(("appearance", "theme_preset"), "rp5_white")
        self.assertIn("#ECECEC", hexes(self.icon("button_a.svg")))

    def test_theme_change_leaves_a_chosen_scheme_alone(self):
        app = self.make_app({"schema_version": 1, "appearance": {"button_colours": "xbox"}})
        bc.apply("xbox", "default", help_dir=self.help)     # the icons ES has from the earlier choice
        config.set_value(app.cfg, ("appearance", "theme_preset"), "rp5_white")
        app.on_setting(("appearance", "theme_preset"), "rp5_white")
        self.assertIn("#E23B2E", hexes(self.icon("button_a.svg")))

    def test_settings_row_cycles_with_readable_labels(self):
        app = self.make_app()
        ctl = self.settings_control(app)
        self.assertTrue(ctl.enabled)
        self.assertEqual(ctl.text, "Match screen theme")

    def test_settings_row_disabled_with_coming_soon_on_other_layouts(self):
        orig = bc.supported
        bc.supported = lambda count=None: False
        self.addCleanup(setattr, bc, "supported", orig)
        app = self.make_app()
        ctl = self.settings_control(app)
        self.assertFalse(ctl.enabled)
        self.assertEqual(ctl.text, bc.COMING_SOON)


if __name__ == "__main__":
    unittest.main()
