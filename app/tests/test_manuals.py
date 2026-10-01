#!/usr/bin/env python3
"""manuals.py: find/render/cache a game's PDF manual.

Offline. `pdftoppm`/`pdfinfo`/`nice` are not installed on this PC (verified:
`where pdftoppm`/`where pdfinfo` found nothing), so every render/page-count
path that would spawn one is exercised through a mocked `manuals._run`
rather than a real subprocess. The one place that reads real PDF bytes
(`_count_page_objects`, the pdfinfo-less fallback) is tested against a real,
tiny, hand-built multi-page PDF - see `make_pdf_bytes()` below and the
committed twin at fixtures/manuals-3page-SYNTHETIC.pdf - so that logic is
proven against actual PDF structure, not just a mock.
"""
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import manuals  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def make_pdf_bytes(num_pages=3, page_size=(300, 400)):
    """A tiny, valid, hand-built multi-page PDF - no reportlab/PIL. Blank
    pages; just enough object/xref structure for poppler to open it and for
    _count_page_objects to count real `/Type /Page` object markers. SYNTHETIC
    (also committed as fixtures/manuals-3page-SYNTHETIC.pdf for 3 pages)."""
    w, h = page_size
    page_ids = [3 + 2 * i for i in range(num_pages)]
    content_ids = [4 + 2 * i for i in range(num_pages)]
    bodies = {1: "<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join("%d 0 R" % pid for pid in page_ids)
    bodies[2] = "<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, num_pages)
    for pid, cid in zip(page_ids, content_ids):
        bodies[pid] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                        "/Resources << >> /Contents %d 0 R >>" % (w, h, cid))
        bodies[cid] = ("STREAM", b"BT ET")
    n_objs = 2 + num_pages * 2
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for idx in range(1, n_objs + 1):
        offsets[idx] = len(out)
        out += ("%d 0 obj\n" % idx).encode()
        body = bodies[idx]
        if isinstance(body, tuple):
            data = body[1]
            out += ("<< /Length %d >>\nstream\n" % len(data)).encode()
            out += data
            out += b"\nendstream\n"
        else:
            out += body.encode() + b"\n"
        out += b"endobj\n"
    xref_off = len(out)
    out += ("xref\n0 %d\n" % (n_objs + 1)).encode()
    out += b"0000000000 65535 f \n"
    for idx in range(1, n_objs + 1):
        out += ("%010d 00000 n \n" % offsets[idx]).encode()
    out += b"trailer\n"
    out += ("<< /Size %d /Root 1 0 R >>\n" % (n_objs + 1)).encode()
    out += b"startxref\n"
    out += (str(xref_off) + "\n").encode()
    out += b"%%EOF"
    return bytes(out)


# --------------------------------------------------------------------------
# 1. Finding a manual
# --------------------------------------------------------------------------

class TestFindManual(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.root1 = os.path.join(self.d, "games-internal", "roms")
        self.root2 = os.path.join(self.d, "roms")
        self.sys_dir = os.path.join(self.root1, "snes")
        os.makedirs(self.sys_dir)
        os.makedirs(os.path.join(self.sys_dir, "manuals"))
        with open(os.path.join(FIXTURES, "manuals-gamelist-snes-sample-SYNTHETIC.xml"),
                  encoding="utf-8") as f:
            self.gamelist_xml = f.read()
        with open(os.path.join(self.sys_dir, "gamelist.xml"), "w", encoding="utf-8") as f:
            f.write(self.gamelist_xml)
        # Only two of the five referenced manuals actually exist on disk -
        # "Dangling Manual Reference" deliberately does not, to test that a
        # <manual> field pointing nowhere is not treated as a match.
        for name in (
            "90 Minutes - European Prime Goal (Europe)-manual.pdf",
            "Bob's Adventure (USA)-manual.pdf",
            "Asterix & Obelix (Europe)-manual.pdf",
        ):
            Path(self.sys_dir, "manuals", name).write_bytes(b"%PDF-1.4 dummy")

    def tearDown(self):
        shutil.rmtree(self.d)

    def find(self, rom_name):
        rom_path = os.path.join(self.sys_dir, rom_name)
        return manuals.find_manual(rom_path, "snes", roots=(self.root1, self.root2))

    def test_relative_path_resolved_against_gamelist_dir(self):
        m = self.find("90 Minutes - European Prime Goal (Europe).sfc")
        self.assertEqual(
            m, Path(self.sys_dir, "manuals", "90 Minutes - European Prime Goal (Europe)-manual.pdf"))

    def test_apostrophe_in_filename_matches(self):
        m = self.find("Bob's Adventure (USA).sfc")
        self.assertEqual(m, Path(self.sys_dir, "manuals", "Bob's Adventure (USA)-manual.pdf"))

    def test_entity_escaped_ampersand_matches_literal_ampersand_on_disk(self):
        # <path> in the gamelist is "Asterix &amp; Obelix ...", the real ROM
        # filename (and our lookup key) has a literal "&". ElementTree
        # un-escapes &amp; on parse, so the comparison is plain-text vs
        # plain-text - this must match.
        m = self.find("Asterix & Obelix (Europe).sfc")
        self.assertEqual(m, Path(self.sys_dir, "manuals", "Asterix & Obelix (Europe)-manual.pdf"))

    def test_gamelist_entry_without_manual_field_is_none(self):
        self.assertIsNone(self.find("No Manual Game (USA).sfc"))

    def test_manual_field_pointing_at_missing_file_is_not_a_match(self):
        self.assertIsNone(self.find("Dangling Manual Reference (USA).sfc"))

    def test_unknown_rom_falls_through_to_none(self):
        self.assertIsNone(self.find("Totally Unknown Game.sfc"))

    def test_second_root_used_when_first_roots_entry_has_no_manual(self):
        # Same system, second root: a gamelist that DOES have the manual for
        # a ROM the first root's gamelist lists with no <manual> field.
        sys_dir2 = os.path.join(self.root2, "snes")
        os.makedirs(os.path.join(sys_dir2, "manuals"))
        with open(os.path.join(sys_dir2, "gamelist.xml"), "w", encoding="utf-8") as f:
            f.write("<gameList><game><path>./No Manual Game (USA).sfc</path>"
                    "<manual>./manuals/No Manual Game (USA)-manual.pdf</manual>"
                    "</game></gameList>")
        Path(sys_dir2, "manuals", "No Manual Game (USA)-manual.pdf").write_bytes(b"%PDF dummy")
        m = self.find("No Manual Game (USA).sfc")
        self.assertEqual(m, Path(sys_dir2, "manuals", "No Manual Game (USA)-manual.pdf"))

    def test_conventional_manuals_folder_used_when_no_gamelist_at_all(self):
        rom_dir = os.path.join(self.d, "other-roms", "gba")
        os.makedirs(os.path.join(rom_dir, "manuals"))
        Path(rom_dir, "manuals", "Some Game-manual.pdf").write_bytes(b"%PDF dummy")
        m = manuals.find_manual(os.path.join(rom_dir, "Some Game.gba"), "gba",
                                 roots=(self.root1, self.root2))
        self.assertEqual(m, Path(rom_dir, "manuals", "Some Game-manual.pdf"))

    def test_conventional_folder_plain_stem_name(self):
        rom_dir = os.path.join(self.d, "other-roms", "gba")
        os.makedirs(os.path.join(rom_dir, "manuals"))
        Path(rom_dir, "manuals", "Some Other Game.pdf").write_bytes(b"%PDF dummy")
        m = manuals.find_manual(os.path.join(rom_dir, "Some Other Game.gba"), "gba",
                                 roots=(self.root1, self.root2))
        self.assertEqual(m, Path(rom_dir, "manuals", "Some Other Game.pdf"))

    def test_gamelist_paths_reports_which_roots_exist(self):
        paths = manuals.gamelist_paths("snes", roots=(self.root1, self.root2))
        self.assertEqual(paths, [os.path.join(self.sys_dir, "gamelist.xml")])

    def test_two_root_elements_sibling_does_not_break_parsing(self):
        # Some ES forks write a sibling root element (e.g.
        # <alternativeEmulator>) beside <gameList>. Because <game> blocks are
        # regex-extracted before XML-parsing each individually, a whole extra
        # root element elsewhere in the document must not matter at all.
        text = ("<alternativeEmulator><label>x</label></alternativeEmulator>\n"
                "<gameList><game><path>./Weird Doc.sfc</path>"
                "<manual>./manuals/Weird Doc-manual.pdf</manual></game></gameList>")
        with open(os.path.join(self.sys_dir, "gamelist.xml"), "w", encoding="utf-8") as f:
            f.write(text)
        Path(self.sys_dir, "manuals", "Weird Doc-manual.pdf").write_bytes(b"%PDF dummy")
        m = self.find("Weird Doc.sfc")
        self.assertEqual(m, Path(self.sys_dir, "manuals", "Weird Doc-manual.pdf"))


# --------------------------------------------------------------------------
# 2. Page count
# --------------------------------------------------------------------------

class TestPageCount(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def pdf(self, num_pages, name="x.pdf"):
        p = os.path.join(self.d, name)
        Path(p).write_bytes(make_pdf_bytes(num_pages))
        return p

    def test_pdfinfo_present_and_parses(self):
        with mock.patch.object(shutil, "which", return_value="/usr/bin/pdfinfo"), \
             mock.patch.object(manuals, "_run", return_value=(0, "Pages:          7\n", "")):
            self.assertEqual(manuals.page_count("/does/not/need/to/exist.pdf"), 7)

    def test_pdfinfo_missing_falls_back_to_real_byte_scan(self):
        p = self.pdf(5)
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertEqual(manuals.page_count(p), 5)

    def test_pdfinfo_present_but_unparsable_falls_back_to_byte_scan(self):
        p = self.pdf(2)
        with mock.patch.object(shutil, "which", return_value="/usr/bin/pdfinfo"), \
             mock.patch.object(manuals, "_run", return_value=(0, "garbage, no Pages: line", "")):
            self.assertEqual(manuals.page_count(p), 2)

    def test_committed_fixture_pdf_reports_three_pages(self):
        p = os.path.join(FIXTURES, "manuals-3page-SYNTHETIC.pdf")
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertEqual(manuals.page_count(p), 3)

    def test_corrupt_pdf_returns_none(self):
        p = os.path.join(self.d, "corrupt.pdf")
        Path(p).write_bytes(b"not actually a pdf at all")
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertIsNone(manuals.page_count(p))

    def test_missing_file_returns_none(self):
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertIsNone(manuals.page_count(os.path.join(self.d, "nope.pdf")))


# --------------------------------------------------------------------------
# 3. Cache key
# --------------------------------------------------------------------------

class TestCacheKey(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pdf = os.path.join(self.d, "manual.pdf")
        Path(self.pdf).write_bytes(b"%PDF-1.4 v1")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_stable_without_change(self):
        k1 = manuals._cache_key(self.pdf, 1, 1080)
        k2 = manuals._cache_key(self.pdf, 1, 1080)
        self.assertEqual(k1, k2)

    def test_changes_with_mtime(self):
        k1 = manuals._cache_key(self.pdf, 1, 1080)
        future = time.time() + 120
        os.utime(self.pdf, (future, future))
        k2 = manuals._cache_key(self.pdf, 1, 1080)
        self.assertNotEqual(k1, k2)

    def test_changes_with_page_and_height(self):
        k = manuals._cache_key(self.pdf, 1, 1080)
        self.assertNotEqual(k, manuals._cache_key(self.pdf, 2, 1080))
        self.assertNotEqual(k, manuals._cache_key(self.pdf, 1, 540))

    def test_changes_with_content_size_even_if_mtime_unchanged(self):
        # Rewrite the file with different content but pin mtime back to the
        # original value (simulates a scrape/edit that happens within the
        # same filesystem-mtime-resolution window) - size is part of the key
        # too, so it still must not collide.
        st = os.stat(self.pdf)
        k1 = manuals._cache_key(self.pdf, 1, 1080)
        Path(self.pdf).write_bytes(b"%PDF-1.4 totally different length now")
        os.utime(self.pdf, (st.st_atime, st.st_mtime))
        k2 = manuals._cache_key(self.pdf, 1, 1080)
        self.assertNotEqual(k1, k2)


# --------------------------------------------------------------------------
# 4. Render page (subprocess mocked)
# --------------------------------------------------------------------------

class TestRenderPage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cache_dir = os.path.join(self.d, "cache")
        self.pdf = os.path.join(self.d, "manual.pdf")
        Path(self.pdf).write_bytes(make_pdf_bytes(3))

    def tearDown(self):
        shutil.rmtree(self.d)

    def _fake_run_writes_png(self, cmd, timeout):
        # Mirror real pdftoppm -singlefile: the last argument is the output
        # prefix, and pdftoppm would write "<prefix>.png".
        prefix = cmd[-1]
        Path(prefix + ".png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return 0, "", ""

    def test_render_success_creates_cached_png(self):
        with mock.patch.object(manuals, "_run", side_effect=self._fake_run_writes_png) as run:
            out = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        self.assertEqual(out.parent, Path(self.cache_dir))
        cmd = run.call_args[0][0]
        self.assertIn("pdftoppm", cmd)
        self.assertIn("-singlefile", cmd)
        self.assertIn("-scale-to-y", cmd)
        self.assertIn("1080", cmd)

    def test_cache_hit_never_calls_subprocess(self):
        with mock.patch.object(manuals, "_run", side_effect=self._fake_run_writes_png):
            first = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        with mock.patch.object(manuals, "_run", side_effect=AssertionError(
                "cache hit must not spawn pdftoppm")) as run:
            second = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        run.assert_not_called()
        self.assertEqual(first, second)

    def test_pdftoppm_nonzero_exit_returns_none(self):
        with mock.patch.object(manuals, "_run", return_value=(1, "", "Syntax Error: bad xref")):
            out = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        self.assertIsNone(out)
        self.assertEqual(os.listdir(self.cache_dir) if os.path.isdir(self.cache_dir) else [], [])

    def test_partial_output_file_is_cleaned_up_on_failure(self):
        def fake_partial(cmd, timeout):
            prefix = cmd[-1]
            Path(prefix + ".png").write_bytes(b"")  # zero-byte partial write
            return 1, "", "killed"
        with mock.patch.object(manuals, "_run", side_effect=fake_partial):
            out = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        self.assertIsNone(out)
        self.assertEqual(os.listdir(self.cache_dir), [])

    def test_missing_pdftoppm_binary_returns_none(self):
        # This PC actually has a `nice.exe` (Git Bash) on PATH, so leaving it
        # real would make _niced() prepend a working "nice" and _run() would
        # genuinely exec `nice pdftoppm ...` and hit the real, absent
        # pdftoppm at the OS level - it happens to still return None, but
        # that is not a hermetic mock. Force BOTH lookups to miss so this
        # test never spawns a real process either way.
        with mock.patch.object(shutil, "which", return_value=None):
            out = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir, height=1080)
        self.assertIsNone(out)

    def test_bad_page_number_returns_none_without_subprocess(self):
        with mock.patch.object(manuals, "_run", side_effect=AssertionError("should not run")):
            self.assertIsNone(manuals.render_page(self.pdf, 0, cache_dir=self.cache_dir))

    def test_missing_pdf_file_returns_none(self):
        with mock.patch.object(manuals, "_run", side_effect=AssertionError("should not run")):
            out = manuals.render_page(os.path.join(self.d, "nope.pdf"), 1, cache_dir=self.cache_dir)
        self.assertIsNone(out)

    def test_timeout_returns_none(self):
        import subprocess as sp

        def fake_run(cmd, timeout):
            return None, "", "exec failed or timed out (TimeoutExpired): pdftoppm ..."

        with mock.patch.object(manuals, "_run", side_effect=fake_run):
            out = manuals.render_page(self.pdf, 1, cache_dir=self.cache_dir)
        self.assertIsNone(out)


class TestRenderSpread(unittest.TestCase):
    def test_both_pages_rendered_side_by_side(self):
        calls = []

        def fake_render(pdf_path, page, cache_dir=None, height=1080, timeout=20.0):
            calls.append(page)
            return Path("/cache/p%d.png" % page)

        with mock.patch.object(manuals, "render_page", side_effect=fake_render):
            left, right = manuals.render_spread("/x.pdf", 4)
        self.assertEqual(calls, [4, 5])
        self.assertEqual(left, Path("/cache/p4.png"))
        self.assertEqual(right, Path("/cache/p5.png"))

    def test_right_is_none_past_last_page(self):
        with mock.patch.object(manuals, "render_page", return_value=Path("/cache/p10.png")) as rp:
            left, right = manuals.render_spread("/x.pdf", 10, total_pages=10)
        self.assertEqual(left, Path("/cache/p10.png"))
        self.assertIsNone(right)
        rp.assert_called_once()  # only the left page - never asked to render 11

    def test_left_failure_short_circuits(self):
        with mock.patch.object(manuals, "render_page", return_value=None) as rp:
            left, right = manuals.render_spread("/x.pdf", 1)
        self.assertIsNone(left)
        self.assertIsNone(right)
        rp.assert_called_once()


# --------------------------------------------------------------------------
# 5. Spread geometry (pure math)
# --------------------------------------------------------------------------

class TestSpreadLayout(unittest.TestCase):
    def test_single_page_centered(self):
        r = manuals.spread_layout((800, 1080), screen=(1920, 1080))
        self.assertIsNone(r["right"])
        x, y, w, h = r["left"]
        self.assertEqual((w, h), (800, 1080))
        self.assertEqual(x, (1920 - 800) // 2)
        self.assertEqual(y, 0)

    def test_spread_centered_as_a_unit_with_gap(self):
        r = manuals.spread_layout((760, 1080), (760, 1080), screen=(1920, 1080), gap=8)
        lx, ly, lw, lh = r["left"]
        rx, ry, rw, rh = r["right"]
        self.assertEqual(rx, lx + lw + 8)
        total_w = lw + 8 + rw
        self.assertEqual(lx, (1920 - total_w) // 2)
        self.assertEqual((lw, lh), (760, 1080))
        self.assertEqual((rw, rh), (760, 1080))

    def test_spread_pages_of_different_widths_each_vertically_centered(self):
        r = manuals.spread_layout((700, 900), (600, 1080), screen=(1920, 1080))
        _, ly, _, lh = r["left"]
        _, ry, _, rh = r["right"]
        self.assertEqual(ly, (1080 - 900) // 2)
        self.assertEqual(ry, (1080 - 1080) // 2)

    def test_at_1920x1080_two_760_wide_pages_fit_with_room_to_spare(self):
        # 760px is roughly a US-Letter-portrait page (612x792) scaled to
        # fill 1080 height (612 * 1080/792 ~= 834; a slightly narrower manual
        # page comes out around 700-800px). Two such pages plus a gap must
        # comfortably fit inside 1920, which is the whole point of offering
        # a spread instead of one page with huge side margins.
        r = manuals.spread_layout((760, 1080), (760, 1080))
        lx, _, lw, _ = r["left"]
        rx, _, rw, _ = r["right"]
        self.assertGreaterEqual(lx, 0)
        self.assertLessEqual(rx + rw, 1920)


class TestPngSize(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def _write_ihdr(self, w, h):
        p = os.path.join(self.d, "t.png")
        data = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\x00\x00\x00\x00")
        Path(p).write_bytes(data)
        return p

    def test_reads_width_height(self):
        p = self._write_ihdr(763, 1080)
        self.assertEqual(manuals.png_size(p), (763, 1080))

    def test_bad_signature_is_none(self):
        p = os.path.join(self.d, "not.png")
        Path(p).write_bytes(b"not a png at all")
        self.assertIsNone(manuals.png_size(p))

    def test_missing_file_is_none(self):
        self.assertIsNone(manuals.png_size(os.path.join(self.d, "nope.png")))


# --------------------------------------------------------------------------
# 6. Prerender-next (background thread)
# --------------------------------------------------------------------------

class TestPrerenderAsync(unittest.TestCase):
    def test_renders_next_page_in_background(self):
        calls = []
        with mock.patch.object(manuals, "render_page",
                                side_effect=lambda pdf, page, cache_dir, height, timeout: calls.append(page)):
            t = manuals.prerender_next_async("/x.pdf", 3, cache_dir="/cache")
            t.join(timeout=2)
        self.assertEqual(calls, [4])
        self.assertFalse(t.is_alive())

    def test_exception_in_render_is_caught_and_logged_not_raised(self):
        def boom(pdf, page, cache_dir, height, timeout):
            raise RuntimeError("pretend pdftoppm exploded")

        with mock.patch.object(manuals, "render_page", side_effect=boom):
            with self.assertLogs("rp5deck.manuals", level="ERROR"):
                t = manuals.prerender_next_async("/x.pdf", 1, cache_dir="/cache")
                t.join(timeout=2)
        self.assertFalse(t.is_alive())  # background thread finished cleanly


def progressive_png(w=300, h=400):
    import zlib
    raw = b"".join(b"\0" + bytes([(x * 7 + y) & 255 for x in range(w * 3)]) for y in range(h))

    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            ch(b"IDAT", zlib.compress(raw, 0)) + ch(b"IEND", b""))


class TestAtomicCache(unittest.TestCase):
    """RV2-M4 (s3_manual_partial.py): pdftoppm writes its PNG progressively;
    a cache hit must never return a half-written file, and two renders of
    the same page must not race on one output file."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cache = os.path.join(self.d, "cache")
        self.pdf = os.path.join(self.d, "Game-manual.pdf")
        Path(self.pdf).write_bytes(make_pdf_bytes(6))
        self.png = progressive_png()
        self.runs = []
        if hasattr(manuals, "_Prerenderer"):
            self.addCleanup(setattr, manuals, "_PRERENDER", manuals._PRERENDER)
            manuals._PRERENDER = manuals._Prerenderer()

    def slow_pdftoppm(self, cmd, timeout):
        """Like pdftoppm -singlefile: creates <prefix>.png, writes it in chunks."""
        import threading as th
        self.runs.append((th.current_thread().name, cmd[-1]))
        with open(cmd[-1] + ".png", "wb") as f:
            step = len(self.png) // 10
            for i in range(0, len(self.png), step):
                f.write(self.png[i:i + step])
                f.flush()
                time.sleep(0.03)
        return 0, "", ""

    def test_a_tap_during_a_prerender_gets_the_complete_page(self):
        with mock.patch.object(manuals, "_run", side_effect=self.slow_pdftoppm):
            t = manuals.prerender_next_async(self.pdf, 2, cache_dir=self.cache, height=400)
            time.sleep(0.1)                         # the user taps while page 3 is written
            p = manuals.render_page(self.pdf, 3, cache_dir=self.cache, height=400)
            got = Path(p).read_bytes() if p is not None else None   # read NOW, as the UI does
            t.join(5)
        self.assertIsNotNone(p)
        self.assertEqual(len(got), len(self.png))                  # complete, not a prefix
        self.assertEqual(got, self.png)
        self.assertEqual(len(self.runs), 1, self.runs)            # one pdftoppm, not two
        self.assertEqual(os.listdir(self.cache), [os.path.basename(p)])   # no temp left

    def test_concurrent_renders_of_one_page_run_pdftoppm_once(self):
        import threading as th
        got = []
        with mock.patch.object(manuals, "_run", side_effect=self.slow_pdftoppm):
            ts = [th.Thread(target=lambda: got.append(
                manuals.render_page(self.pdf, 2, cache_dir=self.cache, height=400)))
                for _ in range(4)]
            for x in ts:
                x.start()
            for x in ts:
                x.join(10)
        self.assertEqual(len(self.runs), 1, self.runs)
        self.assertEqual(len(got), 4)
        self.assertTrue(all(g is not None and Path(g).read_bytes() == self.png for g in got))

    def test_the_cache_name_only_ever_appears_complete(self):
        seen = []

        def spy(cmd, timeout):
            out = self.slow_pdftoppm(cmd, timeout)
            seen.append(sorted(os.listdir(self.cache)))
            return out
        with mock.patch.object(manuals, "_run", side_effect=spy):
            p = manuals.render_page(self.pdf, 1, cache_dir=self.cache, height=400)
        # while pdftoppm ran, only its temp name existed - never the key
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)
        self.assertNotEqual(seen[0][0], os.path.basename(p))
        self.assertTrue(seen[0][0].endswith(".tmp.png"), seen)

    def test_a_failed_render_removes_only_its_own_temp(self):
        os.makedirs(self.cache)
        other = os.path.join(self.cache, "Other-p0001-h400-0123456789abcdef.99.1.tmp.png")
        Path(other).write_bytes(b"another render in progress")

        def half_then_fail(cmd, timeout):
            Path(cmd[-1] + ".png").write_bytes(self.png[:100])
            return None, "", "timed out"
        with mock.patch.object(manuals, "_run", side_effect=half_then_fail):
            self.assertIsNone(manuals.render_page(self.pdf, 1, cache_dir=self.cache, height=400))
        self.assertEqual(os.listdir(self.cache), [os.path.basename(other)])

    def test_eviction_never_deletes_a_render_in_progress(self):
        os.makedirs(self.cache)
        tmp = os.path.join(self.cache, "x-p0001-h400-0123456789abcdef.1.2.tmp.png")
        Path(tmp).write_bytes(b"x" * 5000)
        old = os.path.join(self.cache, "y-p0001-h400-0123456789abcdef.1.3.tmp.png")
        Path(old).write_bytes(b"x" * 10)
        t = time.time() - 2 * 3600 - 60
        os.utime(old, (t, t))
        manuals._evict(self.cache, max_bytes=100)
        self.assertTrue(os.path.exists(tmp))
        self.assertFalse(os.path.exists(old))        # a leftover from a killed render


class TestPrerenderWorker(unittest.TestCase):
    """RV2-M5 / RV1-m6: ONE bounded prerender worker, not a thread per tap."""

    def setUp(self):
        self.assertTrue(hasattr(manuals, "prerender"), "no bounded prerender worker")
        self.addCleanup(setattr, manuals, "_PRERENDER", manuals._PRERENDER)
        manuals._PRERENDER = manuals._Prerenderer()

    def test_a_flick_of_requests_uses_one_thread_and_only_the_latest_pages(self):
        import threading as th
        gate = th.Event()
        calls = []

        def slow(pdf, page, cache_dir, height, timeout):
            calls.append((page, th.current_thread().name))
            gate.wait(5)
        with mock.patch.object(manuals, "render_page", side_effect=slow):
            tickets = [manuals.prerender("/x.pdf", [2 * i + 3, 2 * i + 4], cache_dir="/c")
                       for i in range(20)]
            time.sleep(0.1)
            gate.set()
            for t in tickets:
                self.assertTrue(t.join(5))
        self.assertEqual({n for _, n in calls}, {"rp5deck-manual-prerender"})
        self.assertEqual(manuals._PRERENDER.threads_started, 1)
        pages = [p for p, _ in calls]
        self.assertLessEqual(len(pages), 3, pages)       # the one in progress + the latest 2
        self.assertEqual(pages[-2:], [41, 42])           # the newest spread's neighbours
        self.assertGreaterEqual(manuals._PRERENDER.superseded, 36)

    def test_the_queue_is_bounded_for_queued_requests_too(self):
        import threading as th
        gate = th.Event()
        calls = []

        def slow(pdf, page, cache_dir, height, timeout):
            calls.append(page)
            gate.wait(5)
        with mock.patch.object(manuals, "render_page", side_effect=slow):
            ts = [manuals.prerender_next_async("/x.pdf", p, cache_dir="/c") for p in range(30)]
            time.sleep(0.1)
            gate.set()
            for t in ts:
                self.assertTrue(t.join(5))
        self.assertLessEqual(len(calls), manuals._Prerenderer.MAX_PENDING + 1, calls)
        self.assertEqual(calls[-1], 30)
        self.assertEqual(manuals._PRERENDER.threads_started, 1)


# --------------------------------------------------------------------------
# 7. Cache eviction (size-capped LRU)
# --------------------------------------------------------------------------

class TestEviction(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def _make(self, name, size, age_s):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def test_no_eviction_when_under_cap(self):
        self._make("a.png", 100, age_s=100)
        self._make("b.png", 100, age_s=10)
        manuals._evict(self.d, max_bytes=1_000_000)
        self.assertEqual(sorted(os.listdir(self.d)), ["a.png", "b.png"])

    def test_oldest_evicted_first_until_under_cap(self):
        self._make("oldest.png", 100, age_s=300)
        self._make("middle.png", 100, age_s=200)
        self._make("newest.png", 100, age_s=10)
        # cap allows only ~150 bytes -> exactly one file must survive: newest
        manuals._evict(self.d, max_bytes=150)
        self.assertEqual(os.listdir(self.d), ["newest.png"])

    def test_touch_updates_recency_so_a_touched_old_file_survives(self):
        old = self._make("old.png", 100, age_s=500)
        other = self._make("other.png", 100, age_s=50)
        manuals._touch(old)  # simulate a cache hit on the "old" file
        manuals._evict(self.d, max_bytes=150)  # room for exactly one
        self.assertEqual(os.listdir(self.d), ["old.png"])
        self.assertFalse(os.path.exists(other))

    def test_render_page_triggers_eviction_when_cache_grows(self):
        # End-to-end through render_page(): after a successful render, the
        # cache directory must be swept back under the cap.
        pdf = os.path.join(self.d, "manual.pdf")
        Path(pdf).write_bytes(make_pdf_bytes(2))
        cache_dir = os.path.join(self.d, "cache")

        def fake_run(cmd, timeout):
            prefix = cmd[-1]
            Path(prefix + ".png").write_bytes(b"x" * 1000)
            return 0, "", ""

        with mock.patch.object(manuals, "_run", side_effect=fake_run):
            manuals.render_page(pdf, 1, cache_dir=cache_dir, height=1080, max_cache_bytes=1500)
            time.sleep(0.01)
            manuals.render_page(pdf, 2, cache_dir=cache_dir, height=1080, max_cache_bytes=1500)
        total = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in os.listdir(cache_dir))
        self.assertLessEqual(total, 1500)
        # the second (newer) render must be the one that survived
        self.assertEqual(len(os.listdir(cache_dir)), 1)

class TestSpreadFitsTheScreen(unittest.TestCase):
    """Batch 1: pages wider than portrait used to run off both edges."""

    def inside(self, rect, screen=(1920, 1000)):
        x, y, w, h = rect
        self.assertGreaterEqual(x, 0, rect)
        self.assertGreaterEqual(y, 0, rect)
        self.assertLessEqual(x + w, screen[0], rect)
        self.assertLessEqual(y + h, screen[1], rect)

    def test_two_landscape_pages(self):
        r = manuals.spread_layout((1333, 1000), (1333, 1000), screen=(1920, 1000))
        self.inside(r["left"])
        self.inside(r["right"])
        self.assertAlmostEqual(r["left"][2] / r["left"][3], 1.333, places=2)   # aspect kept

    def test_one_landscape_page(self):
        r = manuals.spread_layout((2600, 1000), screen=(1920, 1000))
        self.inside(r["left"])
        self.assertEqual(r["left"][2], 1920)

    def test_wide_portrait_pair(self):
        r = manuals.spread_layout((1100, 1000), (1100, 1000), screen=(1920, 1000))
        self.inside(r["left"])
        self.inside(r["right"])
        self.assertEqual(r["right"][0] - (r["left"][0] + r["left"][2]), 8)   # the gap survives

    def test_fitting_pair_is_not_scaled(self):
        r = manuals.spread_layout((700, 1000), (700, 1000), screen=(1920, 1000))
        self.assertEqual((r["left"][2], r["left"][3]), (700, 1000))


if __name__ == "__main__":
    unittest.main()
