#!/usr/bin/env python3
"""Unit tests for rp5deck/browser.py.

Runs entirely off-device with stdlib unittest + unittest.mock. No test ever
launches a real Firefox or opens a real TCP socket: Browser uses
subprocess.Popen and socket.create_connection/socket objects respectively,
both faked here.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import browser  # noqa: E402


# ---------------------------------------------------------------------------
# encode_command / try_decode_message - the wire protocol, no socket involved
# ---------------------------------------------------------------------------

class TestWireProtocol(unittest.TestCase):
    def test_encode_command_framing(self):
        frame = browser.encode_command(7, "WebDriver:Navigate", {"url": "https://example.com"})
        # "<decimal length>:<json>" - Mozilla's own marionette_driver framing.
        header, sep, body = frame.partition(b":")
        self.assertEqual(sep, b":")
        self.assertEqual(int(header), len(body))
        decoded = json.loads(body)
        self.assertEqual(decoded, [0, 7, "WebDriver:Navigate", {"url": "https://example.com"}])

    def test_decode_complete_message(self):
        payload = json.dumps([1, 7, None, {"value": "https://example.com/"}]).encode("utf-8")
        buf = str(len(payload)).encode("ascii") + b":" + payload
        msg, remaining = browser.try_decode_message(buf)
        self.assertEqual(msg, [1, 7, None, {"value": "https://example.com/"}])
        self.assertEqual(remaining, b"")

    def test_decode_incomplete_header_returns_none_unchanged(self):
        buf = b"12"  # no colon yet
        msg, remaining = browser.try_decode_message(buf)
        self.assertIsNone(msg)
        self.assertEqual(remaining, buf)

    def test_decode_incomplete_body_returns_none_unchanged(self):
        payload = json.dumps([1, 1, None, {"value": 1}]).encode("utf-8")
        buf = str(len(payload)).encode("ascii") + b":" + payload[:-3]  # short a few bytes
        msg, remaining = browser.try_decode_message(buf)
        self.assertIsNone(msg)
        self.assertEqual(remaining, buf)  # unchanged - caller must read more and retry

    def test_decode_leaves_trailing_bytes_for_next_frame(self):
        p1 = json.dumps([1, 1, None, {"value": 1}]).encode("utf-8")
        p2 = json.dumps([1, 2, None, {"value": 2}]).encode("utf-8")
        buf = (str(len(p1)).encode() + b":" + p1 + str(len(p2)).encode() + b":" + p2)
        msg1, rest = browser.try_decode_message(buf)
        self.assertEqual(msg1[1], 1)
        msg2, rest2 = browser.try_decode_message(rest)
        self.assertEqual(msg2[1], 2)
        self.assertEqual(rest2, b"")

    def test_decode_corrupt_header_raises_rather_than_silently_misparsing(self):
        with self.assertRaises(ValueError):
            browser.try_decode_message(b"not-a-number:{}")


# ---------------------------------------------------------------------------
# build_command() - command-line construction, no process ever launched
# ---------------------------------------------------------------------------

class TestBuildCommand(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop("RP5DECK_BROWSER_HEADLESS", None)

    def tearDown(self):
        if self._old_env is not None:
            os.environ["RP5DECK_BROWSER_HEADLESS"] = self._old_env
        else:
            os.environ.pop("RP5DECK_BROWSER_HEADLESS", None)

    def test_default_command_has_required_flags(self):
        b = browser.Browser()
        cmd = b.build_command()
        self.assertEqual(cmd[0], browser.FIREFOX_BIN)
        self.assertIn("--profile", cmd)
        self.assertIn(browser.PROFILE_DIR, cmd)
        self.assertIn("--marionette", cmd)
        self.assertIn("--name", cmd)
        self.assertIn(browser.APP_ID, cmd)
        self.assertIn("--no-remote", cmd)
        self.assertNotIn("--headless", cmd)

    def test_url_is_appended_last(self):
        b = browser.Browser()
        cmd = b.build_command(url="https://discord.com/app")
        self.assertEqual(cmd[-1], "https://discord.com/app")

    def test_no_url_means_no_trailing_url_arg(self):
        b = browser.Browser()
        cmd = b.build_command()
        self.assertNotIn("https://", " ".join(cmd))

    def test_headless_env_var_adds_headless_flag(self):
        os.environ["RP5DECK_BROWSER_HEADLESS"] = "1"
        b = browser.Browser()
        cmd = b.build_command()
        self.assertIn("--headless", cmd)

    def test_without_headless_env_var_no_headless_flag(self):
        os.environ.pop("RP5DECK_BROWSER_HEADLESS", None)
        b = browser.Browser()
        cmd = b.build_command()
        self.assertNotIn("--headless", cmd)

    def test_custom_app_id_and_profile_are_used(self):
        b = browser.Browser(profile_dir="/tmp/prof", app_id="rp5deck-test")
        cmd = b.build_command()
        self.assertIn("/tmp/prof", cmd)
        self.assertIn("rp5deck-test", cmd)

    def test_env_sets_wayland_defaults_without_clobbering_real_values(self):
        b = browser.Browser()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOZ_ENABLE_WAYLAND", None)
            os.environ.pop("XDG_RUNTIME_DIR", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            env = b._build_env()
        self.assertEqual(env["MOZ_ENABLE_WAYLAND"], "1")
        self.assertEqual(env["XDG_RUNTIME_DIR"], browser.XDG_RUNTIME_DIR_DEFAULT)
        self.assertEqual(env["WAYLAND_DISPLAY"], browser.WAYLAND_DISPLAY_DEFAULT)

        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-9"}, clear=False):
            env2 = b._build_env()
        self.assertEqual(env2["WAYLAND_DISPLAY"], "wayland-9")  # real value wins


# ---------------------------------------------------------------------------
# Constants the spec asks for by exact name
# ---------------------------------------------------------------------------

class TestConstants(unittest.TestCase):
    def test_discord_url_constant(self):
        self.assertEqual(browser.DISCORD_URL, "https://discord.com/app")

    def test_youtube_tv_url_constant(self):
        self.assertEqual(browser.YOUTUBE_TV_URL, "https://www.youtube.com/tv")

    def test_youtube_tv_user_agent_names_a_tv(self):
        # 24 Sep (device + rocknix-config/ua-probe.py): Tizen 2.3 and 8.0 got
        # "This device no longer fully supports YouTube"; PS4 Leanback did not
        ua = browser.YOUTUBE_TV_USER_AGENT
        self.assertIn("Leanback", ua)
        self.assertIn("Gecko/", ua)
        self.assertNotIn("Tizen", ua)

    def test_tv_instance_is_fully_separate_from_the_main_one(self):
        self.assertNotEqual(browser.TV_PROFILE_DIR, browser.PROFILE_DIR)
        self.assertNotEqual(browser.TV_APP_ID, browser.APP_ID)
        self.assertNotEqual(browser.TV_MARIONETTE_PORT, browser.MARIONETTE_PORT)
        self.assertEqual(browser.TV_APP_ID, "rp5deck-ytapp")

    def test_webdriver_keys_are_the_w3c_normalised_codes(self):
        # Selenium's Keys class / the WebDriver spec's own table - not
        # independently re-derived, cited in browser.py's own comment.
        self.assertEqual(browser.WEBDRIVER_KEYS, {
            "left": "", "up": "", "right": "", "down": "",
            "ok": "", "back": "",
        })


# ---------------------------------------------------------------------------
# A fake Marionette socket: end-to-end frame send/receive without a real
# TCP connection, driving Browser._send/_recv exactly as a real socket would.
# ---------------------------------------------------------------------------

class _FakeMarionetteSocket:
    """Stands in for a connected socket. recv() serves bytes from a queue of
    pre-framed server replies; sendall() just records what the client sent."""

    def __init__(self, scripted_replies: list[bytes]):
        self._chunks = list(scripted_replies)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def sendall(self, data: bytes):
        self.sent.append(data)

    def recv(self, _bufsize):
        if not self._chunks:
            return b""  # simulates a closed connection
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


def _frame(msg_id, error, result):
    payload = json.dumps([1, msg_id, error, result]).encode("utf-8")
    return str(len(payload)).encode("ascii") + b":" + payload


class TestSendRecvOverFakeSocket(unittest.TestCase):
    def test_send_then_recv_round_trip(self):
        b = browser.Browser()
        b._sock = _FakeMarionetteSocket([_frame(1, None, {"value": None})])
        resp = b._send("WebDriver:Navigate", {"url": "https://example.com"})
        self.assertEqual(resp, [1, 1, None, {"value": None}])

        # what was actually put on the wire decodes back to the command we asked for
        sent_msg, _ = browser.try_decode_message(b._sock.sent[0])
        self.assertEqual(sent_msg[2], "WebDriver:Navigate")
        self.assertEqual(sent_msg[3], {"url": "https://example.com"})

    def test_recv_assembles_message_split_across_multiple_chunks(self):
        full = _frame(1, None, {"value": "https://example.com/"})
        mid = len(full) // 2
        b = browser.Browser()
        b._sock = _FakeMarionetteSocket([full[:mid], full[mid:]])
        # bypass _send (which would try to send a command first) - drive _recv directly
        msg = b._recv(timeout=1.0)
        self.assertEqual(msg[3]["value"], "https://example.com/")

    def test_recv_returns_none_on_closed_socket(self):
        b = browser.Browser()
        b._sock = _FakeMarionetteSocket([])  # recv() immediately returns b""
        msg = b._recv(timeout=1.0)
        self.assertIsNone(msg)
        self.assertIsNone(b._sock)  # marked dead, not left dangling

    def test_send_without_socket_returns_none(self):
        b = browser.Browser()
        b._sock = None
        self.assertIsNone(b._send("WebDriver:Back", {}))


# ---------------------------------------------------------------------------
# Browser controls - command formation, over a mocked _send (mirrors
# test_youtube.py's TestPlayerControls pattern)
# ---------------------------------------------------------------------------

class TestBrowserControls(unittest.TestCase):
    def setUp(self):
        self.b = browser.Browser()
        self.b._proc = mock.Mock()
        self.b._proc.poll.return_value = None  # "running"
        self.b._sock = mock.Mock()

    def test_navigate_sends_webdriver_navigate(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": None}]) as m:
            ok = self.b.navigate("https://discord.com/app")
        self.assertTrue(ok)
        m.assert_called_once_with("WebDriver:Navigate", {"url": "https://discord.com/app"},
                                   timeout=browser.NAVIGATE_TIMEOUT)

    def test_back_sends_webdriver_back(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": None}]) as m:
            ok = self.b.back()
        self.assertTrue(ok)
        m.assert_called_once_with("WebDriver:Back", {})

    def test_reload_sends_webdriver_refresh(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": None}]) as m:
            ok = self.b.reload()
        self.assertTrue(ok)
        m.assert_called_once_with("WebDriver:Refresh", {}, timeout=browser.NAVIGATE_TIMEOUT)

    def test_home_navigates_to_home_url(self):
        with mock.patch.object(self.b, "navigate", return_value=True) as m:
            ok = self.b.home()
        self.assertTrue(ok)
        m.assert_called_once_with(browser.HOME_URL)

    def test_current_url_returns_value_on_success(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, None, {"value": "https://example.com/"}]):
            self.assertEqual(self.b.current_url(), "https://example.com/")

    def test_current_url_returns_none_on_error_response(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, {"error": "no such window"}, None]):
            self.assertIsNone(self.b.current_url())

    def test_current_url_returns_none_when_transport_fails(self):
        with mock.patch.object(self.b, "_send", return_value=None):
            self.assertIsNone(self.b.current_url())

    def test_navigate_false_on_error_response(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, {"error": "unknown error"}, None]):
            self.assertFalse(self.b.navigate("https://example.com"))

    def test_send_key_performs_a_key_down_and_up(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {}]) as m:
            ok = self.b.send_key("up")
        self.assertTrue(ok)
        name, params = m.call_args[0]
        self.assertEqual(name, "WebDriver:PerformActions")
        self.assertEqual(params, {"actions": [
            {"type": "key", "id": "keyboard",
             "actions": [{"type": "keyDown", "value": ""},
                        {"type": "keyUp", "value": ""}]},
        ]})

    def test_send_key_covers_every_documented_key(self):
        for key, code in browser.WEBDRIVER_KEYS.items():
            with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {}]) as m:
                self.assertTrue(self.b.send_key(key))
            sent_code = m.call_args[0][1]["actions"][0]["actions"][0]["value"]
            self.assertEqual(sent_code, code, key)

    def test_send_key_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            self.b.send_key("nope")

    def test_send_key_false_on_error_response(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, {"error": "unknown error"}, None]):
            self.assertFalse(self.b.send_key("ok"))

    def test_send_key_false_when_transport_fails(self):
        with mock.patch.object(self.b, "_send", return_value=None):
            self.assertFalse(self.b.send_key("back"))


# ---------------------------------------------------------------------------
# Process lifecycle: is_running(), open(), close() - Popen and the connect
# step are mocked; nothing real is ever launched.
# ---------------------------------------------------------------------------

class TestProcessLifecycle(unittest.TestCase):
    def test_is_running_false_before_open(self):
        b = browser.Browser()
        self.assertFalse(b.is_running())

    def test_is_running_true_while_process_alive(self):
        b = browser.Browser()
        b._proc = mock.Mock()
        b._proc.poll.return_value = None
        self.assertTrue(b.is_running())

    def test_is_running_false_once_process_exited(self):
        b = browser.Browser()
        b._proc = mock.Mock()
        b._proc.poll.return_value = 0
        self.assertFalse(b.is_running())

    def test_open_false_when_popen_fails(self):
        b = browser.Browser()
        with mock.patch.object(browser.subprocess, "Popen", side_effect=OSError("no such file")):
            self.assertFalse(b.open())
        self.assertFalse(b.is_running())

    def test_open_false_when_marionette_port_never_comes_up(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch.object(browser.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(b, "_connect_marionette", return_value=False):
            self.assertFalse(b.open())

    def test_open_true_performs_handshake_and_new_session(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None

        def fake_connect(timeout=browser.CONNECT_TIMEOUT):
            b._sock = mock.Mock()
            return True

        with mock.patch.object(browser.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(b, "_connect_marionette", side_effect=fake_connect), \
             mock.patch.object(b, "_recv", return_value={"marionetteProtocol": 3}), \
             mock.patch.object(b, "_send", return_value=[1, 1, None, {"sessionId": "abc"}]):
            self.assertTrue(b.open())

    def test_open_reuses_running_instance_instead_of_relaunching(self):
        b = browser.Browser()
        b._proc = mock.Mock()
        b._proc.poll.return_value = None
        b._sock = mock.Mock()
        with mock.patch.object(browser.subprocess, "Popen") as m_popen, \
             mock.patch.object(b, "navigate", return_value=True) as m_nav:
            ok = b.open("https://discord.com/app")
        m_popen.assert_not_called()
        m_nav.assert_called_once_with("https://discord.com/app")
        self.assertTrue(ok)

    def test_close_terminates_running_process(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None  # still alive when close() checks
        b._proc = fake_proc
        b._sock = mock.Mock()
        b.close()
        fake_proc.terminate.assert_called_once()
        self.assertIsNone(b._proc)
        self.assertFalse(b.is_running())

    def test_close_escalates_to_kill_on_timeout(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="firefox", timeout=5), None]
        b._proc = fake_proc
        b.close()
        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_called_once()

    def test_close_survives_send_dropping_the_socket(self):
        # test day, 24 Sep: Firefox had exited, DeleteSession's send failed,
        # _send set _sock = None and close() then called None.close()
        b = browser.Browser()
        sock = mock.Mock()
        b._sock = sock

        def failing_send(name, *a, **k):
            # W2: close() now navigates home first, then sends Marionette:Quit;
            # the socket dies on the Quit (Firefox exiting), which is where
            # the None.close() crash lived
            if name == "Marionette:Quit":
                b._sock = None
                return None
            return [1, 1, None, {}]
        b._send = failing_send
        b.close()                       # must not raise
        self.assertIsNone(b._sock)
        sock.close.assert_not_called()  # _send had already dropped it

    def test_close_closes_the_socket_when_send_works(self):
        b = browser.Browser()
        sock = mock.Mock()
        b._sock = sock
        b._send = lambda *a, **k: [1, 1, None, {}]
        b.close()
        sock.close.assert_called_once()
        self.assertIsNone(b._sock)

    def test_close_is_a_no_op_when_never_opened(self):
        b = browser.Browser()
        b.close()  # must not raise
        self.assertFalse(b.is_running())

    def test_close_never_leaves_a_dead_process_reported_as_running(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.side_effect = [None, 0]  # alive at terminate-check time, then exited
        b._proc = fake_proc
        b.close()
        self.assertFalse(b.is_running())


class TestGracefulQuit(unittest.TestCase):
    """W2 (owner: "the browser needs to save tabs when closed"): close() now
    navigates home, then asks Firefox to quit ITSELF (Marionette:Quit - a
    real application shutdown that flushes sessionstore/cookies) before
    falling back to the pre-existing SIGTERM -> SIGKILL escalation."""

    def _browser(self, quits_before_deadline=True):
        b = browser.Browser(home_url="https://example.com/home")
        b._sock = mock.Mock()
        fake_proc = mock.Mock()
        b._proc = fake_proc
        self.calls = []

        def fake_send(name, params, timeout=browser.COMMAND_TIMEOUT):
            self.calls.append((name, params))
            return [1, 1, None, {}]
        b._send = fake_send

        polls = [None]                       # alive when close() starts
        if quits_before_deadline:
            polls.append(0)                  # Marionette:Quit worked: exited by the wait()
        else:
            polls += [None, None]            # still alive: falls through to terminate/kill
        fake_proc.poll.side_effect = polls
        return b, fake_proc

    def test_navigates_home_then_sends_marionette_quit_not_delete_session(self):
        b, fake_proc = self._browser()
        b.close()
        self.assertEqual(self.calls[0], ("WebDriver:Navigate", {"url": "https://example.com/home"}))
        self.assertEqual(self.calls[1][0], "Marionette:Quit")
        self.assertNotIn("WebDriver:DeleteSession", [c[0] for c in self.calls])
        fake_proc.terminate.assert_not_called()   # the graceful quit alone was enough

    def test_falls_back_to_terminate_when_quit_does_not_exit_in_time(self):
        b, fake_proc = self._browser(quits_before_deadline=False)
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="firefox", timeout=1)
        b.close()
        self.assertEqual(self.calls[1][0], "Marionette:Quit")
        fake_proc.terminate.assert_called_once()

    def test_home_navigate_uses_a_short_fixed_timeout_not_navigate_timeout(self):
        """Close must never wait behind a slow page (terminate_now()'s own
        doc) - the pre-quit navigate is NOT the public navigate() method
        (which defaults to NAVIGATE_TIMEOUT, 30 s)."""
        b, fake_proc = self._browser()
        with mock.patch.object(b, "_send", wraps=b._send) as spy:
            b.close()
        nav_call = spy.call_args_list[0]
        self.assertEqual(nav_call.kwargs.get("timeout"), 2.0)

    def test_no_socket_at_all_skips_straight_to_terminate(self):
        b = browser.Browser()
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        b._proc = fake_proc
        b._sock = None
        b.close()
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called_once()      # only the terminate-escalation wait


# ---------------------------------------------------------------------------
# HF1: page scripts for the on-screen keyboard, pid, start page
# ---------------------------------------------------------------------------

class TestPageScripts(unittest.TestCase):
    def setUp(self):
        self.b = browser.Browser()
        self.b._proc = mock.Mock(pid=999)
        self.b._proc.poll.return_value = None
        self.b._sock = mock.Mock()

    def test_execute_script_sends_webdriver_execute_script(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": 7}]) as m:
            self.assertEqual(self.b.execute_script("return 7;"), (True, 7))
        m.assert_called_once_with("WebDriver:ExecuteScript", {"script": "return 7;", "args": []})

    def test_execute_script_failure_is_not_a_false_value(self):
        with mock.patch.object(self.b, "_send", return_value=None):
            self.assertEqual(self.b.execute_script("return false;"), (False, None))
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, {"error": "javascript error"}, None]):
            self.assertEqual(self.b.execute_script("x"), (False, None))

    def test_text_input_state_parses_the_probe(self):
        with mock.patch.object(self.b, "_send", return_value=[
                1, 1, None, {"value": {"editable": True, "focus": False}}]) as m:
            self.assertEqual(self.b.text_input_state(), {"editable": True, "focus": False})
        self.assertEqual(m.call_args[0][1]["script"], browser.TEXT_INPUT_PROBE)

    def test_text_input_state_unknown_is_none_never_false(self):
        for resp in (None, [1, 1, {"error": "no such window"}, None], [1, 1, None, {"value": 3}],
                     [1, 1, None, None]):
            with mock.patch.object(self.b, "_send", return_value=resp):
                self.assertIsNone(self.b.text_input_state(), resp)

    def test_probe_covers_the_editable_kinds(self):
        src = browser.TEXT_INPUT_PROBE
        for needle in ("isContentEditable", "TEXTAREA", "INPUT", "shadowRoot", "contentDocument",
                       "readOnly", "disabled", "document.hasFocus()", "return {editable"):
            self.assertIn(needle, src)
        # the braces balance (a quick guard against a truncated edit; there is
        # no JS engine on the PC, the device checklist runs it for real)
        self.assertEqual(src.count("{"), src.count("}"))
        self.assertEqual(src.count("("), src.count(")"))

    def test_scroll_focused_into_view(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": True}]) as m:
            self.assertTrue(self.b.scroll_focused_into_view())
        self.assertEqual(m.call_args[0][1]["script"], browser.SCROLL_FOCUSED_INTO_VIEW)

    # -- YT4: swipe polling -------------------------------------------------
    def test_poll_swipes_runs_the_swipe_poll_script(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, None, {"value": ["left", "up"]}]) as m:
            self.assertEqual(self.b.poll_swipes(), ["left", "up"])
        self.assertEqual(m.call_args[0][1]["script"], browser.SWIPE_POLL_SCRIPT)

    def test_poll_swipes_empty_list_is_a_real_answer_not_a_failure(self):
        with mock.patch.object(self.b, "_send", return_value=[1, 1, None, {"value": []}]):
            self.assertEqual(self.b.poll_swipes(), [])

    def test_poll_swipes_none_on_any_failure(self):
        for resp in (None, [1, 1, {"error": "no such window"}, None], [1, 1, None, {"value": 3}],
                     [1, 1, None, {"value": None}]):
            with mock.patch.object(self.b, "_send", return_value=resp):
                self.assertIsNone(self.b.poll_swipes(), resp)

    def test_poll_swipes_drops_non_string_entries(self):
        with mock.patch.object(self.b, "_send",
                                return_value=[1, 1, None, {"value": ["up", 3, None, "down"]}]):
            self.assertEqual(self.b.poll_swipes(), ["up", "down"])

    def test_swipe_poll_script_installs_once_and_drains(self):
        src = browser.SWIPE_POLL_SCRIPT
        for needle in ("__rp5swipeInstalled", "__rp5swipes", "touchstart", "touchmove",
                       "touchend", "touchcancel", "pointerdown", "pointerup", "passive"):
            self.assertIn(needle, src)
        self.assertEqual(src.count("{"), src.count("}"))
        self.assertEqual(src.count("("), src.count(")"))

    def test_swipe_listener_only_installs_once(self):
        """The install block (which resets window.__rp5swipes = []) is
        gated on the marker being ABSENT - re-running it every poll would
        wipe the queue out from under a real drag still in progress."""
        self.assertIn("if (!window.__rp5swipeInstalled) {", browser.SWIPE_POLL_SCRIPT)

    def test_pid_only_while_running(self):
        self.assertEqual(self.b.pid(), 999)
        self.b._proc.poll.return_value = 0
        self.assertIsNone(self.b.pid())
        self.assertIsNone(browser.Browser().pid())

    def test_terminate_now_signals_only_a_live_process(self):
        self.b.terminate_now()
        self.b._proc.terminate.assert_called_once_with()
        self.b._proc.poll.return_value = 0
        self.b.terminate_now()
        self.b._proc.terminate.assert_called_once_with()          # not again: it is dead
        browser.Browser().terminate_now()                          # never started: harmless
        self.b._proc.poll.return_value = None
        self.b._proc.terminate.side_effect = ProcessLookupError()
        self.b.terminate_now()                                     # raced with its exit
        self.b._sock.sendall.assert_not_called()                   # no socket traffic at all

    def test_start_page_is_a_real_https_page(self):
        self.assertTrue(browser.HOME_URL.startswith("https://"))
        self.assertEqual(browser.Browser().home_url, browser.HOME_URL)
        self.assertEqual(browser.Browser(home_url="https://example.org/").home_url,
                         "https://example.org/")


# ---------------------------------------------------------------------------
# firefox/user.js - the source-of-truth prefs file copied verbatim to
# PROFILE_DIR (BROWSER-NOTES.md). Read as text, never executed: user.js is
# JS syntax but every line here is a `user_pref("name", value);` call, so a
# plain regex is enough and does not need a JS engine.
# ---------------------------------------------------------------------------
USER_JS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "firefox", "user.js")


def _read_user_js_prefs():
    """{pref_name: raw_value_text} for every user_pref() line in the real,
    checked-in firefox/user.js - not a copy, so this fails the moment
    someone edits the file without keeping the pref this test checks."""
    import re
    with open(USER_JS_PATH, encoding="utf-8") as f:
        text = f.read()
    return dict(re.findall(r'user_pref\("([^"]+)",\s*(.+?)\);', text))


class TestUserJsFullscreenPref(unittest.TestCase):
    """FOC2 gap 3: page fullscreen (a video site, etc.) would cover
    rp5deck's BAR strip - the Close/Back/keyboard controls - with no way
    out short of the hardware Back button. full-screen-api.enabled=false
    keeps every page inside the tiled window under the strip."""

    def test_full_screen_api_is_disabled(self):
        prefs = _read_user_js_prefs()
        self.assertIn("full-screen-api.enabled", prefs,
                      "firefox/user.js no longer sets full-screen-api.enabled")
        self.assertEqual(prefs["full-screen-api.enabled"], "false")

    def test_break_restore_the_prefs_reader_itself(self):
        # Prove _read_user_js_prefs() actually parses user_pref lines (not a
        # false-green regex that never matches anything real): a known-good
        # sample line must round-trip, and a line with a different name must
        # not be mistaken for it.
        sample = 'user_pref("some.other.pref", true);\nuser_pref("full-screen-api.enabled", false);\n'
        import re
        found = dict(re.findall(r'user_pref\("([^"]+)",\s*(.+?)\);', sample))
        self.assertEqual(found.get("full-screen-api.enabled"), "false")
        self.assertEqual(found.get("some.other.pref"), "true")
        self.assertNotIn("full-screen-api.enable", found)          # no accidental prefix match


class TestUserJsSessionRestorePrefs(unittest.TestCase):
    """W2 (owner: "the browser needs to save tabs when closed"). Reverses an
    earlier, deliberate "kiosk: never resume" decision - see the git history
    on these same pref names."""

    def test_startup_page_resumes_the_previous_session(self):
        prefs = _read_user_js_prefs()
        self.assertEqual(prefs.get("browser.startup.page"), "3")

    def test_crash_resume_is_on_with_no_give_up_limit(self):
        prefs = _read_user_js_prefs()
        self.assertEqual(prefs.get("browser.sessionstore.resume_from_crash"), "true")
        self.assertEqual(prefs.get("browser.sessionstore.max_resumed_crashes"), "-1")

    def test_marionette_recommended_prefs_are_disabled(self):
        # Otherwise Marionette's own bundle re-forces browser.startup.page=0
        # at every startup, silently undoing the pref above.
        prefs = _read_user_js_prefs()
        self.assertEqual(prefs.get("remote.prefs.recommended"), "false")


class TestMainProfileNeverOverridesUserAgent(unittest.TestCase):
    """W2b: the device proved general.useragent.override.<domain> is not
    honoured by this Firefox (Mozilla bug 1513574, removed in Firefox 71) -
    the shared Browser/Discord profile must never carry ANY UA override
    (global OR per-domain): a global one there would change Browser/Discord's
    UA too; the tile's own override lives in firefox/tvprofile-user.js."""

    def test_no_user_agent_override_of_any_kind(self):
        prefs = _read_user_js_prefs()
        self.assertFalse([k for k in prefs if k.startswith("general.useragent.override")],
                         "firefox/user.js must not set any general.useragent.override* pref")


TV_PROFILE_USER_JS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "firefox", "tvprofile-user.js")


def _read_tv_profile_prefs():
    import re
    with open(TV_PROFILE_USER_JS_PATH, encoding="utf-8") as f:
        text = f.read()
    return dict(re.findall(r'user_pref\("([^"]+)",\s*(.+?)\);', text))


class TestTvProfileUserAgent(unittest.TestCase):
    """firefox/tvprofile-user.js - the YouTube TV tile's OWN, separate
    profile (never firefox/user.js, the shared Browser/Discord one)."""

    def test_tvprofile_user_js_exists(self):
        self.assertTrue(os.path.isfile(TV_PROFILE_USER_JS_PATH))

    def test_override_is_global_not_per_domain(self):
        prefs = _read_tv_profile_prefs()
        self.assertIn("general.useragent.override", prefs)

    def test_matches_browser_pys_constant_exactly(self):
        """One source of truth: browser.py's YOUTUBE_TV_USER_AGENT constant
        and this file must never drift apart silently."""
        prefs = _read_tv_profile_prefs()
        raw = prefs["general.useragent.override"]
        self.assertEqual(raw.strip('"'), browser.YOUTUBE_TV_USER_AGENT)

    def test_remote_prefs_recommended_is_still_disabled_here_too(self):
        # This profile ALSO uses --marionette (the D-pad) - Marionette would
        # otherwise silently override browser.startup.page etc. here too.
        prefs = _read_tv_profile_prefs()
        self.assertEqual(prefs.get("remote.prefs.recommended"), "false")

    def test_marionette_port_matches_the_tv_instance(self):
        """--marionette has no port argument, so the profile's marionette.port
        is the ONLY thing that moves the TV Firefox off 2828. Device, 24 Sep:
        without it Firefox listened on 2828, rp5deck waited on 2829."""
        prefs = _read_tv_profile_prefs()
        self.assertEqual(prefs.get("marionette.port"), str(browser.TV_MARIONETTE_PORT))
        self.assertNotEqual(browser.TV_MARIONETTE_PORT, browser.MARIONETTE_PORT)

    def test_main_profile_keeps_the_default_port(self):
        path = os.path.join(os.path.dirname(TV_PROFILE_USER_JS_PATH), "user.js")
        text = open(path, encoding="utf-8").read()
        import re
        m = re.search(r'user_pref\("marionette\.port",\s*(\d+)\)', text)
        self.assertTrue(m is None or int(m.group(1)) == browser.MARIONETTE_PORT)

    def test_first_run_terms_modal_is_skipped(self):
        """Device, 24 Sep: a fresh TV profile opened with the Terms of Use
        modal over YouTube; the owner chose to skip it like the main profile."""
        prefs = _read_tv_profile_prefs()
        self.assertEqual(prefs.get("browser.preonboarding.enabled"), "false")
        self.assertEqual(prefs.get("termsofuse.bypassNotification"), "true")

    def test_session_restore_is_off_here_deliberately(self):
        # Unlike the main profile (task B): this tile always launches
        # straight to YOUTUBE_TV_URL regardless, so there is nothing to
        # gain from resuming tabs in a single-purpose kiosk profile.
        prefs = _read_tv_profile_prefs()
        self.assertEqual(prefs.get("browser.startup.page"), "1")


if __name__ == "__main__":
    unittest.main()
