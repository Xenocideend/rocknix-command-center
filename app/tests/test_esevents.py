#!/usr/bin/env python3
"""esevents.py and the es-hooks: record format, argument interpretation, the
spool (one file per event, consumed in ES's fire order, stale and excess
files dropped), the /runningGame poll logic (a failed poll is UNKNOWN, never
"not running"), the Watcher (stat mode everywhere, inotify mode on Linux),
and - on Linux - the REAL hook scripts: run under every available POSIX
shell (byte-exact names with apostrophes / & / spaces / newlines / non-ASCII
/ invalid UTF-8, silent exit 0 on failure, no temp files left, atomic
publish observed through inotify), and FIRED THE WAY ES FIRES THEM (its
executeScript() quoting, Platform.cpp's `sh -c` wrapper and double fork):
bursts lose nothing and sort in fire order. Also the installer, into temp
directories: the name guard refusal, --force, the ES-quoting self-test,
rollback, --remove completeness, idempotency, and an interrupted install."""
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import esevents  # noqa: E402
import name_guard  # noqa: E402
from esevents import (GAME_END, GAME_SELECTED, GAME_START, NOT_RUNNING,  # noqa: E402
                      SCREENSAVER_START, SCREENSAVER_STOP, SLEEP, SYSTEM_SELECTED, UNKNOWN,
                      WAKE, build_record, parse_record)

LINUX = sys.platform.startswith("linux")
HOOKS = os.path.join(APP, "es-hooks")
INSTALLER = os.path.join(APP, "tools", "install-es-hooks.sh")

NASTY = [
    "Jimmy White's 'Whirlwind' Snooker",
    "Ren & Stimpy Show Presents, The - Stimpy's Invention",
    "Pokémon - Edición Azul (ポケモン)",
    "  leading and trailing  ",
    "has=equals=signs",
    "newline\ninside",
    "percent %s %d and backslash \\n \\0",
    "-starts-with-dash",
    "$HOME `id` $(id) \"double\"",
    "",
]


def wait_for(pred, timeout=3.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class Game:
    def __init__(self, system, rom, name="G"):
        self.system, self.rom_path, self.name, self.id = system, rom, name, rom


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestRecordFormat(unittest.TestCase):
    def test_round_trip_every_nasty_name(self):
        for name in NASTY:
            rom = "/storage/roms/snes/%s.zip" % name
            ev = parse_record(build_record(GAME_SELECTED, ["snes", rom, name]))
            self.assertIsNotNone(ev, name)
            self.assertEqual((ev.kind, ev.system, ev.rom_path), (GAME_SELECTED, "snes", rom))
            self.assertEqual(ev.args, ("snes", rom, name))
            if name:
                self.assertEqual(ev.name, name)

    def test_invalid_utf8_round_trips_to_the_same_bytes(self):
        raw = b"/storage/roms/nes/caf\xe9 \xff.nes"
        ev = parse_record(build_record(GAME_SELECTED, [b"nes", raw, b"x"]))
        self.assertEqual(os.fsencode(ev.rom_path) if LINUX else
                         ev.rom_path.encode("utf-8", "surrogateescape"), raw)

    def test_truncated_record_is_refused(self):
        rec = build_record(GAME_SELECTED, ["gb", "/storage/roms/gb/a.gb", "A"])
        for cut in (1, 5, len(rec) // 2, len(rec) - 1):
            self.assertIsNone(parse_record(rec[:cut]), cut)
        self.assertIsNone(parse_record(b""))

    def test_missing_promised_arg_is_refused(self):
        rec = b"rp5deck-es-event=1\0event=game-selected\0argc=3\0arg1=gb\0arg2=/x\0"
        self.assertIsNone(parse_record(rec))

    def test_wrong_magic_version_or_event_is_refused(self):
        self.assertIsNone(parse_record(b"event=game-selected\0argc=0\0"))
        self.assertIsNone(parse_record(b"rp5deck-es-event=2\0event=game-selected\0argc=0\0"))
        self.assertIsNone(parse_record(b"rp5deck-es-event=1\0event=quit\0argc=0\0"))
        self.assertIsNone(parse_record(b"rp5deck-es-event=1\0event=game-start\0argc=x\0"))

    def test_the_idle_events_are_records_too(self):
        for kind in (SCREENSAVER_START, SCREENSAVER_STOP, SLEEP, WAKE):
            ev = parse_record(build_record(kind, []))
            self.assertEqual((ev.kind, ev.system, ev.rom_path, ev.args), (kind, "", "", ()))

    def test_key_without_equals_is_refused(self):
        self.assertIsNone(parse_record(b"rp5deck-es-event=1\0event=game-end\0junk\0"))


class TestInterpret(unittest.TestCase):
    def test_game_selected_source_layout(self):
        ev = esevents.interpret(GAME_SELECTED, ["gb", "/storage/roms/gb/Tetris (W).gb", "Tetris"])
        self.assertEqual((ev.system, ev.rom_path, ev.name),
                         ("gb", "/storage/roms/gb/Tetris (W).gb", "Tetris"))

    def test_system_selected_split_form_with_empty_extras(self):
        ev = esevents.interpret(SYSTEM_SELECTED, ["snes", "", ""])
        self.assertEqual((ev.system, ev.rom_path, ev.name), ("snes", "", ""))

    def test_game_start_batocera_layout_rom_basename_name(self):
        ev = esevents.interpret(GAME_START, ["/storage/roms/psx/FF7 (Disc 1).chd",
                                             "FF7 (Disc 1).chd", "Final Fantasy VII"])
        self.assertEqual((ev.system, ev.rom_path, ev.name),
                         ("psx", "/storage/roms/psx/FF7 (Disc 1).chd", "Final Fantasy VII"))

    def test_game_start_with_leading_system(self):
        ev = esevents.interpret(GAME_START, ["n64", "/storage/roms/n64/Mario.z64", "Super Mario 64"])
        self.assertEqual((ev.system, ev.name), ("n64", "Super Mario 64"))

    def test_game_end_path_only_infers_system_and_name(self):
        ev = esevents.interpret(GAME_END, ["/storage/games-internal/roms/gba/Metroid.gba"])
        self.assertEqual((ev.system, ev.name), ("gba", "Metroid"))

    def test_no_path_at_all_gives_empty_rom(self):
        ev = esevents.interpret(GAME_START, ["weird"])
        self.assertEqual(ev.rom_path, "")


class TestBackslashEscapedPaths(unittest.TestCase):
    """FX-E task 3: ES's game-start/game-end hooks pass the ROM path with a
    backslash before every shell-special character, while the /runningGame
    poll gives the clean path (observed on the device, 24 Sep 2026). Without
    normalising, companion.py's running-game match (`self.running.rom_path ==
    ev.rom_path`) and RunningPoll.game_key never see the two as equal."""

    # Exact string observed on the device as the game-start/game-end rom arg.
    OBSERVED_ESCAPED = (r"/storage/roms/gb/Adventures\ of\ Rocky\ and\ "
                        r"Bullwinkle\ and\ Friends,\ The\ \(USA\).zip")
    CLEAN = ("/storage/roms/gb/Adventures of Rocky and Bullwinkle and "
             "Friends, The (USA).zip")

    def test_unescape_helper_on_the_exact_observed_string(self):
        self.assertEqual(esevents.unescape_shell_backslashes(self.OBSERVED_ESCAPED),
                         self.CLEAN)

    def test_game_start_normalises_the_observed_escaped_path(self):
        ev = esevents.interpret(GAME_START, [self.OBSERVED_ESCAPED])
        self.assertEqual(ev.rom_path, self.CLEAN)
        self.assertEqual(ev.system, "gb")

    def test_game_end_normalises_the_observed_escaped_path(self):
        ev = esevents.interpret(GAME_END, [self.OBSERVED_ESCAPED])
        self.assertEqual(ev.rom_path, self.CLEAN)

    def test_hook_and_poll_paths_now_compare_equal(self):
        # The bug, reproduced: a hook-sourced game-start (escaped path) and a
        # poll-sourced game (clean path) for the SAME file must compare equal.
        hook_ev = esevents.interpret(GAME_START, [self.OBSERVED_ESCAPED])
        poll_game = Game("gb", self.CLEAN, "Adventures of Rocky and Bullwinkle")
        self.assertEqual(hook_ev.rom_path, poll_game.rom_path)
        self.assertEqual(esevents.game_key(poll_game), (hook_ev.system, hook_ev.rom_path))

    def test_batocera_layout_with_escaped_basename_and_name(self):
        # (rompath, basename, name) - basename and name can carry the same
        # escaping; normalising the whole arg list keeps them consistent too.
        ev = esevents.interpret(GAME_START, [
            self.OBSERVED_ESCAPED,
            r"Adventures\ of\ Rocky\ and\ Bullwinkle\ and\ Friends,\ The\ \(USA\).zip",
            r"Adventures\ of\ Rocky\ and\ Bullwinkle"])
        self.assertEqual(ev.rom_path, self.CLEAN)
        self.assertEqual(ev.name, "Adventures of Rocky and Bullwinkle")

    def test_game_selected_is_not_touched(self):
        # Only game-start/game-end are documented as arriving pre-escaped;
        # game-selected must pass its value through unchanged (a literal
        # backslash is not expected there, but this pins the scope of the fix).
        ev = esevents.interpret(GAME_SELECTED, ["gb", r"/storage/roms/gb/x\ y.gb", "x y"])
        self.assertEqual(ev.rom_path, r"/storage/roms/gb/x\ y.gb")

    def test_a_literal_backslash_free_path_is_unchanged(self):
        ev = esevents.interpret(GAME_END, ["/storage/roms/n64/Mario.z64"])
        self.assertEqual(ev.rom_path, "/storage/roms/n64/Mario.z64")

    def test_helper_is_a_no_op_without_backslashes(self):
        self.assertEqual(esevents.unescape_shell_backslashes("plain"), "plain")
        self.assertIsNone(esevents.unescape_shell_backslashes(None))


# ---------------------------------------------------------------------------
# The /runningGame poll (RV1-M2 / RV2-M2)
# ---------------------------------------------------------------------------
class TestRunningPoll(unittest.TestCase):
    def test_transitions(self):
        p = esevents.RunningPoll()
        self.assertEqual(p.feed(NOT_RUNNING), [])
        a = Game("snes", "/r/snes/a.sfc", "A")
        evs = p.feed(a)
        self.assertEqual([e.kind for e in evs], [GAME_START])
        self.assertIs(evs[0].game, a)
        self.assertEqual(evs[0].source, "poll")
        self.assertEqual(p.feed(Game("snes", "/r/snes/a.sfc", "A")), [])      # same game
        b = Game("gb", "/r/gb/b.gb", "B")
        self.assertEqual([e.kind for e in p.feed(b)], [GAME_END, GAME_START])
        self.assertEqual(p.feed(NOT_RUNNING), [])                            # 1 of 2
        evs = p.feed(NOT_RUNNING)
        self.assertEqual([(e.kind, e.rom_path) for e in evs], [(GAME_END, "/r/gb/b.gb")])
        self.assertEqual(p.feed(NOT_RUNNING), [])

    def test_a_failed_poll_is_unknown_never_not_running(self):
        p = esevents.RunningPoll()
        p.feed(Game("psx", "/r/psx/a.chd"))
        for bad in (None, UNKNOWN, None, UNKNOWN, None):          # ES slow for 15 s
            self.assertEqual(p.feed(bad), [], bad)
        self.assertEqual(p.key, ("psx", "/r/psx/a.chd"))
        self.assertEqual(p.feed(Game("psx", "/r/psx/a.chd")), [])    # still the same game

    def test_the_clean_answers_must_be_consecutive(self):
        p = esevents.RunningPoll(end_after=2)
        p.feed(Game("psx", "/r/psx/a.chd"))
        self.assertEqual(p.feed(NOT_RUNNING), [])
        self.assertEqual(p.feed(UNKNOWN), [])            # breaks the run
        self.assertEqual(p.feed(NOT_RUNNING), [])
        self.assertEqual([e.kind for e in p.feed(NOT_RUNNING)], [GAME_END])

    def test_end_after_three(self):
        p = esevents.RunningPoll(end_after=3)
        p.feed(Game("psx", "/r/psx/a.chd"))
        self.assertEqual(p.feed(NOT_RUNNING) + p.feed(NOT_RUNNING), [])
        self.assertEqual([e.kind for e in p.feed(NOT_RUNNING)], [GAME_END])


class FakeClient:
    """es_api.ESClient's transport, scripted: each _get returns the next item."""
    base_url = "http://127.0.0.1:1234"

    def __init__(self, answers):
        self.answers = list(answers)
        self.paths = []

    def _get(self, path):
        self.paths.append(path)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def game_detail(self, system, gid):
        return None


class TestProbe(unittest.TestCase):
    def test_probe_tells_failure_from_nothing_running(self):
        import es_api
        game = {"id": "7", "name": "Alpha", "path": "/storage/roms/gb/Alpha.gb",
                "systemName": "gb"}
        c = FakeClient([None, {"msg": "NO GAME RUNNING"}, game, {"msg": "Weird"}, "text",
                        RuntimeError("boom")])
        self.assertIs(esevents.probe_running_game(c), UNKNOWN)           # timeout / refused
        self.assertIs(esevents.probe_running_game(c), NOT_RUNNING)
        g = esevents.probe_running_game(c)
        self.assertIsInstance(g, es_api.Game)
        self.assertEqual(g.name, "Alpha")
        self.assertIs(esevents.probe_running_game(c), UNKNOWN)           # unknown message
        self.assertIs(esevents.probe_running_game(c), UNKNOWN)           # not a dict
        self.assertIs(esevents.probe_running_game(c), UNKNOWN)           # raised
        self.assertEqual(c.paths, ["/runningGame"] * 6)

    # test_real_fixture_answer_is_not_running removed: depended on
    # tests/fixtures/es-runningGame-real-capture-2026-09-23.json, dropped
    # from the public release (see DROPPED-FIXTURES-list.txt).

    def test_watcher_upgrades_es_api_running_game_to_the_probe(self):
        import es_api
        w = esevents.Watcher(lambda e: None, spool_dir=tempfile.gettempdir(),
                             running_game=es_api.running_game)
        self.assertIs(w.running_probe, esevents.probe_running_game)
        w.stop()
        other = lambda: None                                            # noqa: E731
        w = esevents.Watcher(lambda e: None, spool_dir=tempfile.gettempdir(),
                             running_game=other)
        self.assertIs(w.running_probe, other)                           # None -> UNKNOWN
        w.stop()


# ---------------------------------------------------------------------------
# The spool
# ---------------------------------------------------------------------------
class TestSpoolNames(unittest.TestCase):
    def test_key_parsing_and_formatting(self):
        self.assertEqual(esevents.spool_key("000000008453-0000000408-0000000415.ev"),
                         (8453, 408, 415))
        self.assertEqual(esevents.spool_name((8453, 408, 415)),
                         "000000008453-0000000408-0000000415.ev")
        for bad in (".tmp.123", "es-event", "1-2.ev", "a-b-c.ev", "1-2-3.ev.tmp", ""):
            self.assertIsNone(esevents.spool_key(bad), bad)

    def test_text_order_is_numeric_order(self):
        keys = [(9, 5, 1), (10, 1, 1), (10, 1, 2), (100, 3, 3), (10, 20, 0)]
        names = [esevents.spool_name(k) for k in keys]
        self.assertEqual([esevents.spool_key(n) for n in sorted(names)], sorted(keys))

    def test_spool_dir_path(self):
        self.assertEqual(esevents.spool_dir_path({}), "/var/run/rp5deck/es-events")
        self.assertEqual(esevents.spool_dir_path({"RP5DECK_ES_SPOOL": "/x/y"}), "/x/y")
        self.assertEqual(esevents.spool_dir_path({"RP5DECK_ES_EVENT_FILE": "/t/run/es-event"}),
                         os.path.join("/t/run", "es-events"))


class SpoolCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(prefix="rp5deck-spool-"), "es-events")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.dir), True)
        self.logged = []
        self.r = esevents.SpoolReader(self.dir, max_spool=16, log_fn=self.logged.append)

    def put(self, key, kind, *args):
        return esevents.write_spool_event(self.dir, key, kind, list(args))

    def sel(self, key, n):
        return self.put(key, GAME_SELECTED, "gb", "/r/gb/%s.gb" % n, n)


class TestSpoolReader(SpoolCase):
    def test_every_edge_is_delivered_in_fire_order_and_files_are_deleted(self):
        self.put((5, 1, 1), GAME_END, "/r/gb/a.gb")
        self.put((3, 1, 1), GAME_START, "/r/gb/a.gb")
        self.put((7, 1, 1), GAME_START, "/r/gb/b.gb")
        evs = self.r.drain()
        self.assertEqual([(e.kind, e.rom_path) for e in evs],
                         [(GAME_START, "/r/gb/a.gb"), (GAME_END, "/r/gb/a.gb"),
                          (GAME_START, "/r/gb/b.gb")])
        self.assertEqual([e.seq for e in evs], [(3, 1, 1), (5, 1, 1), (7, 1, 1)])
        self.assertEqual(os.listdir(self.dir), [])

    def test_only_the_newest_selection_of_a_batch_is_delivered(self):
        for i in range(10):
            self.sel((100 + i, 9, i), "g%d" % i)
        self.put((104, 9, 99), GAME_START, "/r/gb/g4.gb")
        evs = self.r.drain()
        self.assertEqual([(e.kind, e.name) for e in evs],
                         [(GAME_START, "g4"), (GAME_SELECTED, "g9")])
        self.assertEqual(self.r.superseded, 9)

    def test_a_late_hook_never_brings_back_a_stale_selection(self):
        # ES fired A then B; B's hook finished first and was consumed; A's
        # file lands afterwards. The companion must stay on B.
        self.sel((200, 50, 2), "B")
        self.assertEqual([e.name for e in self.r.drain()], ["B"])
        self.sel((199, 49, 1), "A")
        self.assertEqual(self.r.drain(), [])
        self.assertEqual(self.r.stale, 1)
        self.assertEqual(os.listdir(self.dir), [])
        self.sel((201, 51, 3), "C")
        self.assertEqual([e.name for e in self.r.drain()], ["C"])

    def test_staleness_is_per_class(self):
        self.sel((300, 1, 1), "B")
        self.r.drain()
        self.put((299, 1, 1), GAME_START, "/r/gb/x.gb")      # older, but an edge: delivered
        self.assertEqual([e.kind for e in self.r.drain()], [GAME_START])
        self.put((298, 1, 1), GAME_END, "/r/gb/w.gb")        # older than that edge: stale
        self.assertEqual(self.r.drain(), [])

    def test_idle_events_keep_the_newest_state(self):
        self.put((10, 1, 1), SCREENSAVER_START)
        self.put((11, 1, 1), SCREENSAVER_STOP)
        self.assertEqual([e.kind for e in self.r.drain()], [SCREENSAVER_STOP])
        self.put((9, 1, 1), SCREENSAVER_START)               # late: stale
        self.assertEqual(self.r.drain(), [])
        self.put((12, 1, 1), SLEEP)
        self.assertEqual([e.kind for e in self.r.drain()], [SLEEP])

    def test_the_spool_is_bounded_and_the_overflow_logged(self):
        for i in range(40):
            self.put((1000 + i, 1, i), GAME_END, "/r/gb/%d.gb" % i)
        evs = self.r.drain()
        self.assertEqual(len(evs), 16)
        self.assertEqual(evs[0].rom_path, "/r/gb/24.gb")     # the 24 oldest were dropped
        self.assertEqual(self.r.overflow, 24)
        self.assertTrue(any("dropped the 24 oldest" in m for m in self.logged), self.logged)
        self.assertEqual(os.listdir(self.dir), [])

    def test_garbage_and_foreign_names(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, esevents.spool_name((5, 5, 5))), "wb") as f:
            f.write(b"not a record")
        with open(os.path.join(self.dir, ".tmp.4242"), "wb") as f:
            f.write(build_record(GAME_END, ["/r/gb/x.gb"]))   # a hook still writing
        with open(os.path.join(self.dir, "README"), "w") as f:
            f.write("x")
        self.sel((6, 6, 6), "ok")
        self.assertEqual([e.name for e in self.r.drain()], ["ok"])
        self.assertEqual(self.r.bad, 1)
        self.assertEqual(sorted(os.listdir(self.dir)), [".tmp.4242", "README"])

    def test_missing_directory_is_empty(self):
        r = esevents.SpoolReader(os.path.join(self.dir, "nope"))
        self.assertEqual(r.drain(), [])


# ---------------------------------------------------------------------------
# inotify filter
# ---------------------------------------------------------------------------
class TestInotifyFilter(unittest.TestCase):
    def test_only_spool_names(self):
        n = "000000000001-0000000002-0000000003.ev"
        self.assertTrue(esevents.is_spool_event(esevents.IN_MOVED_TO, n))
        self.assertTrue(esevents.is_spool_event(esevents.IN_CLOSE_WRITE, n))
        self.assertFalse(esevents.is_spool_event(esevents.IN_CLOSE_WRITE, ".tmp.1234"))
        self.assertFalse(esevents.is_spool_event(0x100, n))                   # IN_CREATE
        self.assertTrue(esevents.is_spool_event(esevents.IN_Q_OVERFLOW, ""))  # rescan

    def test_parse_raw_events(self):
        import struct
        name = b"es-event\0\0\0\0\0\0\0\0"
        buf = struct.pack("iIII", 1, esevents.IN_MOVED_TO, 7, len(name)) + name
        buf += struct.pack("iIII", 1, 8, 0, 0)
        self.assertEqual(esevents.parse_inotify_events(buf + b"\x01\x02"),
                         [(1, esevents.IN_MOVED_TO, 7, "es-event"), (1, 8, 0, "")])


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------
class WatcherBase:
    force_stat = False

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="rp5deck-esev-")
        self.dir = os.path.join(self.base, "rp5deck", "es-events")
        self.got = []
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def put(self, kind, args):
        self.n += 1
        return esevents.write_spool_event(self.dir, (self.n, 1, self.n), kind, args)

    def make(self, **kw):
        kw.setdefault("rescan", 0.5)
        w = esevents.Watcher(self.got.append, spool_dir=self.dir, stat_poll=0.03, **kw)
        if self.force_stat:
            orig = esevents._Inotify.create
            esevents._Inotify.create = classmethod(lambda cls, d: None)
            try:
                w.start()
            finally:
                esevents._Inotify.create = orig
        else:
            w.start()
        self.addCleanup(w.stop)
        return w

    def test_delivers_each_event_and_creates_the_directory(self):
        w = self.make()
        self.assertTrue(os.path.isdir(self.dir))
        time.sleep(0.05)
        self.put(GAME_SELECTED, ["gb", "/r/gb/a.gb", "A"])
        self.assertTrue(wait_for(lambda: len(self.got) >= 1))
        time.sleep(0.05)
        self.put(SYSTEM_SELECTED, ["snes", "", ""])
        self.assertTrue(wait_for(lambda: len(self.got) >= 2))
        self.assertEqual([e.kind for e in self.got[:2]], [GAME_SELECTED, SYSTEM_SELECTED])
        self.assertEqual(self.got[0].name, "A")
        self.assertEqual(w.events_seen, len(self.got))
        self.assertTrue(wait_for(lambda: os.listdir(self.dir) == []))

    def test_a_burst_loses_no_edge(self):
        self.make()
        time.sleep(0.05)
        for i in range(30):
            self.put(GAME_SELECTED, ["gb", "/r/gb/%d.gb" % i, str(i)])
            if i == 10:
                self.put(GAME_START, ["/r/gb/10.gb"])
            if i == 20:
                self.put(GAME_END, ["/r/gb/10.gb"])
        self.assertTrue(wait_for(lambda: any(e.name == "29" for e in self.got), 5))
        kinds = [e.kind for e in self.got]
        self.assertIn(GAME_START, kinds)
        self.assertIn(GAME_END, kinds)
        self.assertLess(kinds.index(GAME_START), kinds.index(GAME_END))
        self.assertEqual(self.got[-1].name, "29")
        seqs = [e.seq for e in self.got]
        self.assertEqual(seqs, sorted(seqs))

    def test_garbage_record_is_ignored_and_the_next_good_one_arrives(self):
        self.make()
        time.sleep(0.05)
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, ".tmp.x")
        with open(tmp, "wb") as f:
            f.write(b"not a record at all")
        os.replace(tmp, os.path.join(self.dir, esevents.spool_name((0, 0, 1))))
        time.sleep(0.15)
        self.put(GAME_END, ["/r/n64/x.z64"])
        self.assertTrue(wait_for(lambda: len(self.got) >= 1))
        self.assertEqual([e.kind for e in self.got], [GAME_END])

    def test_reads_the_existing_spool_at_start(self):
        self.put(GAME_SELECTED, ["nes", "/r/nes/y.nes", "Y"])
        self.put(GAME_SELECTED, ["nes", "/r/nes/z.nes", "Z"])
        self.make()
        self.assertTrue(wait_for(lambda: len(self.got) >= 1))
        time.sleep(0.1)
        self.assertEqual([e.rom_path for e in self.got], ["/r/nes/z.nes"])

    def test_stop_is_prompt_and_leaves_no_thread(self):
        w = self.make()
        t0 = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(any(t.is_alive() for t in w._threads))

    def test_running_game_poll_emits_start_and_end(self):
        seq = [NOT_RUNNING, Game("psx", "/r/psx/a.chd", "A"), Game("psx", "/r/psx/a.chd", "A"),
               NOT_RUNNING, NOT_RUNNING]
        calls = []

        def probe():
            calls.append(1)
            return seq[min(len(calls) - 1, len(seq) - 1)]
        self.make(running_probe=probe, poll_interval=0.02)
        self.assertTrue(wait_for(lambda: len(self.got) >= 2))
        self.assertEqual([(e.kind, e.source) for e in self.got[:2]],
                         [(GAME_START, "poll"), (GAME_END, "poll")])

    def test_a_timing_out_poll_never_ends_the_game(self):
        # the old interface: None from running_game() - a timeout looks the same
        seq = [Game("psx", "/r/psx/a.chd", "A"), None, None, None, None, None]
        calls = []

        def rg():
            calls.append(1)
            return seq[min(len(calls) - 1, len(seq) - 1)]
        self.make(running_game=rg, poll_interval=0.01)
        self.assertTrue(wait_for(lambda: len(calls) >= 8))
        self.assertEqual([e.kind for e in self.got], [GAME_START])

    def test_a_raising_consumer_does_not_kill_the_watcher(self):
        boom = [0]

        def on(ev):
            boom[0] += 1
            raise RuntimeError("consumer bug")
        w = esevents.Watcher(on, spool_dir=self.dir, stat_poll=0.03, rescan=0.5)
        w.start()
        self.addCleanup(w.stop)
        with self.assertLogs("rp5deck.esevents", "ERROR") as cm:
            for i in range(2):
                time.sleep(0.06)
                self.put(GAME_END, ["/r/gb/%d.gb" % i])
            self.assertTrue(wait_for(lambda: boom[0] >= 2))
        self.assertIn("on_event failed", cm.output[0])


class TestWatcherStatMode(WatcherBase, unittest.TestCase):
    force_stat = True

    def test_mode(self):
        w = self.make()
        self.assertEqual(w.mode, "stat")


@unittest.skipUnless(LINUX, "inotify needs a Linux kernel (run under WSL / on the device)")
class TestWatcherInotifyMode(WatcherBase, unittest.TestCase):
    def test_mode(self):
        w = self.make()
        self.assertEqual(w.mode, "inotify")

    def test_temp_file_alone_does_not_fire(self):
        self.make(rescan=30)
        time.sleep(0.05)
        with open(os.path.join(self.dir, ".tmp.99"), "wb") as f:
            f.write(build_record(GAME_END, ["/r/gb/x.gb"]))
        time.sleep(0.2)
        self.assertEqual(self.got, [])


# ---------------------------------------------------------------------------
# The real hook scripts (Linux)
# ---------------------------------------------------------------------------
def shells():
    out = []
    for name in ("sh", "dash", "bash", "busybox"):
        p = shutil.which(name)
        if p and os.path.realpath(p) not in [os.path.realpath(q) for _, q in out]:
            out.append((name, p))
    return out


def hook(event):
    return os.path.join(HOOKS, "rp5deck-%s.sh" % event)


class _RawInotify:
    """A broader-mask inotify than esevents' (IN_CREATE/MODIFY/CLOSE_WRITE/
    MOVED_TO/OPEN), to see HOW the event files come into being."""
    MASK = 0x100 | 0x2 | 0x8 | 0x80 | 0x20

    def __init__(self, d):
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK)
        assert self.libc.inotify_add_watch(self.fd, os.fsencode(d), self.MASK) >= 0

    def events(self):
        out = []
        while True:
            try:
                buf = os.read(self.fd, 65536)
            except BlockingIOError:
                return out
            out.extend(esevents.parse_inotify_events(buf))

    def close(self):
        os.close(self.fd)


def es_fire(script, args, env, logdir):
    """Fire one hook EXACTLY the way ES does on Linux: executeScript()'s
    command string (name_guard.es_command_line), Platform.cpp's wrapper, and
    its double fork with execl("/bin/sh", "sh", "-c", ...): ES waits only for
    the intermediate child, never for the hook."""
    cmd = name_guard.es_wrapped(name_guard.es_command_line(script, args), logdir)
    envb = {os.fsencode(k): os.fsencode(v) for k, v in env.items()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)     # fork with threads: exec at once
        pid = os.fork()
        if pid == 0:
            try:
                if os.fork() == 0:
                    os.execve(b"/bin/sh", [b"sh", b"-c", os.fsencode(cmd)], envb)
            finally:
                os._exit(0)
    os.waitpid(pid, 0)


@unittest.skipUnless(LINUX and shutil.which("sh"), "hooks run under a POSIX shell on Linux")
class TestHookScripts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rp5deck-hook-")
        self.sp = os.path.join(self.dir, "run", "es-events")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self, spool=None):
        return dict(os.environ, RP5DECK_ES_SPOOL=spool or self.sp)

    def run_hook(self, event, args, shell="sh", spool=None):
        cmd = ([shell, "sh"] if os.path.basename(shell) == "busybox" else [shell]) + \
              [hook(event)] + list(args)
        return subprocess.run(cmd, env=self.env(spool), capture_output=True, timeout=10)

    def spooled(self):
        return esevents.list_spool(self.sp)

    def take(self):
        """The one event file written since the last take(), removed."""
        files = self.spooled()
        self.assertEqual(len(files), 1, files)
        p = os.path.join(self.sp, files[0][1])
        with open(p, "rb") as f:
            data = f.read()
        os.remove(p)
        return data

    def test_the_hooks_differ_only_in_their_event_line(self):
        bodies = {}
        for ev in esevents.EVENTS:
            with open(hook(ev), "rb") as f:
                data = f.read()
            self.assertNotIn(b"\r", data, ev)              # CRLF would break /bin/sh
            self.assertTrue(os.access(hook(ev), os.X_OK), ev)
            lines = data.split(b"\n")
            self.assertIn(b"EVENT=%s" % ev.encode(), lines)
            bodies[ev] = b"\n".join(l for l in lines if not l.startswith(b"EVENT=")
                                    and not l.startswith(b"# rp5deck ES event hook:"))
        self.assertEqual(len(bodies), 8)
        self.assertEqual(len(set(bodies.values())), 1)

    def test_no_hook_claims_es_runs_it_synchronously(self):
        for ev in esevents.EVENTS:
            with open(hook(ev)) as f:
                text = f.read()
            self.assertNotRegex(text, r"(?<![A-Z])SYNCHRONOUSLY")
            self.assertIn("ASYNCHRONOUSLY", text)

    def test_every_shell_writes_every_nasty_name_byte_exact(self):
        tried = 0
        for sname, sh in shells():
            for name in NASTY:
                rom = "/storage/roms/megadrive/%s.md" % name
                r = self.run_hook(GAME_SELECTED, ["megadrive", rom, name], shell=sh)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""), (sname, name))
                data = self.take()
                self.assertEqual(data, build_record(GAME_SELECTED, ["megadrive", rom, name]),
                                 (sname, name))
                self.assertEqual(parse_record(data).args, ("megadrive", rom, name))
                tried += 1
        self.assertGreaterEqual(tried, len(NASTY))
        print("\n  hook shells tried: %s" % ", ".join(n for n, _ in shells()), end=" ")

    def test_invalid_utf8_argument_is_byte_exact(self):
        raw = b"/storage/roms/nes/caf\xe9 \xff.nes"
        r = subprocess.run([b"sh", os.fsencode(hook(GAME_SELECTED)), b"nes", raw, b"n"],
                           env=self.env(), capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        ev = parse_record(self.take())
        self.assertEqual(os.fsencode(ev.rom_path), raw)

    def test_every_event_and_arg_count(self):
        for ev in esevents.EVENTS:
            for args in ([], ["one"], ["gb", "", ""], ["/storage/roms/gb/a b.gb", "a b.gb", "A B"]):
                r = self.run_hook(ev, args)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(self.take(), build_record(ev, args), (ev, args))

    def test_one_file_per_event_and_no_temp_file_left_behind(self):
        for i in range(20):
            self.run_hook(GAME_SELECTED, ["gb", "/r/gb/%d.gb" % i, str(i)])
        names = sorted(os.listdir(self.sp))
        self.assertEqual(len(names), 20)
        self.assertTrue(all(esevents.spool_key(n) for n in names), names)
        got = [parse_record(open(os.path.join(self.sp, n), "rb").read()).name for n in names]
        self.assertEqual(got, [str(i) for i in range(20)])        # sequential: in order

    def test_the_legacy_record_variable_still_locates_the_spool(self):
        env = dict(os.environ, RP5DECK_ES_EVENT_FILE=os.path.join(self.dir, "legacy", "es-event"))
        env.pop("RP5DECK_ES_SPOOL", None)
        subprocess.run(["sh", hook(GAME_END), "/r/gb/a.gb"], env=env, timeout=10)
        self.assertEqual(len(esevents.list_spool(os.path.join(self.dir, "legacy", "es-events"))), 1)

    def test_failure_is_silent_and_exit_zero(self):
        # a directory that can never be created (a path under a regular FILE)
        blocker = os.path.join(self.dir, "iam-a-file")
        with open(blocker, "w") as f:
            f.write("x")
        r = self.run_hook(GAME_SELECTED, ["gb", "/r/gb/a.gb", "A"],
                          spool=os.path.join(blocker, "sub", "es-events"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_written_atomically_each_file_only_ever_appears_by_rename(self):
        os.makedirs(self.sp)
        ino = _RawInotify(self.sp)
        self.addCleanup(ino.close)
        for i in range(10):
            self.run_hook(GAME_SELECTED, ["gb", "/r/gb/%d.gb" % i, "Name's & %d" % i])
        evs = [(m, n) for _, m, _, n in ino.events()]
        on_final = [(m, n) for m, n in evs if esevents.spool_key(n)]
        self.assertEqual(len({n for _, n in on_final}), 10, evs)
        self.assertTrue(all(m == esevents.IN_MOVED_TO for m, _ in on_final),
                        "an event file was opened / modified in place: %r" % evs)
        self.assertTrue(any(n.startswith(".tmp.") for m, n in evs))   # via a temp file

    def test_concurrent_reader_never_sees_a_partial_record(self):
        os.makedirs(self.sp)
        stop = threading.Event()
        bad, reads = [], [0]

        def reader():
            while not stop.is_set():
                for _, n in esevents.list_spool(self.sp):
                    try:
                        with open(os.path.join(self.sp, n), "rb") as f:
                            data = f.read()
                    except FileNotFoundError:
                        continue
                    reads[0] += 1
                    if parse_record(data) is None:
                        bad.append(data)
        t = threading.Thread(target=reader)
        t.start()
        try:
            long_name = "Long Name 'x' & y " * 200
            for i in range(60):
                self.run_hook(GAME_SELECTED, ["gb", "/r/gb/%d.gb" % i, long_name + str(i)])
        finally:
            stop.set()
            t.join()
        self.assertGreater(reads[0], 60)
        self.assertEqual(bad, [])

    def test_the_hook_bounds_the_spool_when_nothing_consumes_it(self):
        os.makedirs(self.sp)
        for i in range(515):
            with open(os.path.join(self.sp, esevents.spool_name((1, 1, i))), "wb") as f:
                f.write(build_record(GAME_END, ["/r/gb/%d.gb" % i]))
        r = self.run_hook(GAME_SELECTED, ["gb", "/r/gb/new.gb", "new"])
        self.assertEqual(r.returncode, 0)
        files = self.spooled()
        self.assertEqual(len(files), 256)
        self.assertEqual(files[0][0], (1, 1, 260))            # the oldest were dropped
        newest = parse_record(open(os.path.join(self.sp, files[-1][1]), "rb").read())
        self.assertEqual(newest.name, "new")

    def test_hook_is_fast(self):
        n = 30
        t0 = time.monotonic()
        for i in range(n):
            self.run_hook(GAME_SELECTED, ["gb", "/r/gb/%d.gb" % i, str(i)])
        per = (time.monotonic() - t0) / n
        print("\n  hook: %.1f ms per call including process start" % (per * 1000), end=" ")
        self.assertLess(per, 0.25)


@unittest.skipUnless(LINUX and os.path.exists("/bin/sh") and hasattr(os, "fork"),
                     "fires hooks the way ES does: fork + execl /bin/sh -c")
class TestFiredLikeES(unittest.TestCase):
    """RV2-M3 / RV1-m1: ES launches each hook asynchronously, so bursts
    overlap. Every event must survive, and sort in the order ES fired it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rp5deck-esfire-")
        self.sp = os.path.join(self.dir, "run", "es-events")
        self.env = dict(os.environ, RP5DECK_ES_EVENT_FILE=os.path.join(self.dir, "run", "es-event"))
        self.env.pop("RP5DECK_ES_SPOOL", None)
        os.makedirs(os.path.join(self.dir, "logs"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fire(self, event, args):
        es_fire(hook(event), args, self.env, os.path.join(self.dir, "logs"))

    def test_a_burst_of_overlapping_hooks_loses_nothing_and_keeps_fire_order(self):
        fired = []
        for i in range(40):
            if i == 17:
                self.fire(GAME_START, ["/storage/roms/gb/Game 17.gb", "Game 17.gb", "Game 17"])
                fired.append(("start", "Game 17"))
            if i == 29:
                self.fire(GAME_END, ["/storage/roms/gb/Game 17.gb", "Game 17.gb", "Game 17"])
                fired.append(("end", "Game 17"))
            self.fire(GAME_SELECTED, ["gb", "/storage/roms/gb/Game %02d.gb" % i, "Game %02d" % i])
            fired.append(("sel", "Game %02d" % i))
        self.assertTrue(wait_for(lambda: len(esevents.list_spool(self.sp)) >= len(fired), 20),
                        "only %d of %d events arrived" % (len(esevents.list_spool(self.sp)),
                                                          len(fired)))
        time.sleep(0.2)
        files = esevents.list_spool(self.sp)
        self.assertEqual(len(files), len(fired))
        short = {GAME_SELECTED: "sel", GAME_START: "start", GAME_END: "end"}
        got = []
        for key, name in files:
            ev = esevents.read_record(os.path.join(self.sp, name))
            got.append((short[ev.kind], ev.name))
        self.assertEqual(got, fired)
        # the key is ES's `sh -c` process (the anchor), not the hook's own pid
        self.assertTrue(all(k[1] != k[2] for k, _ in files), files[:3])
        self.assertEqual(len({k[1] for k, _ in files}), len(files))

    def test_the_watcher_ends_on_the_last_selection(self):
        got = []
        w = esevents.Watcher(got.append, spool_dir=self.sp, stat_poll=0.02, rescan=0.3)
        w.start()
        self.addCleanup(w.stop)
        for i in range(25):
            self.fire(GAME_SELECTED, ["gb", "/storage/roms/gb/G%02d.gb" % i, "G%02d" % i])
            if i == 12:
                self.fire(GAME_START, ["/storage/roms/gb/G12.gb", "G12.gb", "G12"])
        self.assertTrue(wait_for(lambda: got and got[-1].name == "G24", 20),
                        [(e.kind, e.name) for e in got])
        time.sleep(0.3)
        sels = [e for e in got if e.kind == GAME_SELECTED]
        self.assertEqual(sels[-1].name, "G24")
        self.assertEqual([e.seq for e in sels], sorted(e.seq for e in sels))
        self.assertEqual(sum(1 for e in got if e.kind == GAME_START), 1)
        self.assertEqual(esevents.list_spool(self.sp), [])


# ---------------------------------------------------------------------------
# The installer (Linux)
# ---------------------------------------------------------------------------
@unittest.skipUnless(LINUX and shutil.which("sh") and shutil.which("python3"),
                     "installer is a POSIX sh script; its self-test runs python3")
class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-inst-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.scripts = os.path.join(self.d, "scripts")
        self.spool = os.path.join(self.d, "run", "rp5deck", "es-events")
        self.roms = os.path.join(self.d, "roms")
        os.makedirs(os.path.join(self.roms, "gb"))
        open(os.path.join(self.roms, "gb", "Tetris (World).gb"), "w").close()

    def inst(self, *args, shell="sh"):
        return subprocess.run([shell, INSTALLER, "--scripts-dir", self.scripts,
                               "--spool-dir", self.spool, "--rom-root", self.roms] + list(args),
                              capture_output=True, text=True, timeout=120)

    def ours(self):
        out = []
        for p, dirs, fs in os.walk(self.scripts):
            for f in fs + [x for x in dirs if x.startswith(".")]:
                out.append(os.path.relpath(os.path.join(p, f), self.scripts))
        return sorted(out)

    def all_hooks(self):
        return sorted(os.path.join(ev, "rp5deck-%s.sh" % ev) for ev in esevents.EVENTS)

    def test_install_check_remove_into_a_temp_dir(self):
        other = os.path.join(self.scripts, "game-selected", "someone-elses.sh")
        os.makedirs(os.path.dirname(other))
        with open(other, "w") as f:
            f.write("#!/bin/sh\n")
        r = self.inst()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESULT: OK", r.stdout)
        self.assertIn("name guard: scanned", r.stdout)
        self.assertEqual(r.stdout.count("selftest "), 13, r.stdout)
        self.assertEqual(r.stdout.count(": PASS via ES's sh -c quoting"), 13, r.stdout)
        for ev in esevents.EVENTS:
            dst = os.path.join(self.scripts, ev, "rp5deck-%s.sh" % ev)
            self.assertTrue(os.access(dst, os.X_OK), dst)
            with open(dst, "rb") as a, open(hook(ev), "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertNotIn(".rp5deck-stage", " ".join(os.listdir(self.scripts)))
        r = self.inst("--check")
        self.assertEqual(r.stdout.count("installed (matches)"), 8, r.stdout)
        self.assertIn("name guard: OK", r.stdout)
        r = self.inst("--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ours(), [os.path.join("game-selected", "someone-elses.sh")])

    def test_the_sh_port_of_es_quoting_matches_the_python_port(self):
        with open(INSTALLER, encoding="utf-8") as f:
            text = f.read()
        start = text.index("es_cmd() {")
        func = text[start:text.index("\n}\n", start) + 3]
        cases = [["gb", "/r/gb/Tetris.gb", "Tetris"], ["gb", "/r/a b.gb", "A B"],
                 ["snes", "", "x"], ['say "hi" now'], ['"quoted"x'], ['"'], ["a", "b", "c"],
                 ["Jimmy White's Whirlwind Snooker"], ["Ren &amp; Stimpy", "Pokémon (ポケモン)"],
                 ["Ma$ter", "`id`"], ["tab\there"], []]
        for script in ("/s/h.sh", "/my scripts/h.sh"):
            for args in cases:
                sh = func + '\nes_cmd "$@"\nprintf "%s" "$ES_CMD"\n'
                r = subprocess.run(["sh", "-c", sh, "sh", script] + args, capture_output=True,
                                   timeout=10)
                self.assertEqual(r.stdout.decode("utf-8"),
                                 name_guard.es_command_line(script, args), (script, args))

    def test_the_self_test_has_no_dangerous_name_of_its_own(self):
        with open(INSTALLER, encoding="utf-8") as f:
            text = f.read()
        start = text.index("# -- self-test")
        body = text[start:text.index("PYEOF", start)]
        self.assertNotIn("`x`", body)
        self.assertNotIn("$HOME", body)
        for v in ("Jimmy White's Whirlwind Snooker", "Ren &amp; Stimpy Show Presents",
                  "/storage/roms/snes/Ren & Stimpy Show, The (USA).sfc",
                  "Pokémon Edición Azul (ポケモン)", "/storage/roms/gb/Pokémon Azul.gb"):
            self.assertIn(v.split("/")[-1], body)
            self.assertEqual(name_guard.value_reasons(v), [], v)     # safe by the guard

    def test_refuses_on_dangerous_names_and_leaves_no_hook(self):
        r = self.inst()                                       # a previous good install
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        open(os.path.join(self.roms, "gb", "Tom&Jerry.gb"), "w").close()
        with open(os.path.join(self.roms, "gb", "gamelist.xml"), "w") as f:
            f.write("<gameList><game><path>./a.gb</path><name>Super $(reboot) Game</name>"
                    "</game></gameList>")
        r = self.inst()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("RESULT: REFUSED", r.stdout)
        self.assertIn("Tom&Jerry.gb", r.stdout)
        self.assertIn("Super $(reboot) Game", r.stdout)
        self.assertEqual(self.ours(), [])                     # nothing left behind
        r = self.inst("--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--force given", r.stdout)
        self.assertIn("Tom&Jerry.gb", r.stdout)
        self.assertEqual(self.ours(), self.all_hooks())

    def test_refuses_when_the_guard_saw_nothing(self):
        r = subprocess.run(["sh", INSTALLER, "--scripts-dir", self.scripts, "--spool-dir",
                            self.spool, "--rom-root", os.path.join(self.d, "no-such-roms")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("no ROM root", r.stderr)
        self.assertEqual(self.ours(), [])

    def test_a_failing_self_test_rolls_back(self):
        # a hook that prints: the self-test must fail and remove every hook
        broken = os.path.join(self.d, "app")
        shutil.copytree(APP, broken, ignore=shutil.ignore_patterns("__pycache__", "tests",
                                                                   "screenshots", "firefox"))
        p = os.path.join(broken, "es-hooks", "rp5deck-sleep.sh")
        with open(p) as f:
            text = f.read()
        with open(p, "w") as f:
            f.write(text.replace("\nMAX=512\n", "\necho oops\nMAX=512\n", 1))
        r = subprocess.run(["sh", os.path.join(broken, "tools", "install-es-hooks.sh"),
                            "--scripts-dir", self.scripts, "--spool-dir", self.spool,
                            "--rom-root", self.roms], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("selftest sleep [no args]: FAIL", r.stdout)
        self.assertIn("RESULT: FAILED", r.stdout)
        self.assertEqual(self.ours(), [])

    def test_idempotent_and_remove_is_complete(self):
        for _ in range(2):
            r = self.inst()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.ours(), self.all_hooks())
        # leftovers an install / the hooks / the pre-FX-B installer can create
        os.makedirs(os.path.join(self.scripts, ".rp5deck-stage.4242"))
        open(os.path.join(self.scripts, ".rp5deck-stage.4242", "rp5deck-wake.sh"), "w").close()
        open(os.path.join(self.scripts, "game-end", "rp5deck-game-end.sh.tmp.77"), "w").close()
        os.makedirs(self.spool)
        esevents.write_spool_event(self.spool, (1, 2, 3), GAME_END, ["/r/a.gb"])
        open(os.path.join(self.spool, ".tmp.99"), "w").close()
        open(os.path.join(os.path.dirname(self.spool), "es-event"), "w").close()
        for _ in range(2):
            r = self.inst("--remove")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.scripts), os.listdir(self.d))
        self.assertFalse(os.path.exists(os.path.dirname(self.spool)))

    def test_an_interrupted_install_leaves_nothing_es_would_run(self):
        # stop the installer while it runs (SIGTERM at several points)
        for delay in (0.05, 0.15, 0.3):
            shutil.rmtree(self.scripts, ignore_errors=True)
            p = subprocess.Popen(["sh", INSTALLER, "--scripts-dir", self.scripts, "--spool-dir",
                                  self.spool, "--rom-root", self.roms],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(delay)
            p.send_signal(signal.SIGTERM)
            p.wait(60)
            for rel in self.ours():
                self.assertFalse(".tmp" in rel or rel.startswith(".rp5deck-stage"), rel)
                d, f = os.path.split(rel)
                self.assertEqual(f, "rp5deck-%s.sh" % d, rel)    # only complete, named hooks
        self.inst("--remove")
        self.assertEqual(self.ours(), [])

    @unittest.skipUnless(shutil.which("dash"), "dash")
    def test_runs_under_dash_and_without_date_nanoseconds(self):
        # a `date` that prints a literal %N (busybox without FEATURE_DATE_NANO)
        fake = os.path.join(self.d, "bin")
        os.makedirs(fake)
        with open(os.path.join(fake, "date"), "w") as f:
            f.write('#!/bin/sh\necho "1695480001%N"\n')
        os.chmod(os.path.join(fake, "date"), 0o755)
        env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"])
        r = subprocess.run(["dash", INSTALLER, "--scripts-dir", self.scripts, "--spool-dir",
                            self.spool, "--rom-root", self.roms], capture_output=True, text=True,
                           timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESULT: OK", r.stdout)
        self.assertNotIn(" us)", r.stdout)                     # no timing, and no crash


if __name__ == "__main__":
    unittest.main()
