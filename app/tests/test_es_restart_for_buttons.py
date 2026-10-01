"""The Settings row that restarts ES after a button colour change, on the real main.App (no SDL)."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import main  # noqa: E402
import settings_view  # noqa: E402
from test_charge_stuck_app import Case  # noqa: E402


class Label(unittest.TestCase):
    def test_every_state(self):
        f = settings_view.es_restart_label
        self.assertEqual(f(False, False, False), ("ES is showing these buttons", False))
        self.assertEqual(f(True, True, False), ("Close the game to restart ES", False))
        self.assertEqual(f(True, False, False), ("Restart ES to show the new buttons", True))
        self.assertEqual(f(True, False, True), ("Tap again to restart ES", True))
        self.assertEqual(f(True, False, False, busy=True), ("Restarting ES...", False))


class OwnCgroup(unittest.TestCase):
    """The real reader, not a fake: the device crash came from a check the tests had mocked away."""

    def test_reads_the_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".cgroup") as f:
            f.write("0::/system.slice/essway.service\n")
        self.addCleanup(os.remove, f.name)
        self.assertIn("essway", main.own_cgroup(f.name))

    def test_unreadable_is_empty(self):
        self.assertEqual(main.own_cgroup(os.path.join(HERE, "no-such-cgroup-file")), "")

    def test_the_default_path_is_the_process_own(self):
        self.assertEqual(main.own_cgroup.__defaults__, ("/proc/self/cgroup",))


class Row(Case):
    def setUp(self):
        super().setUp()
        self.calls = []
        run = mock.patch.object(main.subprocess, "run", side_effect=self.fake_run)
        cg = mock.patch.object(main, "own_cgroup", return_value="0::/rp5deck.scope\n")
        run.start()
        self.cgroup_patch = cg
        self.cgroup = cg.start()
        self.addCleanup(run.stop)
        self.addCleanup(cg.stop)
        self.app = self.build()
        self.row = self.app.ui.cc.settings.es_restart

    def fake_run(self, cmd, **kw):
        self.calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout="", stderr="")

    def pending(self):
        self.app.es_restart_pending = True
        self.app.refresh_es_restart_row()

    def test_the_row_sits_on_an_appearance_page(self):
        self.assertTrue(any(self.row in pg for pg in self.app.ui.cc.settings.pages["appearance"]))

    def test_nothing_to_restart_until_a_colour_changes(self):
        self.assertFalse(self.row.button.enabled)
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [])

    def test_the_real_cgroup_check_runs(self):
        self.cgroup_patch.stop()
        self.pending()
        self.app.restart_es_for_buttons()
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [["systemctl", "restart", "essway.service"]])
        self.cgroup_patch.start()

    def test_two_taps_restart_es_once(self):
        self.pending()
        self.app.restart_es_for_buttons()
        self.assertEqual(self.row.button.text, "Tap again to restart ES")
        self.assertEqual(self.calls, [])
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [["systemctl", "restart", "essway.service"]])
        self.assertFalse(self.app.es_restart_pending)
        self.assertEqual(self.row.button.text, "ES is showing these buttons")

    def test_the_first_tap_times_out(self):
        self.pending()
        self.app.restart_es_for_buttons()
        self.app._disarm_es_restart()
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.row.button.text, "Tap again to restart ES")

    def test_never_while_a_game_runs(self):
        self.pending()
        self.app.companion.running = object()
        self.app.refresh_es_restart_row()
        self.assertEqual(self.row.button.text, "Close the game to restart ES")
        self.app.restart_es_for_buttons()
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [])

    def test_the_scheme_at_start_is_the_baseline(self):
        import button_colours
        self.assertEqual(self.app._button_scheme, button_colours.resolve(
            main.config.get_value(self.app.cfg, ("appearance", "button_colours")),
            main.config.get_value(self.app.cfg, ("appearance", "theme_preset"))))
        self.assertFalse(getattr(self.app, "es_restart_pending", False))

    def test_the_first_change_after_start_needs_a_restart(self):
        import button_colours
        start = self.app._button_scheme
        with mock.patch.object(button_colours, "resolve", return_value="xbox" if start != "xbox" else "sfc"), \
                mock.patch.object(button_colours, "apply", return_value=("xbox", "ok")):
            self.app._apply_button_colours()
        self.assertTrue(self.app.es_restart_pending)
        self.assertTrue(self.row.button.enabled)

    def test_the_same_scheme_again_changes_nothing(self):
        import button_colours
        with mock.patch.object(button_colours, "resolve", return_value=self.app._button_scheme), \
                mock.patch.object(button_colours, "apply") as apply:
            self.app._apply_button_colours()
        apply.assert_not_called()
        self.assertFalse(getattr(self.app, "es_restart_pending", False))

    def test_never_from_inside_essway(self):
        self.cgroup.return_value = "0::/system.slice/essway.service\n"
        self.pending()
        self.app.restart_es_for_buttons()
        self.app.restart_es_for_buttons()
        self.app.post.drain()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.app.es_restart_pending)


if __name__ == "__main__":
    unittest.main()
