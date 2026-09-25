#!/usr/bin/env python3
"""name_guard.py: find game names / paths that ES's hook command line would
let the shell re-parse (RV1-M3, owner's decision of 23 Sep).

ES (Scripting.cpp executeScript) wraps an argument in double quotes ONLY if it
contains an ASCII space, strips every '"', escapes nothing, and runs the
string with `sh -c`. So `$` and backticks are dangerous in every value, and
shell metacharacters are dangerous in values without a space.

The fixture tree is built the way the device's is: <root>/<system>/ with ROM
files, a gamelist.xml written ES-style (only '&' escaped), a gamelist with a
second root element, scraped names with U+00A0, and media folders."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import name_guard  # noqa: E402

NBSP = chr(0xA0)          # U+00A0, as found in 329 scraped ES fields


def write(path, text=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))


def gamelist(*games, extra=""):
    body = "".join("\t<game>\n\t\t<path>%s</path>\n\t\t<name>%s</name>\n\t</game>\n" % g
                   for g in games)
    return '<?xml version="1.0"?>\n<gameList>\n%s</gameList>\n%s' % (body, extra)


def write_es_systems_cfg(path, systems):
    """A minimal es_systems.cfg, in the real device's syntax (tests/fixtures/
    device/es_systems.cfg), covering just what name_guard reads: <name>,
    <path>, <extension>. `systems`: {name: (system_dir, "space list .ext")}."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<systemList>"]
    for name, (sysdir, exts) in systems.items():
        parts.append("\t<system>\n\t\t<name>%s</name>\n\t\t<path>%s</path>\n"
                     "\t\t<extension>%s</extension>\n\t</system>" % (name, sysdir, exts))
    parts.append("</systemList>")
    write(path, "\n".join(parts) + "\n")


class TestValueRules(unittest.TestCase):
    """The pure rule: which values ES's quoting makes dangerous."""

    def reasons(self, v):
        return name_guard.value_reasons(v)

    def test_dollar_and_backtick_are_dangerous_everywhere(self):
        for v in ("Lode Runner (Ma$ter)", "Ma$ter", "a `b` c", "`x`", "$(id)", "x $(id) y"):
            self.assertTrue(self.reasons(v), v)

    def test_metacharacters_only_matter_without_a_space(self):
        for ch in "'\"&();|<>\\\n":
            spaceless = "Tom%sJerry" % ch
            self.assertTrue(self.reasons(spaceless), repr(spaceless))
            spaced = "Tom %s Jerry" % ch             # ES double-quotes it
            self.assertEqual(self.reasons(spaced), [], repr(spaced))

    def test_safe_values(self):
        for v in ("Jimmy White's Whirlwind Snooker", "Ren & Stimpy", "Ren &amp; Stimpy Show",
                  "Pokémon Edición Azul ポケモン", "Tetris.gb", "/storage/roms/gb/Tetris.gb",
                  "Super Mario Bros. 3 (USA) [!]", "100% Pure", "a=b", "-dash", ""):
            self.assertEqual(self.reasons(v), [], v)

    def test_nbsp_is_not_a_space_for_es(self):
        # ES tests find(" ") - ASCII 0x20 only. A scraped name whose only
        # "spaces" are U+00A0 is passed UNQUOTED, so its '&' backgrounds.
        v = "Tom" + NBSP + "&" + NBSP + "Jerry"
        self.assertTrue(self.reasons(v))
        self.assertEqual(self.reasons("Tom & Jerry"), [])

    def test_tab_is_not_a_space_for_es(self):
        self.assertTrue(self.reasons("Tom\t&\tJerry"))


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ng-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "roms")

    def scan(self, **kw):
        kw.setdefault("roots", [self.root])
        kw.setdefault("gamelist_dirs", [])
        return name_guard.scan(**kw)


class TestScan(TreeCase):
    def test_clean_tree_is_ok_and_counts_what_it_saw(self):
        write(os.path.join(self.root, "gb", "Tetris (World).gb"))
        write(os.path.join(self.root, "megadrive", "Jimmy White's Whirlwind Snooker (Europe).md"))
        write(os.path.join(self.root, "megadrive", "gamelist.xml"), gamelist(
            ("./Jimmy White's Whirlwind Snooker (Europe).md", "Jimmy White's Whirlwind Snooker"),
            ("./Ren &amp; Stimpy.md", "Ren &amp; Stimpy Show Presents")))
        r = self.scan()
        self.assertTrue(r["ok"], r["findings"])
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["gamelists"], 1)
        self.assertEqual(r["entries_checked"], 4)          # 2 names + 2 paths
        self.assertGreaterEqual(r["files_checked"], 2)
        self.assertEqual(r["roots"], [os.path.realpath(self.root)])

    def test_rom_filenames_with_dangerous_characters(self):
        write(os.path.join(self.root, "zxspectrum", "Lode Runner 3 48K (1999)(Ma$ter)(Hack).tap"))
        write(os.path.join(self.root, "megadrive", "Tom&Jerry.md"))
        write(os.path.join(self.root, "snes", "Sonic's.sfc"))
        write(os.path.join(self.root, "snes", "Fine Game (USA).sfc"))
        r = self.scan()
        self.assertFalse(r["ok"])
        vals = sorted(f["value"] for f in r["findings"])
        self.assertEqual(vals, ["Lode Runner 3 48K (1999)(Ma$ter)(Hack).tap", "Sonic's.sfc",
                                "Tom&Jerry.md"])
        f = next(f for f in r["findings"] if f["value"] == "Tom&Jerry.md")
        self.assertEqual(f["source"], "filesystem")
        self.assertEqual(f["file"], os.path.join(os.path.realpath(self.root), "megadrive",
                                                 "Tom&Jerry.md"))
        self.assertTrue(f["reasons"])

    def test_a_folder_name_counts_too(self):
        os.makedirs(os.path.join(self.root, "psx", "Disc(1)"))
        write(os.path.join(self.root, "psx", "Disc(1)", "track 1.bin"))
        r = self.scan()
        self.assertEqual([(f["field"], f["value"]) for f in r["findings"]],
                         [("dirname", "Disc(1)")])

    def test_gamelist_names_and_paths_with_es_escaping(self):
        # ES writes only '&' escaped; a scraped name can hold $() or backticks
        write(os.path.join(self.root, "nes", "gamelist.xml"), gamelist(
            ("./a.nes", "Super $(touch /tmp/pwned) Game"),
            ("./b.nes", "Mega `id` Man"),
            ("./c.nes", "Tom&amp;Jerry"),                  # decodes to Tom&Jerry: no space
            ("./d.nes", "Tom &amp; Jerry"),                # spaced: safe
            ("./Bad;name.nes", "Fine Name"),
            ("./e.nes", "Tom" + NBSP + "&amp;" + NBSP + "Jerry")))
        r = self.scan()
        got = sorted((f["field"], f["value"]) for f in r["findings"])
        self.assertEqual(got, sorted([
            ("name", "Super $(touch /tmp/pwned) Game"),
            ("name", "Mega `id` Man"),
            ("name", "Tom&Jerry"),
            ("path", "./Bad;name.nes"),
            ("name", "Tom" + NBSP + "&" + NBSP + "Jerry")]))
        f = next(f for f in r["findings"] if f["value"] == "Mega `id` Man")
        self.assertEqual(f["source"], "gamelist")
        self.assertTrue(f["file"].endswith("gamelist.xml"))
        self.assertEqual(f["line"], 9)

    def test_spaced_folder_does_not_protect_a_spaceless_file_name(self):
        # game-start passes the bare file name as its own (unquoted) argument
        write(os.path.join(self.root, "nes", "gamelist.xml"), gamelist(
            ("./My Games/Tom&Jerry.nes", "Tom and Jerry")))
        r = self.scan()
        self.assertEqual([(f["field"], f["value"]) for f in r["findings"]],
                         [("path", "./My Games/Tom&Jerry.nes")])
        self.assertIn("Tom&Jerry.nes", r["findings"][0]["reasons"][0])
        write(os.path.join(self.root, "nes", "My Games", "Tom&Jerry.nes"))
        r = self.scan()
        self.assertIn(("filename", "Tom&Jerry.nes"),
                      [(f["field"], f["value"]) for f in r["findings"]])

    def test_gamelist_with_two_root_elements_and_a_broken_block(self):
        text = gamelist(("./a.gb", "Ma$ter"), extra=(
            "<alternativeEmulator>\n\t<label>x</label>\n</alternativeEmulator>\n"))
        text = text.replace("<gameList>", "<gameList>\n\t<game><path>./broken.gb</path>"
                            "<name>unterminated", 1)
        write(os.path.join(self.root, "gb", "gamelist.xml"), text)
        r = self.scan()
        self.assertIn("Ma$ter", [f["value"] for f in r["findings"]])
        self.assertEqual(r["errors"], [])

    def test_entities_numeric_and_cdata(self):
        write(os.path.join(self.root, "gb", "gamelist.xml"), gamelist(
            ("./a.gb", "Price&#36;"), ("./b.gb", "<![CDATA[Cash$]]>"),
            ("./c.gb", "Fine &apos;Name&apos; &lt;3&gt;"), ("./d.gb", "Mr&#x27;s")))
        r = self.scan()
        self.assertEqual(sorted(f["value"] for f in r["findings"]),
                         ["Cash$", "Mr's", "Price$"])

    def test_invalid_utf8_and_unreadable_files_never_raise(self):
        write(os.path.join(self.root, "gb", "gamelist.xml"),
              b"<gameList><game><path>./\xff\xfe.gb</path><name>Ma$ter \xff</name></game>")
        r = self.scan()
        self.assertEqual(len(r["findings"]), 1)
        r = name_guard.scan(roots=[os.path.join(self.d, "missing")], gamelist_dirs=[])
        self.assertFalse(r["ok"])
        self.assertTrue(r["nothing_scanned"])
        r = name_guard.scan(roots=[None, 42, ""], gamelist_dirs=[None])      # garbage in
        self.assertTrue(r["nothing_scanned"])

    def test_media_folders_and_bios_are_skipped(self):
        write(os.path.join(self.root, "snes", "images", "Ma$ter-image.png"))
        write(os.path.join(self.root, "snes", "videos", "Ma$ter-video.mp4"))
        write(os.path.join(self.root, "snes", "manuals", "Ma$ter-manual.pdf"))
        write(os.path.join(self.root, "bios", "Ma$ter.bin"))
        write(os.path.join(self.root, "snes", "ok.sfc"))
        r = self.scan()
        self.assertEqual(r["findings"], [])

    def test_two_roots_that_are_the_same_directory_are_scanned_once(self):
        write(os.path.join(self.root, "gb", "Ma$ter.gb"))
        r = self.scan(roots=[self.root, self.root + os.sep, os.path.join(self.root, ".")])
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(len(r["roots"]), 1)

    def test_only_gamelists_es_loads_are_read(self):
        # ES reads <root>/<system>/gamelist.xml; a backup copy deeper down
        # (the PC library has 139 hits in _rom-organizer/_recovery-*) is not
        write(os.path.join(self.root, "_rom-organizer", "_recovery-1", "gamelists", "nes",
                           "gamelist.xml"), gamelist(("./a.nes", "Toobin'")))
        write(os.path.join(self.root, "nes", "sub", "gamelist.xml"),
              gamelist(("./b.nes", "Toobin'")))
        write(os.path.join(self.root, "nes", "gamelist.xml"), gamelist(("./c.nes", "Skitchin'")))
        r = self.scan()
        self.assertEqual([f["value"] for f in r["findings"]], ["Skitchin'"])
        self.assertEqual((r["gamelists"], r["gamelists_skipped"]), (1, 2))

    def test_extra_gamelist_dir(self):
        gl = os.path.join(self.d, "config", "gamelists", "gb", "gamelist.xml")
        write(gl, gamelist(("./a.gb", "Ma$ter")))
        write(os.path.join(self.root, "gb", "a.gb"))
        r = self.scan(gamelist_dirs=[os.path.join(self.d, "config", "gamelists")])
        self.assertEqual([(f["file"], f["value"]) for f in r["findings"]], [(gl, "Ma$ter")])

    def test_symlink_loop_does_not_hang(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        os.makedirs(os.path.join(self.root, "gb"))
        try:
            os.symlink(self.root, os.path.join(self.root, "gb", "loop"))
        except OSError:
            self.skipTest("symlinks not permitted here")
        write(os.path.join(self.root, "gb", "a.gb"))
        r = self.scan()
        self.assertTrue(r["ok"])

    def test_deep_symlink_is_not_followed(self):
        # device, 24 Sep: steam/.../pfx/dosdevices/z: -> / gave 1320 findings
        # from /var/run/udev; only a system folder itself may be a link
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        outside = os.path.join(self.d, "outside")
        write(os.path.join(outside, "Ma$ter.gb"))
        os.makedirs(os.path.join(self.root, "steam", "pfx", "dosdevices"))
        try:
            os.symlink(outside, os.path.join(self.root, "steam", "pfx", "dosdevices", "z:"))
            os.symlink(outside, os.path.join(self.root, "gb"))
        except OSError:
            self.skipTest("symlinks not permitted here")
        r = self.scan()
        # the symlinked SYSTEM folder gb is walked (one finding); z: is not
        files = sorted(f["file"] for f in r["findings"])
        self.assertEqual(len(files), 1, files)
        self.assertIn(os.path.join(self.root, "gb"), files[0])

    def test_20k_files_is_fast(self):
        # (the 100k measurement is in the FX-B report; 20k keeps the suite quick)
        base = os.path.join(self.root, "big")
        for i in range(40):
            dd = os.path.join(base, "d%03d" % i)
            os.makedirs(dd)
            for j in range(500):
                open(os.path.join(dd, "Game %05d (USA).zip" % j), "wb").close()
        write(os.path.join(base, "d007", "Ma$ter.zip"))
        t0 = time.monotonic()
        r = self.scan()
        dt = time.monotonic() - t0
        print("\n  name_guard: %d files in %.2f s" % (r["files_checked"], dt), end=" ")
        self.assertGreaterEqual(r["files_checked"], 20000)
        self.assertEqual([f["value"] for f in r["findings"]], ["Ma$ter.zip"])
        self.assertLess(dt, 10.0)


class TestCli(TreeCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", os.path.join(APP, "name_guard.py")] +
                              list(args), capture_output=True, text=True, timeout=60)

    def test_json_clean_findings_and_nothing(self):
        write(os.path.join(self.root, "gb", "ok.gb"))
        r = self.run_cli("--json", "--root", self.root, "--no-default-gamelist-dirs")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])
        write(os.path.join(self.root, "gb", "Tom&Jerry.gb"))
        r = self.run_cli("--json", "--root", self.root, "--no-default-gamelist-dirs")
        self.assertEqual(r.returncode, 1)
        j = json.loads(r.stdout)
        self.assertEqual([f["value"] for f in j["findings"]], ["Tom&Jerry.gb"])
        r = self.run_cli("--json", "--root", os.path.join(self.d, "nope"),
                         "--no-default-gamelist-dirs")
        self.assertEqual(r.returncode, 3)
        self.assertTrue(json.loads(r.stdout)["nothing_scanned"])

    def test_text_output_names_the_file_and_value(self):
        write(os.path.join(self.root, "gb", "Tom&Jerry.gb"))
        r = self.run_cli("--root", self.root, "--no-default-gamelist-dirs")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Tom&Jerry.gb", r.stdout)
        self.assertIn("1 dangerous", r.stdout)


class TestEsCommandLine(unittest.TestCase):
    """name_guard.es_command_line is a port of executeScript()'s quoting."""

    def test_quoting_rules(self):
        c = name_guard.es_command_line
        self.assertEqual(c("/s/h.sh", ["gb", "/r/gb/Tetris.gb", "Tetris"]),
                         "/s/h.sh gb /r/gb/Tetris.gb Tetris")
        self.assertEqual(c("/s/h.sh", ["gb", "/r/a b.gb", "A B"]),
                         '/s/h.sh gb "/r/a b.gb" "A B"')
        self.assertEqual(c("/my scripts/h.sh", ["x"]), '"/my scripts/h.sh" x')
        self.assertEqual(c("/s/h.sh", ["snes", "", "ignored"]), "/s/h.sh snes")   # stops at empty
        self.assertEqual(c("/s/h.sh", ['say "hi" now']), '/s/h.sh "say hi now"')  # quotes stripped
        self.assertEqual(c("/s/h.sh", ['"quoted"x']), "/s/h.sh quoted")          # first+last cut
        self.assertEqual(c("/s/h.sh", ['"']), "/s/h.sh ")                        # empty data
        self.assertEqual(c("/s/h.sh", ["a", "b", "c", "d"]), "/s/h.sh a b c")    # 3 args only


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.exists("/bin/sh"),
                     "runs ES's command line under the real /bin/sh")
class TestGuardAgainstTheRealShell(unittest.TestCase):
    """The guard's rules checked against what /bin/sh actually does with the
    command line ES builds: every value the guard calls SAFE must reach the
    hook byte-exact; the dangerous examples must really be mangled or run."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ngsh-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.probe = os.path.join(self.d, "probe.sh")
        with open(self.probe, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\000" "$@" > "$PROBE_OUT"\n')
        os.chmod(self.probe, 0o755)

    def through_es(self, value):
        out = os.path.join(self.d, "out")
        if os.path.exists(out):
            os.remove(out)
        cmd = name_guard.es_command_line(self.probe, ["gb", value, "x"])
        subprocess.run(["/bin/sh", "-c", cmd], cwd=self.d, timeout=10, capture_output=True,
                       env=dict(os.environ, PROBE_OUT=out))
        if not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            return f.read().split(b"\0")[:-1]

    def exact(self, value):
        return self.through_es(value) == [b"gb", value.encode("utf-8"), b"x"]

    SAFE = ["Jimmy White's Whirlwind Snooker", "Ren & Stimpy Show", "Ren &amp; Stimpy",
            "Pokémon Edición Azul (ポケモン)", "Tetris.gb", "/storage/roms/gb/Tetris.gb",
            "Super Mario Bros. 3 (USA) [!]", "100% Pure", "a=b", "-dash", "Tom; Jerry",
            "Disc (1) of 2", "Mario | Luigi", "A < B > C", "back \\ slash", "new\nline x"]
    DANGEROUS = ["Ma$ter", "Lode Runner (Ma$ter)", "Mega `echo x` Man", "Tom&Jerry",
                 "Sonic's", "Disc(1)", "a;b", "a|b", "a<b", "a>b", "back\\slash", "new\nline",
                 'say"hi', "Tom & Jerry", "Tom\t&\tJerry"]

    def test_every_safe_value_survives_byte_exact(self):
        for v in self.SAFE:
            self.assertEqual(name_guard.value_reasons(v), [], repr(v))
            self.assertTrue(self.exact(v), (v, self.through_es(v)))

    def test_every_dangerous_example_is_really_mangled(self):
        for v in self.DANGEROUS:
            self.assertTrue(name_guard.value_reasons(v), repr(v))
            self.assertFalse(self.exact(v), repr(v))

    def test_command_substitution_really_executes(self):
        canary = os.path.join(self.d, "canary")
        v = "Super $(touch %s) Game" % canary
        self.assertTrue(name_guard.value_reasons(v))
        self.through_es(v)
        self.assertTrue(os.path.exists(canary))     # why the guard exists

    def test_known_gaps_outside_the_owner_rules(self):
        # Outside the owner's rule set (23 Sep), so NOT flagged: these are
        # mangled (never executed) by the shell in a space-less value.
        # Documented in the FX-B report; this test pins the behaviour.
        open(os.path.join(self.d, "Superb"), "w").close()
        for v in ("#Tetris.gb", "Super*", "~"):
            self.assertEqual(name_guard.value_reasons(v), [], v)
            self.assertFalse(self.exact(v), v)


class TestAppHooks(TreeCase):
    def test_check_async_logs_a_warning_and_reports(self):
        write(os.path.join(self.root, "gb", "Ma$ter.gb"))
        got, logged = [], []
        t = name_guard.check_async(got.append, log_fn=logged.append, roots=[self.root],
                                   gamelist_dirs=[])
        t.join(10)
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["ok"])
        self.assertTrue(any("Ma$ter.gb" in m for m in logged), logged)
        s = name_guard.summary(got[0])
        self.assertIn("1", s)

    def test_check_async_survives_a_raising_callback(self):
        write(os.path.join(self.root, "gb", "ok.gb"))

        def boom(r):
            raise RuntimeError("consumer bug")
        t = name_guard.check_async(boom, log_fn=lambda m: None, roots=[self.root],
                                   gamelist_dirs=[])
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_summary_of_a_clean_result_is_empty(self):
        write(os.path.join(self.root, "gb", "ok.gb"))
        self.assertEqual(name_guard.summary(self.scan()), "")


class TestSummaryText(unittest.TestCase):
    """Task 4 (FX-E): the owner's finding is that ES's hook quoting GARBLES
    most game names (apostrophes, '&', ';', a stray '$') - it does not run
    them as shell code. Only $( ) / backtick can execute anything. The old
    text ("52 game names would be run as shell code by ES's hooks") was
    alarming and wrong for the common case."""

    def _result(self, values):
        return {"ok": False, "findings": [{"source": "filesystem", "file": "/x/%d" % i,
                                           "field": "filename", "value": v, "reasons": ["x"]}
                                          for i, v in enumerate(values)]}

    def test_ok_result_has_no_summary(self):
        self.assertEqual(name_guard.summary({"ok": True, "findings": []}), "")

    def test_garbled_names_are_not_called_shell_code(self):
        # 52 names with ordinary shell metacharacters (apostrophe, &, ;) that
        # ES's quoting mangles but never executes.
        s = name_guard.summary(self._result(["Sonic's"] * 52))
        self.assertIn("52 game names are garbled by ES's hook quoting", s)
        self.assertIn("see log", s)
        self.assertNotIn("would be run as shell code", s)
        self.assertNotIn("run as shell code", s)

    def test_singular_name_uses_is_not_are(self):
        s = name_guard.summary(self._result(["Tom&Jerry"]))
        self.assertIn("1 game name is garbled", s)

    def test_dollar_alone_is_garbled_not_executable(self):
        # 'Ma$ter': plain '$' expansion truncates the name; it cannot run a
        # command by itself (RV1-M3), so no "could run commands" wording.
        s = name_guard.summary(self._result(["Ma$ter"]))
        self.assertIn("garbled", s)
        self.assertNotIn("could run commands", s)

    def test_command_substitution_adds_the_stronger_warning(self):
        s = name_guard.summary(self._result(["Super $(touch /tmp/pwned) Game"]))
        self.assertIn("garbled", s)
        self.assertIn("could run commands", s)

    def test_backtick_adds_the_stronger_warning(self):
        s = name_guard.summary(self._result(["Mega `id` Man"]))
        self.assertIn("could run commands", s)

    def test_stronger_warning_only_when_a_finding_actually_has_it(self):
        # A mix: only the $( ) one should trigger "could run commands", but
        # since it applies to the whole (short) summary line, a batch with NO
        # such finding must never mention commands at all.
        no_exec = self._result(["Sonic's", "Tom&Jerry", "Ma$ter"])
        self.assertNotIn("could run commands", name_guard.summary(no_exec))
        with_exec = self._result(["Sonic's", "Mega `id` Man"])
        self.assertIn("could run commands", name_guard.summary(with_exec))


# ---------------------------------------------------------------------------
# NM2 (24 Sep 2026 test-day fix): name_guard now derives what to scan from
# ES's own es_systems.cfg instead of walking whole ROM roots. The tests above
# (TestScan et al.) never pass es_systems_cfg, so on a dev machine without a
# real /storage/.config/emulationstation/es_systems.cfg they exercise the
# LEGACY fallback path - byte-for-byte the pre-NM2 behaviour. These new
# classes exercise the cfg-driven scoped path itself.

class TestParseEsSystems(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ng-cfg-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = os.path.join(self.d, "es_systems.cfg")

    def test_parses_name_path_extension_case_insensitively(self):
        write_es_systems_cfg(self.cfg, {"nes": (os.path.join(self.d, "roms", "nes"),
                                                ".NES .Zip .7z")})
        systems, err = name_guard.parse_es_systems(self.cfg)
        self.assertIsNone(err)
        self.assertEqual(len(systems), 1)
        s = systems[0]
        self.assertEqual(s["name"], "nes")
        self.assertEqual(s["path"], os.path.join(self.d, "roms", "nes"))
        self.assertEqual(s["extensions"], frozenset((".nes", ".zip", ".7z")))

    def test_system_with_no_path_is_skipped(self):
        write(self.cfg, '<?xml version="1.0"?>\n<systemList>\n\t<system>\n\t\t<name>x</name>\n'
                       "\t</system>\n\t<system>\n\t\t<name>y</name>\n\t\t<path></path>\n"
                       "\t</system>\n</systemList>\n")
        systems, err = name_guard.parse_es_systems(self.cfg)
        self.assertIsNone(err)
        self.assertEqual(systems, [])

    def test_rompath_variable_is_expanded(self):
        write_es_systems_cfg(self.cfg, {"nes": ("%ROMPATH%/nes", ".nes")})
        systems, _ = name_guard.parse_es_systems(self.cfg)
        self.assertEqual(systems[0]["path"], name_guard.DEFAULT_ROOTS[0] + "/nes")

    def test_unparseable_cfg_reports_an_error_not_an_exception(self):
        write(self.cfg, b"not xml at all <<<")
        systems, err = name_guard.parse_es_systems(self.cfg)
        self.assertEqual(systems, [])
        self.assertIsNotNone(err)

    def test_missing_cfg_falls_back_to_legacy_scan(self):
        legacy_root = os.path.join(self.d, "roms")
        write(os.path.join(legacy_root, "gb", "Tom&Jerry.gb"))
        r = name_guard.scan(es_systems_cfg=os.path.join(self.d, "does-not-exist.cfg"),
                            roots=[legacy_root], gamelist_dirs=[])
        self.assertTrue(r["legacy_scan"])
        self.assertEqual([f["value"] for f in r["findings"]], ["Tom&Jerry.gb"])


class ScopedCase(unittest.TestCase):
    """Base for tests that exercise the NEW cfg-scoped scan path directly
    (as opposed to TreeCase above, whose self.scan() never passes an
    es_systems_cfg and so always exercises the legacy fallback)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ng-scoped-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "roms")

    def scan(self, systems, **kw):
        cfg = os.path.join(self.d, "es_systems.cfg")
        write_es_systems_cfg(cfg, systems)
        kw.setdefault("gamelist_dirs", [])
        r = name_guard.scan(es_systems_cfg=cfg, **kw)
        self.assertFalse(r["legacy_scan"], r.get("es_systems_cfg_error"))
        return r


class TestScopedWalk(ScopedCase):
    def test_extension_filter_only_flags_matching_files(self):
        write(os.path.join(self.root, "nes", "Ma$ter.nes"))
        write(os.path.join(self.root, "nes", "Ma$ter.txt"))          # dangerous name, wrong ext
        r = self.scan({"nes": (os.path.join(self.root, "nes"), ".nes")})
        self.assertEqual([f["value"] for f in r["findings"]], ["Ma$ter.nes"])

    def test_directory_flagged_only_if_it_holds_a_matching_file_at_any_depth(self):
        # Both "Disc(1)" and "Sub;Folder" have dangerous names AND (at some
        # depth) a matching file, so both are flagged, two levels apart; a
        # SAFE directory/file name is never a finding regardless of what it
        # contains (that is just value_reasons()); a dangerous name with
        # NOTHING playable inside ("Bad;Empty") is not something ES would
        # ever pass to a hook, so it must not be flagged either.
        write(os.path.join(self.root, "psx", "Disc(1)", "Sub;Folder", "Ma$ter.bin"))
        os.makedirs(os.path.join(self.root, "psx", "Bad;Empty"))     # dangerous name, no ROM inside
        r = self.scan({"psx": (os.path.join(self.root, "psx"), ".bin")})
        got = sorted((f["field"], f["value"]) for f in r["findings"])
        self.assertEqual(got, [("dirname", "Disc(1)"), ("dirname", "Sub;Folder"),
                               ("filename", "Ma$ter.bin")])

    def test_gamelist_at_system_path_is_read_a_deeper_one_is_a_backup(self):
        write(os.path.join(self.root, "nes", "gamelist.xml"), gamelist(("./a.nes", "Skitchin'")))
        write(os.path.join(self.root, "nes", "sub", "gamelist.xml"),
              gamelist(("./b.nes", "Toobin'")))
        r = self.scan({"nes": (os.path.join(self.root, "nes"), ".nes")})
        self.assertEqual([f["value"] for f in r["findings"]], ["Skitchin'"])
        self.assertEqual((r["gamelists"], r["gamelists_skipped"]), (1, 1))

    def test_root_filter_narrows_to_systems_under_the_given_directory(self):
        write(os.path.join(self.d, "a", "nes", "Ma$ter.nes"))
        write(os.path.join(self.d, "b", "gb", "Ma$ter.gb"))
        r = self.scan({"nes": (os.path.join(self.d, "a", "nes"), ".nes"),
                       "gb": (os.path.join(self.d, "b", "gb"), ".gb")},
                      roots=[os.path.join(self.d, "a")])
        self.assertEqual([f["value"] for f in r["findings"]], ["Ma$ter.nes"])
        self.assertEqual(r["roots"], [os.path.realpath(os.path.join(self.d, "a", "nes"))])

    def test_system_folder_itself_may_be_a_symlink_but_nothing_inside_is_followed(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        outside = os.path.join(self.d, "outside")
        write(os.path.join(outside, "Ma$ter.gb"))
        elsewhere = os.path.join(self.d, "elsewhere")
        write(os.path.join(elsewhere, "Deep$.gb"))
        try:
            os.symlink(outside, os.path.join(self.root, "gb"))              # the SYSTEM folder
            os.symlink(elsewhere, os.path.join(outside, "inner_link"))      # something INSIDE it
        except OSError:
            self.skipTest("symlinks not permitted here")
        r = self.scan({"gb": (os.path.join(self.root, "gb"), ".gb")})
        # Ma$ter.gb (through the system-folder symlink) is found; Deep$.gb
        # (through inner_link, not followed) never is
        self.assertEqual([f["value"] for f in r["findings"]], ["Ma$ter.gb"])


class TestBindMountSystemDedupe(ScopedCase):
    def test_two_systems_that_are_secretly_the_same_inode_scan_once(self):
        # A bind mount looks like two DIFFERENT paths with the SAME (st_dev,
        # st_ino) - simulated here with a monkeypatched os.stat rather than a
        # real mount, since the test environment cannot create one.
        sys_a = os.path.join(self.root, "gbA")
        sys_b = os.path.join(self.root, "gbB")
        write(os.path.join(sys_a, "Ma$ter.gb"))
        os.makedirs(sys_b, exist_ok=True)
        real_stat = os.stat
        rp_a, rp_b = os.path.realpath(sys_a), os.path.realpath(sys_b)

        def fake_stat(p, *a, **kw):
            if os.path.realpath(p) == rp_b:
                return real_stat(rp_a, *a, **kw)   # "b" is secretly the same inode as "a"
            return real_stat(p, *a, **kw)

        import unittest.mock as mock
        with mock.patch("os.stat", side_effect=fake_stat):
            r = self.scan({"gbA": (sys_a, ".gb"), "gbB": (sys_b, ".gb")})
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(len(r["roots"]), 1)
        self.assertEqual(len(r["systems"]), 1)


class TestRealDeviceScoping(unittest.TestCase):
    """Built from the real device capture (tests/fixtures/device/
    es_systems.cfg, 139 systems; tests/fixtures/device/guard-td2-2026-09-24.json,
    94 raw findings / 47 unique after the bind-mount root is collapsed - see
    the session report for the by-hand cross-check). Reconstructs, under a
    temp tree, the real extensions for the 9 systems the 19 non-Steam/theme
    findings live in, the real dangerous ROM/gamelist values, AND the real
    Steam/theme clutter (steamapps, userdata, the runtime, a theme .svg) -
    with steam and themes NOT declared as ES systems (they never are on the
    real device either: steam's real <path> is
    /storage/.local/share/applications with extension .desktop; there is no
    "themes" system at all). Proves the exact expected remaining count."""

    # The real extensions, copied from tests/fixtures/device/es_systems.cfg.
    SYSTEMS = {
        "nes": ".nes .unif .unf .zip .7z",
        "gba": ".gba .zip .7z",
        "nds": ".nds .zip .7z",
        "genesis": ".bin .gen .md .sg .smd .zip .7z",
        "megadrive": ".bin .gen .md .sg .smd .zip .7z",
        "famicom": ".nes .unif .unf .zip .7z",
        "gbc": ".gb .gbc .zip .7z",
        "msx": ".dsk .mx1 .mx2 .rom .zip .7z .m3u",
        "snes": ".smc .fig .sfc .swc .zip .7z",
    }

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rp5deck-ng-real-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "roms")
        systems = {name: (os.path.join(self.root, name), exts)
                  for name, exts in self.SYSTEMS.items()}
        self.cfg = os.path.join(self.d, "es_systems.cfg")
        write_es_systems_cfg(self.cfg, systems)
        self._build_rom_findings()
        self._build_gamelists()
        self._build_steam_and_theme_clutter()

    def _build_rom_findings(self):
        write(os.path.join(self.root, "gba", "WarioWare, Inc. - Mega Microgame$! (USA).gba"))
        write(os.path.join(self.root, "nds", "Metropolis Crime$ (Europe) (En,Fr,De,Es,It).zip"))
        write(os.path.join(self.root, "nes", "Ren & Stimpy Show, The - Buckeroo$! (USA).nes"))

    def _build_gamelists(self):
        write(os.path.join(self.root, "famicom", "gamelist.xml"),
              gamelist(("./Wits.nes", "Wit's")))
        write(os.path.join(self.root, "gba", "gamelist.xml"), gamelist(
            ("./WarioWare, Inc. - Mega Microgame$! (USA).gba", "WarioWare, Inc. : Minigame Mania")))
        write(os.path.join(self.root, "gbc", "gamelist.xml"),
              gamelist(("./Toobin.gbc", "Toobin'")))
        write(os.path.join(self.root, "genesis", "gamelist.xml"), gamelist(
            ("./Skitchin.md", "Skitchin'"), ("./WizNLiz.md", "Wiz'n'Liz")))
        write(os.path.join(self.root, "megadrive", "gamelist.xml"),
              gamelist(("./WizNLiz.md", "Wiz'n'Liz")))
        write(os.path.join(self.root, "msx", "gamelist.xml"),
              gamelist(("./Squishem.rom", "Squish'em")))
        write(os.path.join(self.root, "nds", "gamelist.xml"), gamelist(
            ("./Metropolis Crime$ (Europe) (En,Fr,De,Es,It).zip", "Metropolis Crimes")))
        write(os.path.join(self.root, "nes", "gamelist.xml"), gamelist(
            ("./Ren & Stimpy Show, The - Buckeroo$! (USA).nes", "The Ren & Stimpy Show : Buckeroo$!"),
            ("./Empereur.nes", "L'Empereur"),
            ("./Toobin.nes", "Toobin'"),
            ("./Spookotron.nes", "Spook-o'-tron"),
            ("./DVeelNg.nes", "D'Veel'Ng"),
            ("./SteinsGate.nes", "Steins;Gate")))
        write(os.path.join(self.root, "snes", "gamelist.xml"),
              gamelist(("./Buckeroos.sfc", "The Ren & Stimpy Show : Buckeroo$!")))

    def _build_steam_and_theme_clutter(self):
        # Real device names where portable across Windows+WSL (a literal
        # backslash inside one path COMPONENT, e.g. "steamrtarm64\libs", is a
        # Windows path separator and cannot be created as one component on
        # NTFS - represented here as real subdirectories instead; that detail
        # does not matter for THIS test, which only proves the whole steam/
        # tree is never visited at all).
        steam = os.path.join(self.root, "steam")
        write(os.path.join(steam, "steamapps", "common", "Fallout 4", "Data",
                           "ccbgsfo4014-pipboy(white).esl"))
        write(os.path.join(steam, "steamapps", "common", "Batman Arkham City GOTY", "BmGame",
                           "CookedPCConsole", "English(US)", "dummy.bin"))
        write(os.path.join(steam, "steamapps", "common",
                           "FINAL FANTASY FFX&FFX-2 HD Remaster", "FFX&X-2_LAUNCHER.exe"))
        write(os.path.join(steam, "userdata", "30897629", "241100", "remote",
                           "controller_config", "413080", "$$$autosave.vdf"))
        write(os.path.join(steam, "steamrtarm64", "libs", "libcurl.so"))
        write(os.path.join(self.root, "themes", "AI-HYPERTOCERA-THE-MINI-CAKE-TV", "art", "logos",
                           "collections", "Baldur`s Gate.svg"))

    # The 19 non-Steam/theme findings that should remain (source, field, value).
    EXPECTED = sorted([
        ("gamelist", "name", "Wit's"),
        ("filesystem", "filename", "WarioWare, Inc. - Mega Microgame$! (USA).gba"),
        ("gamelist", "path", "./WarioWare, Inc. - Mega Microgame$! (USA).gba"),
        ("gamelist", "name", "Toobin'"),          # gbc
        ("gamelist", "name", "Skitchin'"),
        ("gamelist", "name", "Wiz'n'Liz"),        # genesis
        ("gamelist", "name", "Wiz'n'Liz"),         # megadrive (same text, different file)
        ("gamelist", "name", "Squish'em"),
        ("filesystem", "filename", "Metropolis Crime$ (Europe) (En,Fr,De,Es,It).zip"),
        ("gamelist", "path", "./Metropolis Crime$ (Europe) (En,Fr,De,Es,It).zip"),
        ("filesystem", "filename", "Ren & Stimpy Show, The - Buckeroo$! (USA).nes"),
        ("gamelist", "name", "L'Empereur"),
        ("gamelist", "path", "./Ren & Stimpy Show, The - Buckeroo$! (USA).nes"),
        ("gamelist", "name", "The Ren & Stimpy Show : Buckeroo$!"),   # nes
        ("gamelist", "name", "Toobin'"),           # nes (distinct file from gbc's)
        ("gamelist", "name", "Spook-o'-tron"),
        ("gamelist", "name", "D'Veel'Ng"),
        ("gamelist", "name", "Steins;Gate"),
        ("gamelist", "name", "The Ren & Stimpy Show : Buckeroo$!"),   # snes
    ])

    def test_exactly_the_19_non_steam_theme_findings_remain(self):
        r = name_guard.scan(es_systems_cfg=self.cfg, gamelist_dirs=[])
        self.assertFalse(r["legacy_scan"], r.get("es_systems_cfg_error"))
        self.assertEqual(len(r["findings"]), 19, r["findings"])
        got = sorted((f["source"], f["field"], f["value"]) for f in r["findings"])
        self.assertEqual(got, self.EXPECTED)

    def test_zero_steam_or_theme_findings_and_they_are_never_even_visited(self):
        r = name_guard.scan(es_systems_cfg=self.cfg, gamelist_dirs=[])
        for f in r["findings"]:
            self.assertNotIn(os.sep + "steam" + os.sep, f["file"])
            self.assertNotIn(os.sep + "themes" + os.sep, f["file"])
        # "steam" and "themes" are not declared systems at all, so their
        # directories are outside every scanned root
        scanned = r["roots"]
        steam_dir = os.path.realpath(os.path.join(self.root, "steam"))
        themes_dir = os.path.realpath(os.path.join(self.root, "themes"))
        for s in scanned:
            self.assertFalse(steam_dir == s or steam_dir.startswith(s + os.sep))
            self.assertFalse(themes_dir == s or themes_dir.startswith(s + os.sep))


if __name__ == "__main__":
    unittest.main()
