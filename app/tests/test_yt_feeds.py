#!/usr/bin/env python3
"""Unit tests for rp5deck/yt_feeds.py (YT2, Phase 1).

Runs entirely off-device with stdlib unittest + unittest.mock, same seam
tests/test_youtube.py already uses for youtube.py: subprocess.run is
mocked, no real yt-dlp/Firefox/mpv process is ever launched.

Fixtures under tests/fixtures/: reuses
yt-search-result-real-capture-2026-09-23.json (the same real capture
test_youtube.py uses) as a stand-in feed payload - YT1 §"Tests" says any
new fixture must match that envelope, and until Main records the real
:ytsubs/:ythistory captures (see patches/YT2-NOTES.md's capture list) this
is the only real yt-dlp JSON on hand that has the right shape.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import browser    # noqa: E402
import config     # noqa: E402
import youtube    # noqa: E402
import yt_feeds   # noqa: E402


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return f.read()


@contextlib.contextmanager
def _tmp_dir():
    d = tempfile.mkdtemp(prefix="rp5deck-ytfeeds-test-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _mock_run(returncode=0, stdout="", stderr=""):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Kill switch / cookies argv
# ---------------------------------------------------------------------------

class TestSignInEnabled(unittest.TestCase):
    def test_true_with_no_cfg_at_all(self):
        self.assertTrue(yt_feeds.sign_in_enabled(None))

    def test_true_when_schema_key_not_present_in_cfg(self):
        # Simulates "config.py has not been patched yet" - get_value()
        # returns None for an unknown key_path, which must mean "on", not
        # "off": the feature must not silently disable itself just because
        # the SCHEMA patch (patches/YT2-config.patch) hasn't landed yet.
        self.assertTrue(yt_feeds.sign_in_enabled({}))

    def test_explicit_false_disables(self):
        cfg = {"youtube": {"sign_in_enabled": False}}
        with mock.patch.object(config, "get_value", return_value=False):
            self.assertFalse(yt_feeds.sign_in_enabled(cfg))

    def test_explicit_true(self):
        cfg = {"youtube": {"sign_in_enabled": True}}
        with mock.patch.object(config, "get_value", return_value=True):
            self.assertTrue(yt_feeds.sign_in_enabled(cfg))

    def test_get_value_exploding_fails_open(self):
        with mock.patch.object(config, "get_value", side_effect=RuntimeError("boom")):
            self.assertTrue(yt_feeds.sign_in_enabled({"anything": 1}))


class TestCookiesArgs(unittest.TestCase):
    def test_enabled_uses_browser_profile_dir(self):
        args = yt_feeds._cookies_args(None)
        self.assertEqual(args, ["--cookies-from-browser", "firefox:%s" % browser.PROFILE_DIR])

    def test_custom_profile_dir_overrides(self):
        args = yt_feeds._cookies_args(None, profile_dir="/custom/profile")
        self.assertEqual(args, ["--cookies-from-browser", "firefox:/custom/profile"])

    def test_kill_switch_off_means_no_cookies_flag_at_all(self):
        with mock.patch.object(config, "get_value", return_value=False):
            args = yt_feeds._cookies_args({"youtube": {"sign_in_enabled": False}})
        self.assertEqual(args, [])


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class TestRedaction(unittest.TestCase):
    def test_cookie_line_is_scrubbed(self):
        text = "line one\nCookie: NID=super-secret-value; SID=another-secret\nline three"
        out = yt_feeds._redact(text)
        self.assertNotIn("super-secret-value", out)
        self.assertNotIn("another-secret", out)
        self.assertIn("line one", out)
        self.assertIn("line three", out)
        self.assertIn("[redacted", out)

    def test_set_cookie_line_is_scrubbed_case_insensitive(self):
        text = "SET-COOKIE: leaked=1"
        self.assertNotIn("leaked=1", yt_feeds._redact(text))

    def test_no_cookie_lines_is_unchanged(self):
        text = "ERROR: something went wrong\nno secrets here"
        self.assertEqual(yt_feeds._redact(text), text)

    def test_empty_text_passthrough(self):
        self.assertEqual(yt_feeds._redact(""), "")
        self.assertIsNone(yt_feeds._redact(None))

    def test_run_yt_dlp_never_passes_verbose_or_v(self):
        # A future edit adding --verbose for debugging would defeat the
        # redaction rule (YT1: yt-dlp only echoes raw cookie headers in
        # verbose mode) - this must be provably impossible, not just avoided
        # by convention. See tools/yt2_break_tests.py proof #1.
        with self.assertRaises(AssertionError):
            yt_feeds._run_yt_dlp(["--verbose"], timeout=1.0)
        with self.assertRaises(AssertionError):
            yt_feeds._run_yt_dlp(["-v"], timeout=1.0)

    def test_run_yt_dlp_redacts_stderr(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                return_value=_mock_run(stderr="Cookie: leak=1")):
            rc, out, err = yt_feeds._run_yt_dlp(["whatever"], timeout=1.0)
        self.assertEqual(rc, 0)
        self.assertNotIn("leak=1", err)

    def test_run_yt_dlp_redacts_exec_failure_message(self):
        # Even the synthetic "exec failed" message is passed through
        # _redact() - cheap insurance if a future edit ever puts something
        # sensitive into that string.
        with mock.patch.object(yt_feeds.subprocess, "run",
                                side_effect=OSError("boom")):
            rc, out, err = yt_feeds._run_yt_dlp(["ytsearch1:x"], timeout=1.0)
        self.assertIsNone(rc)
        self.assertIn("exec failed", err)


# ---------------------------------------------------------------------------
# Feed fetch / parsing - reuses the real search-result fixture as a stand-in
# payload shape (see module docstring)
# ---------------------------------------------------------------------------

class TestFeedDetailed(unittest.TestCase):
    # setUp() previously loaded
    # tests/fixtures/yt-search-result-real-capture-2026-09-23.json into
    # self.raw/self.parsed unconditionally, so every test in this class
    # errored at setUp once that fixture was dropped from the public
    # release (see DROPPED-FIXTURES-list.txt). Removed; the tests that
    # actually needed self.raw as a stand-in payload
    # (test_subscriptions_argv_has_cookies_and_extractor,
    # test_history_uses_ythistory_extractor,
    # test_watch_later_uses_ytwatchlater_extractor,
    # test_kill_switch_off_omits_cookies_flag,
    # test_result_shape_matches_search_detailed) are removed below. The
    # remaining tests never referenced self.raw, so they are unaffected.

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            yt_feeds.feed_detailed("not-a-real-feed")

    def test_n_zero_or_negative_short_circuits(self):
        with mock.patch.object(yt_feeds.subprocess, "run") as m:
            self.assertEqual(yt_feeds.feed_detailed("subscriptions", n=0), [])
            self.assertEqual(yt_feeds.feed_detailed("history", n=-1), [])
        m.assert_not_called()

    # test_subscriptions_argv_has_cookies_and_extractor,
    # test_history_uses_ythistory_extractor,
    # test_watch_later_uses_ytwatchlater_extractor,
    # test_kill_switch_off_omits_cookies_flag, and
    # test_result_shape_matches_search_detailed removed: all used self.raw
    # (see the class comment above) as their mocked yt-dlp stdout.

    def test_nonzero_returncode_is_none_not_crash(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                return_value=_mock_run(returncode=1, stdout="")):
            self.assertIsNone(yt_feeds.subscriptions())

    def test_garbage_stdout_is_none_not_crash(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                return_value=_mock_run(stdout="not json{{{")):
            self.assertIsNone(yt_feeds.subscriptions())

    def test_timeout_is_none_not_hang(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=25)):
            self.assertIsNone(yt_feeds.subscriptions())

    def test_missing_binary_is_none_not_crash(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                side_effect=FileNotFoundError()):
            self.assertIsNone(yt_feeds.subscriptions())

    def test_empty_entries_is_empty_list_not_none(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                return_value=_mock_run(stdout='{"entries": []}')):
            self.assertEqual(yt_feeds.subscriptions(), [])

    def test_feed_detailed_ex_returns_stderr_too(self):
        with mock.patch.object(yt_feeds.subprocess, "run",
                                return_value=_mock_run(returncode=1, stdout="",
                                                        stderr="ERROR: Sign in to confirm you're not a bot")):
            results, err = yt_feeds.feed_detailed_ex("history")
        self.assertIsNone(results)
        self.assertIn("Sign in to confirm", err)


# ---------------------------------------------------------------------------
# Auth-state heuristic
# ---------------------------------------------------------------------------

class TestAuthHeuristic(unittest.TestCase):
    def test_looks_like_auth_error_matches_known_markers(self):
        self.assertTrue(yt_feeds.looks_like_auth_error("ERROR: Sign in to confirm you're not a bot"))
        self.assertTrue(yt_feeds.looks_like_auth_error("could not find firefox cookies database in /x"))
        self.assertTrue(yt_feeds.looks_like_auth_error("Please sign in to continue"))

    def test_looks_like_auth_error_false_for_unrelated_text(self):
        self.assertFalse(yt_feeds.looks_like_auth_error("Connection timed out"))
        self.assertFalse(yt_feeds.looks_like_auth_error(""))
        self.assertFalse(yt_feeds.looks_like_auth_error(None))

    def test_classify_ok_when_feed_succeeded(self):
        self.assertEqual(yt_feeds.classify_failure([], None), "ok")
        self.assertEqual(yt_feeds.classify_failure([{"id": "x"}], [{"id": "y"}]), "ok")

    def test_classify_cookies_expired_when_marker_present(self):
        got = yt_feeds.classify_failure(None, None, feed_stderr="Sign in to confirm you're not a bot")
        self.assertEqual(got, "cookies_expired")

    def test_classify_cookies_expired_when_plain_search_still_works(self):
        # YT1's core heuristic: feed failed, plain search succeeded in the
        # same session -> auth, not network.
        got = yt_feeds.classify_failure(None, [{"id": "x"}], feed_stderr="")
        self.assertEqual(got, "cookies_expired")

    def test_classify_network_error_when_everything_fails(self):
        got = yt_feeds.classify_failure(None, None, feed_stderr="Connection timed out")
        self.assertEqual(got, "network_error")


# ---------------------------------------------------------------------------
# Resume store
# ---------------------------------------------------------------------------

class TestResumeStore(unittest.TestCase):
    def test_round_trip(self):
        with _tmp_dir() as d:
            yt_feeds.record("abc123", 42.5, duration=300.0, state_dir=d)
            self.assertEqual(yt_feeds.resume_for("abc123", state_dir=d), 42.5)

    def test_unknown_video_is_none(self):
        with _tmp_dir() as d:
            self.assertIsNone(yt_feeds.resume_for("never-seen", state_dir=d))

    def test_missing_file_is_none_not_crash(self):
        with _tmp_dir() as d:
            self.assertIsNone(yt_feeds.resume_for("x", state_dir=os.path.join(d, "does-not-exist")))

    def test_below_min_seconds_is_none(self):
        with _tmp_dir() as d:
            yt_feeds.record("short", 1.0, state_dir=d)
            self.assertIsNone(yt_feeds.resume_for("short", state_dir=d))

    def test_near_end_is_treated_as_finished(self):
        with _tmp_dir() as d:
            yt_feeds.record("finished", 295.0, duration=300.0, state_dir=d)
            self.assertIsNone(yt_feeds.resume_for("finished", state_dir=d))

    def test_exactly_at_min_seconds_boundary(self):
        # pos < RESUME_MIN_SECONDS is filtered; pos == RESUME_MIN_SECONDS is
        # kept (only strictly-less is excluded).
        with _tmp_dir() as d:
            yt_feeds.record("just_under", yt_feeds.RESUME_MIN_SECONDS - 0.01, state_dir=d)
            self.assertIsNone(yt_feeds.resume_for("just_under", state_dir=d))
            yt_feeds.record("boundary", yt_feeds.RESUME_MIN_SECONDS, state_dir=d)
            self.assertIsNotNone(yt_feeds.resume_for("boundary", state_dir=d))

    def test_record_overwrites_previous_position(self):
        with _tmp_dir() as d:
            yt_feeds.record("vid", 10.0, state_dir=d)
            yt_feeds.record("vid", 20.0, state_dir=d)
            self.assertEqual(yt_feeds.resume_for("vid", state_dir=d), 20.0)

    def test_no_video_id_is_a_no_op(self):
        with _tmp_dir() as d:
            yt_feeds.record("", 10.0, state_dir=d)
            yt_feeds.record(None, 10.0, state_dir=d)
            self.assertEqual(yt_feeds._load_store(yt_feeds._resume_path(d)), {})

    def test_none_pos_is_a_no_op(self):
        with _tmp_dir() as d:
            yt_feeds.record("vid", None, state_dir=d)
            self.assertIsNone(yt_feeds.resume_for("vid", state_dir=d))

    def test_size_cap_evicts_oldest(self):
        # Monotonic fake clock: one time.time() call per record(), so
        # eviction order is deterministic regardless of filesystem/clock
        # resolution (real wall-clock ties are exactly what made the first
        # version of this test flaky).
        n = yt_feeds.RESUME_MAX_ENTRIES + 20
        with _tmp_dir() as d:
            ticks = iter(range(n))
            with mock.patch.object(yt_feeds.time, "time", side_effect=lambda: next(ticks)):
                for i in range(n):
                    yt_feeds.record("vid-%d" % i, 20.0, state_dir=d)
            store = yt_feeds._load_store(yt_feeds._resume_path(d))
            self.assertEqual(len(store), yt_feeds.RESUME_MAX_ENTRIES)
            self.assertNotIn("vid-0", store)
            self.assertNotIn("vid-19", store)
            self.assertIn("vid-20", store)
            self.assertIn("vid-%d" % (n - 1), store)

    def test_corrupt_json_recovers_as_empty_store(self):
        with _tmp_dir() as d:
            path = yt_feeds._resume_path(d)
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not valid json at all")
            self.assertEqual(yt_feeds._load_store(path), {})
            self.assertIsNone(yt_feeds.resume_for("anything", state_dir=d))

    def test_corrupt_file_is_replaced_cleanly_by_next_record(self):
        with _tmp_dir() as d:
            path = yt_feeds._resume_path(d)
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("not even json")
            yt_feeds.record("recovered", 50.0, state_dir=d)
            self.assertEqual(yt_feeds.resume_for("recovered", state_dir=d), 50.0)
            with open(path, encoding="utf-8") as f:
                json.load(f)   # must be valid JSON again

    def test_wrong_top_level_type_is_empty_store(self):
        with _tmp_dir() as d:
            path = yt_feeds._resume_path(d)
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)
            self.assertEqual(yt_feeds._load_store(path), {})

    def test_malformed_single_entry_is_skipped_not_fatal(self):
        with _tmp_dir() as d:
            path = yt_feeds._resume_path(d)
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "good": {"pos": 30.0, "duration": 100.0, "at": 123.0},
                    "bad_no_pos": {"duration": 100.0, "at": 123.0},
                    "bad_not_a_dict": "oops",
                }, f)
            store = yt_feeds._load_store(path)
            self.assertIn("good", store)
            self.assertNotIn("bad_no_pos", store)
            self.assertNotIn("bad_not_a_dict", store)

    def test_default_state_dir_matches_config_dir(self):
        with mock.patch.object(config, "config_path", return_value="/storage/rp5deck/config.json"):
            self.assertEqual(yt_feeds._resume_path(),
                              os.path.join("/storage/rp5deck", "yt_resume.json"))

    def test_state_dir_falls_back_if_config_explodes(self):
        with mock.patch.object(config, "config_path", side_effect=RuntimeError("boom")):
            self.assertEqual(yt_feeds._resume_path(),
                              os.path.join("/storage/rp5deck", "yt_resume.json"))


if __name__ == "__main__":
    unittest.main()
