"""The Companion's Steam enrichment: a Steam game's title, artwork and facts come from Steam's own files, and
while Steam is the running 'game' the screen follows which game is actually up inside Big Picture.
Fixtures are invented."""
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import companion  # noqa: E402
import esevents  # noqa: E402
import steam_library  # noqa: E402
from test_companion import FakeResolver, game_ev, make_controller  # noqa: E402

NOW = 1_790_000_000
GAME_TS = NOW - 3 * 86400


class NoEs:
    def games(self, system):
        return []

    def game_detail(self, system, gid):
        return None

    def systems(self):
        return []

    def _get(self, path):
        return None


class SteamFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="steam-companion-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "steamapps"))
        self.apps = os.path.join(self.root, "applications")
        os.makedirs(self.apps)

    def game(self, appid, name, size=2 << 30, played=GAME_TS, art=("library_hero.jpg", "library_600x900.jpg",
                                                                  "header.jpg")):
        with open(os.path.join(self.root, "steamapps", "appmanifest_%d.acf" % appid), "w", encoding="utf-8") as f:
            f.write('"AppState"\n{\n "appid" "%d"\n "name" "%s"\n "StateFlags" "4"\n "installdir" "x"\n'
                    ' "LastPlayed" "%d"\n "SizeOnDisk" "%d"\n}\n' % (appid, name, played, size))
        d = os.path.join(self.root, "appcache", "librarycache", str(appid))
        os.makedirs(d, exist_ok=True)
        for a in art:
            with open(os.path.join(d, a), "wb") as f:
                f.write(b"\xff\xd8\xff")

    def shortcut(self, name, exec_line):
        p = os.path.join(self.apps, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nName=x\nExec=%s\nType=Application\n" % exec_line)
        return p

    def resolver(self, steam=None):
        return companion.Resolver(es=NoEs(), find_manual=lambda *a: None, steam=steam, steam_root=self.root,
                                  now=lambda: NOW)

    def target(self, rom, running=True, system="steam"):
        return companion.Target("game", system, rom, "Name From ES", None, running)


class TestDesktopFiles(SteamFixture):
    def test_the_appid_is_read_from_the_run_uri(self):
        p = self.shortcut("Game.desktop", "steam steam://rungameid/377160")
        self.assertEqual(steam_library.appid_from_desktop(p), 377160)

    def test_applaunch_form_too(self):
        p = self.shortcut("Old.desktop", "steam -applaunch 440")
        self.assertEqual(steam_library.appid_from_desktop(p), 440)

    def test_the_big_picture_launcher_has_no_appid(self):
        open(os.path.join(self.apps, "Steam.desktop"), "w").close()
        self.assertIsNone(steam_library.appid_from_desktop(os.path.join(self.apps, "Steam.desktop")))
        self.assertIsNone(steam_library.appid_from_desktop(os.path.join(self.apps, "missing.desktop")))


class TestFormatting(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(steam_library.fmt_size(0), "")
        self.assertEqual(steam_library.fmt_size(500 << 10), "")
        self.assertEqual(steam_library.fmt_size(300 << 20), "300 MB")
        self.assertEqual(steam_library.fmt_size(int(11.5 * (1 << 30))), "11.5 GB")
        self.assertEqual(steam_library.fmt_size(120 << 30), "120 GB")

    def test_last_played(self):
        f = steam_library.fmt_last_played
        self.assertEqual(f(0, NOW), "")
        self.assertEqual(f(NOW - 100, NOW), "Last played today")
        self.assertEqual(f(NOW - 86400 - 5, NOW), "Last played yesterday")
        self.assertEqual(f(NOW - 5 * 86400, NOW), "Last played 5 days ago")
        self.assertTrue(f(NOW - 40 * 86400, NOW).startswith("Last played "))
        self.assertNotIn("days ago", f(NOW - 40 * 86400, NOW))


class TestResolverAddsSteam(SteamFixture):
    def test_a_shortcut_gets_the_steam_name_art_and_facts(self):
        self.game(377160, "Fallout 4")
        rom = self.shortcut("Fallout 4.desktop", "steam steam://rungameid/377160")
        out = self.resolver().resolve(self.target(rom))
        self.assertEqual(out["title"], "Fallout 4")
        self.assertEqual(out["steam_appid"], 377160)
        for kind in ("fanart", "image", "mix", "titleshot", "thumbnail"):
            self.assertTrue(os.path.isfile(out["media"][kind]), kind)
        self.assertTrue(out["media"]["fanart"].endswith("library_hero.jpg"))
        self.assertTrue(out["media"]["image"].endswith("library_600x900.jpg"))
        self.assertEqual(out["extra_facts"], ["Last played 3 days ago", "2.0 GB"])

    def test_es_media_wins_over_steams(self):
        self.game(5, "Five")
        rom = self.shortcut("Five.desktop", "steam steam://rungameid/5")
        out = {"system": "steam", "running": False, "media": {"fanart": "/es/fanart.png"}, "title": "Five"}
        self.resolver()._add_steam(out, rom)
        self.assertEqual(out["media"]["fanart"], "/es/fanart.png")
        self.assertTrue(out["media"]["image"].endswith("library_600x900.jpg"))

    def test_the_big_picture_launcher_shows_the_game_steam_is_running(self):
        self.game(7, "Seven")
        rom = os.path.join(self.apps, "Steam.desktop")
        open(rom, "w").close()
        steam = types.SimpleNamespace(appid_from_desktop=steam_library.appid_from_desktop,
                                      running_appid=lambda: 7, default_root=steam_library.default_root,
                                      info=steam_library.info, facts=steam_library.facts)
        out = self.resolver(steam).resolve(self.target(rom))
        self.assertEqual((out["title"], out["steam_appid"]), ("Seven", 7))

    def test_the_launcher_alone_adds_nothing(self):
        rom = os.path.join(self.apps, "Steam.desktop")
        open(rom, "w").close()
        steam = types.SimpleNamespace(appid_from_desktop=steam_library.appid_from_desktop,
                                      running_appid=lambda: None, default_root=steam_library.default_root,
                                      info=steam_library.info, facts=steam_library.facts)
        out = self.resolver(steam).resolve(self.target(rom))
        self.assertNotIn("steam_appid", out)
        self.assertEqual(out["media"], {})

    def test_not_running_does_not_ask_steam_what_is_up(self):
        rom = os.path.join(self.apps, "Steam.desktop")
        open(rom, "w").close()
        asked = []
        steam = types.SimpleNamespace(appid_from_desktop=steam_library.appid_from_desktop,
                                      running_appid=lambda: asked.append(1),
                                      default_root=steam_library.default_root, info=steam_library.info,
                                      facts=steam_library.facts)
        self.resolver(steam).resolve(self.target(rom, running=False))
        self.assertEqual(asked, [])

    def test_other_systems_are_untouched(self):
        self.game(9, "Nine")
        rom = self.shortcut("Nine.desktop", "steam steam://rungameid/9")
        out = self.resolver().resolve(self.target(rom, system="gb"))
        self.assertNotIn("steam_appid", out)

    def test_a_steam_failure_leaves_the_es_info(self):
        rom = self.shortcut("Broken.desktop", "steam steam://rungameid/1")

        def boom(*a):
            raise RuntimeError("no")
        steam = types.SimpleNamespace(appid_from_desktop=steam_library.appid_from_desktop, running_appid=boom,
                                      default_root=steam_library.default_root, info=boom, facts=boom)
        with self.assertLogs("rp5deck.companion", level="ERROR"):
            out = self.resolver(steam).resolve(self.target(rom))
        self.assertEqual(out["title"], "Name From ES")
        self.assertNotIn("steam_appid", out)

    def test_a_game_with_no_art_still_gets_its_name(self):
        self.game(11, "No Art", art=())
        rom = self.shortcut("NoArt.desktop", "steam steam://rungameid/11")
        out = self.resolver().resolve(self.target(rom))
        self.assertEqual(out["title"], "No Art")
        self.assertEqual(out["media"], {})


class TestMetadataLines(unittest.TestCase):
    def test_the_steam_facts_follow_the_usual_ones(self):
        info = {"title": "T", "developer": "Dev", "extra_facts": ["2.0 GB", "Last played today"]}
        title, facts, desc = companion.metadata_lines(info, {"developer": True})
        self.assertEqual((title, facts), ("T", "Dev  ·  2.0 GB  ·  Last played today"))

    def test_no_extra_facts_changes_nothing(self):
        title, facts, desc = companion.metadata_lines({"title": "T", "developer": "Dev"}, {})
        self.assertEqual(facts, "Dev")


class FakeSteam:
    """poll() gives the next entry of `steps` (the last one repeats): an appid or None, or a dict
    {"appid", "seconds", "downloads"}."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.asked = 0

    @staticmethod
    def default_root():
        return "/steam"

    def poll(self, root=None):
        self.asked += 1
        s = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(s, dict):
            return dict({"appid": None, "seconds": 0, "downloads": []}, **s)
        return {"appid": s, "seconds": 0, "downloads": []}


class TestFollowingTheRunningSteamGame(unittest.TestCase):
    ROM = "/storage/.local/share/applications/Steam.desktop"

    def setup_steam(self, ids):
        c, view, timers, video, submit, cfg = make_controller(infos={self.ROM: {"kind": "game", "title": "Steam",
                                                                               "media": {}, "manual": None}})
        c.resolver.steam = FakeSteam(ids)
        return c, timers

    def start(self, c):
        c.on_es_event(game_ev(self.ROM, "Steam", "steam", esevents.GAME_START))

    def test_the_poll_runs_only_while_steam_runs(self):
        c, timers = self.setup_steam([None])
        self.assertIsNone(c.steam_timer)
        self.start(c)
        self.assertIsNotNone(c.steam_timer)
        c.on_es_event(game_ev(self.ROM, "Steam", "steam", esevents.GAME_END))
        self.assertIsNone(c.steam_timer)
        timers.advance(30)
        self.assertEqual(c.resolver.steam.asked, 0)

    def test_a_game_appearing_in_big_picture_refreshes_the_screen_once(self):
        c, timers = self.setup_steam([None, 7, 7, 7])
        self.start(c)
        before = len([x for x in c.resolver.calls if isinstance(x, companion.Target)])
        timers.advance(5)                       # nothing up yet
        self.assertEqual(len([x for x in c.resolver.calls if isinstance(x, companion.Target)]), before)
        timers.advance(5)                       # game 7 is up: the screen is resolved again
        self.assertEqual(len([x for x in c.resolver.calls if isinstance(x, companion.Target)]), before + 1)
        timers.advance(5)                       # still 7, nothing more to do
        timers.advance(5)
        self.assertEqual(len([x for x in c.resolver.calls if isinstance(x, companion.Target)]), before + 1)
        self.assertEqual(c.steam_appid, 7)

    def test_a_shown_game_is_not_refreshed_again(self):
        c, timers = self.setup_steam([7])
        self.start(c)
        c.info = dict(c.info or {}, steam_appid=7)
        n = len([x for x in c.resolver.calls if isinstance(x, companion.Target)])
        timers.advance(5)
        self.assertEqual(len([x for x in c.resolver.calls if isinstance(x, companion.Target)]), n)

    def test_other_systems_never_start_the_poll(self):
        c, timers = self.setup_steam([7])
        c.on_es_event(game_ev("/storage/roms/gb/A.gb", "A", "gb", esevents.GAME_START))
        self.assertIsNone(c.steam_timer)


class TestSteamInGameDisplay(unittest.TestCase):
    """The Steam page in Settings: what the bottom screen shows during a Steam game."""
    ROM = "/storage/.local/share/applications/Fallout 4.desktop"
    GB = "/storage/roms/gb/A.gb"

    def controller(self, companion_mode="clock", steam_mode="same"):
        import config
        c, view, timers, video, submit, cfg = make_controller(infos={
            self.ROM: {"kind": "game", "title": "Fallout 4", "media": {}, "manual": None},
            self.GB: {"kind": "game", "title": "A", "media": {}, "manual": None}})
        config.set_value(cfg, ("companion", "in_game_display"), companion_mode)
        config.set_value(cfg, ("steam", "in_game_display"), steam_mode)
        return c, cfg

    def start(self, c, rom, system):
        c.on_es_event(game_ev(rom, "G", system, esevents.GAME_START))

    def test_the_default_is_the_same_as_other_games(self):
        import config
        f = config.field_for(("steam", "in_game_display"))
        self.assertEqual(f["default"], "same")
        self.assertEqual(f["group"], "steam")
        self.assertNotIn("manual", f["values"])
        self.assertEqual(config.defaults()["steam"]["in_game_display"], "same")

    def test_same_follows_the_companion_setting(self):
        c, cfg = self.controller("clock", "same")
        self.start(c, self.ROM, "steam")
        self.assertEqual(c.ingame_mode_applied, "clock")

    def test_steam_can_differ_from_other_games_both_ways(self):
        c, cfg = self.controller("clock", "art")
        self.start(c, self.ROM, "steam")
        self.assertEqual(c.ingame_mode_applied, "art")
        c2, cfg2 = self.controller("art", "clock")
        self.start(c2, self.GB, "gb")
        self.assertEqual(c2.ingame_mode_applied, "art")          # a non-Steam game ignores the Steam choice

    def test_changing_it_takes_effect_on_the_running_game(self):
        import config
        c, cfg = self.controller("clock", "same")
        self.start(c, self.ROM, "steam")
        self.assertEqual(c.ingame_mode_applied, "clock")
        config.set_value(cfg, ("steam", "in_game_display"), "off")
        c.config_changed()
        self.assertEqual(c.ingame_mode_applied, "off")

    def test_the_settings_sheet_has_a_steam_tab_with_readable_choices(self):
        import settings_view
        self.assertIn("steam", settings_view.GROUP_ORDER)
        self.assertEqual(settings_view.TAB_TITLES["steam"], "Steam")
        show = settings_view._ENUM_DISPLAY_OVERRIDES[("steam", "in_game_display")]
        self.assertEqual(show("same"), "Same as other games")
        self.assertEqual(show("clock"), "Clock")


class TestSteamSettingReachesTheApp(unittest.TestCase):
    def test_changing_it_in_settings_reaches_the_companion(self):
        from test_companion import AppCase

        class Case(AppCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)
        app = case.make_app()
        calls = []
        app.companion.config_changed = lambda: calls.append(1)
        app.on_setting(("steam", "in_game_display"), "art")
        self.assertEqual(calls, [1])


class TestSteamSlideshow(SteamFixture):
    def test_a_slide_comes_from_steams_own_art(self):
        self.game(1, "One")
        p = self.resolver().system_art("steam")
        self.assertTrue(p and os.path.isfile(p) and p.endswith("library_hero.jpg"))

    def test_no_installed_games_or_no_art_is_no_slide(self):
        self.assertIsNone(self.resolver().system_art("steam"))
        self.game(2, "No Art", art=())
        self.assertIsNone(self.resolver().system_art("steam"))

    def test_other_systems_still_use_es(self):
        self.game(3, "Three")
        self.assertIsNone(self.resolver().system_art("gb"))        # NoEs has no games


class TestLiveLines(TestFollowingTheRunningSteamGame):
    def facts_now(self, c):
        return c._last_display[2][1]

    def test_the_session_line_and_the_download_line(self):
        f = companion.CompanionController.steam_live_facts
        self.assertEqual(f({"appid": 7, "seconds": 90}), ["Playing for 1 min"])
        self.assertEqual(f({"appid": 7, "seconds": 30}), [])
        self.assertEqual(f({"appid": None, "seconds": 600}), [])
        self.assertEqual(f({"downloads": [{"name": "Fallout 4", "percent": 45}]}), ["Updating Fallout 4 45%"])
        self.assertEqual(f({"downloads": [{"name": "A", "percent": 1}, {"name": "B", "percent": 2}]}),
                         ["Updating A 1% (+1 more)"])
        self.assertEqual(f({"downloads": [{"name": "X" * 40, "percent": 5}]}), ["Updating " + "X" * 24 + " 5%"])

    def test_a_new_minute_changes_the_line_without_resolving_again(self):
        c, timers = self.setup_steam([{"appid": 7, "seconds": 125}, {"appid": 7, "seconds": 190}])
        c.info = {"kind": "game", "system": "steam", "title": "S", "running": True, "steam_appid": 7, "media": {}}
        self.start(c)
        c.info = dict(c.info, steam_appid=7, system="steam", running=True)
        n = len([x for x in c.resolver.calls if isinstance(x, companion.Target)])
        drawn = []
        orig = c.view.show_info
        c.view.show_info = lambda info, image, logo, lines, dim, bg=None: (drawn.append(lines),
                                                                         orig(info, image, logo, lines, dim, bg))
        self.assertFalse(c.active)              # drawn all the same, like the first resolve is
        timers.advance(5)
        self.assertIn("Playing for 2 min", self.facts_now(c))
        timers.advance(5)
        self.assertIn("Playing for 3 min", self.facts_now(c))
        self.assertNotIn("2 min", self.facts_now(c))
        self.assertEqual(len([x for x in c.resolver.calls if isinstance(x, companion.Target)]), n)
        self.assertIn("Playing for 3 min", drawn[-1][1])        # and it reached the screen, not just the cache

    def test_an_unchanged_line_is_not_redrawn(self):
        c, timers = self.setup_steam([{"appid": 7, "seconds": 130}])
        self.start(c)
        c.info = dict(c.info or {}, steam_appid=7, system="steam", running=True)
        timers.advance(5)
        shown = []
        orig = c._show_live_facts
        c._show_live_facts = lambda: (shown.append(1), orig())
        timers.advance(5)
        timers.advance(5)
        self.assertEqual(shown, [])

    def test_a_download_line_comes_and_goes(self):
        dl = [{"name": "Some Game", "percent": 30}]
        c, timers = self.setup_steam([{"appid": 7, "seconds": 0, "downloads": dl},
                                      {"appid": 7, "seconds": 0, "downloads": []}])
        self.start(c)
        c.info = dict(c.info or {}, steam_appid=7, system="steam", running=True)
        timers.advance(5)
        self.assertIn("Updating Some Game 30%", self.facts_now(c))
        timers.advance(5)
        self.assertNotIn("Updating", self.facts_now(c))

    def test_the_lines_reset_when_steam_ends(self):
        c, timers = self.setup_steam([{"appid": 7, "seconds": 300}])
        self.start(c)
        c.info = dict(c.info or {}, steam_appid=7, system="steam", running=True)
        timers.advance(5)
        self.assertEqual(c.steam_live, ["Playing for 5 min"])
        c.on_es_event(game_ev(self.ROM, "Steam", "steam", esevents.GAME_END))
        self.assertEqual(c.steam_live, [])


if __name__ == "__main__":
    unittest.main()
