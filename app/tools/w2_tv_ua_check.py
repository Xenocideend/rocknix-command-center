#!/usr/bin/env python3
"""Device proof for the YouTube TV tile's dedicated Firefox profile (W2b).

Run on the RP5 while the YouTube TV tile is open (tap it, wait for the page
to load) - NEVER on a PC (no real Firefox/Marionette to connect to):

    python3 tools/w2_tv_ua_check.py

Connects directly to the ALREADY-RUNNING tv-profile Firefox's Marionette
port (browser.TV_MARIONETTE_PORT, 2829 - never the main Browser/Discord
profile's port 2828) and reads back navigator.userAgent/location.href with
WebDriver:ExecuteScript, using the same wire primitives browser.py's own
Browser class uses (encode_command/try_decode_message) - no new dependency,
and this never launches or closes Firefox itself, only observes it.

Expects userAgent to be browser.YOUTUBE_TV_USER_AGENT (the TV
string firefox/tvprofile-user.js sets; PS4 Leanback since 24 Sep) and location.href to already be on
https://www.youtube.com/tv (not accounts.google.com - the exact failure
mode Main reported: a global user-agent override in the WRONG, shared
profile did nothing, and Google served the normal desktop sign-in page
instead of the TV UI). PASS/FAIL to stdout; exit 0 iff both hold.

UNVERIFIED assumption, called out here rather than hidden: rp5deck's own
YtAppSession already holds the one Marionette SESSION this server allows
(Marionette generally supports a single active session per server, not per
TCP connection) - this script does NOT call WebDriver:NewSession for that
reason (it would very likely error "session already started" and abort
before reading anything back). It sends WebDriver:ExecuteScript directly
over its OWN new connection instead, on the understanding that a session is
a property of the Marionette SERVER, not the specific socket that created
it - if that is wrong on this Firefox build, this script will fail cleanly
with whatever error Marionette actually returns, which is itself useful
device-checklist evidence (Y-series, patches/W2-NOTES.md).
"""
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import browser  # noqa: E402

HOST = "127.0.0.1"
CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 10.0

READ_UA_AND_URL = "return [navigator.userAgent, location.href];"


def _recv_one(sock, buf, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg, buf = browser.try_decode_message(buf)
        if msg is not None:
            return msg, buf
        sock.settimeout(max(0.1, deadline - time.time()))
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("Marionette closed the connection")
        buf += chunk
    raise TimeoutError("no reply from Marionette within %.1fs" % timeout)


def main():
    ok = True
    print("connecting to 127.0.0.1:%d (browser.TV_MARIONETTE_PORT) ..." %
         browser.TV_MARIONETTE_PORT)
    try:
        sock = socket.create_connection((HOST, browser.TV_MARIONETTE_PORT),
                                        timeout=CONNECT_TIMEOUT)
    except OSError as e:
        print("FAIL  could not connect: %s "
             "(is the YouTube TV tile open on the device right now?)" % e)
        return 1

    buf = b""
    try:
        hello, buf = _recv_one(sock, buf, COMMAND_TIMEOUT)
        print("PASS  Marionette handshake: %s" % json.dumps(hello))

        msg_id = 1
        frame = browser.encode_command(msg_id, "WebDriver:ExecuteScript",
                                       {"script": READ_UA_AND_URL, "args": []})
        sock.sendall(frame)
        resp, buf = _recv_one(sock, buf, COMMAND_TIMEOUT)
        print("raw ExecuteScript response: %s" % json.dumps(resp))
        error, result = resp[2], resp[3]
        if error is not None:
            print("FAIL  ExecuteScript returned an error: %s" % json.dumps(error))
            return 1
        ua, href = (result or {}).get("value") or (None, None)
    except Exception as e:                # noqa: BLE001 - report, do not traceback-spam
        print("FAIL  %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass

    print("navigator.userAgent = %r" % ua)
    print("location.href       = %r" % href)

    if ua == browser.YOUTUBE_TV_USER_AGENT:
        print("PASS  userAgent matches browser.YOUTUBE_TV_USER_AGENT exactly")
    else:
        print("FAIL  userAgent does NOT match browser.YOUTUBE_TV_USER_AGENT\n"
             "      expected: %r" % browser.YOUTUBE_TV_USER_AGENT)
        ok = False

    if isinstance(href, str) and href.startswith("https://www.youtube.com/tv"):
        print("PASS  location.href is the TV UI (youtube.com/tv), not the desktop site")
    else:
        print("FAIL  location.href is NOT youtube.com/tv - if this is "
             "accounts.google.com/... the UA override is not taking effect "
             "(see patches/W2-NOTES.md, checklist item Y1)")
        ok = False

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
