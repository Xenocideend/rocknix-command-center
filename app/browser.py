#!/usr/bin/env python3
"""Browser backend for rp5deck (official Firefox), checked on the RP5, see BROWSER-NOTES.md.

Firefox 156.0.1 linux-aarch64 (Mozilla's own build), SHA512 checked, unpacked to
/storage/rp5deck-firefox/firefox/. Every library resolves under `ldd` with LD_LIBRARY_PATH set
the way Firefox's launcher sets it. The "not found" hits like libmozsandbox.so are Firefox's
own bundled libs next to libxul.so, not missing system ones.

Wayland app_id: GTK sets xdg_toplevel.set_app_id from g_get_prgname() (GTK commit e1fd8772),
and Firefox's --name calls g_set_prgname() (Mozilla bug 1826330). That cant be seen
headless, `swaymsg -t get_tree` on a real screen confirms it.

Control channel: Marionette, a raw TCP length-prefixed JSON protocol Firefox opens with
--marionette on 127.0.0.1:2828, loopback only
(https://firefox-source-docs.mozilla.org/remote/Security.html). It's the classic WebDriver
transport (WebDriver:Navigate/Back/Refresh/GetCurrentURL are the literal command names) with
no WebSocket handshake, so plain socket + json does it, unlike WebDriver BiDi. Tested headless
end to end: navigate, navigate, back, current_url matches, then a clean shutdown. Framing
matches Mozilla's own marionette_driver client (transport.py): `<decimal length>:<json bytes>`,
commands `[0, id, name, params]`, responses `[1, id, error, result]`.

A dead Firefox never keeps running unseen. is_running() checks the real process
(Popen.poll()), and close() goes SIGTERM then SIGKILL with a bounded wait, like youtube.py's
Player.

RP5DECK_BROWSER_HEADLESS=1 adds --headless for tests.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import signin_vault
import logging
log = logging.getLogger("rp5deck.browser")
from typing import Optional

# --------------------------------------------------------------------------
# Settings and device paths
# --------------------------------------------------------------------------

FIREFOX_BIN = "/storage/rp5deck-firefox/firefox/firefox"
PROFILE_DIR = "/storage/rp5deck-firefox/profile"
APP_ID = "rp5deck-web"

# Discord's own unmodified web client in this same Firefox, never a third party or Electron
# client.
DISCORD_URL = "https://discord.com/app"

# YouTube's TV (leanback) web app, a remote-control UI (big tiles, no cursor) only served to a
# TV or console user agent, anything else gets sent to the normal www.youtube.com. It has its own
# separate Firefox profile, process and app_id (TV_PROFILE_DIR below), never the same window as
# Browser/Discord. "Sign in with your phone" (a QR code plus a youtube.com/activate code) signs
# you in on your phone, so Google's automated browser check (navigator.webdriver, which
# Marionette sets while a session is active) never comes into it, and rp5deck never sees or
# types a password either way.
YOUTUBE_TV_URL = "https://www.youtube.com/tv"
# the label for this tile, not "YouTube TV", which is Google's name for an unrelated paid live
# TV product
YOUTUBE_TV_LABEL = "YouTube App"

# A Firefox profile, process, app_id and Marionette port just for this tile, since a global user
# agent override cant share the normal Browser/Discord profile without changing them too. The
# normal profile, app_id and port above are never touched by it.
TV_PROFILE_DIR = "/storage/rp5deck-firefox/tvprofile"
TV_APP_ID = "rp5deck-ytapp"
# Files put into the TV profile before every launch (dest relative to the profile -> source in
# this app folder), so a deploy never needs a manual copy and the profile cant drift from what
# ships. The main Browser/Discord profile gets none since you might have changed it.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
TV_PROFILE_FILES = {
    "user.js": os.path.join(_APP_DIR, "firefox", "tvprofile-user.js"),
    "chrome/userChrome.css": os.path.join(_APP_DIR, "firefox", "tvprofile-userChrome.css"),
}
TV_MARIONETTE_PORT = 2829

# firefox/tvprofile-user.js (a separate profile from firefox/user.js) sets the global
# general.useragent.override (no domain suffix) to this.
#
# A per-domain general.useragent.override.youtube.com doesnt work. Firefox dropped that in
# version 71 (Mozilla bug 1513574, "Remove UserAgentOverrides.jsm", with bug 1589607 tracking
# the regression reports), and only the global override still works. So this tile gets its own
# profile and the override cant leak into Browser/Discord. On the device the per-domain pref was
# quietly ignored and youtube.com/tv served the normal desktop sign-in page, just like that bug
# says.
#
# Tizen smart TV strings follow Samsung's documented format
# (https://developer.samsung.com/internet/user-agent-string-format.html) and did reach the TV UI,
# but YouTube showed "This device no longer fully supports YouTube". rocknix-config/ua-probe.py
# loaded youtube.com/tv headless on a copy of the signed-in profile: Tizen 2.3 and 8.0 got the
# banner, while webOS 23, PS4 Leanback and Xbox One got no banner, the TV UI, and stayed signed
# in. The PS4 Leanback string names Firefox's own engine (Gecko), so YouTube serves code that
# suits it. tools/w2_tv_ua_check.py reads navigator.userAgent and location.href back from the
# running instance to prove it. If Google changes which TV strings it accepts, try the other
# reported ones (Switch, another console, Chrome's "Cobalt") before deciding the tile is broken.
YOUTUBE_TV_USER_AGENT = (
    "Mozilla/5.0 (PS4; Leanback Shell) Gecko/20100101 Firefox/65.0 "
    "LeanbackShell/01.00.01.75 Sony PS4/ (PS4, , no, CH)"
)

# WebDriver's normalised key codes (W3C WebDriver spec section 17.2 "Keyboard Actions", the
# Private Use Area codepoints every WebDriver client agrees on, Marionette's
# WebDriver:PerformActions takes them as is, not scancodes). The leanback D-pad
# (Browser.send_key(), screens.Bar's tv_* buttons) only needs these six. "back" is Escape, the
# usual leanback back key, not checked against today's youtube.com/tv. If Escape doesnt back out
# on the device, try Backspace (""), the other known leanback back key, before deciding
# the D-pad is wired wrong.
WEBDRIVER_KEYS = {
    "left": "", "up": "", "right": "", "down": "",
    "ok": "",    # Enter
    "back": "",  # Escape
}
# Where the Browser tile opens and where the strip's Home button goes. A search page, not
# about:blank, since its search box is a page text field and the keyboard can come up by itself
# (osk.py "auto") without hunting for Firefox's address bar. Can be changed per instance
# (Browser(home_url=...)).
HOME_URL = "https://duckduckgo.com/"

# Marionette probe for osk.py: does the page's focused element take text? Runs in the page
# through WebDriver:ExecuteScript, follows open shadow roots and same-origin iframes, and a
# cross-origin iframe reads as not editable (the Keyboard button still works there). Returns
# {"editable": bool, "focus": document.hasFocus()}.
TEXT_INPUT_PROBE = """
let e = document.activeElement, depth = 0;
while (e && depth++ < 8) {
  if (e.shadowRoot && e.shadowRoot.activeElement) { e = e.shadowRoot.activeElement; continue; }
  if ((e.tagName === "IFRAME" || e.tagName === "FRAME")) {
    let d = null;
    try { d = e.contentDocument; } catch (x) { d = null; }
    if (d && d.activeElement) { e = d.activeElement; continue; }
  }
  break;
}
let ed = false;
if (e) {
  const tag = (e.tagName || "").toUpperCase();
  if (e.isContentEditable) ed = true;
  else if (tag === "TEXTAREA") ed = !e.readOnly && !e.disabled;
  else if (tag === "INPUT") {
    const t = (e.getAttribute("type") || "text").toLowerCase();
    ed = ["text", "search", "email", "url", "tel", "password", "number"].indexOf(t) >= 0
         && !e.readOnly && !e.disabled;
  }
}
return {editable: ed, focus: document.hasFocus()};
"""

# after the keyboard comes up the window is shorter, keep the field in view
SCROLL_FOCUSED_INTO_VIEW = """
const e = document.activeElement;
if (e && e !== document.body && e.scrollIntoView) { e.scrollIntoView({block: "center"}); return true; }
return false;
"""

# Swipes for the YouTube TV tile. Its window gets real touch straight from the compositor (it's
# a normal tiled window, rp5deck doesnt read its input), but youtube.com/tv only listens for
# D-pad keys, never swipes. This runs in that page through WebDriver:ExecuteScript
# (Browser.poll_swipes(), from YtAppSession's poll on the web worker, never the UI thread) and
# does two things per call in one round trip:
# 1. Installs a touchstart/touchend listener pair on `document` (with a pointerdown/pointerup
#    fallback, guarded so one touch doesnt fire twice) exactly once. window.__rp5swipeInstalled
#    is the marker. It lives on `window`, so a new page load clears it and the next poll puts
#    the listener back, no navigation hook needed.
# 2. Drains window.__rp5swipes (a bounded queue of "up"/"down"/"left"/"right", oldest first) and
#    returns it, emptying the queue.
# A stroke under SWIPE_THRESHOLD_PX (60) is a tap and is left alone, it still reaches the page as
# a normal click since nothing calls preventDefault. Past that the bigger axis (|dx| or |dy|)
# picks up/down vs left/right, and the distance on that axis splits into SWIPE_STEP_PX (150)
# steps (a quick flick is 1, a long drag more), capped at SWIPE_MAX_STEPS (5) so one drag cant
# queue endless key presses. Which way each direction maps is decided in Python
# (YtAppSession._direction_to_key / youtube.tv_swipe_natural), this only reports the raw finger
# direction.
SWIPE_POLL_SCRIPT = """
if (!window.__rp5swipeInstalled) {
  window.__rp5swipeInstalled = true;
  window.__rp5swipes = [];
  var THRESH = 60, STEP = 150, MAX_STEPS = 5, MAX_QUEUE = 40;
  var sx = 0, sy = 0, active = false, lastTouchT = 0;
  function push(dir, steps) {
    for (var i = 0; i < steps; i++) window.__rp5swipes.push(dir);
    if (window.__rp5swipes.length > MAX_QUEUE) {
      window.__rp5swipes.splice(0, window.__rp5swipes.length - MAX_QUEUE);
    }
  }
  function classify(dx, dy) {
    var adx = Math.abs(dx), ady = Math.abs(dy);
    var mag = Math.max(adx, ady);
    if (mag < THRESH) return;             // a tap, not a swipe - leave it alone
    var steps = Math.min(MAX_STEPS, Math.max(1, Math.round(mag / STEP)));
    var horizontal = adx >= ady;
    var dir = horizontal ? (dx < 0 ? "left" : "right") : (dy < 0 ? "up" : "down");
    push(dir, steps);
  }
  function start(x, y) { sx = x; sy = y; active = true; }
  function end(x, y) { if (!active) return; active = false; classify(x - sx, y - sy); }
  document.addEventListener("touchstart", function (e) {
    lastTouchT = Date.now();
    var t = e.touches && e.touches[0];
    if (t) start(t.clientX, t.clientY);
  }, {passive: true, capture: true});
  document.addEventListener("touchmove", function (e) { lastTouchT = Date.now(); },
                            {passive: true, capture: true});
  document.addEventListener("touchend", function (e) {
    lastTouchT = Date.now();
    var t = e.changedTouches && e.changedTouches[0];
    if (t) end(t.clientX, t.clientY);
  }, {passive: true, capture: true});
  document.addEventListener("touchcancel", function () { active = false; },
                            {passive: true, capture: true});
  // pointer events: a fallback for whatever does not deliver real touch
  // events (e.g. a mouse-driven PC check) - skipped if a real touch just
  // fired, so one physical touch is never counted twice.
  document.addEventListener("pointerdown", function (e) {
    if (Date.now() - lastTouchT < 500) return;
    start(e.clientX, e.clientY);
  }, {capture: true});
  document.addEventListener("pointerup", function (e) {
    if (Date.now() - lastTouchT < 500) return;
    end(e.clientX, e.clientY);
  }, {capture: true});
}
var out = window.__rp5swipes;
window.__rp5swipes = [];
return out;
"""

MARIONETTE_HOST = "127.0.0.1"
MARIONETTE_PORT = 2828

# The device's Wayland environment, only as defaults, real values already in os.environ (like
# under the launcher) are never overridden.
XDG_RUNTIME_DIR_DEFAULT = "/run/0-runtime-dir"
WAYLAND_DISPLAY_DEFAULT = "wayland-1"

CONNECT_TIMEOUT = 20.0  # how long to wait for the marionette socket to show up
HANDSHAKE_TIMEOUT = 10.0
NAVIGATE_TIMEOUT = 30.0  # a full page load (Discord's app bundle took about 5 s)
COMMAND_TIMEOUT = 10.0  # back/reload/current_url, no fresh network load
STOP_WAIT_TIMEOUT = 5.0
QUIT_WAIT_TIMEOUT = 6.0  # Marionette:Quit's round trip plus Firefox's own shutdown and flush


# --------------------------------------------------------------------------
# Marionette wire protocol, pure functions tested without a socket
# --------------------------------------------------------------------------

def encode_command(msg_id: int, name: str, params: dict) -> bytes:
    """Builds one length-prefixed Marionette command frame, `<decimal length>:<json>`, where the
    length is the JSON body's byte length and the body is [0, msg_id, name, params] (0 means a
    command). Same as Mozilla's marionette_driver client (transport.py).
    """
    payload = json.dumps([0, msg_id, name, params]).encode("utf-8")
    return str(len(payload)).encode("ascii") + b":" + payload


def try_decode_message(buf: bytes):
    """Tries to pull one complete frame off the front of `buf`. Returns (parsed_json_or_None,
    remaining_buf), or (None, buf) unchanged if there isnt a whole frame yet, so read more and try
    again. Never raises for a short buffer, but a broken header (non-digits before ':') raises
    ValueError since the stream is corrupt, not just short.
    """
    if b":" not in buf:
        return None, buf
    header, sep, rest = buf.partition(b":")
    length = int(header)  # ValueError on a corrupt header, not swallowed
    if len(rest) < length:
        return None, buf
    body, remaining = rest[:length], rest[length:]
    return json.loads(body.decode("utf-8")), remaining


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

# files Firefox writes into the profile (cookies are sign-ins) are owner only
PROFILE_UMASK = 0o077


class Browser:
    """Drives one Firefox over its Marionette control channel.

    open() launches Firefox once, and navigate()/back()/reload()/home()/current_url() all reuse the
    same process and socket. With RP5DECK_BROWSER_HEADLESS=1 --headless is added, so tests never
    open a visible window.
    """

    def __init__(
        self,
        firefox_bin: str = FIREFOX_BIN,
        profile_dir: str = PROFILE_DIR,
        app_id: str = APP_ID,
        marionette_host: str = MARIONETTE_HOST,
        marionette_port: int = MARIONETTE_PORT,
        home_url: str = HOME_URL,
        extra_args: Optional[list[str]] = None,
        profile_files: Optional[dict] = None,
        vault=None,
    ):
        self.firefox_bin = firefox_bin
        self.profile_dir = profile_dir
        self.app_id = app_id
        self.marionette_host = marionette_host
        self.marionette_port = marionette_port
        self.home_url = home_url
        self.extra_args = list(extra_args or [])
        self.profile_files = dict(profile_files or {})
        # sign-ins (cookies, site storage) are only kept as an encrypted vault and Firefox runs on a RAM
        # copy (signin_vault.py). None means a plain profile (PCs, tests).
        self.vault = (vault if vault is not None else signin_vault.default()) or None
        self.run_profile = profile_dir

        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._msg_id = 0

    # -- command line ------------------------------------------------------

    def _headless(self) -> bool:
        return os.environ.get("RP5DECK_BROWSER_HEADLESS") == "1"

    def build_command(self, url: Optional[str] = None) -> list[str]:
        """Builds the firefox argv. Public so tests can check the exact flags without starting Firefox."""
        cmd = [
            self.firefox_bin,
            "--profile", self.run_profile,
            "--marionette",           # opens the control channel on 127.0.0.1
            "--name", self.app_id,  # g_set_prgname() -> the Wayland app_id
            "--no-remote",  # never reuse or signal some other instance
        ]
        if self._headless():
            cmd.append("--headless")
        cmd += self.extra_args
        if url:
            cmd.append(url)
        return cmd

    def install_profile_files(self) -> list:
        """Copies self.profile_files into the profile when missing or different (Firefox reads them at
        start, so this runs before every launch). Returns what got written. A failure gets skipped,
        Firefox still starts with whatever the profile has.
        """
        written = []
        for rel, src in self.profile_files.items():
            dest = os.path.join(self.run_profile, *rel.split("/"))
            try:
                with open(src, "rb") as f:
                    data = f.read()
                try:
                    with open(dest, "rb") as f:
                        if f.read() == data:
                            continue
                except OSError:
                    pass
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
                written.append(dest)
            except OSError:
                continue
        return written

    def install_policies(self) -> bool:
        """Copies firefox/policies.json next to the Firefox binary (distribution/), where Firefox reads
        it: password manager off, no updates, no telemetry. Only written when different, a failure
        gets skipped.
        """
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firefox", "policies.json")
        dest = os.path.join(os.path.dirname(self.firefox_bin), "distribution", "policies.json")
        try:
            with open(src, "rb") as f:
                data = f.read()
            try:
                with open(dest, "rb") as f:
                    if f.read() == data:
                        return False
            except OSError:
                pass
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except OSError:
            return False

    def lock_profile(self) -> int:
        """The profile holds sign-ins (session cookies), so it's owner only, folders 0700 and files 0600.
        Returns how many modes changed, a failure gets skipped.
        """
        changed = 0
        for root, dirs, files in os.walk(self.run_profile):
            for name, mode in [(root, 0o700)] + [(os.path.join(root, f), 0o600) for f in files]:
                try:
                    if os.stat(name).st_mode & 0o777 != mode:
                        os.chmod(name, mode)
                        changed += 1
                except OSError:
                    continue
        return changed

    def _build_env(self) -> dict:
        env = dict(os.environ)
        env.setdefault("MOZ_ENABLE_WAYLAND", "1")
        env.setdefault("XDG_RUNTIME_DIR", XDG_RUNTIME_DIR_DEFAULT)
        env.setdefault("WAYLAND_DISPLAY", WAYLAND_DISPLAY_DEFAULT)
        return env

    # -- process lifecycle ---------------------------------------------

    def pid(self) -> Optional[int]:
        """Firefox's main pid while it runs, else None. main saves it so a Firefox left behind by an
        rp5deck crash can be stopped at the next start.
        """
        return self._proc.pid if self.is_running() else None

    def terminate_now(self) -> None:
        """SIGTERMs Firefox right away from any thread (Popen.terminate is one kill(2) and no socket).
        Close shouldnt wait behind a control call stuck in a slow page load (NAVIGATE_TIMEOUT is 30
        s). That call fails fast once the process is gone, and the close() queued after it finds
        nothing left.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def is_running(self) -> bool:
        """True only if the real process is still alive. Never trusts an internal flag, so a Firefox
        that crashed reads as not running the moment this is called.
        """
        return self._proc is not None and self._proc.poll() is None

    def _connect_marionette(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False  # firefox exited before the port ever opened
            try:
                sock = socket.create_connection(
                    (self.marionette_host, self.marionette_port), timeout=2.0,
                )
                sock.settimeout(NAVIGATE_TIMEOUT)
                self._sock = sock
                self._buf = b""
                return True
            except OSError as e:
                last_err = e
                time.sleep(0.2)
        self._sock = None
        return False

    def open(self, url: Optional[str] = None) -> bool:
        """Starts Firefox (its own profile, Wayland, app_id, Marionette on 127.0.0.1) and opens a
        WebDriver session. If an instance from an earlier open() is still alive it reuses it and just
        navigates, never a second Firefox. True once a session is live (and once `url` finished
        loading if given), False if Firefox couldnt start, the control channel never came up, or the
        session or navigation failed.
        """
        if self.is_running() and self._sock is not None:
            if url:
                return self.navigate(url)
            return True

        self.close()  # clear out anything stale before a fresh launch
        self.run_profile = self.profile_dir
        if self.vault is not None:
            try:
                self.run_profile = self.vault.prepare(self.profile_dir)
            except Exception as e:      # noqa: BLE001 - never start on a half-open vault
                log.warning("sign-in vault could not open (%s); Browser not started", e)
                return False
        self.install_profile_files()
        self.install_policies()
        self.lock_profile()

        try:
            self._proc = subprocess.Popen(
                self.build_command(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._build_env(),
                umask=PROFILE_UMASK,  # files Firefox creates are owner only
            )
        except OSError:
            self._proc = None
            return False

        if not self._connect_marionette():
            self.close()
            return False

        try:
            hello = self._recv(timeout=HANDSHAKE_TIMEOUT)
        except (OSError, ValueError, TimeoutError):
            hello = None
        if hello is None or hello.get("marionetteProtocol") is None:
            self.close()
            return False

        resp = self._send("WebDriver:NewSession", {}, timeout=HANDSHAKE_TIMEOUT)
        if resp is None or resp[2] is not None:  # resp[2] is the error slot
            self.close()
            return False

        if url:
            return self.navigate(url)
        return True

    def close(self) -> None:
        """Stops Firefox if running and never leaves a process behind, safe whether or not open() ever
        worked.

        Tabs have to survive a close, so it asks Firefox to quit itself first (Marionette:Quit, a
        real app shutdown running the same "quit-application" observers a normal Quit does, which is
        what flushes sessionstore and cookies), then falls back to SIGTERM and SIGKILL if it doesnt
        exit in time. WebDriver:DeleteSession only ends the session, not the app, so this replaces it
        instead of sending both.

        First, if it can, it goes to home_url with a short fixed timeout (Close must never wait behind
        a slow page, see terminate_now()), so the tab session restore brings back as active is never
        mid-Discord or the YouTube TV app. Every other open tab is left alone, and the navigate() a
        tile tap does fixes the active tab anyway.
        """
        if self._sock is not None:
            try:
                self._send("WebDriver:Navigate", {"url": self.home_url}, timeout=2.0)
            except Exception:
                pass

        quit_sent = False
        if self._sock is not None:
            try:
                self._send("Marionette:Quit", {"flags": []}, timeout=QUIT_WAIT_TIMEOUT)
                quit_sent = True
            except Exception:
                pass
            # _send drops the socket itself when the send fails (Firefox already gone), so take whatever's
            # left and never .close() a None
            sock, self._sock = self._sock, None
            self._buf = b""
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            if quit_sent:
                # Marionette:Quit's reply only means shutdown started, not that the process exited, so give it
                # a moment before falling back to signals
                try:
                    proc.wait(timeout=QUIT_WAIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=STOP_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=STOP_WAIT_TIMEOUT)
                except Exception:
                    pass
            except Exception:
                pass
        self._seal_signins()

    def _seal_signins(self) -> None:
        """After Firefox is gone: its sign-ins go back into the vault and the RAM copy is removed."""
        if self.vault is None or self.run_profile == self.profile_dir:
            return
        if self._proc is not None and self._proc.poll() is None:
            return
        try:
            self.vault.release(self.profile_dir)
        except Exception as e:          # noqa: BLE001 - the last checkpoint still holds
            log.warning("sign-in vault seal failed: %s", e)

    # -- Marionette transport -------------------------------------------

    def _recv(self, timeout: float = COMMAND_TIMEOUT):
        """Reads exactly one complete Marionette message. Returns the parsed JSON array, or None on any
        failure (timeout, socket closed, broken stream). Never raises when no reply comes in time, so
        callers cant hang forever.
        """
        if self._sock is None:
            return None
        self._sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg, self._buf = try_decode_message(self._buf)
            if msg is not None:
                return msg
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self._sock = None
                return None
            if not chunk:
                self._sock = None
                return None
            self._buf += chunk
        return None

    def _send(self, name: str, params: dict, timeout: float = COMMAND_TIMEOUT):
        """Sends one Marionette/WebDriver command and returns its reply `[1, id, error, result]`, or None
        on any transport failure. Never raises.
        """
        if self._sock is None:
            return None
        self._msg_id += 1
        frame = encode_command(self._msg_id, name, params)
        try:
            self._sock.sendall(frame)
        except OSError:
            self._sock = None
            return None
        return self._recv(timeout=timeout)

    # -- controls ---------------------------------------------------------

    def navigate(self, url: str) -> bool:
        resp = self._send("WebDriver:Navigate", {"url": url}, timeout=NAVIGATE_TIMEOUT)
        return bool(resp is not None and resp[2] is None)

    def back(self) -> bool:
        resp = self._send("WebDriver:Back", {})
        return bool(resp is not None and resp[2] is None)

    def reload(self) -> bool:
        resp = self._send("WebDriver:Refresh", {}, timeout=NAVIGATE_TIMEOUT)
        return bool(resp is not None and resp[2] is None)

    def home(self) -> bool:
        return self.navigate(self.home_url)

    def current_url(self) -> Optional[str]:
        resp = self._send("WebDriver:GetCurrentURL", {})
        if resp is None or resp[2] is not None:
            return None
        return resp[3].get("value")

    def execute_script(self, script: str, args: Optional[list] = None):
        """Runs a script in the current page (WebDriver:ExecuteScript, the body's `return` value comes
        back). Returns (ok, value), ok False on any transport or script error, so a caller can tell
        "the script said false" from no answer.
        """
        resp = self._send("WebDriver:ExecuteScript",
                          {"script": script, "args": list(args or [])})
        if resp is None or resp[2] is not None:
            return False, None
        result = resp[3]
        return True, (result.get("value") if isinstance(result, dict) else None)

    def text_input_state(self) -> Optional[dict]:
        """{"editable": bool, "focus": bool} for the page's focused element (TEXT_INPUT_PROBE), or None
        when Firefox didnt answer. None is unknown, never "not editable".
        """
        ok, value = self.execute_script(TEXT_INPUT_PROBE)
        if not ok or not isinstance(value, dict):
            return None
        return {"editable": bool(value.get("editable")), "focus": bool(value.get("focus"))}

    def scroll_focused_into_view(self) -> bool:
        ok, value = self.execute_script(SCROLL_FOCUSED_INTO_VIEW)
        return bool(ok and value)

    def poll_swipes(self) -> Optional[list]:
        """Makes sure the swipe listener is installed (a no-op if it is) and drains what it queued since
        the last call. Returns a list of "up"/"down"/"left"/"right" (oldest first, maybe empty, which
        means checked and nothing happened), or None if the page didnt answer at all (mid navigation,
        a crashed content process). Never raises. YtAppSession polls again either way.
        """
        ok, value = self.execute_script(SWIPE_POLL_SCRIPT)
        if not ok or not isinstance(value, list):
            return None
        return [v for v in value if isinstance(v, str)]

    def send_key(self, key: str) -> bool:
        """One WebDriver key press (a WEBDRIVER_KEYS name like "up", "ok" or "back") through
        WebDriver:PerformActions. The YouTube TV tile's D-pad never scripts or clicks the page like the
        rest of this class, leanback takes bare key events.
        """
        code = WEBDRIVER_KEYS.get(key)
        if code is None:
            raise ValueError("unknown key %r" % (key,))
        actions = [{"type": "key", "id": "keyboard",
                   "actions": [{"type": "keyDown", "value": code},
                              {"type": "keyUp", "value": code}]}]
        resp = self._send("WebDriver:PerformActions", {"actions": actions})
        return bool(resp is not None and resp[2] is None)


# --------------------------------------------------------------------------
# On-device end-to-end check (always RP5DECK_BROWSER_HEADLESS=1)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    os.environ["RP5DECK_BROWSER_HEADLESS"] = "1"
    b = Browser()
    print("open(example.com):", b.open("https://example.com"))
    print("is_running:", b.is_running())
    print("current_url:", b.current_url())
    print("navigate(example.org):", b.navigate("https://example.org"))
    print("current_url:", b.current_url())
    print("back():", b.back())
    print("current_url (should be example.com again):", b.current_url())
    b.close()
    print("is_running after close:", b.is_running())
