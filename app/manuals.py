#!/usr/bin/env python3
"""manuals: finds, renders and caches a game's PDF manual for the bottom screen (1920x1080
landscape), natively, no Firefox or pdf.js.

What it relies on, checked on the device:

libpoppler and pdftoppm are installed. No pdftocairo is assumed.

Gamelists are at /storage/games-internal/roms/<system>/gamelist.xml and/or
/storage/roms/<system>/gamelist.xml. Both can exist for one system and either can have the
manual, so both get tried.

A <game>'s <manual> is relative to that gamelist's own folder (like
./manuals/Foo-manual.pdf), not the folder rom_path is under.

ES here escapes only & in text (&amp;) and writes apostrophes as is, which is valid XML.
Nothing here does its own escaping. Each <game> block goes through xml.etree, which
unescapes &amp;, and the plain text gets compared to plain filesystem paths, so apostrophes
just work.

<game> blocks are pulled out with a regex first and parsed one at a time instead of parsing
the whole gamelist, so a second root element (<alternativeEmulator> beside <gameList>) or one
broken <game> doesnt lose every other game.

Pipeline:
  find_manual(rom_path, system)              -> Path | None
  page_count(pdf_path)                       -> int  | None
  render_page(pdf_path, page, ...)           -> Path | None   (cached PNG)
  render_spread(pdf_path, left_page, ...)    -> (Path|None, Path|None)
  spread_layout(left_size, right_size, ...)  -> dict           (pure math)
  prerender(pdf_path, pages, ...)            -> ticket          (latest wins)
  prerender_next_async(pdf_path, page, ...)  -> ticket          (fire and forget)
  png_size(png_path)                         -> (w, h) | None

The cache only ever shows complete files. pdftoppm writes a private temp name in the cache
folder (`<key>.<pid>.<thread>.tmp.png`) and a finished PNG gets renamed onto the key, so a hit
never returns a half written file and a failure only deletes its own temp. Two renders of the
same page in this process share one pdftoppm, the second waits for the first. Prerendering
runs on one bounded background worker and prerender() replaces whatever's still pending, so
fast page turns never pile up pdftoppm processes.

Manual pages are portrait on a landscape screen, so one page fit to height leaves wide empty
sides. render_spread renders page N and N+1 separately (each scaled to the height by
pdftoppm) and spread_layout() works out the placement so the UI draws both side by side,
centred with a gap. Nothing composites the two PNGs, there's no PIL on the device and the UI
composites at draw time anyway.

Never raises into the UI. A missing manual, a corrupt PDF, a missing pdftoppm/pdfinfo or a
pdftoppm failure all return None ((None, None) for a spread) and log why at WARNING.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

log = logging.getLogger("rp5deck.manuals")

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

SCREEN_W = 1920
SCREEN_H = 1080

# both are checked in this order for every system, a manual can be under either
DEFAULT_ROM_ROOTS = ("/storage/games-internal/roms", "/storage/roms")

DEFAULT_CACHE_DIR = "/storage/rp5deck/cache/manuals"
DEFAULT_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB, LRU capped by size
DEFAULT_RENDER_TIMEOUT = 20.0
NICE_LEVEL = 10

_GAME_BLOCK_RE = re.compile(r"<game(?:\s[^>]*)?>.*?</game>", re.DOTALL | re.IGNORECASE)
_PDFINFO_PAGES_RE = re.compile(r"^Pages:\s*(\d+)", re.MULTILINE)
_PAGE_OBJ_RE = re.compile(rb"/Type\s*/Page(?!s)\b")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# Process helper, the only place that touches subprocess
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float):
    """Runs a command and captures output. Never raises, never hangs.

    Returns (returncode, stdout, stderr). returncode is None if the process never gave a result
    (missing binary, timeout, any OS failure), and callers must treat that as a failure whatever
    stdout says.
    """
    if shutil.which(cmd[0]) is None:
        return None, "", "binary not found: %s" % cmd[0]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, "", "exec failed or timed out (%s): %s" % (type(e).__name__, " ".join(cmd))
    return proc.returncode, proc.stdout, proc.stderr


def _niced(cmd: list[str]) -> list[str]:
    if shutil.which("nice") is not None:
        return ["nice", "-n", str(NICE_LEVEL)] + cmd
    return cmd


# --------------------------------------------------------------------------
# 1. Finding a game's manual
# --------------------------------------------------------------------------

def _iter_games(xml_text: str):
    """Yields one parsed <game> Element per block in the raw gamelist text. Each block is parsed on
    its own, so another root element or one broken <game> cant take out the rest.
    """
    for m in _GAME_BLOCK_RE.finditer(xml_text):
        block = m.group(0)
        try:
            yield ET.fromstring(block)
        except ET.ParseError as e:
            log.debug("skipping unparsable <game> block: %s", e)
            continue


def _field(elem: ET.Element, tag: str) -> Optional[str]:
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def gamelist_paths(system: str, roots=DEFAULT_ROM_ROOTS) -> list[str]:
    """Candidate gamelist.xml paths for `system` in root order, only the ones that exist. Public so
    a survey script can report which layout a device uses.
    """
    return [p for p in (os.path.join(root, system, "gamelist.xml") for root in roots)
            if os.path.isfile(p)]


def find_manual(rom_path, system: str, roots=DEFAULT_ROM_ROOTS) -> Optional[Path]:
    """Finds the PDF manual for one game.

    rom_path: the game's ROM path as ES knows it, it doesnt have to be under the same root as
              the gamelist that matched
    system:   the ES system name (like "snes")

    Tries, in order:
      1. Each existing gamelist.xml for `system`: find the <game> whose <path> has the same
         basename as rom_path, and if its <manual> points at a real file (relative to that
         gamelist's folder) return it.
      2. A manuals/ folder next to the ROM's folder: `<stem>-manual.pdf` (how the device names
         them) then `<stem>.pdf`.
      3. None, logged at INFO.
    """
    rom_path = os.fspath(rom_path)
    rom_base = os.path.basename(rom_path)
    stem = os.path.splitext(rom_base)[0]

    for gamelist in gamelist_paths(system, roots):
        try:
            with open(gamelist, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            log.warning("cannot read %s: %s", gamelist, e)
            continue
        gdir = os.path.dirname(gamelist)
        for elem in _iter_games(text):
            path_text = _field(elem, "path")
            if not path_text or os.path.basename(path_text) != rom_base:
                continue
            manual_text = _field(elem, "manual")
            if manual_text:
                candidate = os.path.normpath(os.path.join(gdir, manual_text))
                if os.path.isfile(candidate):
                    return Path(candidate)
                log.info("%s: <manual> %s does not exist on disk", gamelist, candidate)
            break  # matched the game in this gamelist, try the next root

    rom_dir = os.path.dirname(rom_path)
    for name in (stem + "-manual.pdf", stem + ".pdf"):
        candidate = os.path.join(rom_dir, "manuals", name)
        if os.path.isfile(candidate):
            return Path(candidate)

    log.info("no manual found for %s (system=%s)", rom_path, system)
    return None


# --------------------------------------------------------------------------
# 2. Page count
# --------------------------------------------------------------------------

def _count_page_objects(pdf_path: str) -> Optional[int]:
    """Page count without pdfinfo: counts `/Type /Page` markers in the raw bytes (not
    `/Type /Pages`, the page tree node, thanks to the negative lookahead). Can be wrong for an
    odd page tree (a page referenced twice, or split into an object stream), but right for the
    plain manuals we actually get, and it doesnt need poppler.
    """
    try:
        with open(pdf_path, "rb") as f:
            data = f.read()
    except OSError as e:
        log.warning("cannot read %s: %s", pdf_path, e)
        return None
    n = len(_PAGE_OBJ_RE.findall(data))
    return n or None


def page_count(pdf_path, timeout: float = DEFAULT_RENDER_TIMEOUT) -> Optional[int]:
    """Number of pages in a PDF. Uses pdfinfo, falls back to a raw byte scan if pdfinfo is missing
    or fails. None if neither works, like a corrupt file.
    """
    pdf_path = os.fspath(pdf_path)
    if shutil.which("pdfinfo") is not None:
        rc, out, err = _run(["pdfinfo", pdf_path], timeout=timeout)
        if rc == 0:
            m = _PDFINFO_PAGES_RE.search(out)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
        log.warning("pdfinfo failed for %s (rc=%s): %s", pdf_path, rc, (err or "").strip())
    return _count_page_objects(pdf_path)


# --------------------------------------------------------------------------
# 3. Cache: key, touch (LRU recency), eviction
# --------------------------------------------------------------------------

def _cache_key(pdf_path: str, page: int, height: int) -> str:
    """A filename built from the pdf path + mtime + size + page + height, so an edited or rescraped
    manual or a different page or size never hits a stale entry.
    """
    try:
        st = os.stat(pdf_path)
        mtime_ns, size = st.st_mtime_ns, st.st_size
    except OSError:
        mtime_ns, size = 0, 0
    h = hashlib.sha256()
    h.update(os.path.abspath(pdf_path).encode("utf-8", "surrogateescape"))
    h.update(("|%d|%d|p%d|h%d" % (mtime_ns, size, page, height)).encode("ascii"))
    stem = _SAFE_STEM_RE.sub("_", os.path.splitext(os.path.basename(pdf_path))[0])[:40]
    return "%s-p%04d-h%d-%s.png" % (stem, page, height, h.hexdigest()[:16])


def _touch(path: str) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


TMP_SUFFIX = ".tmp.png"
TMP_MAX_AGE = 3600.0


def _evict(cache_dir: str, max_bytes: int = DEFAULT_CACHE_MAX_BYTES) -> None:
    """LRU eviction by size: deletes the least recently touched PNGs until the cache is under
    `max_bytes`. Recency is mtime (a hit calls _touch()), so it's real LRU, not FIFO. Another
    render's temp file is never touched since it's still being written, but one older than
    TMP_MAX_AGE is a leftover and goes.
    """
    try:
        entries = []
        total = 0
        now = time.time()
        with os.scandir(cache_dir) as it:
            for e in it:
                if not e.is_file():
                    continue
                st = e.stat()
                if e.name.endswith(TMP_SUFFIX):
                    if now - st.st_mtime > TMP_MAX_AGE:
                        try:
                            os.remove(e.path)
                        except OSError:
                            pass
                    continue
                entries.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
    except OSError as e:
        log.warning("cannot scan cache dir %s: %s", cache_dir, e)
        return
    if total <= max_bytes:
        return
    entries.sort(key=lambda t: t[0])  # oldest (least recently touched) first
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


# --------------------------------------------------------------------------
# 4. Rendering
# --------------------------------------------------------------------------

def render_page(pdf_path, page: int, cache_dir=DEFAULT_CACHE_DIR,
                 height: int = SCREEN_H, timeout: float = DEFAULT_RENDER_TIMEOUT,
                 max_cache_bytes: int = DEFAULT_CACHE_MAX_BYTES) -> Optional[Path]:
    """Renders one PDF page to a cached PNG, fit to height (`-scale-to-y H -scale-to-x -1`, so the
    width follows the page's aspect). Runs pdftoppm niced with a timeout and `-singlefile` so the
    output is exactly `<prefix>.png`. A cache hit never starts a process. Returns None and logs
    why on any failure: missing file, bad page number, missing pdftoppm, timeout, bad exit, or a
    PDF pdftoppm rejects.
    """
    pdf_path = os.fspath(pdf_path)
    if page < 1:
        log.warning("render_page: page must be >= 1, got %d", page)
        return None
    if not os.path.isfile(pdf_path):
        log.warning("render_page: no such file: %s", pdf_path)
        return None
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        log.error("cannot create cache dir %s: %s", cache_dir, e)
        return None

    key = _cache_key(pdf_path, page, height)
    out_path = os.path.join(cache_dir, key)
    for _attempt in range(2):
        if os.path.isfile(out_path):  # only ever shows up complete, by rename
            _touch(out_path)
            return Path(out_path)
        with _inflight_lock:
            busy = _inflight.get(out_path)
            if busy is None:
                mine = _inflight[out_path] = threading.Event()
        if busy is not None:
            # the same page is rendering right now (prerender vs a tap), wait for it instead of racing it
            # on the same file
            busy.wait(timeout + 5.0)
            continue
        try:
            return _render_into_cache(pdf_path, page, out_path, height, timeout,
                                      cache_dir, max_cache_bytes)
        finally:
            with _inflight_lock:
                _inflight.pop(out_path, None)
            mine.set()
    log.warning("render_page: %s page %d: a concurrent render did not produce it",
                pdf_path, page)
    return None


_inflight_lock = threading.Lock()
_inflight = {}          # cache path -> Event set when that render finishes


def _render_into_cache(pdf_path, page, out_path, height, timeout, cache_dir, max_cache_bytes):
    """pdftoppm into a private temp name, then an atomic rename onto the key"""
    prefix = "%s.%d.%d.tmp" % (out_path[:-len(".png")], os.getpid(), threading.get_ident())
    tmp_png = prefix + ".png"
    cmd = _niced(["pdftoppm", "-png", "-f", str(page), "-l", str(page),
                  "-scale-to-y", str(height), "-scale-to-x", "-1",
                  "-singlefile", pdf_path, prefix])
    rc, _out, err = _run(cmd, timeout=timeout)
    if rc != 0 or not os.path.isfile(tmp_png):
        log.warning("pdftoppm failed for %s page %d (rc=%s): %s",
                    pdf_path, page, rc, (err or "").strip())
        _remove_quietly(tmp_png)  # only ever our own temp file
        return None
    try:
        os.replace(tmp_png, out_path)
    except OSError as e:
        log.warning("cannot publish %s: %s", out_path, e)
        _remove_quietly(tmp_png)
        return None
    _evict(cache_dir, max_cache_bytes)
    return Path(out_path)


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def render_spread(pdf_path, left_page: int, cache_dir=DEFAULT_CACHE_DIR,
                   height: int = SCREEN_H, timeout: float = DEFAULT_RENDER_TIMEOUT,
                   total_pages: Optional[int] = None):
    """Renders `left_page` and `left_page + 1` at the same height so the UI can put them side by
    side. Returns (left_path, right_path). right is None when left_page is the last page (per
    `total_pages` if given) or its render fails, and the caller shows one centred page instead.
    left is None only if left_page itself fails.
    """
    left = render_page(pdf_path, left_page, cache_dir, height, timeout)
    if left is None:
        return None, None
    right_page = left_page + 1
    if total_pages is not None and right_page > total_pages:
        return left, None
    right = render_page(pdf_path, right_page, cache_dir, height, timeout)
    return left, right


class PrerenderTicket:
    """What prerender() returns. join(timeout) waits until every page it asked for was rendered,
    failed or replaced, and is_alive() is True until then.
    """

    def __init__(self, n):
        self._left = n
        self._lock = threading.Lock()
        self._done = threading.Event()
        if n <= 0:
            self._done.set()

    def _one_done(self):
        with self._lock:
            self._left -= 1
            if self._left <= 0:
                self._done.set()

    def join(self, timeout=None):
        return self._done.wait(timeout)

    def is_alive(self):
        return not self._done.is_set()


class _Prerenderer:
    """One daemon thread renders queued pages one at a time, niced like every render. The queue is
    bounded (MAX_PENDING) and prerender() replaces what's pending, only the newest spread's
    neighbours matter.
    """
    MAX_PENDING = 4

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = collections.deque()
        self._thread = None
        self.rendered = 0
        self.superseded = 0
        self.threads_started = 0

    def request(self, pdf_path, pages, cache_dir, height, timeout, replace):
        pages = [int(p) for p in pages if int(p) >= 1]
        ticket = PrerenderTicket(len(pages))
        with self._cond:
            if replace:
                while self._pending:
                    self._pending.popleft()[-1]._one_done()
                    self.superseded += 1
            for p in pages:
                self._pending.append((pdf_path, p, cache_dir, height, timeout, ticket))
            while len(self._pending) > self.MAX_PENDING:
                self._pending.popleft()[-1]._one_done()
                self.superseded += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="rp5deck-manual-prerender")
                self.threads_started += 1
                self._thread.start()
            self._cond.notify()
        return ticket

    def _run(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                pdf_path, page, cache_dir, height, timeout, ticket = self._pending.popleft()
            try:
                render_page(pdf_path, page, cache_dir, height, timeout)
                self.rendered += 1
            except Exception:
                log.exception("prerender failed for %s page %d", pdf_path, page)
            finally:
                ticket._one_done()


_PRERENDER = _Prerenderer()


def prerender(pdf_path, pages, cache_dir=DEFAULT_CACHE_DIR, height: int = SCREEN_H,
              timeout: float = DEFAULT_RENDER_TIMEOUT) -> PrerenderTicket:
    """Warms the cache for `pages` on the background worker, dropping any earlier request still
    pending. Never raises or blocks.
    """
    try:
        return _PRERENDER.request(os.fspath(pdf_path), pages, cache_dir, height, timeout,
                                  replace=True)
    except Exception:
        log.exception("prerender request failed")
        return PrerenderTicket(0)


def prerender_next_async(pdf_path, page: int, cache_dir=DEFAULT_CACHE_DIR,
                          height: int = SCREEN_H, timeout: float = DEFAULT_RENDER_TIMEOUT
                          ) -> PrerenderTicket:
    """Fire and forget: renders page + 1 on the background worker so the next tap or swipe is
    usually a cache hit (queued behind earlier requests, not replacing them, the queue stays
    bounded). Never raises or blocks, a failure is only logged. Returns a ticket with
    .join()/.is_alive() for anything that wants to wait.
    """
    try:
        return _PRERENDER.request(os.fspath(pdf_path), [page + 1], cache_dir, height, timeout,
                                  replace=False)
    except Exception:
        log.exception("prerender request failed")
        return PrerenderTicket(0)


# --------------------------------------------------------------------------
# 5. Spread geometry (pure math, no files or subprocess)
# --------------------------------------------------------------------------

def spread_layout(left_size, right_size=None, screen=(SCREEN_W, SCREEN_H), gap: int = 8):
    """Given the rendered PNG sizes (w, h) for one or two pages (both already the same height),
    returns the placement as {"left": (x, y, w, h), "right": (x, y, w, h) | None}, centred as a
    unit with `gap` px between. Pure geometry so it tests without a real PDF.
    """
    # Pages come in fit to height, so anything wider than a portrait page (a landscape scan, a
    # spread scanned as one page, two wide pages side by side) would run off both edges.
    # Everything gets shrunk by one common factor until it fits both ways, portrait pairs that
    # already fit stay as they are.
    screen_w, screen_h = screen
    lw, lh = left_size
    rw, rh = right_size if right_size is not None else (0, 0)
    g = gap if right_size is not None else 0
    pages_w = lw + rw                    # the gap stays fixed, only the pages shrink
    tall = max(lh, rh)
    k = min(1.0, (screen_w - g) / float(pages_w) if pages_w else 1.0,
            screen_h / float(tall) if tall else 1.0)
    sc = lambda v: int(v * k)
    lw, lh = sc(lw), sc(lh)
    if right_size is None:
        x = (screen_w - lw) // 2
        y = (screen_h - lh) // 2
        return {"left": (x, y, lw, lh), "right": None}
    rw, rh = sc(rw), sc(rh)
    total_w = lw + g + rw
    x_left = (screen_w - total_w) // 2
    x_right = x_left + lw + g
    y_left = (screen_h - lh) // 2
    y_right = (screen_h - rh) // 2
    return {"left": (x_left, y_left, lw, lh), "right": (x_right, y_right, rw, rh)}


def png_size(png_path) -> Optional[tuple[int, int]]:
    """(width, height) straight from a PNG's IHDR chunk. There's no PIL on the device and the
    header is all spread_layout() needs.
    """
    try:
        with open(png_path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            f.read(4)  # chunk length, always 13 for IHDR
            if f.read(4) != b"IHDR":
                return None
            w, h = struct.unpack(">II", f.read(8))
            return w, h
    except (OSError, struct.error):
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: manuals.py <rom_path> <system>")
        raise SystemExit(2)
    m = find_manual(sys.argv[1], sys.argv[2])
    print(m or "no manual found")
    if m:
        print("pages:", page_count(m))
