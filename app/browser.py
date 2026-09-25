#!/usr/bin/env python3
"""
Browser backend for rp5deck (Retroid Pocket 5 / ROCKNIX / official Firefox).

Verified on a real RP5, ROCKNIX 20260923, per BROWSER-NOTES.md:

- Firefox 156.0.1 linux-aarch64 (Mozilla official build), SHA512-verified,
  unpacked to /storage/rp5deck-firefox/firefox/. Every runtime library
  resolves (`ldd`, with LD_LIBRARY_PATH set as the real firefox launcher
  script sets it) - the "not found" hits for libmozsandbox.so etc. are
  Firefox's own bundled libs next to libxul.so, not missing system libs.
- Wayland app_id: the GTK Wayland backend sets `xdg_toplevel.set_app_id` from
  `g_get_prgname()` (GTK commit e1fd8772, gnome bug 746435). Firefox's `--name`
  flag calls `g_set_prgname()` (Mozilla bug 1826330, MOZ_APP_REMOTINGNAME is
  the same mechanism via an env var since Firefox 124). This cannot be
  observed headlessly (no real Wayland compositor in `--headless` mode) - the
  lead engineer confirms it with `swaymsg -t get_tree` on a real screen.
- Control channel: Marionette (a raw TCP, length-prefixed-JSON protocol
  Firefox exposes via `--marionette`, listening on 127.0.0.1:2828 by default,
  restricted to loopback - https://firefox-source-docs.mozilla.org/remote/Security.html).
  It is the WebDriver classic transport (WebDriver:Navigate/Back/Refresh/
  GetCurrentURL are literal command names on the wire) and needs no
  WebSocket handshake, so it is implementable in pure stdlib (socket + json),
  unlike WebDriver BiDi. Proven headlessly end to end: navigate(example.com)
  -> navigate(example.org) -> back() -> current_url() == example.com again,
  then a clean --marionette-triggered shutdown. Framing confirmed against
  Mozilla's own marionette_driver client (transport.py): `<decimal length>:
  <json bytes>`, commands are `[0, id, name, params]`, responses are
  `[1, id, error, result]`.
- A dead Firefox is never left running invisibly: is_running() checks the
  actual OS process (Popen.poll()), and close() escalates SIGTERM -> SIGKILL
  with a bounded wait either way, exactly like rp5deck/youtube.py's Player.
- RP5DECK_BROWSER_HEADLESS=1 adds --headless, for tests and for anything run
  while another agent/the owner has the only display.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Optional

# --------------------------------------------------------------------------
# Tunables / device paths
# --------------------------------------------------------------------------

FIREFOX_BIN = "/storage/rp5deck-firefox/firefox/firefox"
PROFILE_DIR = "/storage/rp5deck-firefox/profile"
APP_ID = "rp5deck-web"

# Discord's own, unmodified web client. D4 (owner-decided, R5): the official
# web app in this same Firefox, never a third-party/Electron Discord client.
DISCORD_URL = "https://discord.com/app"

# YouTube's TV/leanback web app - a remote-control-shaped UI (big tiles, no
# mouse cursor) served only to a recognised TV/console user agent; any other
# UA gets redirected to the normal www.youtube.com. Its own SEPARATE Firefox
# profile/process/app_id (W2b, 24 Sep evening - see TV_PROFILE_DIR below),
# NOT the same window as Browser/Discord. "Sign in with your phone" (a QR
# code + a youtube.com/activate code) signs the owner in on their PHONE, so
# Google's automated-browser check (navigator.webdriver, which Marionette
# sets true whenever a WebDriver session is active - see firefox/user.js)
# never comes into it - rp5deck never sees or types a credential either way.
YOUTUBE_TV_URL = "https://www.youtube.com/tv"
# The window_switcher/web_tiles label used for this tile - not literally
# "YouTube TV" (Google's own name for a DIFFERENT, unrelated paid live-TV
# product) to avoid the owner confusing the two.
YOUTUBE_TV_LABEL = "YouTube App"

# A dedicated Firefox profile/process/app_id/Marionette port for this tile
# ONLY (W2b: the device proved a global-only UA override cannot share the
# normal Browser/Discord profile without affecting them too). The normal
# Browser profile (PROFILE_DIR)/app_id (APP_ID)/port (MARIONETTE_PORT)
# above are never touched by this tile.
TV_PROFILE_DIR = "/storage/rp5deck-firefox/tvprofile"
TV_APP_ID = "rp5deck-ytapp"
# Files installed into the TV profile before every launch (dest relative to
# the profile -> source in this app dir), so a deploy never needs a manual
# copy and the profile cannot drift from the shipped settings. The main
# Browser/Discord profile gets none: the owner may have changed it.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
TV_PROFILE_FILES = {
    "user.js": os.path.join(_APP_DIR, "firefox", "tvprofile-user.js"),
    "chrome/userChrome.css": os.path.join(_APP_DIR, "firefox", "tvprofile-userChrome.css"),
}
TV_MARIONETTE_PORT = 2829

# firefox/tvprofile-user.js (a SEPARATE profile from firefox/user.js) sets
# the GLOBAL general.useragent.override (no domain suffix) to this.
#
# W2a shipped a PER-DOMAIN general.useragent.override.youtube.com in the
# shared profile instead, on the (wrong) assumption that Firefox still
# honoured that classic Firefox-24-era mechanism. It does not, and has not
# since Firefox 71: Mozilla bug 1513574 ("Remove UserAgentOverrides.jsm",
# fixed in Firefox 71, 2019) deleted the code that read
# general.useragent.override.<domain> entirely - bug 1589607 tracks the
# resulting regression reports ("Site specific user agent override stopped
# working"), and Valentin Gosu's own bug-1513574 comment gives the
# rationale (the per-site path was "quite inefficient, running for all
# loads" and had "a years old bug" with subresource loads; WebExtensions/
# webcompat interventions were judged the more maintainable replacement).
# Only the GLOBAL general.useragent.override (no domain part) still works -
# hence this tile's own profile, so the override cannot leak into Browser/
# Discord. Confirmed on the device (test day, 24 Sep evening): the per-
# domain pref was silently ignored and youtube.com/tv served the normal
# desktop sign-in page, not the TV UI - exactly what bug 1513574 predicts.
#
# A real, current (Jan 2026) Samsung Tizen Smart TV string - the documented
# format is Samsung's own
# (https://developer.samsung.com/internet/user-agent-string-format.html:
# DEVICE_TYPE "SMART-TV", "Linux; Tizen <ver>", APP_VER "Version/<ver>",
# UX_RECOMMENDED "TV"); a near-identical one was reported still working for
# exactly this purpose in Jan 2026 (Make Tech Easier, "How to Cast YouTube
# from Your Phone to Your PC": "Mozilla/5.0 (Linux; Tizen 2.3) AppleWebKit/
# 538.1 (KHTML, like Gecko)Version/2.3 TV Safari/538.1", there missing the
# "SMART-TV;" device-type token and a space before "Version" - both fixed
# here against Samsung's own documented shape). tools/w2_tv_ua_check.py is
# the device proof: read navigator.userAgent/location.href back from the
# real running instance over Marionette. If Google changes its TV-UA
# allow-list, device checklist item Y1 should try the alternatives multiple
# sources also report working as of 2024-2026 (a Nintendo Switch or PS4 UA,
# or Chrome's "Cobalt" trick) before assuming the tile itself is broken.
# 24 Sep evening (device): the Tizen 2.3 UA worked but YouTube showed "This
# device no longer fully supports YouTube". rocknix-config/ua-probe.py loaded
# youtube.com/tv headless on a copy of the signed-in profile: Tizen 2.3 and
# Tizen 8.0 -> banner; webOS 23, PS4 Leanback and Xbox One -> no banner, TV UI,
# still signed in. The PS4 Leanback UA is the one that names Firefox's own
# engine (Gecko), so YouTube serves code paths that suit it.
YOUTUBE_TV_USER_AGENT = (
    "Mozilla/5.0 (PS4; Leanback Shell) Gecko/20100101 Firefox/65.0 "
    "LeanbackShell/01.00.01.75 Sony PS4/ (PS4, , no, CH)"
)

# WebDriver's normalised key codes (W3C WebDriver spec ยง17.2 "Keyboard
# Actions", the Private Use Area codepoints every WebDriver client - e.g.
# Selenium's Keys class - agrees on; Marionette's WebDriver:PerformActions
# takes these literally, not real scancodes). The leanback D-pad
# (Browser.send_key(), screens.Bar's tv_* buttons, patches/W2-screens.patch)
# only ever needs these six. "back": Escape, per leanback's own documented
# convention (closing a video/menu) - UNVERIFIED against the CURRENT
# youtube.com/tv (leanback proper was retired by Google in 2023; this UA
# trick reaches whatever TV-shaped UI Google still serves a Tizen browser,
# which is presumably the same lineage but not proven identical). If Escape
# does not back out on the device, Backspace ("") is the other
# historically-documented leanback "back" key - try it there before
# concluding the D-pad itself is wired wrong.
WEBDRIVER_KEYS = {
    "left": "", "up": "", "right": "", "down": "",
    "ok": "",    # Enter
    "back": "",  # Escape
}
# Where the Browser tile opens and the strip's Home button goes (HF1). A
# search page, not about:blank: its search box is a page text field, so the
# on-screen keyboard can come up by itself (osk.py "auto") without the owner
# having to find Firefox's own address bar first. Overridable per instance
# (Browser(home_url=...)); a config key for it is requested in the HF1 report.
HOME_URL = "https://duckduckgo.com/"

# Marionette probe for osk.py: does the page's focused element take text?
# Runs in content via WebDriver:ExecuteScript; follows open shadow roots and
# same-origin iframes; a cross-origin iframe reads as "not editable" (the
# Keyboard button still works there). Returns {"editable": bool,
# "focus": document.hasFocus()}.
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

# After the keyboard appears the window is shorter; keep the field in view.
SCROLL_FOCUSED_INTO_VIEW = """
const e = document.activeElement;
if (e && e !== document.body && e.scrollIntoView) { e.scrollIntoView({block: "center"}); return true; }
return false;
"""

# YT4 (owner: "can we make youtube swipeable"): the YouTube TV tile's own
# window gets real touch input directly from the compositor (it is a normal
# tiled window, not something rp5deck reads input for - see web_tiles.py's
# own module doc) but youtube.com/tv's leanback UI never listens for swipes
# itself, only D-pad-shaped key events. This script runs in that page via
# WebDriver:ExecuteScript (Browser.poll_swipes(), called by web_tiles.
# YtAppSession's own poll loop, on the web worker - never the UI thread) and
# does two things every call, in one round trip:
#   1. Installs a touchstart/touchend (+ pointerdown/pointerup fallback,
#      guarded against firing twice for one physical touch - some engines
#      synthesise a compatibility pointer event after a real touch one)
#      listener pair on `document`, exactly once (window.__rp5swipeInstalled
#      is the cheap marker checked every call - this is what "re-inject if
#      missing after navigation" means in practice: the marker lives on
#      `window`, so a fresh top-level navigation's new global naturally
#      clears it and the very next poll re-installs, no separate navigation
#      hook needed).
#   2. Drains window.__rp5swipes (a bounded queue of "up"/"down"/"left"/
#      "right" strings, oldest first) and returns it, clearing the queue.
# Classification (owner's own numbers): a stroke under SWIPE_THRESHOLD_PX
# (60) is a tap, left alone (it still reaches the page as an ordinary
# click/touch - nothing here calls preventDefault, so taps on real leanback
# controls keep working); starting there, the DOMINANT axis (whichever of
# |dx|/|dy| is larger) decides up/down vs left/right, and the swipe's total
# distance on that axis divides into SWIPE_STEP_PX (150)-sized steps (a
# quick short flick is 1 step, a long drag is more), capped at
# SWIPE_MAX_STEPS (5) so one drag can never queue an unbounded run of key
# presses. Sign (which of the two directions on that axis) is decided
# Python-side (YtAppSession._direction_to_key / youtube.tv_swipe_natural),
# not here - this script only ever reports the raw finger direction.
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

# Device Wayland environment (R4 Q4 architecture section). Only applied as
# defaults - real values already in os.environ (e.g. under the real 094
# supervisor) are never overridden.
XDG_RUNTIME_DIR_DEFAULT = "/run/0-runtime-dir"
WAYLAND_DISPLAY_DEFAULT = "wayland-1"

CONNECT_TIMEOUT = 20.0     # time to wait for the marionette socket to appear
HANDSHAKE_TIMEOUT = 10.0
NAVIGATE_TIMEOUT = 30.0    # a full page load (Discord's SPA bundle measured ~5s)
COMMAND_TIMEOUT = 10.0     # back/reload/current_url - no fresh network load
STOP_WAIT_TIMEOUT = 5.0
QUIT_WAIT_TIMEOUT = 6.0   # W2: Marionette:Quit's round trip + Firefox's own shutdown/flush


# --------------------------------------------------------------------------
# Marionette wire protocol - pure functions, unit-tested without a socket
# --------------------------------------------------------------------------

def encode_command(msg_id: int, name: str, params: dict) -> bytes:
    """Build one length-prefixed Marionette command frame:
    `<decimal length>:<json>` where the length is the byte length of the
    JSON body and the body is the 4-element array [0, msg_id, name, params]
    (0 = "this is a command"). Matches Mozilla's own marionette_driver
    client (testing/marionette/client/marionette_driver/transport.py)."""
    payload = json.dumps([0, msg_id, name, params]).encode("utf-8")
    return str(len(payload)).encode("ascii") + b":" + payload


def try_decode_message(buf: bytes):
    """Try to pull one complete length-prefixed frame off the front of
    `buf`. Returns (parsed_json_or_None, remaining_buf). Returns
    (None, buf) unchanged if the buffer does not yet hold a complete
    frame - callers should read more bytes and retry. Never raises for an
    incomplete buffer; a malformed header (non-digits before ':') raises
    ValueError, since that means the stream is corrupt, not just short."""
    if b":" not in buf:
        return None, buf
    header, sep, rest = buf.partition(b":")
    length = int(header)  # ValueError on a corrupt header - not swallowed
    if len(rest) < length:
        return None, buf
    body, remaining = rest[:length], rest[length:]
    return json.loads(body.decode("utf-8")), remaining


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

class Browser:
    """Drives one Firefox instance over its Marionette control channel.

    Firefox is launched once by open(); navigate()/back()/reload()/home()/
    current_url() all reuse the same process and socket. Nothing here ever
    opens a visible window unless RP5DECK_BROWSER_HEADLESS is unset - with
    RP5DECK_BROWSER_HEADLESS=1, --headless is added so tests (and this
    module's own on-device end-to-end check) are always headless.
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
    ):
        self.firefox_bin = firefox_bin
        self.profile_dir = profile_dir
        self.app_id = app_id
        self.marionette_host = marionette_host
        self.marionette_port = marionette_port
        self.home_url = home_url
        self.extra_args = list(extra_args or [])
        self.profile_files = dict(profile_files or {})

        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._msg_id = 0

    # -- command line ------------------------------------------------------

    def _headless(self) -> bool:
        return os.environ.get("RP5DECK_BROWSER_HEADLESS") == "1"

    def build_command(self, url: Optional[str] = None) -> list[str]:
        """Build the firefox argv. Exposed (not just used internally) so
        tests can check the exact flags without launching a real process."""
        cmd = [
            self.firefox_bin,
            "--profile", self.profile_dir,
            "--marionette",           # opens the control channel on 127.0.0.1
            "--name", self.app_id,    # g_set_prgname() -> Wayland app_id
            "--no-remote",            # never reuse/signal an unrelated instance
        ]
        if self._headless():
            cmd.append("--headless")
        cmd += self.extra_args
        if url:
            cmd.append(url)
        return cmd

    def install_profile_files(self) -> list:
        """Copy self.profile_files into the profile when missing or different
        (read at Firefox start, so this runs before every launch). Returns the
        destinations written. A failure is skipped, never fatal: Firefox still
        starts with whatever the profile already holds."""
        written = []
        for rel, src in self.profile_files.items():
            dest = os.path.join(self.profile_dir, *rel.split("/"))
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

    def _build_env(self) -> dict:
        env = dict(os.environ)
        env.setdefault("MOZ_ENABLE_WAYLAND", "1")
        env.setdefault("XDG_RUNTIME_DIR", XDG_RUNTIME_DIR_DEFAULT)
        env.setdefault("WAYLAND_DISPLAY", WAYLAND_DISPLAY_DEFAULT)
        return env

    # -- process lifecycle ---------------------------------------------

    def pid(self) -> Optional[int]:
        """The Firefox main process id while it runs, else None (HF1: main
        records it so a Firefox orphaned by a hard crash of rp5deck can be
        stopped at the next start)."""
        return self._proc.pid if self.is_running() else None

    def terminate_now(self) -> None:
        """SIGTERM Firefox at once, from any thread (Popen.terminate is one
        kill(2) and touches no socket). HF1: the owner's Close must not wait
        behind a control call stuck in a slow page load (NAVIGATE_TIMEOUT is
        30 s) - that call fails fast once the process is gone, and the normal
        close() queued after it then finds nothing left to stop."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def is_running(self) -> bool:
        """True only if the actual OS process is still alive. Never trusts
        an internal flag: a Firefox that crashed under us reads as not
        running the moment this is called, not on the next explicit check."""
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
        """Start Firefox (dedicated profile, Wayland, app_id, Marionette
        control channel on 127.0.0.1) and establish a WebDriver session.
        If an instance from a previous open() is still alive, this reuses
        it and just navigates - it does not launch a second Firefox.
        Returns True once a session is live (and, if `url` was given, once
        that navigation has completed); False if Firefox could not be
        launched, the control channel never came up, or the session/
        navigation failed."""
        if self.is_running() and self._sock is not None:
            if url:
                return self.navigate(url)
            return True

        self.close()  # clear out anything stale before a fresh launch
        self.install_profile_files()

        try:
            self._proc = subprocess.Popen(
                self.build_command(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._build_env(),
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
        """Terminate Firefox if running. Always leaves no process behind
        either way; safe to call whether or not open() ever succeeded.

        W2 (owner: "the browser needs to save tabs when closed"): asks
        Firefox to quit ITSELF first (Marionette:Quit - a real application
        shutdown, running the same "quit-application" observers a normal
        Quit does, which is what flushes sessionstore/cookies) before
        falling back to the old SIGTERM -> SIGKILL escalation if it does
        not exit in time. WebDriver:DeleteSession (sent here before W2)
        only ends the WebDriver session, not the application - close()
        always needed a following terminate() for that reason, which is
        why this replaces it rather than sending both.

        Best-effort first: navigates to home_url (a short, fixed timeout -
        Close must never wait behind a slow page the way a real navigation
        can, see terminate_now()'s own doc), so whichever tab session
        restore resumes as ACTIVE next time is never mid-Discord or the
        YouTube TV app ("make sure it doesn't reopen the TV UI inside the
        normal Browser tile") - every OTHER open tab is untouched, and the
        explicit navigate() every tile tap already does (web_tiles.py)
        corrects the active tab again the moment a tile is next used
        anyway."""
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
            # _send drops the socket itself when the send fails (Firefox
            # already gone): take whatever is left, never .close() a None
            # (test day 24 Sep: AttributeError here at the Close tap).
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
                # Marionette:Quit's response only means "shutdown started",
                # not "the process has exited" - give it a moment before
                # falling back to signals.
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

    # -- Marionette transport -------------------------------------------

    def _recv(self, timeout: float = COMMAND_TIMEOUT):
        """Read exactly one complete Marionette message. Returns the parsed
        JSON array, or None on any failure (timeout, socket closed,
        malformed stream) - never raises for the "no reply in time" case,
        so callers cannot hang forever."""
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
        """Send one Marionette/WebDriver command and return its response
        array `[1, id, error, result]`, or None on any transport failure.
        Never raises."""
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
        """Run a script in the current page (WebDriver:ExecuteScript; the
        body's `return` value comes back). Returns (ok, value): ok is False
        on any transport or script error, so a caller can tell "the script
        said false" from "no answer"."""
        resp = self._send("WebDriver:ExecuteScript",
                          {"script": script, "args": list(args or [])})
        if resp is None or resp[2] is not None:
            return False, None
        result = resp[3]
        return True, (result.get("value") if isinstance(result, dict) else None)

    def text_input_state(self) -> Optional[dict]:
        """{"editable": bool, "focus": bool} for the page's focused element
        (TEXT_INPUT_PROBE), or None when Firefox did not answer - None is
        "unknown", never "not editable"."""
        ok, value = self.execute_script(TEXT_INPUT_PROBE)
        if not ok or not isinstance(value, dict):
            return None
        return {"editable": bool(value.get("editable")), "focus": bool(value.get("focus"))}

    def scroll_focused_into_view(self) -> bool:
        ok, value = self.execute_script(SCROLL_FOCUSED_INTO_VIEW)
        return bool(ok and value)

    def poll_swipes(self) -> Optional[list]:
        """YT4: ensure the swipe listener is installed (SWIPE_POLL_SCRIPT's
        own cheap marker check - a no-op if it already is) and drain
        whatever it queued since the last call. Returns a list of "up"/
        "down"/"left"/"right" strings (oldest first, possibly empty - a
        real "checked, nothing happened"), or None if the page did not
        answer at all (mid-navigation, a crashed content process, ...) -
        never raises. Callers (YtAppSession) re-poll regardless of which of
        these it gets; None is not itself an error worth surfacing."""
        ok, value = self.execute_script(SWIPE_POLL_SCRIPT)
        if not ok or not isinstance(value, list):
            return None
        return [v for v in value if isinstance(v, str)]

    def send_key(self, key: str) -> bool:
        """W2: one WebDriver key press (a WEBDRIVER_KEYS name, e.g. "up" /
        "ok" / "back") via WebDriver:PerformActions - the YouTube TV tile's
        D-pad (screens.Bar's tv_* buttons) never scripts or clicks the page
        like the rest of this class; leanback takes bare key events."""
        code = WEBDRIVER_KEYS.get(key)
        if code is None:
            raise ValueError("unknown key %r" % (key,))
        actions = [{"type": "key", "id": "keyboard",
                   "actions": [{"type": "keyDown", "value": code},
                              {"type": "keyUp", "value": code}]}]
        resp = self._send("WebDriver:PerformActions", {"actions": actions})
        return bool(resp is not None and resp[2] is None)


# --------------------------------------------------------------------------
# On-device end-to-end check (RP5DECK_BROWSER_HEADLESS=1 always for this)
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
