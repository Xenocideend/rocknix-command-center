"""steam_library: parse Steam's files, find the artwork, spot the running game. All fixtures are invented
(no real appids of an owner's library, no account ids)."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import steam_library as sl  # noqa: E402


def acf(appid, name, state=4, played=0, size=1000, installdir="Game"):
    return ('"AppState"\n{\n\t"appid"\t\t"%d"\n\t"name"\t\t"%s"\n\t"StateFlags"\t\t"%d"\n'
            '\t"installdir"\t\t"%s"\n\t"LastPlayed"\t\t"%d"\n\t"SizeOnDisk"\t\t"%d"\n'
            '\t"InstalledDepots"\n\t{\n\t\t"1"\n\t\t{\n\t\t\t"size"\t\t"5"\n\t\t}\n\t}\n}\n'
            % (appid, name, state, installdir, played, size))


class Root(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="steam-fixture-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "steamapps"))

    def manifest(self, appid, name, **kw):
        with open(os.path.join(self.root, "steamapps", "appmanifest_%d.acf" % appid), "w",
                  encoding="utf-8") as f:
            f.write(acf(appid, name, **kw))

    def art_file(self, appid, name):
        d = os.path.join(self.root, "appcache", "librarycache", str(appid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(b"\xff\xd8\xff")

    def proc(self, pid, args):
        d = os.path.join(self.root, "proc", str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in args) + b"\0")
        return os.path.join(self.root, "proc")


class TestParse(unittest.TestCase):
    def test_nested_blocks_and_escapes(self):
        d = sl.parse_vdf('"A"\n{\n "k" "v\\\\w"\n "B" { "x" "he said \\"hi\\"" }\n}\n')
        self.assertEqual(d, {"A": {"k": "v\\w", "B": {"x": 'he said "hi"'}}})

    def test_unbalanced_input_raises(self):
        for bad in ('"A" {', '}', '{ "k" "v" }'):
            with self.assertRaises(ValueError, msg=bad):
                sl.parse_vdf(bad)


class TestGames(Root):
    def test_lists_games_newest_played_first_then_by_name(self):
        self.manifest(11, "Zed", played=5)
        self.manifest(12, "Alpha", played=0)
        self.manifest(13, "Beta", played=0)
        self.manifest(14, "Mid", played=9)
        self.assertEqual([g["name"] for g in sl.games(self.root)], ["Mid", "Zed", "Alpha", "Beta"])

    def test_steams_own_plumbing_is_not_a_game(self):
        self.manifest(20, "Steamworks Common Redistributables")
        self.manifest(21, "Proton 9.0")
        self.manifest(22, "Proton Experimental")
        self.manifest(23, "Steam Linux Runtime 3.0 (sniper)")
        self.manifest(24, "Protonaut")                      # a game that merely starts with Proton
        self.assertEqual([g["name"] for g in sl.games(self.root)], ["Protonaut"])

    def test_fields_and_the_installed_flag(self):
        self.manifest(31, "Half Installed", state=1026, size=42, played=7, installdir="HalfDir")
        g = sl.games(self.root)[0]
        self.assertEqual((g["appid"], g["size_bytes"], g["last_played"], g["installdir"]), (31, 42, 7, "HalfDir"))
        self.assertFalse(g["installed"])                    # StateFlags 1026 has bit 4 clear

    def test_a_broken_manifest_is_skipped_not_fatal(self):
        self.manifest(41, "Fine")
        with open(os.path.join(self.root, "steamapps", "appmanifest_42.acf"), "w") as f:
            f.write('"AppState" {')
        with open(os.path.join(self.root, "steamapps", "appmanifest_43.acf"), "w") as f:
            f.write('"AppState" { "appid" "x" }')           # no usable appid or name
        self.assertEqual([g["name"] for g in sl.games(self.root)], ["Fine"])

    def test_a_second_library_folder_is_read(self):
        other = tempfile.mkdtemp(prefix="steam-fixture-other-")
        self.addCleanup(shutil.rmtree, other, True)
        os.makedirs(os.path.join(other, "steamapps"))
        with open(os.path.join(other, "steamapps", "appmanifest_51.acf"), "w", encoding="utf-8") as f:
            f.write(acf(51, "On The Other Drive"))
        with open(os.path.join(self.root, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8") as f:
            f.write('"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n}\n' % other.replace("\\", "\\\\"))
        self.manifest(52, "On This Drive")
        self.assertEqual(sorted(g["name"] for g in sl.games(self.root)),
                         ["On The Other Drive", "On This Drive"])

    def test_no_steam_folder_is_an_empty_list(self):
        self.assertEqual(sl.games(os.path.join(self.root, "nothing-here")), [])


class TestArt(Root):
    def test_finds_what_exists_and_nothing_else(self):
        self.art_file(7, "header.jpg")
        self.art_file(7, "library_600x900.jpg")
        got = sl.art(self.root, 7)
        self.assertEqual(sorted(got), ["capsule", "header"])
        for p in got.values():
            self.assertTrue(os.path.isfile(p))

    def test_the_other_header_name_is_used_when_there_is_no_plain_one(self):
        self.art_file(8, "library_header.jpg")
        self.assertTrue(sl.art(self.root, 8)["header"].endswith("library_header.jpg"))

    def test_plain_header_wins_over_the_library_one(self):
        self.art_file(9, "header.jpg")
        self.art_file(9, "library_header.jpg")
        self.assertTrue(sl.art(self.root, 9)["header"].endswith(os.sep + "header.jpg"))

    def test_unknown_game_has_no_art(self):
        self.assertEqual(sl.art(self.root, 123456), {})


class TestRunning(Root):
    def test_the_game_steam_launched(self):
        proc = self.proc(100, ["/usr/bin/bash", "x"])
        self.proc(101, ["reaper", "SteamLaunch", "AppId=424242", "--", "/game/run"])
        self.assertEqual(sl.running_appid(proc), 424242)

    def test_no_steam_game_running(self):
        proc = self.proc(100, ["/usr/bin/bash", "x"])
        self.proc(102, ["steam", "-gamepadui"])
        self.assertIsNone(sl.running_appid(proc))

    def test_a_process_that_vanished_or_has_no_cmdline_is_skipped(self):
        proc = self.proc(100, ["reaper", "SteamLaunch", "AppId=77", "--", "x"])
        os.makedirs(os.path.join(proc, "101"))              # no cmdline file
        os.makedirs(os.path.join(proc, "notapid"))
        self.assertEqual(sl.running_appid(proc), 77)

    def test_no_proc_at_all(self):
        self.assertIsNone(sl.running_appid(os.path.join(self.root, "no-proc")))


class TestInfo(Root):
    def test_an_installed_running_game_with_art(self):
        self.manifest(5, "Some Game", played=9, size=99)
        self.art_file(5, "library_hero.jpg")
        proc = self.proc(1, ["reaper", "SteamLaunch", "AppId=5", "--", "x"])
        i = sl.info(self.root, 5, proc)
        self.assertEqual((i["name"], i["installed"], i["running"], i["size_bytes"]), ("Some Game", True, True, 99))
        self.assertEqual(list(i["art"]), ["hero"])

    def test_a_running_game_with_no_manifest_still_answers(self):
        proc = self.proc(1, ["reaper", "SteamLaunch", "AppId=6", "--", "x"])
        i = sl.info(self.root, 6, proc)
        self.assertIsNone(i["name"])
        self.assertFalse(i["installed"])
        self.assertTrue(i["running"])
        self.assertEqual(i["art"], {})


LOCALCONFIG = ('"UserLocalConfigStore"\n{\n "Software"\n {\n  "Valve"\n  {\n   "Steam"\n   {\n    "apps"\n    {\n'
               '     "42"\n     {\n      "LastPlayed"\t\t"1700000000"\n      "Playtime"\t\t"125"\n'
               '      "Playtime2wks"\t\t"30"\n     }\n'
               '     "43"\n     {\n      "LastPlayed"\t\t"1"\n     }\n    }\n   }\n  }\n }\n}\n')
NO_PLAYTIME = {"total_min": 0, "two_weeks_min": 0}


class TestPlaytime(Root):
    def config(self, text=LOCALCONFIG):
        d = os.path.join(self.root, "userdata", "123456", "config")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "localconfig.vdf"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_total_and_two_week_minutes(self):
        self.config()
        self.assertEqual(sl.playtime(self.root, 42), {"total_min": 125, "two_weeks_min": 30})

    def test_a_game_with_no_playtime_or_no_entry_is_zero(self):
        self.config()
        self.assertEqual(sl.playtime(self.root, 43), NO_PLAYTIME)
        self.assertEqual(sl.playtime(self.root, 999), NO_PLAYTIME)

    def test_no_config_or_a_broken_one_is_zero(self):
        self.assertEqual(sl.playtime(self.root, 42), NO_PLAYTIME)
        self.config('"UserLocalConfigStore" {')
        self.assertEqual(sl.playtime(self.root, 42), NO_PLAYTIME)

    def test_a_changed_file_is_read_again(self):
        self.config()
        self.assertEqual(sl.playtime(self.root, 42)["total_min"], 125)
        self.config(LOCALCONFIG.replace('"125"', '"9999"'))
        self.assertEqual(sl.playtime(self.root, 42)["total_min"], 9999)

    def test_info_carries_the_playtime(self):
        self.config()
        self.manifest(42, "Forty Two")
        i = sl.info(self.root, 42, os.path.join(self.root, "no-proc"))
        self.assertEqual((i["playtime_min"], i["two_weeks_min"]), (125, 30))
        self.assertEqual(sl.facts(i, 1700000000 + 5 * 86400)[0], "2.1 h played")


class TestDurations(unittest.TestCase):
    def test_played(self):
        self.assertEqual(sl.fmt_played(0), "")
        self.assertEqual(sl.fmt_played(20), "20 min played")
        self.assertEqual(sl.fmt_played(95), "1.6 h played")
        self.assertEqual(sl.fmt_played(3217), "54 h played")

    def test_session_length(self):
        self.assertEqual(sl.fmt_duration(30), "")
        self.assertEqual(sl.fmt_duration(90), "1 min")
        self.assertEqual(sl.fmt_duration(42 * 60), "42 min")
        self.assertEqual(sl.fmt_duration(72 * 60 + 5), "1 h 12 min")


class TestRunningSince(Root):
    def fake_proc(self, appid, start_ticks, uptime="1000.00 400.00"):
        proc = self.proc(101, ["reaper", "SteamLaunch", "AppId=%d" % appid, "--", "x"])
        with open(os.path.join(proc, "101", "stat"), "w", encoding="utf-8") as f:
            f.write("101 (reaper) S 1 1 1 0 -1 4194304 1 0 0 0 0 0 0 0 20 0 1 0 %d 1 1\n" % start_ticks)
        with open(os.path.join(proc, "uptime"), "w", encoding="utf-8") as f:
            f.write(uptime)
        return proc

    def test_age_is_uptime_minus_start(self):
        proc = self.fake_proc(5, 60000)                  # started at 600 s, now 1000 s
        appid, secs = sl.running_since(proc, clk=100)
        self.assertEqual(appid, 5)
        self.assertAlmostEqual(secs, 400.0)

    def test_no_steam_game_is_none_and_zero(self):
        proc = self.proc(100, ["bash"])
        self.assertEqual(sl.running_since(proc), (None, 0))

    def test_an_unreadable_stat_gives_the_game_with_no_age(self):
        proc = self.proc(101, ["reaper", "SteamLaunch", "AppId=9", "--", "x"])
        self.assertEqual(sl.running_since(proc), (9, 0))


class TestDownloads(Root):
    def manifest_dl(self, appid, name, flags, want, got):
        with open(os.path.join(self.root, "steamapps", "appmanifest_%d.acf" % appid), "w", encoding="utf-8") as f:
            f.write('"AppState"\n{\n "appid" "%d"\n "name" "%s"\n "StateFlags" "%d"\n'
                    ' "BytesToDownload" "%d"\n "BytesDownloaded" "%d"\n}\n' % (appid, name, flags, want, got))

    def test_a_game_being_updated_has_a_percentage(self):
        self.manifest_dl(1, "Updating", 1026 | 4, 1000, 250)
        self.assertEqual(sl.downloads(self.root), [{"appid": 1, "name": "Updating", "percent": 25}])

    def test_finished_idle_and_empty_ones_are_left_out(self):
        self.manifest_dl(2, "Done", 4, 1000, 1000)
        self.manifest_dl(3, "Stale numbers", 4, 1000, 100)          # the flags say nothing is running
        self.manifest_dl(4, "Nothing to fetch", 1026, 0, 0)
        self.assertEqual(sl.downloads(self.root), [])

    def test_no_steam_folder(self):
        self.assertEqual(sl.downloads(os.path.join(self.root, "nothing")), [])


if __name__ == "__main__":
    unittest.main()
