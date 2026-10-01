"""Owner, 26 Sep: "I changed the bottom screen resolution and now can't change it back".
A smaller screen size preset made the layout shorter, but Settings kept 5 rows per page,
so the lower rows - Screen size preset among them - sat below the screen. Rows per page
now follow the space, and a swipe pages Settings."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import config  # noqa: E402
import screen_presets  # noqa: E402
import settings_view as sv  # noqa: E402
import summon  # noqa: E402
from test_screen_presets import FakeGfx, FakeSdl  # noqa: E402
from test_companion import AppCase  # noqa: E402

PULL = summon.PullDownStateMachine


def _open(case, app):
    """Settings opens from the Command Center: pull it down first."""
    case.swipe(app, app.w // 2, 20, app.w // 2, int(app.h * 0.4))
    app.open_settings()


class SettingsFit(AppCase):
    def open(self, app):
        _open(self, app)

    def app_at(self, preset):
        app = self.make_app()
        app.sdl, app.win, app.gfx = FakeSdl(), object(), FakeGfx()
        app.tex = app.canvas = None
        config.set_value(app.cfg, ("screens", "ui_resolution"), preset)
        app._resize(1920, 1080)
        self.open(app)
        self.assertEqual(app.pull.state, PULL.SETTINGS)
        return app

    def reachable_rows(self, app):
        """Walk every tab and every page with Next; return {key_path: rect seen}."""
        sheet = app.ui.settings
        seen = {}
        for g in sv.GROUP_ORDER:
            sheet._select_group(g)
            for _ in range(40):
                for key, row in sheet.rows.items():
                    if row.visible and row.rect[3] > 0:
                        seen.setdefault(key, row.rect)
                if not sheet.next_btn.enabled:
                    break
                sheet.change_page(1)
        return seen

    def test_every_row_reachable_and_on_screen_at_every_preset(self):
        for preset in screen_presets.VALUES:
            app = self.app_at(preset)
            seen = self.reachable_rows(app)
            want = set(app.ui.settings.rows)
            self.assertEqual(want - set(seen), set(), "%s: rows never shown" % preset)
            for key, (x, y, w, h) in seen.items():
                self.assertLessEqual(y + h, app.h, "%s: %s below the screen" % (preset, key))

    def test_the_preset_row_can_be_found_after_a_big_preset(self):
        app = self.app_at("1280x720")                 # the layout the owner got stuck in
        self.assertLess(app.ui.settings._fit, sv.ROWS_PER_PAGE)
        seen = self.reachable_rows(app)
        self.assertIn(("screens", "ui_resolution"), seen)

    def test_rp5_default_keeps_five_rows(self):
        app = self.app_at("auto")
        self.assertEqual(app.ui.settings._fit, sv.ROWS_PER_PAGE)


class SettingsSwipe(AppCase):
    def test_swipe_up_next_page_down_previous(self):
        app = self.make_app()
        _open(self, app)
        sheet = app.ui.settings
        sheet._select_group("companion")              # several pages
        self.assertEqual(sheet.page_index, 0)
        app.on_gesture("swipe_up")
        self.assertEqual(sheet.page_index, 1)
        app.on_gesture("swipe_down")
        self.assertEqual(sheet.page_index, 0)
        self.assertEqual(app.pull.state, PULL.SETTINGS)

    def test_swipe_up_from_the_bottom_still_closes(self):
        app = self.make_app()
        _open(self, app)
        app.on_gesture("swipe_up_from_bottom")
        self.assertNotEqual(app.pull.state, PULL.SETTINGS)

    def test_settings_claims_swipe_down(self):
        app = self.make_app()
        _open(self, app)
        self.assertTrue(app.pull.wants("swipe_down"))
        app.close_settings()
        self.assertFalse(app.pull.wants("swipe_down"))


if __name__ == "__main__":
    unittest.main()
