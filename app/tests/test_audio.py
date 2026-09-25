#!/usr/bin/env python3
"""Unit tests for rp5deck/audio.py.

Runs entirely off-device with stdlib unittest + unittest.mock. Every test
that would otherwise touch the network/audio hardware mocks
audio.subprocess.run (and, for the Subscriber, Subscriber._spawn_pwmon /
_make_inotify / _resolve_ids) - no setter ever actually runs a real command,
per the task's read-only constraint. Subscriber's own integration tests use
a REAL os.pipe() as the fake pw-mon's stdout (see _FakePwMon below), because
Subscriber reads pw-mon via select() + os.read() on a raw fd - a plain
Python line iterator would not exercise that code path at all.

Fixtures under tests/fixtures/:
  pw-dump-real-capture-2026-09-23.json   REAL capture from the device. Has
                                          exactly one Stream/Output/Audio
                                          node (EmulationStation's own,
                                          idle UI-sound stream, id 62).
  wpctl-status-real-capture.txt          REAL `wpctl status` capture.
  wpctl-get-volume-real-capture.txt      REAL `wpctl get-volume
                                          @DEFAULT_AUDIO_SINK@` capture:
                                          "Volume: 0.05".
  wpctl-inspect-real-capture.txt         REAL `wpctl inspect
                                          @DEFAULT_AUDIO_SINK@` capture.
  pw-dump-SYNTHETIC-two-streams.json     SYNTHETIC. No second real playback
                                          stream was ever observed on the
                                          device (R4 and this module's own
                                          on-device run both saw zero). Built
                                          from the real EmulationStation
                                          node's exact shape with invented
                                          ids/names/volumes for a RetroArch-
                                          shaped and a YouTube(mpv)-shaped
                                          stream. Every test using it says so.
  pw-mon-real-capture-added-dump-tail.txt        REAL `pw-mon --no-colors`
                                          capture (23 Sep, B2b), tail of the
                                          startup dump: pure "added:" blocks
                                          for the default sink node (id 47)
                                          and its owning ALSA device (id 51),
                                          idle - no volume change yet.
  pw-mon-real-capture-node-changed-2026-09-23.txt   REAL, one "changed:"
                                          block for id 47 (the sink NODE)
                                          captured live during `/usr/bin/
                                          volume 6`. Only PropInfo metadata,
                                          no actual channelVolumes value -
                                          see AUDIO-NOTES.md.
  pw-mon-real-capture-device-changed-2026-09-23.txt REAL, one "changed:"
                                          block for id 51 (the ALSA DEVICE
                                          that owns the sink) captured live
                                          during the same `/usr/bin/volume 6`
                                          - carries the real Route Props,
                                          channelVolumes 0.000216 == 0.06**3.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio  # noqa: E402


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Low-level parsing
# ---------------------------------------------------------------------------

class TestVolumeLineParsing(unittest.TestCase):
    def test_plain_volume(self):
        self.assertEqual(audio._wpctl_volume_line("Volume: 0.05"), (0.05, False))

    def test_muted_volume(self):
        self.assertEqual(
            audio._wpctl_volume_line("Volume: 0.30 [MUTED]"), (0.3, True)
        )

    def test_boosted_volume_above_one(self):
        self.assertEqual(audio._wpctl_volume_line("Volume: 1.35"), (1.35, False))

    def test_garbage_is_none_not_zero(self):
        # This is the critical failure-path guarantee: a failure must never
        # look like a real, quiet volume (e.g. 0.0). Garbage input must
        # produce (None, None), not (0.0, False).
        volume, muted = audio._wpctl_volume_line("Node '999999' not found")
        self.assertIsNone(volume)
        self.assertIsNone(muted)

    def test_empty_is_none(self):
        self.assertEqual(audio._wpctl_volume_line(""), (None, None))


class TestInspectPropsParsing(unittest.TestCase):
    # test_real_capture and test_header_line_id removed: both depended on
    # tests/fixtures/wpctl-inspect-real-capture.txt, a real on-device capture
    # dropped from the public release (see DROPPED-FIXTURES-list.txt).
    pass


class TestSinkLabel(unittest.TestCase):
    def test_prefers_description_and_strips_prefix(self):
        props = {
            "node.description": "Built-in Audio Speaker Playback",
            "node.name": "irrelevant_for_this_test",
        }
        self.assertEqual(audio._sink_label(props), "Speaker Playback")

    def test_falls_back_to_name_for_speaker(self):
        props = {"node.name": "alsa_output.....HiFi__Speaker__sink"}
        self.assertEqual(audio._sink_label(props), "Speaker Playback")

    def test_falls_back_to_name_for_headphones(self):
        props = {"node.name": "alsa_output.....HiFi__Headphones__sink"}
        self.assertEqual(audio._sink_label(props), "Headphones Playback")

    def test_unknown_name_passthrough(self):
        props = {"node.name": "some_other_sink"}
        self.assertEqual(audio._sink_label(props), "some_other_sink")

    def test_nothing_present_is_none(self):
        self.assertIsNone(audio._sink_label({}))


class TestParseInotifyEvents(unittest.TestCase):
    """Raw struct bytes, as inotify's read() actually returns them - see
    struct inotify_event { int wd; uint32_t mask; uint32_t cookie;
    uint32_t len; char name[]; } (16-byte header, then `len` bytes of name
    padded with NUL). Real-device evidence for this exact struct format:
    experiments/e4g-sources.py used the identical "iIII" unpack against a
    live inotify fd and correctly counted 24 events across two nudges."""

    @staticmethod
    def _pack_event(wd, mask, cookie, name, pad_to=None):
        name_bytes = name.encode("utf-8") + b"\x00"
        if pad_to is not None:
            name_bytes = name_bytes.ljust(pad_to, b"\x00")
        return struct.pack("iIII", wd, mask, cookie, len(name_bytes)) + name_bytes

    def test_single_event_round_trips(self):
        buf = self._pack_event(3, audio.IN_MOVED_TO, 0, "system.cfg", pad_to=16)
        events = audio._parse_inotify_events(buf)
        self.assertEqual(events, [(3, audio.IN_MOVED_TO, 0, "system.cfg")])

    def test_multiple_events_in_one_buffer(self):
        buf = (
            self._pack_event(3, audio.IN_CLOSE_WRITE, 0, "system.cfgAbC123", pad_to=20)
            + self._pack_event(3, audio.IN_MOVED_TO, 1, "system.cfg", pad_to=16)
        )
        events = audio._parse_inotify_events(buf)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][3], "system.cfgAbC123")
        self.assertEqual(events[1][3], "system.cfg")

    def test_truncated_trailing_event_is_dropped_not_crashed(self):
        whole = self._pack_event(3, audio.IN_MOVED_TO, 0, "system.cfg", pad_to=16)
        buf = whole + whole[:10]  # a partial second event, as a short read might give
        events = audio._parse_inotify_events(buf)
        self.assertEqual(events, [(3, audio.IN_MOVED_TO, 0, "system.cfg")])

    def test_empty_buffer_is_no_events(self):
        self.assertEqual(audio._parse_inotify_events(b""), [])


class TestConfigRewriteEventFilter(unittest.TestCase):
    """The exact-name filter that stops /usr/bin/volume's temp file
    (system.cfgXXXXXX) from ever firing, while never missing the real
    rename-into-place event - see the module docstring for why."""

    def test_rename_onto_system_cfg_fires(self):
        self.assertTrue(audio._config_rewrite_event(audio.IN_MOVED_TO, "system.cfg"))

    def test_close_write_on_system_cfg_fires(self):
        self.assertTrue(audio._config_rewrite_event(audio.IN_CLOSE_WRITE, "system.cfg"))

    def test_temp_file_close_write_never_fires(self):
        self.assertFalse(audio._config_rewrite_event(audio.IN_CLOSE_WRITE, "system.cfgAbC123"))

    def test_temp_file_moved_to_never_fires(self):
        # Not how /usr/bin/volume actually behaves (it renames ONTO
        # system.cfg, not the temp name), but the filter must not be
        # fooled by a MOVED_TO on the wrong name either.
        self.assertFalse(audio._config_rewrite_event(audio.IN_MOVED_TO, "system.cfgAbC123"))

    def test_unrelated_mask_on_system_cfg_never_fires(self):
        # IN_CREATE (0x100) is not one of the two masks this module watches.
        self.assertFalse(audio._config_rewrite_event(0x100, "system.cfg"))

    def test_a_real_nudge_sequence_fires_exactly_once(self):
        # Modelled on the real e4g-sources.py capture: /usr/bin/volume
        # writes system.cfgXXXXXX (CREATE, then CLOSE_WRITE on the temp
        # name), then renames it onto system.cfg (MOVED_TO on "system.cfg").
        sequence = [
            (0x100, "system.cfgAbC123"),        # IN_CREATE - not watched at all
            (audio.IN_CLOSE_WRITE, "system.cfgAbC123"),  # temp name - must NOT fire
            (audio.IN_MOVED_TO, "system.cfg"),           # the real rewrite - MUST fire
        ]
        fires = [audio._config_rewrite_event(mask, name) for mask, name in sequence]
        self.assertEqual(fires, [False, False, True])
        self.assertEqual(sum(fires), 1)


class TestResolveMasterIds(unittest.TestCase):
    # test_real_capture_resolves_node_and_device_id removed: depended on
    # tests/fixtures/wpctl-inspect-real-capture.txt, dropped from the public
    # release (see DROPPED-FIXTURES-list.txt).

    def test_wpctl_missing_is_none_none(self):
        with mock.patch.object(audio, "_run", return_value=(None, "", "binary not found")):
            self.assertEqual(audio._resolve_master_ids(), (None, None))

    def test_no_default_sink_is_none_none(self):
        with mock.patch.object(audio, "_run", return_value=(1, "", "Object 'default' not found.")):
            self.assertEqual(audio._resolve_master_ids(), (None, None))

    def test_missing_device_id_property_is_node_id_and_none(self):
        text = "id 99, type PipeWire:Interface:Node\n    node.name = \"x\"\n"
        with mock.patch.object(audio, "_run", return_value=(0, text, "")):
            self.assertEqual(audio._resolve_master_ids(), (99, None))


class TestPwMonClassifier(unittest.TestCase):
    """Block-based classification, exercised against REAL pw-mon captures
    (tests/fixtures/pw-mon-real-capture-*) from the 23 Sep device session,
    plus synthetic edge cases for shapes not naturally observed."""

    # test_added_dump_never_fires_even_for_watched_ids,
    # test_real_node_changed_block_fires_master,
    # test_real_device_changed_block_fires_master,
    # test_device_only_id_requires_device_id_watched, and
    # test_changed_block_for_an_unwatched_id_never_fires removed: all
    # depended on tests/fixtures/pw-mon-real-capture-*.txt, real on-device
    # captures dropped from the public release (see
    # DROPPED-FIXTURES-list.txt).

    def test_volumebase_and_volumestep_are_not_the_volume_marker(self):
        # "Props:volumeBase" / "Props:volumeStep" both contain the substring
        # "Props:volume" - the \b boundary in _VALUE_MARKER_RE is what stops
        # a naive substring match from misfiring on those. This is
        # synthetic: it isolates the trap from the rest of a real block.
        block = (
            "changed:\n"
            "\tid: 51\n"
            "            Prop: key Spa:Pod:Object:Param:Props:volumeBase (65545), flags 00000001\n"
            "              Float 0.125895\n"
            "            Prop: key Spa:Pod:Object:Param:Props:volumeStep (65546), flags 00000001\n"
            "              Float 0.000015\n"
        )
        c = audio._PwMonClassifier(node_id=47, device_id=51)
        self.assertEqual(c.feed(block), [])

    def test_removed_block_never_fires(self):
        block = "removed:\n\tid: 51\n\tProp: key Spa:Pod:Object:Param:Props:channelVolumes\n"
        c = audio._PwMonClassifier(node_id=47, device_id=51)
        self.assertEqual(c.feed(block), [])

    # test_fires_at_most_once_per_block and
    # test_feed_handles_a_line_split_across_two_chunks removed: both
    # depended on tests/fixtures/pw-mon-real-capture-device-changed-*.txt,
    # dropped from the public release (see DROPPED-FIXTURES-list.txt).


# ---------------------------------------------------------------------------
# get_master()
# ---------------------------------------------------------------------------

class TestGetMaster(unittest.TestCase):
    def _mock_run(self, inspect_result, volume_result):
        """inspect_result / volume_result: (returncode, stdout, stderr)."""
        calls = {"n": 0}

        def fake_run(cmd, timeout=audio.DEFAULT_TIMEOUT):
            calls["n"] += 1
            if cmd[:2] == ["wpctl", "inspect"]:
                return inspect_result
            if cmd[:2] == ["wpctl", "get-volume"]:
                return volume_result
            raise AssertionError("unexpected command: %r" % (cmd,))
        return fake_run, calls

    # test_ok_real_capture and test_muted removed: both depended on
    # tests/fixtures/wpctl-inspect-real-capture.txt (and test_ok_real_capture
    # also on wpctl-get-volume-real-capture.txt), dropped from the public
    # release (see DROPPED-FIXTURES-list.txt).

    def test_no_default_sink_is_restoring_not_error_or_zero(self):
        # Simulates the HDMI-profile window where the speaker/headphone sink
        # has been deleted and 092 has not yet restored HiFi.
        fake_run, _ = self._mock_run(
            (1, "", "Object 'default' not found."), (0, "Volume: 0.05\n", "")
        )
        with mock.patch.object(audio, "_run", side_effect=fake_run):
            result = audio.get_master()
        self.assertEqual(result["state"], "restoring")
        self.assertIsNone(result["volume"])
        self.assertIsNone(result["muted"])

    def test_wpctl_missing_binary_is_error_not_zero(self):
        def fake_run(cmd, timeout=audio.DEFAULT_TIMEOUT):
            return None, "", "binary not found: wpctl"
        with mock.patch.object(audio, "_run", side_effect=fake_run):
            result = audio.get_master()
        self.assertEqual(result["state"], "error")
        self.assertIsNone(result["volume"])
        self.assertIsNone(result["muted"])

    # test_inconsistent_second_call_is_error_not_zero removed: depended on
    # tests/fixtures/wpctl-inspect-real-capture.txt, dropped from the public
    # release (see DROPPED-FIXTURES-list.txt).


# ---------------------------------------------------------------------------
# Setters - command construction only, subprocess is always mocked.
# ---------------------------------------------------------------------------

class TestSettersNeverRunForReal(unittest.TestCase):
    """Every one of these patches audio.subprocess.run directly (the
    lowest level) and asserts on the argv it was given. If any of these
    tests actually touched a device or a real audio stack, that would be a
    bug in the test itself, not just the module."""

    def setUp(self):
        self.which_patch = mock.patch.object(audio.shutil, "which", return_value="/usr/bin/true")
        self.which_patch.start()
        self.run_patch = mock.patch.object(audio.subprocess, "run")
        self.mock_run = self.run_patch.start()
        self.mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")

    def tearDown(self):
        self.run_patch.stop()
        self.which_patch.stop()

    def _argv(self):
        return self.mock_run.call_args[0][0]

    def test_set_master_builds_wpctl_set_volume(self):
        ok = audio.set_master(0.42)
        self.assertTrue(ok)
        argv = self._argv()
        self.assertEqual(argv[:3], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@"])
        self.assertAlmostEqual(float(argv[3]), 0.42, places=4)

    def test_set_master_clamps_high(self):
        audio.set_master(5.0)
        argv = self._argv()
        self.assertAlmostEqual(float(argv[3]), audio.MAX_LIVE_VOLUME, places=4)

    def test_set_master_clamps_negative(self):
        audio.set_master(-3.0)
        argv = self._argv()
        self.assertAlmostEqual(float(argv[3]), 0.0, places=4)

    def test_commit_master_calls_volume_script_with_integer_percent(self):
        ok = audio.commit_master(0.42)
        self.assertTrue(ok)
        argv = self._argv()
        self.assertEqual(argv, [audio.VOLUME_SCRIPT, "42"])

    def test_commit_master_clamps_to_script_range(self):
        audio.commit_master(1.5)
        argv = self._argv()
        self.assertEqual(argv, [audio.VOLUME_SCRIPT, "100"])

    def test_commit_master_zero(self):
        audio.commit_master(0.0)
        argv = self._argv()
        self.assertEqual(argv, [audio.VOLUME_SCRIPT, "0"])

    def test_toggle_master_mute_command(self):
        audio.toggle_master_mute()
        argv = self._argv()
        self.assertEqual(argv, ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])

    def test_set_stream_volume_command(self):
        audio.set_stream_volume(202, 0.4)
        argv = self._argv()
        self.assertEqual(argv[:2], ["wpctl", "set-volume"])
        self.assertEqual(argv[2], "202")
        self.assertAlmostEqual(float(argv[3]), 0.4, places=4)

    def test_toggle_stream_mute_command(self):
        audio.toggle_stream_mute(202)
        argv = self._argv()
        self.assertEqual(argv, ["wpctl", "set-mute", "202", "toggle"])

    def test_setter_failure_returns_false(self):
        self.mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="Node '9' not found")
        self.assertFalse(audio.set_stream_volume(9, 0.5))
        self.assertFalse(audio.toggle_stream_mute(9))
        self.assertFalse(audio.commit_master(0.5))
        self.assertFalse(audio.toggle_master_mute())


# ---------------------------------------------------------------------------
# list_streams()
# ---------------------------------------------------------------------------

class TestListStreams(unittest.TestCase):
    def _with_pw_dump_output(self, text, rc=0):
        return mock.patch.object(
            audio, "_run", return_value=(rc, text, "")
        )

    # test_real_capture_single_es_stream removed: depended on
    # tests/fixtures/pw-dump-real-capture-2026-09-23.json, dropped from the
    # public release (see DROPPED-FIXTURES-list.txt).

    def test_synthetic_two_streams(self):
        # SYNTHETIC fixture - see module docstring above and AUDIO-NOTES.md.
        text = _read_fixture("pw-dump-SYNTHETIC-two-streams.json")
        with self._with_pw_dump_output(text):
            streams = audio.list_streams()
        self.assertEqual(len(streams), 2)
        by_id = {s["id"]: s for s in streams}

        retro = by_id[201]
        # application.name was deliberately absent on this synthetic node;
        # display_name must fall back to application.process.binary.
        self.assertIsNone(retro["app_name"])
        self.assertEqual(retro["display_name"], "retroarch")
        self.assertAlmostEqual(retro["volume"], 0.7, places=3)
        self.assertFalse(retro["muted"])

        yt = by_id[202]
        self.assertEqual(yt["app_name"], "YouTube")
        self.assertEqual(yt["display_name"], "YouTube")
        self.assertAlmostEqual(yt["volume"], 0.4, places=3)
        self.assertTrue(yt["muted"])

    # test_no_streams_is_empty_list_not_none removed: derived its input from
    # tests/fixtures/pw-dump-real-capture-2026-09-23.json, dropped from the
    # public release (see DROPPED-FIXTURES-list.txt).

    def test_pw_dump_unavailable_is_none_not_empty_list(self):
        with mock.patch.object(audio, "_run", return_value=(None, "", "binary not found: pw-dump")):
            streams = audio.list_streams()
        self.assertIsNone(streams)

    def test_pw_dump_invalid_json_is_none(self):
        with self._with_pw_dump_output("not json {{{"):
            streams = audio.list_streams()
        self.assertIsNone(streams)

    def test_pw_dump_nonzero_exit_is_none(self):
        with self._with_pw_dump_output("", rc=1):
            streams = audio.list_streams()
        self.assertIsNone(streams)


# ---------------------------------------------------------------------------
# Subscriber - inotify + pw-mon, no real syscall or subprocess ever touched.
#
# Both fakes below are backed by a REAL os.pipe(), not a Python iterator:
# Subscriber reads both sources via select() + os.read() on a raw fd
# (self._inotify.fd / self._pwmon_proc.stdout.fileno()), so a fake that
# only offers a line iterator (the old _FakeProc) would never exercise
# that code path - select() would never see it as readable.
# ---------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


class _FakePwMon:
    """Stand-in for subprocess.Popen(["pw-mon", ...])."""

    def __init__(self):
        r, w = os.pipe()
        self._w = w
        self.stdout = _FakeStdout(r)
        self.terminated = False
        self.waited = False

    def write(self, text):
        os.write(self._w, text.encode("utf-8"))

    def close_write_end(self):
        if self._w is not None:
            try:
                os.close(self._w)
            except OSError:
                pass
            self._w = None

    def terminate(self):
        self.terminated = True
        self.close_write_end()

    def wait(self, timeout=None):
        self.waited = True
        return 0


def _raise_file_not_found():
    raise FileNotFoundError("no pw-mon")


class _FakeInotify:
    """Stand-in for _InotifyWatch."""

    def __init__(self):
        r, w = os.pipe()
        self.fd = r
        self._w = w
        self.closed = False

    def push_bytes(self, buf):
        os.write(self._w, buf)

    def read_events(self):
        try:
            buf = os.read(self.fd, 4096)
        except OSError:
            return []
        return audio._parse_inotify_events(buf) if buf else []

    def close(self):
        self.closed = True
        for fd in (self.fd, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


def _padded_inotify_event(wd, mask, cookie, name, pad_to):
    name_bytes = name.encode("utf-8") + b"\x00"
    name_bytes = name_bytes.ljust(pad_to, b"\x00")
    return struct.pack("iIII", wd, mask, cookie, len(name_bytes)) + name_bytes


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Subscriber's select() loop reads real pipe fds (self-pipe, inotify, "
    "pw-mon stdout). Windows' select() only accepts sockets - a pipe fd "
    "raises WinError 10093/10038 immediately - so these fake-pipe "
    "integration tests can only run on Linux, matching where Subscriber "
    "actually runs (the ROCKNIX device). The pure parsing/classification "
    "tests above (TestParseInotifyEvents, TestConfigRewriteEventFilter, "
    "TestPwMonClassifier, TestResolveMasterIds) cover the same logic on any "
    "platform; this class is exercised on the device itself - see the "
    "device evidence in AUDIO-NOTES.md / the task report.",
)
class TestSubscriberInotifyPath(unittest.TestCase):
    def _make_sub(self, callback, **kw):
        sub = audio.Subscriber(callback=callback, **kw)
        sub._resolve_ids = lambda: (47, 51)
        sub._spawn_pwmon = _raise_file_not_found  # isolate: inotify only
        return sub

    def test_config_rewrite_triggers_master(self):
        received = []
        fake_ino = _FakeInotify()
        sub = self._make_sub(received.append, coalesce=0.03, restart_backoff=0.5)
        sub._make_inotify = lambda: fake_ino
        sub.start()
        time.sleep(0.05)
        fake_ino.push_bytes(_padded_inotify_event(3, audio.IN_MOVED_TO, 0, "system.cfg", 16))
        time.sleep(0.15)
        sub.stop(join_timeout=2)
        self.assertEqual(received, ["master"])
        self.assertTrue(fake_ino.closed)

    def test_temp_file_event_never_triggers(self):
        received = []
        fake_ino = _FakeInotify()
        sub = self._make_sub(received.append, coalesce=0.03, restart_backoff=0.5)
        sub._make_inotify = lambda: fake_ino
        sub.start()
        time.sleep(0.05)
        fake_ino.push_bytes(
            _padded_inotify_event(3, audio.IN_CLOSE_WRITE, 0, "system.cfgAbC123", 20)
        )
        time.sleep(0.15)
        sub.stop(join_timeout=2)
        self.assertEqual(received, [])

    def test_a_real_nudge_sequence_fires_exactly_once(self):
        # Temp-file CLOSE_WRITE, then the real rename onto system.cfg -
        # modelled on the real e4g-sources.py capture (24 raw events for
        # two nudges collapse to the one meaningful rename each time).
        received = []
        fake_ino = _FakeInotify()
        sub = self._make_sub(received.append, coalesce=0.05, restart_backoff=0.5)
        sub._make_inotify = lambda: fake_ino
        sub.start()
        time.sleep(0.05)
        fake_ino.push_bytes(
            _padded_inotify_event(3, audio.IN_CLOSE_WRITE, 0, "system.cfgAbC123", 20)
            + _padded_inotify_event(3, audio.IN_MOVED_TO, 1, "system.cfg", 16)
        )
        time.sleep(0.2)
        sub.stop(join_timeout=2)
        self.assertEqual(received, ["master"])

    def test_missing_inotify_does_not_crash_the_thread(self):
        # _make_inotify returning None (unavailable) must not stop the
        # thread from running or being stopped cleanly.
        sub = self._make_sub(lambda k: None, restart_backoff=0.5)
        sub._make_inotify = lambda: None
        sub.start()
        time.sleep(0.05)
        self.assertTrue(sub._thread.is_alive())
        sub.stop(join_timeout=2)
        self.assertFalse(sub._thread.is_alive())


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Same reason as TestSubscriberInotifyPath: needs real select() on pipe "
    "fds, which Windows does not support.",
)
class TestSubscriberPwMonPath(unittest.TestCase):
    def _make_sub(self, callback, **kw):
        sub = audio.Subscriber(callback=callback, **kw)
        sub._make_inotify = lambda: None  # isolate: pw-mon only
        sub._resolve_ids = lambda: (47, 51)
        return sub

    # test_real_device_changed_block_triggers_master, test_added_dump_is_silent,
    # test_burst_coalesces_to_one_trailing_call, and
    # test_two_separated_changes_are_two_trailing_calls removed: all depended
    # on tests/fixtures/pw-mon-real-capture-*.txt, real on-device captures
    # dropped from the public release (see DROPPED-FIXTURES-list.txt).

    def test_restarts_when_pwmon_exits_immediately(self):
        spawn_calls = {"n": 0}

        def fake_spawn():
            spawn_calls["n"] += 1
            p = _FakePwMon()
            p.close_write_end()  # EOF at once - pw-mon exited on its own
            return p

        sub = self._make_sub(lambda k: None, restart_backoff=0.02)
        sub._spawn_pwmon = fake_spawn
        sub.start()
        time.sleep(0.2)
        sub.stop(join_timeout=2)
        self.assertGreaterEqual(spawn_calls["n"], 2)
        self.assertFalse(sub._thread.is_alive())

    def test_survives_pwmon_missing_binary(self):
        spawn_calls = {"n": 0}

        def fake_spawn():
            spawn_calls["n"] += 1
            raise FileNotFoundError("no pw-mon")

        sub = self._make_sub(lambda k: None, restart_backoff=0.02)
        sub._spawn_pwmon = fake_spawn
        sub.start()
        time.sleep(0.15)
        sub.stop(join_timeout=2)
        self.assertGreaterEqual(spawn_calls["n"], 2)
        self.assertFalse(sub._thread.is_alive())

    def test_stop_is_clean_idempotent_and_kills_pwmon(self):
        fake = _FakePwMon()
        sub = self._make_sub(lambda k: None, restart_backoff=0.5)
        sub._spawn_pwmon = lambda: fake
        sub.start()
        time.sleep(0.05)
        sub.stop(join_timeout=2)
        self.assertFalse(sub._thread.is_alive())
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.waited)
        # calling stop() again must not raise or hang
        sub.stop(join_timeout=2)

    # test_callback_exception_does_not_break_the_subscriber removed: depended
    # on tests/fixtures/pw-mon-real-capture-device-changed-2026-09-23.txt,
    # dropped from the public release (see DROPPED-FIXTURES-list.txt).


if __name__ == "__main__":
    unittest.main()
