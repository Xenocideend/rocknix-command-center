#!/usr/bin/env python3
"""HF1 device check (headless, no display needed, touches nothing else):
does browser.TEXT_INPUT_PROBE really tell a focused text field from the
page body? There is no JS engine on the PC, so this is where it gets run.

    RP5DECK_BROWSER_HEADLESS=1 python3 tools/hf1_probe_check.py

Opens Firefox headless on a local file: page (no network needed), focuses each
element through Marionette and prints what the probe says. Exit 0 only if
every row matches; Firefox is closed either way. Needs port 2828 free (no
other rp5deck Firefox running).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["RP5DECK_BROWSER_HEADLESS"] = "1"
import browser  # noqa: E402

HTML = ("<input id=t type=text><input id=p type=password>"
        "<input id=c type=checkbox><input id=r type=text readonly>"
        "<textarea id=a></textarea><div id=e contenteditable=true>x</div>"
        "<button id=b>b</button>")
# A file: page, not data: - Firefox refuses top-level navigation to data:
# URLs (security.data_uri.block_toplevel_data_uri_navigations), which made
# open() fail on the device, 24 Sep.
PAGE_FILE = "/tmp/rp5deck-hf1-probe.html"
PAGE = "file://" + PAGE_FILE
CASES = [("t", True), ("p", True), ("c", False), ("r", False), ("a", True), ("e", True),
         ("b", False), (None, False)]


def main():
    with open(PAGE_FILE, "w") as f:
        f.write("<!doctype html><html><body>%s</body></html>" % HTML)
    b = browser.Browser()
    ok_all = True
    try:
        if not b.open(PAGE):
            print("FAIL: Firefox did not open (is another instance holding port 2828?)")
            return 2
        for el, want in CASES:
            if el is None:
                b.execute_script("document.activeElement && document.activeElement.blur();")
            else:
                b.execute_script("document.getElementById(%r).focus();" % el)
            st = b.text_input_state()
            got = None if st is None else st["editable"]
            ok = got == want
            ok_all &= ok
            print("%-5s %-8s editable=%-5s want=%-5s %s" % ("PASS" if ok else "FAIL", el or "(body)",
                                                          got, want, st))
    finally:
        b.close()
        print("firefox still running after close:", b.is_running())
    print("ALL PASS" if ok_all else "SOME FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
