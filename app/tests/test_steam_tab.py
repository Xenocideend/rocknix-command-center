"""The Steam library tab: the library listing and launch handoff (steam_library), the tab model (app_tabs) and the
sheet's controller (steam_view). Fixtures are invented."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import app_tabs  # noqa: E402
import steam_library as sl  # noqa: E402
import steam_view  # noqa: E402
import window_switcher as wsw  # noqa: E402
from test_steam_library import Root  # noqa: E402

NOW = 1_800_000_000


class TestLibraryListing(Root):
    def test_lists_installed_games_with_card_art_and_a_short_line(self):
        self.manifest(11, "Old Favourite", played=NOW - 3 * 86400, size=5 << 30)
        self.manifest(12, "Never Played", played=0, size=2 << 30)
        self.art_file(11, "header.jpg")
        self.art_file(11, "library_600x900.jpg")
        self.art_file(12, "library_600x900.jpg")
        got = sl.library(self.root, NOW)
        self.assertEqual([g["name"] for g in got], ["Old Favourite", "Never Played"])
        self.assertTrue(got[0]["card"].endswith("header.jpg"))          # the wide header suits a grid best
        self.assertTrue(got[1]["card"].endswith("library_600x900.jpg"))  # next best when there is no header
        self.assertEqual(got[0]["line"], "Last played 3 days ago")
        self.assertEqual(got[1]["line"], "2.0 GB")

    def test_a_game_with_no_art_has_no_card_and_a_half_installed_one_is_left_out(self):
        self.manifest(21, "No Art")
        self.manifest(22, "Downloading", state=1026)
        got = sl.library(self.root, NOW)
        self.assertEqual([(g["name"], g["card"]) for g in got], [("No Art", None)])


class TestClientAndLaunch(Root):
    def client(self, pid, env=None, exe="/storage/.local/share/Steam/steamrtarm64/steam"):
        proc = self.proc(pid, [exe, "-deckard", "-gamepadui"])
        with open(os.path.join(proc, str(pid), "environ"), "wb") as f:
            f.write(b"\0".join(("%s=%s" % kv).encode() for kv in (env or {}).items()) + b"\0")
        return proc

    def test_finds_the_client_with_its_environment(self):
        proc = self.client(300, {"HOME": "/storage", "WAYLAND_DISPLAY": "wayland-1"})
        pid, argv, env = sl.client_process(proc)
        self.assertEqual((pid, argv[0]), (300, "/storage/.local/share/Steam/steamrtarm64/steam"))
        self.assertEqual(env, {"HOME": "/storage", "WAYLAND_DISPLAY": "wayland-1"})

    def test_the_web_helper_and_a_game_are_not_the_client(self):
        proc = self.proc(301, ["/storage/.local/share/Steam/ubuntu12_64/steamwebhelper", "-lang=en"])
        self.proc(302, ["reaper", "SteamLaunch", "AppId=5", "--", "x"])
        self.assertIsNone(sl.client_process(proc))

    def test_launch_hands_the_url_to_the_client_with_its_environment(self):
        proc = self.client(300, {"HOME": "/storage"})
        calls = []
        self.assertTrue(sl.launch(424242, proc, popen=lambda *a, **kw: calls.append((a, kw))))
        (argv,), kw = calls[0]
        self.assertEqual(argv, ["/storage/.local/share/Steam/steamrtarm64/steam", "steam://rungameid/424242"])
        self.assertEqual(kw["env"], {"HOME": "/storage"})

    def test_launch_with_steam_closed_starts_nothing(self):
        calls = []
        proc = self.proc(100, ["/usr/bin/bash", "x"])
        self.assertFalse(sl.launch(1, proc, popen=lambda *a, **kw: calls.append(a)))
        self.assertEqual(calls, [])


class TestSorting(unittest.TestCase):
    G = [{"name": "banana", "last_played": 5, "playtime_min": 30},
         {"name": "Apple", "last_played": 9, "playtime_min": 30},
         {"name": "cherry", "last_played": 0, "playtime_min": 120},
         {"name": "Date", "last_played": 0, "playtime_min": 0}]

    def names(self, mode):
        return [g["name"] for g in sl.sort_games(self.G, mode)]

    def test_recent_is_last_played_first_and_never_played_by_name(self):
        self.assertEqual(self.names("recent"), ["Apple", "banana", "cherry", "Date"])

    def test_az_ignores_case(self):
        self.assertEqual([g["name"] for g in sl.sort_games([{"name": "Zed"}, {"name": "apple"}, {"name": "Bob"}],
                                                           "az")], ["apple", "Bob", "Zed"])

    def test_most_played_first_with_ties_by_name(self):
        self.assertEqual(self.names("played"), ["cherry", "Apple", "banana", "Date"])

    def test_an_unknown_mode_falls_back_to_recent_and_the_input_is_not_changed(self):
        before = list(self.G)
        self.assertEqual(self.names("nonsense"), self.names("recent"))
        self.assertEqual(self.G, before)

    def test_the_library_carries_what_the_sorts_need(self):
        self.assertEqual(sl.SORTS, ("recent", "az", "played"))


class TestTheTab(unittest.TestCase):
    def ids(self, avail):
        return [t["id"] for t in app_tabs.build_tabs(None, {}, avail, {"mode": "FULL", "cc_open": True})]

    def test_the_tab_only_exists_while_steam_is_open(self):
        self.assertNotIn(app_tabs.STEAM, self.ids({"web": True}))
        self.assertIn(app_tabs.STEAM, self.ids({"web": True, "steam": True}))

    def test_it_sits_after_notes(self):
        ids = self.ids({"web": True, "steam": True})
        self.assertEqual(ids[ids.index(app_tabs.NOTES) + 1], app_tabs.STEAM)

    def test_it_is_active_while_its_sheet_is_open(self):
        view = {"mode": "FULL", "cc_open": True, "sheet": "steam"}
        self.assertEqual(app_tabs.active_tab(None, {}, view), app_tabs.STEAM)

    def test_it_is_an_internal_view_like_notes(self):
        self.assertEqual(app_tabs.route(app_tabs.STEAM, None, {}), (wsw.INTERNAL, app_tabs.STEAM))
        self.assertTrue(hasattr(app_tabs.TabsController, "_then_steam"))


class FakeSteam:
    def __init__(self, games, running=None, ok=True):
        self.games, self.running, self.ok = games, running, ok
        self.launched = []

    sort_games = staticmethod(sl.sort_games)

    def default_root(self):
        return "/steam"

    def library(self, root, now):
        return list(self.games)

    def running_appid(self):
        return self.running

    def launch(self, appid):
        self.launched.append(appid)
        return self.ok


class FakePull:
    def __init__(self):
        self.opened, self.closed = True, []

    def is_open(self):
        return self.opened

    def close(self, why):
        self.opened = False
        self.closed.append(why)


class FakeUi:
    def __init__(self):
        self.opened = []

    def open(self, name):
        self.opened.append(name)


class FakeHost:
    def __init__(self):
        self.ui, self.pull, self.state_dirty, self.sheets_closed = FakeUi(), FakePull(), False, 0

    def close_sheet(self):
        self.sheets_closed += 1


def games(n):
    return [{"appid": 100 + i, "name": "Game %d" % i, "card": "/art/%d.jpg" % i, "line": "1 h played",
             "last_played": 1000 - i, "playtime_min": i * 10} for i in range(n)]


class TestTheSheet(unittest.TestCase):
    def make(self, n=5, **kw):
        self.steam = FakeSteam(games(n), **kw)
        self.decoded = []

        def load(path, w, h, mode):
            self.decoded.append(path)
            return type("Img", (), {"w": w, "h": h})()
        self.host = FakeHost()
        run_now = lambda fn, *a, done=None: done(fn(*a)) if done else fn(*a)  # noqa: E731
        c = steam_view.SteamLibraryController(self.host, run_now, run_now, steam=self.steam, load_image=load,
                                              now=lambda: NOW)
        c.sheet.layout((0, 0, 1920, 810))
        return c

    def shown(self, c):
        return [(card.game or {}).get("appid") for card in c.sheet.cards if card.visible]

    def test_opening_lists_the_games_and_decodes_their_covers_once(self):
        c = self.make(5)
        c.open()
        self.assertEqual(self.host.ui.opened, ["steam"])
        self.assertEqual(self.shown(c), [100, 101, 102, 103, 104])
        self.assertEqual(len(self.decoded), 5)
        c.action("steam.next")                       # one page only, nothing moves
        c.action("steam.prev")
        self.assertEqual(len(self.decoded), 5)

    def test_pages_of_twelve(self):
        c = self.make(14)
        c.open()
        self.assertEqual(len(self.shown(c)), steam_view.PER_PAGE)
        self.assertFalse(c.sheet.prev.enabled)
        self.assertTrue(c.sheet.next.enabled)
        c.action("steam.next")
        self.assertEqual(self.shown(c), [112, 113])
        self.assertTrue(c.sheet.prev.enabled)
        self.assertFalse(c.sheet.next.enabled)
        self.assertEqual(c.sheet.title.text, "Steam library  2/2")
        c.action("steam.prev")
        self.assertEqual(len(self.decoded), 14)      # going back reuses the covers already decoded

    def test_the_sort_button_cycles_and_goes_back_to_the_first_page(self):
        c = self.make(14)
        c.open()
        c.action("steam.next")
        self.assertEqual(c.page, 1)
        self.assertEqual(c.sheet.sort.text, "Recent")
        c.action("steam.sort")
        self.assertEqual((c.sort_mode, c.page, c.sheet.sort.text), ("az", 0, "A-Z"))
        self.assertEqual(self.shown(c)[:3], [100, 101, 110])           # "Game 0", "Game 1", "Game 10" by name
        c.action("steam.sort")
        self.assertEqual((c.sort_mode, c.sheet.sort.text), ("played", "Most played"))
        self.assertEqual(self.shown(c)[0], 113)                        # the most played
        c.action("steam.sort")
        self.assertEqual((c.sort_mode, c.sheet.sort.text), ("recent", "Recent"))
        self.assertEqual(self.shown(c)[0], 100)

    def test_sorting_decodes_nothing_again_and_keeps_the_running_mark(self):
        c = self.make(5, running=104)
        c.open()
        n = len(self.decoded)
        c.action("steam.sort")
        c.action("steam.sort")
        self.assertEqual(len(self.decoded), n)
        shown = [(card.game["appid"], card.playing) for card in c.sheet.cards if card.visible]
        self.assertEqual([a for a, p in shown if p], [104])

    def test_the_library_is_sorted_when_it_arrives_whatever_order_it_comes_in(self):
        c = self.make(5)
        self.steam.games.reverse()
        c.open()
        self.assertEqual(self.shown(c), [100, 101, 102, 103, 104])     # recent: last played first

    def test_the_sort_choice_survives_closing_and_reopening(self):
        c = self.make(5)
        c.open()
        c.action("steam.sort")
        c.open()
        self.assertEqual((c.sort_mode, c.sheet.sort.text), ("az", "A-Z"))

    def test_the_running_game_is_marked(self):
        c = self.make(3, running=101)
        c.open()
        self.assertEqual([card.playing for card in c.sheet.cards[:3]], [False, True, False])

    def test_tapping_a_card_launches_that_game_and_closes_the_command_center(self):
        c = self.make(3)
        c.open()
        c.action("steam.card1")
        self.assertEqual(self.steam.launched, [101])
        self.assertEqual(self.host.pull.closed, ["steam launch"])

    def test_a_card_with_no_game_does_nothing(self):
        c = self.make(2)
        c.open()
        c.action("steam.card7")
        self.assertEqual(self.steam.launched, [])

    def test_launch_with_steam_closed_says_so_and_stays_open(self):
        c = self.make(2, ok=False)
        c.open()
        c.action("steam.card0")
        self.assertEqual(c.sheet.note.text, steam_view.CHECK_DESC)
        self.assertEqual(self.host.pull.closed, [])

    def test_no_installed_games_says_so(self):
        c = self.make(0)
        c.open()
        self.assertEqual(c.sheet.note.text, "No installed games found")
        self.assertEqual(self.shown(c), [])

    def test_a_late_answer_for_a_sheet_already_left_is_dropped(self):
        later = []
        self.steam = FakeSteam(games(3))
        host = FakeHost()
        c = steam_view.SteamLibraryController(host, lambda fn, *a, done=None: later.append((fn, a, done)),
                                              lambda fn, *a, done=None: None, steam=self.steam,
                                              load_image=lambda *a: None, now=lambda: NOW)
        c.sheet.layout((0, 0, 1920, 810))
        c.open()
        c.close()
        fn, a, done = later[0]
        done(fn(*a))
        self.assertEqual([x for x in c.games], [])

    def test_a_cover_that_will_not_decode_leaves_the_name_card(self):
        c = self.make(2)

        def bad(path, w, h, mode):
            raise ValueError("bad jpeg")
        c.load_image = bad
        c.open()
        self.assertEqual(self.shown(c), [100, 101])
        self.assertIsNone(c.sheet.cards[0].image)


if __name__ == "__main__":
    unittest.main()
