#!/usr/bin/env python3
"""test_fix_bad_names.py - NM1: tests for tools/fix-bad-names.py.

The nes/gba/nds/genesis/snes gamelist fixtures under tests/fixtures/
nm1-gamelist-*-real-capture-2026-09-24.xml hold REAL bytes fetched from the
device on 2026-09-24 (path text, name text, description text, exact media
filenames from `find`) for the games name_guard actually flagged: Ren &
Stimpy Show (nes ROM + name; snes name only, its ROM has no '$'), WarioWare
(gba, path only - its <name> has no '$'), Metropolis Crime$ (nds, path only),
Skitchin' / Wiz'n'Liz (genesis, name only). The surrounding neighbour <game>
blocks and the media-tag list (image/thumbnail/video/manual/marquee/
titleshot/cartridge/boxback/mix/fanart) are assembled from the same real
`find`/`sed` capture into one small file per system, noted in each fixture.

fix-bad-names.py is loaded via importlib because its filename has a hyphen
(matches the deliverable path in TASKS.md NM1) and so cannot be `import`ed
directly.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
TOOLS = os.path.join(APP, "tools")
FIXTURES = os.path.join(HERE, "fixtures")
TOOL_PATH = os.path.join(TOOLS, "fix-bad-names.py")

sys.path.insert(0, APP)
import name_guard  # noqa: E402


def _load_fbn():
    spec = importlib.util.spec_from_file_location("rp5deck_fix_bad_names", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fbn = _load_fbn()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


# es(), sha(), fixture(), write_es_systems_cfg(), and only_lines_changed()
# removed: all were only used by tests that read the dropped
# nm1-gamelist-*-real-capture fixtures (see DROPPED-FIXTURES-list.txt) and
# had no remaining callers once those tests were removed.


# ---------------------------------------------------------------------------
# Pure logic

class TestCharsAndFixText(unittest.TestCase):
    def test_dollar_and_backtick_always_dangerous(self):
        self.assertEqual(fbn.chars_to_fix("Ma$ter"), {"$"})
        self.assertEqual(fbn.chars_to_fix("Lode Runner (Ma$ter)"), {"$"})   # has a space: $ still counts
        self.assertEqual(fbn.chars_to_fix("Mega `id` Man"), {"`"})

    def test_apostrophe_and_semicolon_only_without_a_space(self):
        self.assertEqual(fbn.chars_to_fix("Skitchin'"), {"'"})
        self.assertEqual(fbn.chars_to_fix("Steins;Gate"), {";"})
        self.assertEqual(fbn.chars_to_fix("Jimmy White's Whirlwind Snooker"), set())
        self.assertEqual(fbn.chars_to_fix("Tom; Jerry"), set())

    def test_safe_values(self):
        for v in ("Ren & Stimpy Show", "Tetris.gb", "Super Mario Bros. 3 (USA) [!]", "", "100% Pure"):
            self.assertEqual(fbn.chars_to_fix(v), set())

    def test_fix_text_applies_the_lookalike_map(self):
        self.assertEqual(fbn.fix_text("Skitchin'"), ("Skitchin’", {"'"}))
        self.assertEqual(fbn.fix_text("Steins;Gate"), ("Steins；Gate", {";"}))
        self.assertEqual(fbn.fix_text("Ma$ter"), ("Ma＄ter", {"$"}))
        self.assertEqual(fbn.fix_text("Mega `id` Man"), ("Mega ’id’ Man", {"`"}))
        self.assertEqual(fbn.fix_text("D'Veel'Ng"), ("D’Veel’Ng", {"'"}))
        self.assertEqual(fbn.fix_text("safe name"), ("safe name", set()))

    def test_fixed_value_is_never_dangerous_again(self):
        for v in ("Skitchin'", "Steins;Gate", "Ma$ter", "Mega `id` Man",
                  "The Ren & Stimpy Show : Buckeroo$!"):
            fixed, _ = fbn.fix_text(v)
            self.assertEqual(fbn.chars_to_fix(fixed), set(), fixed)
            # and name_guard itself agrees the fixed value is clean
            self.assertEqual(name_guard.value_reasons(fixed), [], fixed)


class TestSubstituteRaw(unittest.TestCase):
    def test_literal_ascii_replace_is_byte_exact(self):
        raw = b"The Ren &amp; Stimpy Show : Buckeroo$!"
        out = fbn.substitute_raw(raw, {"$"})
        self.assertEqual(out, "The Ren &amp; Stimpy Show : Buckeroo＄!".encode("utf-8"))
        # the ampersand entity, untouched by our replace, survives byte-exact
        self.assertIn(b"&amp;", out)


class TestIsSystemTopLevel(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-fbn-top-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "roms")
        os.makedirs(os.path.join(self.root, "nes"))
        os.makedirs(os.path.join(self.root, "nes", "images"))
        write(os.path.join(self.root, "nes", "Game$.nes"))
        write(os.path.join(self.root, "nes", "images", "Game$-image.png"))
        # is_system_top_level takes a {realpath(system dir): extensions}
        # index (built by _build_system_index from a name_guard.scan()
        # result) rather than a bare list of roots since NM2.
        self.index = {os.path.realpath(os.path.join(self.root, "nes")): None}

    def test_a_rom_directly_in_a_system_folder_is_top_level(self):
        ok, system = fbn.is_system_top_level(os.path.join(self.root, "nes", "Game$.nes"),
                                             self.index)
        self.assertTrue(ok)
        self.assertEqual(system, "nes")

    def test_a_file_in_a_media_folder_is_not(self):
        ok, _ = fbn.is_system_top_level(
            os.path.join(self.root, "nes", "images", "Game$-image.png"), self.index)
        self.assertFalse(ok)

    def test_unrelated_root_is_not(self):
        other_index = {os.path.realpath(os.path.join(self.d, "other")): None}
        ok, _ = fbn.is_system_top_level(os.path.join(self.root, "nes", "Game$.nes"), other_index)
        self.assertFalse(ok)


class TestFindMediaAndSaves(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-fbn-media-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.system_dir = os.path.join(self.d, "nes")
        for p in ("images/Game$-image.png", "images/Game$-thumb.png", "images/Other-image.png",
                 "videos/Game$-video.mp4", "manuals/Game$-manual.pdf", "Game$.nes"):
            write(os.path.join(self.system_dir, p))

    def test_finds_every_stem_prefixed_media_file_across_subfolders(self):
        media = fbn.find_media(self.system_dir, "Game$")
        names = sorted(os.path.basename(m) for m in media)
        self.assertEqual(names, ["Game$-image.png", "Game$-manual.pdf", "Game$-thumb.png",
                                 "Game$-video.mp4"])

    def test_does_not_match_a_different_game_with_a_shared_prefix(self):
        write(os.path.join(self.system_dir, "images", "Game$2-image.png"))
        media = fbn.find_media(self.system_dir, "Game$")
        self.assertNotIn("Game$2-image.png", [os.path.basename(m) for m in media])

    def test_finds_adjacent_save_and_state_files(self):
        write(os.path.join(self.system_dir, "Game$.srm"))
        write(os.path.join(self.system_dir, "Game$.state1"))
        hits = fbn.find_saves(self.system_dir, "Game$", [])
        self.assertEqual(sorted(os.path.basename(h) for h in hits), ["Game$.srm", "Game$.state1"])

    def test_finds_saves_in_a_configured_save_dir(self):
        savestates = os.path.join(self.d, "savestates")
        write(os.path.join(savestates, "Game$.state.auto"))
        hits = fbn.find_saves(self.system_dir, "Game$", [savestates])
        self.assertEqual(len(hits), 1)

    def test_no_saves_is_a_clean_empty_list(self):
        self.assertEqual(fbn.find_saves(self.system_dir, "Game$", []), [])
        self.assertEqual(fbn.find_saves(self.system_dir, "Game$", ["/does/not/exist"]), [])


class TestRewriteMediaRef(unittest.TestCase):
    def test_path_exact_match_is_renamed(self):
        got = fbn._rewrite_media_ref("./Game$.nes", "Game$.nes", "Game＄.nes", "Game$", "Game＄")
        self.assertEqual(got, "./Game＄.nes")

    def test_media_prefix_match_is_renamed(self):
        got = fbn._rewrite_media_ref("./images/Game$-image.png", "Game$.nes", "Game＄.nes",
                                     "Game$", "Game＄")
        self.assertEqual(got, "./images/Game＄-image.png")

    def test_unrelated_value_is_left_alone(self):
        self.assertIsNone(fbn._rewrite_media_ref("./Other Game.nes", "Game$.nes", "Game＄.nes",
                                                 "Game$", "Game＄"))

    def test_a_similarly_prefixed_different_game_is_not_touched(self):
        # "Game$2" must not be treated as "Game$" + "-2..."
        self.assertIsNone(fbn._rewrite_media_ref("./images/Game$2-image.png", "Game$.nes",
                                                 "Game＄.nes", "Game$", "Game＄"))


# ---------------------------------------------------------------------------
# Real-capture gamelist fixtures: byte-exact editing
#
# TestScanGamelistRealCapture removed entirely (test_name_only_genesis_two_
# entries_untouched_elsewhere, test_snes_name_only_dollar_rom_has_no_dollar,
# test_nes_rename_fixes_name_path_and_every_media_tag,
# test_gba_rename_fixes_path_and_media_but_name_was_never_dangerous,
# test_nds_rename_stem_does_not_collide_with_commas_in_name): every method
# copied one of tests/fixtures/nm1-gamelist-{nes,gba,nds,genesis,snes}-real-
# capture-2026-09-24.xml, real on-device ES gamelist captures dropped from
# the public release (see DROPPED-FIXTURES-list.txt). No synthetic
# equivalent exists for this shape.


class TestEntityEncodedSafetyNet(unittest.TestCase):
    """Never guess: if a target character is entity-encoded rather than
    literal, a raw-byte replace cannot find it, so the fix must not be
    silently skipped without a trace - it must be REPORTED as an error, and
    no half-applied edit must reach the file. This is the failure mode a
    literal byte-replace strategy is exposed to; proving it is caught is the
    point of this test (see the session report for a live break/restore of
    this exact check)."""

    def test_numeric_entity_dollar_is_reported_not_silently_left_broken(self):
        d = tempfile.mkdtemp(prefix="rp5deck-fbn-entity-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "gamelist.xml")
        write(path, '<?xml version="1.0"?>\n<gameList>\n\t<game>\n\t\t<path>./a.nes</path>\n'
                    "\t\t<name>Price&#36;Tag</name>\n\t</game>\n</gameList>\n")
        old = read(path)
        result = fbn.scan_gamelist(path, [])
        self.assertEqual(result["edits"], [])                  # nothing silently changed
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Price$Tag", result["errors"][0])
        new = fbn.apply_edits(old, result["edits"])
        self.assertEqual(new, old)                              # a no-op, not a corruption


# ---------------------------------------------------------------------------
# Full pipeline: dedup across bind-mounted roots, apply, undo, skip, refuse

class TreeCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-fbn-tree-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root_a = os.path.join(self.d, "roms")
        self.root_b = os.path.join(self.d, "roms-internal")

    # mirror_file(), build_system(), build_full_tree(), scan(), and
    # snapshot() removed: build_full_tree() built each system via
    # build_system() with one of tests/fixtures/nm1-gamelist-{nes,gba,nds,
    # genesis}-real-capture-2026-09-24.xml as the gamelist, all dropped from
    # the public release (see DROPPED-FIXTURES-list.txt). Every test that
    # used build_full_tree() (directly, or via TestApplyAndUndo.setUp()) is
    # removed below, and mirror_file()/scan()/snapshot() had no remaining
    # callers once those tests were gone.


class TestBuildPlanDedup(TreeCase):
    # test_bind_mounted_duplicate_findings_collapse_to_one_plan_item_each
    # removed: depended on build_full_tree(), which used the dropped
    # nm1-gamelist-*-real-capture fixtures (see DROPPED-FIXTURES-list.txt).

    def test_excludes_are_never_touched(self):
        write(os.path.join(self.root_a, "steam", "userdata", "1", "413080",
                          "$$$autosave.vdf"))
        write(os.path.join(self.root_a, "themes", "pack", "art", "logos",
                          "Baldur`s Gate.svg"))
        write(os.path.join(self.root_a, "steam", "steamrtarm64\\swiftshader"))
        result = name_guard.scan(roots=[self.root_a], gamelist_dirs=[])
        plan = fbn.build_plan(result)
        self.assertEqual(plan["renames"], [])
        reasons = {os.path.basename(x["file"]): x["reason"] for x in plan["excluded"]}
        self.assertIn("$$$autosave.vdf", reasons)
        self.assertIn("Baldur`s Gate.svg", reasons)


class TestDefenceInDepth(unittest.TestCase):
    """NM2 (24 Sep 2026): name_guard's own scoped scan should never again
    produce a Steam/theme-style finding (see tests/test_name_guard.py), but
    fix-bad-names must independently refuse - HARD, not just quietly exclude
    - any rename candidate that is not under one of ES's own declared system
    directories with a matching extension. These tests hand-craft a
    name_guard.scan()-shaped result (as if it came from a stale --guard-json
    file) to prove that defence fires even though nothing on disk actually
    triggers it through a real scan."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-fbn-defence-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.nes_dir = os.path.join(self.d, "roms", "nes")
        write(os.path.join(self.nes_dir, "Game$.nes"), b"ROM")
        self.systems = [{"name": "nes", "path": os.path.realpath(self.nes_dir),
                         "extensions": [".nes", ".zip"]}]

    def _result(self, findings):
        return {"roots": [os.path.realpath(self.nes_dir)], "systems": self.systems,
               "findings": findings, "files_checked": 1, "gamelists": 0,
               "entries_checked": 0, "elapsed_s": 0.0}

    def test_a_steam_style_path_outside_every_system_is_refused(self):
        # Not under the (only) declared system path at all - e.g. a stale
        # --guard-json from before NM2, or one hand-edited to add it back.
        steam_file = os.path.join(self.d, "roms", "steam", "userdata", "1", "413080",
                                  "$$$autosave.vdf")
        write(steam_file, b"x")
        findings = [{"source": "filesystem", "field": "filename",
                    "file": steam_file, "value": "$$$autosave.vdf", "reasons": ["x"]}]
        plan = fbn.build_plan(self._result(findings))
        self.assertEqual(plan["renames"], [])
        reasons = [x["reason"] for x in plan["hard_refusals"]]
        self.assertEqual(len(reasons), 1)
        self.assertIn("not under any ES system path", reasons[0])
        # never even considered "excluded" (a softer, non-refusing bucket)
        self.assertEqual(plan["excluded"], [])
        problems = fbn.validate_plan(plan)
        self.assertTrue(any("REFUSED" in p for p in problems))

    def test_wrong_extension_directly_in_a_system_folder_is_refused(self):
        # dirname matches the declared "nes" system exactly, but this
        # extension is not one nes declares - a real scoped scan could never
        # produce this (it only looks at nes's own declared extensions), so
        # this only exercises the independent re-check.
        bogus = os.path.join(self.nes_dir, "Something$.exe")
        write(bogus, b"x")
        findings = [{"source": "filesystem", "field": "filename",
                    "file": bogus, "value": "Something$.exe", "reasons": ["x"]}]
        plan = fbn.build_plan(self._result(findings))
        self.assertEqual(plan["renames"], [])
        self.assertEqual(len(plan["hard_refusals"]), 1)

    def test_apply_refuses_outright_when_a_hard_refusal_is_present(self):
        steam_file = os.path.join(self.d, "roms", "steam", "$$$autosave.vdf")
        write(steam_file, b"x")
        # a LEGITIMATE nes rename alongside the bogus Steam finding
        findings = [
            {"source": "filesystem", "field": "filename", "file": steam_file,
             "value": "$$$autosave.vdf", "reasons": ["x"]},
            {"source": "filesystem", "field": "filename",
             "file": os.path.join(self.nes_dir, "Game$.nes"), "value": "Game$.nes",
             "reasons": ["x"]},
        ]
        plan = fbn.build_plan(self._result(findings))
        self.assertEqual(len(plan["hard_refusals"]), 1)
        self.assertEqual(len(plan["renames"]), 1)          # the legitimate nes rename
        log_dir = os.path.join(self.d, "logs")
        os.makedirs(log_dir)
        log_path = fbn.apply_plan(plan, log_dir=log_dir)
        self.assertIsNone(log_path)                         # refused entirely
        self.assertEqual(os.listdir(log_dir), [])
        # the legitimate rename did NOT happen either - a hard refusal blocks
        # the whole apply, not just the offending entry
        self.assertTrue(os.path.exists(os.path.join(self.nes_dir, "Game$.nes")))


# TestApplyAndUndo removed entirely (test_apply_renames_and_fixes_then_is_
# idempotent, test_undo_restores_every_file_byte_identical,
# test_refuses_and_changes_nothing_when_a_rename_target_already_exists,
# test_skips_a_rom_with_adjacent_save_data_but_still_fixes_independent_
# names): its setUp() called build_full_tree() unconditionally, so every
# test in the class errored at setUp - build_full_tree() used the dropped
# nm1-gamelist-*-real-capture fixtures (see DROPPED-FIXTURES-list.txt).


# ---------------------------------------------------------------------------
# CLI

class TestCli(TreeCase):
    def run_cli(self, *args, timeout=60):
        return subprocess.run([sys.executable, "-B", TOOL_PATH] + list(args),
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout)

    # test_dry_run_default_prints_a_plan_and_changes_nothing,
    # test_json_plan_is_valid_json_with_expected_counts,
    # test_apply_without_i_stopped_es_refuses,
    # test_apply_with_flag_then_undo_round_trips, and
    # test_guard_json_input_mode removed: all called build_full_tree(),
    # which used the dropped nm1-gamelist-*-real-capture fixtures (see
    # DROPPED-FIXTURES-list.txt).

    def test_nothing_scanned_is_exit_3(self):
        r = self.run_cli("--root", os.path.join(self.d, "does-not-exist"))
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()
