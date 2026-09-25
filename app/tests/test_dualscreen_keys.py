#!/usr/bin/env python3
"""dualscreen_keys.py - the dual-screen settings guard: missing_keys() (pure),
check() (tolerant file read), restore() (privileged fix, every command/clock/
probe seam injected - no real systemctl, no real sysfs, no real ES process)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dualscreen_keys as dk  # noqa: E402
import es_health  # noqa: E402


# ---------------------------------------------------------------------------
# missing_keys() - pure
# ---------------------------------------------------------------------------
class TestMissingKeysPure(unittest.TestCase):
    def test_all_present_base_keys_only(self):
        text = "foo=1\n3ds.screen_layout=5\nwiiu.gamepad_enabled=true\nbar=2\n"
        self.assertEqual(dk.missing_keys(text), [])

    def test_all_absent(self):
        text = "foo=1\nbar=2\n"
        self.assertEqual(dk.missing_keys(text), [dk.KEY_3DS, dk.KEY_WIIU])

    def test_partial(self):
        text = "3ds.screen_layout=5\n"
        self.assertEqual(dk.missing_keys(text), [dk.KEY_WIIU])

    def test_a_changed_value_still_counts_as_present(self):
        # the owner may have edited it by hand through ES - any value counts.
        text = "3ds.screen_layout=1\nwiiu.gamepad_enabled=false\n"
        self.assertEqual(dk.missing_keys(text), [])

    def test_extra_key_only_when_expected(self):
        text = "3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n"
        extra_key = 'nds["Test Game (Synthetic)"].screen_layout'
        self.assertEqual(dk.missing_keys(text, expected_extra=[]), [])
        self.assertEqual(dk.missing_keys(text, expected_extra=[extra_key]), [extra_key])

    def test_extra_key_present_and_expected(self):
        extra_key = 'nds["Test Game (Synthetic)"].screen_layout'
        extra_line = extra_key + "=6"
        text = ("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n" + extra_line + "\n")
        self.assertEqual(dk.missing_keys(text, expected_extra=[extra_key]), [])

    def test_multiple_extra_keys_in_order(self):
        text = "3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n"
        self.assertEqual(dk.missing_keys(text, expected_extra=["extra.a", "extra.b"]),
                         ["extra.a", "extra.b"])

    def test_crlf_line_endings(self):
        text = "3ds.screen_layout=5\r\nwiiu.gamepad_enabled=true\r\n"
        self.assertEqual(dk.missing_keys(text), [])

    def test_no_trailing_newline(self):
        text = "3ds.screen_layout=5\nwiiu.gamepad_enabled=true"
        self.assertEqual(dk.missing_keys(text), [])

    def test_prefix_false_positive_is_not_a_match(self):
        # a longer key that merely starts with an expected one must not
        # count as that key being present.
        text = "3ds.screen_layout_x=1\nwiiu.gamepad_enabled_extra=true\n"
        self.assertEqual(dk.missing_keys(text), [dk.KEY_3DS, dk.KEY_WIIU])

    def test_empty_text(self):
        self.assertEqual(dk.missing_keys(""), [dk.KEY_3DS, dk.KEY_WIIU])


# ---------------------------------------------------------------------------
# check() - tolerant file read
# ---------------------------------------------------------------------------
class TestCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ds-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = os.path.join(self.d, "system.cfg")
        self.roms = os.path.join(self.d, "roms")
        os.makedirs(os.path.join(self.roms, "nds"), exist_ok=True)

    def write(self, text):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_cannot_check(self):
        r = dk.check(cfg_path=os.path.join(self.d, "nope"), roms_dir=self.roms)
        self.assertFalse(r["available"])
        self.assertIsNone(r["missing"])
        self.assertIn("cannot check", r["error"])

    def test_unreadable_directory_cannot_check(self):
        # a directory where a file is expected: OSError on open(), same
        # "cannot check" path as a missing file.
        r = dk.check(cfg_path=self.d, roms_dir=self.roms)
        self.assertFalse(r["available"])
        self.assertIsNone(r["missing"])

    def test_undecodable_bytes_cannot_check_never_full_content_in_error(self):
        with open(self.cfg, "wb") as f:
            f.write(b"\x00\x00\x00binary-looking-data\xff\xfe")
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms)
        self.assertFalse(r["available"])
        self.assertIsNone(r["missing"])
        self.assertNotIn("\x00", r["error"])

    def test_present_file_reports_missing(self):
        self.write("foo=1\n")
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms)
        self.assertTrue(r["available"])
        self.assertEqual(r["missing"], [dk.KEY_3DS, dk.KEY_WIIU])
        self.assertIsNone(r["error"])

    def test_default_extra_key_specs_is_empty(self):
        # public default: no extra_key_specs given, no config.json on disk ->
        # config.load() returns defaults(), whose dualscreen.extra_keys is
        # []. A fresh install checks only the two generic BASE_KEYS.
        self.write("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        with unittest.mock.patch.dict(
                os.environ, {"RP5DECK_CONFIG": os.path.join(self.d, "no-such-config.json")}):
            r = dk.check(cfg_path=self.cfg, roms_dir=self.roms)
        self.assertEqual(r["missing"], [])

    def test_extra_key_with_rom_present_adds_the_key(self):
        # SYNTHETIC game name/path - never the owner's real ROM filename.
        rom_rel = os.path.join("nds", "Test Game (Synthetic).zip")
        extra_key = 'nds["Test Game (Synthetic).zip"].screen_layout'
        rom = os.path.join(self.roms, rom_rel)
        with open(rom, "wb") as f:
            f.write(b"rom")
        self.write("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        specs = [{"key": extra_key, "line": extra_key + "=6", "rom": rom_rel}]
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms, extra_key_specs=specs)
        self.assertEqual(r["missing"], [extra_key])

    def test_extra_key_with_rom_absent_is_not_expected(self):
        extra_key = 'nds["Test Game (Synthetic).zip"].screen_layout'
        self.write("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        specs = [{"key": extra_key, "line": extra_key + "=6",
                 "rom": os.path.join("nds", "Test Game (Synthetic).zip")}]
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms, extra_key_specs=specs)
        self.assertEqual(r["missing"], [])

    def test_extra_key_with_no_rom_is_always_expected(self):
        extra_key = "some.always_expected_key"
        self.write("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        specs = [{"key": extra_key, "line": extra_key + "=1"}]
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms, extra_key_specs=specs)
        self.assertEqual(r["missing"], [extra_key])

    def test_nothing_missing(self):
        self.write("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        r = dk.check(cfg_path=self.cfg, roms_dir=self.roms)
        self.assertEqual(r["missing"], [])


# ---------------------------------------------------------------------------
# check()/restore() reading dualscreen.extra_keys from a real config.json -
# proves the config.py wiring end to end, with a SYNTHETIC game name (never
# the owner's real ROM filename - see upstream-drafts/public-release/
# DEVICE-CONFIG-NOTE.md, gitignored, for the real one).
# ---------------------------------------------------------------------------
class TestConfigDrivenExtraKeys(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ds-cfg-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg_path = os.path.join(self.d, "system.cfg")
        self.roms = os.path.join(self.d, "roms")
        os.makedirs(os.path.join(self.roms, "nds"), exist_ok=True)
        self.config_json = os.path.join(self.d, "config.json")
        self.extra_key = 'nds["Test Game (Synthetic).zip"].screen_layout'
        self.extra_line = self.extra_key + "=6"
        self.rom_rel = os.path.join("nds", "Test Game (Synthetic).zip")

    def write_system_cfg(self, text):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(text)

    def write_config_json(self, extra_keys):
        with open(self.config_json, "w", encoding="utf-8") as f:
            json.dump({"dualscreen": {"extra_keys": extra_keys}}, f)

    def test_configured_synthetic_entry_behaves_like_the_old_hardcoded_one(self):
        # a config listing one entry (rom present) behaves exactly as the
        # old hard-coded per-game check did: missing while absent,
        # present once the exact line is appended.
        self.write_config_json([{"key": self.extra_key, "line": self.extra_line,
                                 "rom": self.rom_rel}])
        with open(os.path.join(self.roms, self.rom_rel), "wb") as f:
            f.write(b"rom")
        self.write_system_cfg("3ds.screen_layout=5\nwiiu.gamepad_enabled=true\n")
        with unittest.mock.patch.dict(os.environ, {"RP5DECK_CONFIG": self.config_json}):
            r = dk.check(cfg_path=self.cfg_path, roms_dir=self.roms)
            self.assertEqual(r["missing"], [self.extra_key])

            res = dk.restore(
                r["missing"], cfg_path=self.cfg_path, backup_dir=os.path.join(self.d, "backups"),
                run=lambda argv, **kw: unittest.mock.Mock(returncode=0, stdout="", stderr=""),
                probe=unittest.mock.Mock(es_pids=lambda: []),
                game_running=lambda: False, clock=lambda: 0.0, sleep=lambda s: None,
                stamp_fn=lambda: "STAMP")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["restored"], [self.extra_key])

            with open(self.cfg_path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn(self.extra_line, text)

            r2 = dk.check(cfg_path=self.cfg_path, roms_dir=self.roms)
            self.assertEqual(r2["missing"], [])


# ---------------------------------------------------------------------------
# restore() - every seam faked
# ---------------------------------------------------------------------------
class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProbe:
    def __init__(self, es_pids_sequence):
        # each call to es_pids() pops the next answer; the last answer
        # repeats once the list is exhausted (a wait that never resolves).
        self.seq = list(es_pids_sequence)

    def es_pids(self):
        if len(self.seq) > 1:
            return self.seq.pop(0)
        return self.seq[0] if self.seq else []


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class RunLog:
    """Records every command; lets a test fail one by argv prefix."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, argv, **kw):
        t = tuple(argv)
        self.calls.append(t)
        if t in self.fail:
            return FakeCompleted(1, stderr="boom")
        return FakeCompleted(0)


class RestoreCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ds-restore-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = os.path.join(self.d, "system.cfg")
        self.backups = os.path.join(self.d, "backups")

    def write(self, data):
        # newline="" on the text path: byte-level assertions below must see
        # exactly what was written, never Windows' own "\n" -> "\r\n" text
        # mode translation on write.
        if isinstance(data, bytes):
            with open(self.cfg, "wb") as f:
                f.write(data)
        else:
            with open(self.cfg, "w", encoding="utf-8", newline="") as f:
                f.write(data)

    def read_bytes(self):
        with open(self.cfg, "rb") as f:
            return f.read()

    def restore(self, missing, run=None, probe=None, game_running=False, backup_dir=None, **kw):
        run = run or RunLog()
        probe = probe or FakeProbe([[]])            # ES already gone by the first check
        clock = Clock()
        return run, dk.restore(
            missing, cfg_path=self.cfg, backup_dir=backup_dir or self.backups, run=run,
            probe=probe, game_running=(lambda: game_running), clock=clock, sleep=clock.sleep,
            stamp_fn=lambda: "STAMP", **kw)


class TestRestoreOrderAndSuccess(RestoreCase):
    def test_full_success_order_and_content(self):
        self.write("existing=1\n")
        run, res = self.restore([dk.KEY_3DS, dk.KEY_WIIU])
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["restored"]), sorted([dk.KEY_3DS, dk.KEY_WIIU]))
        self.assertEqual(run.calls, [dk.CMD_STOP_ES, dk.CMD_CHKSYSCONFIG, dk.CMD_START_ES])
        text = self.read_bytes().decode("utf-8")
        self.assertTrue(text.startswith("existing=1\n"))
        self.assertIn(dk.KEY_LINES[dk.KEY_3DS], text)
        self.assertIn(dk.KEY_LINES[dk.KEY_WIIU], text)

    def test_backup_made_before_the_append_and_matches_pre_edit_content(self):
        self.write("existing=1\n")
        _, res = self.restore([dk.KEY_3DS])
        backup_path = res["backup"]
        self.assertTrue(os.path.exists(backup_path))
        self.assertTrue(os.path.basename(backup_path).startswith(
            "system.cfg.before-keys-restore-"))
        with open(backup_path, "rb") as f:
            self.assertEqual(f.read(), b"existing=1\n")

    def test_only_missing_keys_are_appended(self):
        self.write("3ds.screen_layout=5\n")     # already present
        run, res = self.restore([dk.KEY_3DS, dk.KEY_WIIU])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["restored"], [dk.KEY_WIIU])
        text = self.read_bytes().decode("utf-8")
        self.assertEqual(text.count("3ds.screen_layout="), 1)   # never duplicated

    def test_existing_lines_stay_byte_identical(self):
        original = b"a=1\r\nb=2\nc=weird\x20trailing spaces  \n"
        self.write(original)
        _, res = self.restore([dk.KEY_3DS])
        self.assertTrue(res["ok"], res)
        after = self.read_bytes()
        self.assertTrue(after.startswith(original))

    def test_no_trailing_newline_gets_exactly_one_added(self):
        self.write("existing=1")            # no trailing newline
        _, res = self.restore([dk.KEY_3DS])
        after = self.read_bytes().decode("utf-8")
        self.assertEqual(after, "existing=1\n" + dk.KEY_LINES[dk.KEY_3DS] + "\n")

    def test_nothing_to_restore_is_a_no_op_success(self):
        self.write("existing=1\n")
        run, res = self.restore([])
        self.assertTrue(res["ok"])
        self.assertEqual(run.calls, [])
        self.assertEqual(self.read_bytes(), b"existing=1\n")

    def test_all_requested_keys_already_present_reruns_nothing_but_still_ok(self):
        self.write(dk.KEY_LINES[dk.KEY_3DS] + "\n")
        run, res = self.restore([dk.KEY_3DS])
        self.assertTrue(res["ok"])
        self.assertEqual(res["restored"], [])
        # essway was still stopped/started around the safe no-op re-check.
        self.assertIn(dk.CMD_STOP_ES, run.calls)
        self.assertIn(dk.CMD_START_ES, run.calls)


class TestRestoreRefusals(RestoreCase):
    def test_refuses_while_a_game_is_running_and_touches_nothing(self):
        self.write("existing=1\n")
        run, res = self.restore([dk.KEY_3DS], game_running=True)
        self.assertFalse(res["ok"])
        self.assertIn("game is running", res["detail"])
        self.assertEqual(run.calls, [])                 # not even the stop was attempted
        self.assertEqual(self.read_bytes(), b"existing=1\n")

    def test_refuses_an_unknown_key_name(self):
        run, res = self.restore(["not.a.real.key"])
        self.assertFalse(res["ok"])
        self.assertEqual(run.calls, [])


class TestRestoreFailureHandling(RestoreCase):
    def test_essway_restarted_when_stop_wait_times_out(self):
        self.write("existing=1\n")
        probe = FakeProbe([[1234]])          # ES never goes away
        run = RunLog()
        _, res = self.restore([dk.KEY_3DS], run=run, probe=probe, stop_wait_s=1.0, poll_s=0.5)
        self.assertFalse(res["ok"])
        self.assertIn("did not exit", res["detail"])
        self.assertEqual(run.calls, [dk.CMD_STOP_ES, dk.CMD_START_ES])   # still restarted
        self.assertEqual(self.read_bytes(), b"existing=1\n")             # untouched

    def test_essway_restarted_when_chksysconfig_fails(self):
        self.write("existing=1\n")
        run = RunLog(fail={dk.CMD_CHKSYSCONFIG})
        _, res = self.restore([dk.KEY_3DS], run=run)
        self.assertFalse(res["ok"])
        self.assertIn("chksysconfig", res["detail"])
        self.assertEqual(run.calls, [dk.CMD_STOP_ES, dk.CMD_CHKSYSCONFIG, dk.CMD_START_ES])
        # the append still happened - only chksysconfig's own snapshot failed.
        self.assertIn(dk.KEY_LINES[dk.KEY_3DS], self.read_bytes().decode("utf-8"))

    def test_essway_restarted_when_stop_itself_fails(self):
        self.write("existing=1\n")
        run = RunLog(fail={dk.CMD_STOP_ES})
        _, res = self.restore([dk.KEY_3DS], run=run)
        self.assertFalse(res["ok"])
        self.assertIn("could not stop essway", res["detail"])
        self.assertEqual(self.read_bytes(), b"existing=1\n")

    def test_backup_failure_never_appends(self):
        self.write("existing=1\n")
        # a backup_dir that cannot be created (a file already occupies that
        # path) makes os.makedirs raise -> _backup() returns None.
        blocked = os.path.join(self.d, "blocked")
        with open(blocked, "w") as f:
            f.write("x")
        run, res = self.restore([dk.KEY_3DS], backup_dir=blocked)
        self.assertFalse(res["ok"])
        self.assertIn("back up", res["detail"])
        self.assertEqual(self.read_bytes(), b"existing=1\n")
        self.assertEqual(run.calls, [dk.CMD_STOP_ES, dk.CMD_START_ES])


class TestDefaultGameRunning(unittest.TestCase):
    """restore()'s own default game_running: es_health.game_running() against
    a real (but empty/harmless) Probe - proves the wiring, not es_health
    itself (that module has its own tests)."""

    def test_no_runemu_no_kill_data_is_not_running(self):
        d = tempfile.mkdtemp(prefix="rp5deck-ds-proc-")
        self.addCleanup(shutil.rmtree, d, True)
        probe = es_health.Probe(proc=d, kill_data=os.path.join(d, "no-such-kill-file"))
        with unittest.mock.patch.object(es_health, "Probe", return_value=probe):
            self.assertFalse(dk._default_game_running())


class TestAppendNeverRewrites(unittest.TestCase):
    """_append_lines must open system.cfg for APPEND only: a rewrite ("w")
    truncates first, and a crash in that window leaves the truncated file
    chksysconfig then 'repairs' from a stale backup at the next boot."""

    def test_system_cfg_is_only_read_or_appended(self):
        d = tempfile.mkdtemp(prefix="rp5deck-ds-append-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = os.path.join(d, "system.cfg")
        with open(cfg, "wb") as f:
            f.write(b"system.hostname=ROCKNIX\nglobal.x=1")
        modes = []
        real_open = open

        def spy(path, mode="r", *a, **k):
            if os.path.abspath(str(path)) == os.path.abspath(cfg):
                modes.append(mode)
            return real_open(path, mode, *a, **k)
        with unittest.mock.patch("builtins.open", spy):
            self.assertTrue(dk._append_lines(cfg, [dk.KEY_3DS]))
        self.assertTrue(modes)
        self.assertEqual([m for m in modes if "w" in m or "+" in m], [], modes)
        with open(cfg, "rb") as f:
            self.assertEqual(f.read(), b"system.hostname=ROCKNIX\nglobal.x=1\n"
                             + (dk.KEY_LINES[dk.KEY_3DS] + "\n").encode())


if __name__ == "__main__":
    unittest.main()
