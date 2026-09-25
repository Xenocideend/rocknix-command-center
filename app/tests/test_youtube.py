#!/usr/bin/env python3
"""Unit tests for rp5deck/youtube.py.

Runs entirely off-device with stdlib unittest + unittest.mock. No test ever
launches a real mpv or yt-dlp process: search()/Player use subprocess.run /
subprocess.Popen respectively, both of which are mocked here.

Fixtures under tests/fixtures/:
  yt-search-result-real-capture-2026-09-23.json   REAL capture from the
      device: `python3 yt-dlp --js-runtimes deno:<path>
      "ytsearch10:retroid pocket 5 review" --flat-playlist -J`. 10 entries,
      each with id/title/channel/duration/thumbnails - see YOUTUBE-NOTES.md.
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
import youtube  # noqa: E402


@contextlib.contextmanager
def _tmp_dir():
    d = tempfile.mkdtemp(prefix="rp5deck-yt-test-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# search() - parsing of a real captured -J result
# ---------------------------------------------------------------------------

class TestSearchParsing(unittest.TestCase):
    # setUp() previously loaded
    # tests/fixtures/yt-search-result-real-capture-2026-09-23.json into
    # self.raw/self.parsed unconditionally, so every test in this class
    # errored at setUp once that fixture was dropped from the public
    # release (see DROPPED-FIXTURES-list.txt). Removed; the three tests
    # that actually needed the real fixture's content
    # (test_fixture_shape_sanity, test_search_returns_all_entries_with_
    # expected_fields, test_detailed_real_fixture_matches_search) are
    # removed below. The remaining tests never relied on self.raw's default
    # (they all pass an explicit stdout/side_effect to _mock_run), so they
    # are unaffected by setUp()'s removal.

    def _mock_run(self, returncode=0, stdout=None, stderr=""):
        stdout = self.raw if stdout is None else stdout
        result = mock.Mock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    # test_fixture_shape_sanity and
    # test_search_returns_all_entries_with_expected_fields removed: both
    # depended on the real fixture content loaded by the now-removed
    # setUp() (see the class comment above).

    def test_search_empty_query_short_circuits(self):
        with mock.patch.object(youtube.subprocess, "run") as m:
            self.assertEqual(youtube.search(""), [])
            self.assertEqual(youtube.search("x", n=0), [])
        m.assert_not_called()

    def test_search_nonzero_returncode_is_empty_not_crash(self):
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(returncode=1, stdout="")):
            self.assertEqual(youtube.search("anything"), [])

    def test_search_garbage_stdout_is_empty_not_crash(self):
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(stdout="not json{{{")):
            self.assertEqual(youtube.search("anything"), [])

    def test_search_timeout_is_empty_not_hang(self):
        with mock.patch.object(
            youtube.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=20),
        ):
            self.assertEqual(youtube.search("anything"), [])

    def test_search_missing_binary_is_empty_not_crash(self):
        with mock.patch.object(youtube.subprocess, "run",
                                side_effect=FileNotFoundError()):
            self.assertEqual(youtube.search("anything"), [])


    # HF1: search_detailed() separates "failed" (None) from "no results" ([])
    def test_detailed_failure_is_none_and_empty_result_is_empty_list(self):
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(returncode=1, stdout="")):
            self.assertIsNone(youtube.search_detailed("anything"))
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(stdout="not json{{{")):
            self.assertIsNone(youtube.search_detailed("anything"))
        with mock.patch.object(youtube.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=20)):
            self.assertIsNone(youtube.search_detailed("anything"))
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(stdout='{"entries": []}')):
            self.assertEqual(youtube.search_detailed("anything"), [])
        with mock.patch.object(youtube.subprocess, "run",
                                return_value=self._mock_run(stdout='["not", "a", "dict"]')):
            self.assertIsNone(youtube.search_detailed("anything"))

    # test_detailed_real_fixture_matches_search removed: depended on the
    # real fixture content loaded by the now-removed setUp() (see the class
    # comment above).


# ---------------------------------------------------------------------------
# fetch_thumbnail()
# ---------------------------------------------------------------------------

class TestFetchThumbnail(unittest.TestCase):
    def test_downloads_and_caches(self):
        with mock.patch.object(youtube.urllib.request, "urlopen") as m_open, \
             _tmp_dir() as cache_dir:
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = b"fake-jpeg-bytes"
            m_open.return_value = cm

            path = youtube.fetch_thumbnail("https://i.ytimg.com/vi/x/hq720.jpg", cache_dir)
            self.assertIsNotNone(path)
            self.assertTrue(os.path.exists(path))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"fake-jpeg-bytes")

            # second call is a cache hit: urlopen must not be called again
            m_open.reset_mock()
            path2 = youtube.fetch_thumbnail("https://i.ytimg.com/vi/x/hq720.jpg", cache_dir)
            self.assertEqual(path, path2)
            m_open.assert_not_called()

    def test_empty_url_returns_none(self):
        self.assertIsNone(youtube.fetch_thumbnail("", "/tmp/whatever"))

    def test_download_failure_returns_none_not_crash(self):
        with mock.patch.object(youtube.urllib.request, "urlopen",
                                side_effect=OSError("boom")), _tmp_dir() as cache_dir:
            self.assertIsNone(youtube.fetch_thumbnail("https://example.com/x.jpg", cache_dir))


# ---------------------------------------------------------------------------
# Player - command line construction (no real mpv ever launched)
# ---------------------------------------------------------------------------

class TestPlayerCommandLine(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop("RP5DECK_YT_HEADLESS", None)

    def tearDown(self):
        if self._old_env is not None:
            os.environ["RP5DECK_YT_HEADLESS"] = self._old_env
        else:
            os.environ.pop("RP5DECK_YT_HEADLESS", None)

    def test_command_has_required_flags(self):
        p = youtube.Player(ipc_socket="/run/rp5deck/mpv-yt.sock")
        cmd = p.build_command(url="https://www.youtube.com/watch?v=abc123")

        self.assertEqual(cmd[0], "mpv")
        self.assertIn("--wayland-app-id=rp5deck-yt", cmd)
        self.assertIn("--audio-client-name=YouTube", cmd)
        self.assertIn("--input-ipc-server=/run/rp5deck/mpv-yt.sock", cmd)
        self.assertIn("--osc=no", cmd)
        # HF1: a fullscreen mpv is drawn above rp5deck's TOP-layer strip and
        # ignores its exclusive zone, so the transport would vanish.
        self.assertNotIn("--fullscreen", cmd)
        self.assertFalse([c for c in cmd if c.startswith("--fs")], cmd)
        for flag in ("--force-window=immediate", "--keep-open=yes",
                     "--input-default-bindings=no", "--input-vo-keyboard=no",
                     "--native-touch=no"):
            self.assertIn(flag, cmd)
        # owner request 2: tap-to-pause via mpv's own input.conf, not
        # rp5deck reading the tap - and --input-cursor=no would have blocked
        # the very click the binding needs to see.
        self.assertNotIn("--input-cursor=no", cmd)
        self.assertIn("--input-conf=%s" % youtube.INPUT_CONF_PATH, cmd)
        self.assertIn("--ytdl=yes", cmd)
        self.assertEqual(cmd[-1], "https://www.youtube.com/watch?v=abc123")

        script_opts = [c for c in cmd if c.startswith("--script-opts=")][0]
        self.assertIn("ytdl_hook-ytdl_path=%s" % youtube.YTDLP_PATH, script_opts)
        self.assertIn("js-runtimes=deno:%s" % youtube.DENO_PATH, script_opts)
        self.assertIn("vcodec^=avc1", script_opts)

        # not headless by default: no forced-silent video/audio outputs
        self.assertNotIn("--vo=null", cmd)
        self.assertNotIn("--ao=null", cmd)

    def test_headless_env_var_forces_null_vo_ao(self):
        os.environ["RP5DECK_YT_HEADLESS"] = "1"
        p = youtube.Player()
        cmd = p.build_command(url="https://www.youtube.com/watch?v=abc123")
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)

    def test_without_headless_env_var_no_null_vo_ao(self):
        os.environ.pop("RP5DECK_YT_HEADLESS", None)
        p = youtube.Player()
        cmd = p.build_command(url="https://www.youtube.com/watch?v=abc123")
        self.assertNotIn("--vo=null", cmd)
        self.assertNotIn("--ao=null", cmd)


# ---------------------------------------------------------------------------
# The shipped --input-conf (owner request 2: tap-to-pause)
# ---------------------------------------------------------------------------

class TestInputConfFile(unittest.TestCase):
    """youtube.INPUT_CONF_PATH points at a real file next to youtube.py (not
    the owner's own mpv config), and that file is the thing that actually
    makes a tap pause the video - a raw text check, no mpv involved."""

    def setUp(self):
        with open(youtube.INPUT_CONF_PATH, "r", encoding="utf-8") as f:
            self.lines = [ln.split("#", 1)[0].split() for ln in f
                         if ln.split("#", 1)[0].strip()]

    def test_path_sits_next_to_youtube_py(self):
        self.assertEqual(os.path.dirname(youtube.INPUT_CONF_PATH), youtube.HERE)
        self.assertTrue(os.path.isfile(youtube.INPUT_CONF_PATH))

    def test_binds_a_tap_to_cycle_pause(self):
        self.assertIn(["MBTN_LEFT", "cycle", "pause"], self.lines)

    def test_never_binds_a_double_tap_to_fullscreen(self):
        dbl = [ln for ln in self.lines if ln and ln[0] == "MBTN_LEFT_DBL"]
        for ln in dbl:
            self.assertNotIn("fullscreen", ln)                # HF1: must not cover the strip

    def test_no_other_bindings_at_all(self):
        bound = {ln[0] for ln in self.lines}
        self.assertLessEqual(bound, {"MBTN_LEFT", "MBTN_LEFT_DBL"}, self.lines)


# ---------------------------------------------------------------------------
# Player - controls over a faked IPC layer (no real socket/process)
# ---------------------------------------------------------------------------

class TestPlayerControls(unittest.TestCase):
    def setUp(self):
        self.p = youtube.Player()
        # Pretend a healthy mpv + socket are already present, without
        # spawning anything real.
        self.p._proc = mock.Mock()
        self.p._proc.poll.return_value = None  # still running
        self.p._sock = mock.Mock()

    def test_is_alive_true_when_process_running(self):
        self.assertTrue(self.p.is_alive())

    def test_is_alive_false_when_process_exited(self):
        self.p._proc.poll.return_value = 0
        self.assertFalse(self.p.is_alive())

    def test_get_status_reports_dead_mpv_explicitly(self):
        self.p._proc.poll.return_value = 1  # exited
        status = self.p.get_status()
        self.assertIsNone(status["title"])
        self.assertIsNone(status["time_pos"])
        self.assertIsNone(status["paused"])
        self.assertIn("error", status)

    def test_toggle_pause_sends_cycle_pause(self):
        with mock.patch.object(self.p, "_send_ipc",
                                return_value={"error": "success"}) as m:
            ok = self.p.toggle_pause()
        self.assertTrue(ok)
        m.assert_called_once_with(["cycle", "pause"])

    def test_seek_sends_absolute_seek(self):
        with mock.patch.object(self.p, "_send_ipc",
                                return_value={"error": "success"}) as m:
            ok = self.p.seek(42.5)
        self.assertTrue(ok)
        m.assert_called_once_with(["seek", 42.5, "absolute"])

    def test_set_volume_clamps_and_sends_property(self):
        with mock.patch.object(self.p, "_send_ipc",
                                return_value={"error": "success"}) as m:
            self.p.set_volume(999)
        m.assert_called_once_with(["set_property", "volume", 150.0])

    def test_get_status_uses_get_property_for_each_field(self):
        def fake_send(cmd, timeout=youtube.IPC_TIMEOUT):
            name = cmd[1]
            values = {"media-title": "Some Video", "time-pos": 12.3,
                      "duration": 300.0, "pause": False}
            return {"error": "success", "data": values[name]}

        with mock.patch.object(self.p, "_send_ipc", side_effect=fake_send):
            status = self.p.get_status()
        self.assertEqual(status, {
            "title": "Some Video", "time_pos": 12.3,
            "duration": 300.0, "paused": False,
        })

    def test_pid_only_while_alive(self):
        p = youtube.Player()
        self.assertIsNone(p.pid())
        p._proc = mock.Mock(pid=4321)
        p._proc.poll.return_value = None
        self.assertEqual(p.pid(), 4321)
        p._proc.poll.return_value = 0
        self.assertIsNone(p.pid())

    def test_terminate_now_signals_only_a_live_process(self):
        p = youtube.Player()
        p.terminate_now()                                          # never started
        p._proc = mock.Mock()
        p._proc.poll.return_value = None
        p._sock = mock.Mock()
        p.terminate_now()
        p._proc.terminate.assert_called_once_with()
        p._sock.sendall.assert_not_called()
        p._proc.poll.return_value = 0
        p.terminate_now()
        p._proc.terminate.assert_called_once_with()

    def test_seek_relative_is_passed_through(self):
        p = youtube.Player()
        with mock.patch.object(p, "_send_ipc", return_value={"error": "success"}) as m:
            self.assertTrue(p.seek(-10, "relative"))
        m.assert_called_once_with(["seek", -10, "relative"])

    def test_send_ipc_returns_none_without_socket(self):
        self.p._sock = None
        self.assertIsNone(self.p._send_ipc(["get_property", "pause"]))


if __name__ == "__main__":
    unittest.main()
