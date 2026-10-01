#!/usr/bin/env python3
"""SW1: the screen swap in rocknix-config/dual-screen-layout-and-power.

Four kinds of check, all on the REAL script text (never a reimplementation):

 1. Byte-identity (pure Python, any platform): every function of the pre-SW1
    092 (tests/fixtures/dual-screen-layout-and-power-pre-SW1-copy-2026-09-24 - since
    test day this is the INSTALLED a1d2f772 = main d519672c + the prefix-tolerant
    second-window pattern (wip/092-title-prefix) + the rp5deck-ytapp
    placement rule in ensure_rp5deck_rules (24 Sep evening, owner-approved;
    the YouTube App mapped behind ES without it - tests/test_092_app_ids.py),
    sha256 03a6cc...) is compared with the working copy, function by
    function. The charger session's functions and the PR_Swap retry block
    must be byte-identical; only dominant_for, park_es and apply_layout may
    change; the main loop may differ ONLY by the SW1 block.
 2. The reader contract: 092's rp5_es_screen() (run under dash, with its own
    python3 snippet) and rp5deck's config.es_screen_from_raw()/load() must
    give the same answer for the same file, for every file shape below.
 3. Function behaviour under dash (dominant_for / cc_output_for / the
    stat-cached re-read).
 4. The whole daemon loop under dash against a stateful sway stand-in
    (tests/sw1_sway_sim.py), paths redirected into a temp dir, sleep/pidof/
    mount/wpctl stubbed. Old vs new command logs must be IDENTICAL whenever
    the swap is off (docked, undocked, boot-then-dock); with the swap on, ES
    and workspace 1 must converge on the built-in panel and back, under both
    models of sway's `workspace N output X`.

Shell parts need dash: directly on Linux, through `wsl.exe -e` on Windows;
skipped (not passed) if neither exists.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import config  # noqa: E402

REPO = os.path.dirname(os.path.dirname(APP))
# SW1_092_PATH: run the suite against another copy (the break-restore proofs
# mutate a scratch copy, never the working file)
NEW_092 = os.environ.get("SW1_092_PATH") or os.path.join(os.path.dirname(APP), "scripts", "dual-screen-layout-and-power")
OLD_092 = os.path.join(HERE, "fixtures", "dual-screen-layout-and-power-pre-SW1-copy-2026-09-24")
# 24 Sep evening: re-pinned after the rp5deck-ytapp rule was folded in (was 7d0bf8ff...)
# 25 Sep: re-pinned after the Azahar game-window rule in ensure_rp5deck_rules
# was folded in (was 4c5d0df6..., owner-approved: "do 2")
# 25 Sep ~16:40: re-pinned after W1 was folded in - the second-window pattern
# now allows Cemu's " - FPS: n" suffix, plus the WINFIX block and its two
# one-argument fix_game_windows calls in the loop (was 10d6b423..., owner:
# "lets do the screen fix"). W1 is shared text in both copies, so these
# checks still isolate SW1; W1 itself is tested in test_092_winfix.py.
# 25 Sep ~20:10: re-pinned after top-screen-off was folded in (TOPOFF flag,
# external_active/enforce_top_off, the dual->single mapping, the no-nudge
# guard, detach clears; was 98c08826..., owner: "lets do 1"). Shared text in
# both copies; top-off itself is tested in test_092_topoff.py.
# ~20:40: re-pinned again for the top-off power cut (was b913c81f...).
# ~21:00: re-pinned for the daemon renames (was aed3e6e4...).
# batch 1: re-pinned for the cut-boot re-attach nudge (was 41e20cae...).
# 26 Sep: re-pinned for the names without numbers (was 3d5561ab...).
# 26 Sep: re-pinned for the sway-restart re-layout (was fd3d4104...).
# 26 Sep: re-pinned for the touchscreen watch (was 9d3a12b0...).
# 26 Sep: re-pinned for the keyboard-screen rule (was f7c9840f...).
OLD_SHA = "6e25936e647da2776599ed9dda8344114d5567b52435418299f9185f1816561c"   # ... + touch watch + keyboard on the Command Center screen + add-on replug notice + keyboard service restart + cheaper poll (fewer processes per poll)
SIM = os.path.join(HERE, "sw1_sway_sim.py")
LINUX = sys.platform.startswith("linux")

# The charger session's code (task: must stay BYTE-IDENTICAL), plus
# everything else SW1 had no reason to touch.
PROTECTED = ["power_present", "role_is", "try_wake_dp", "settle_to_sink",
             "dp_race_detected", "reset_port_for_dp", "dp_altmode_dir", "prefer_screen",
             "probe_state", "es_output", "set_audio_internal", "stop_output_monitor",
             "neutralise_output_monitor", "rotate_log", "log", "ensure_rp5deck_rules"]
ALLOWED_CHANGED = {"dominant_for", "park_es", "apply_layout"}
SW1_NEW = {"rp5_es_screen", "place_cc_windows", "ensure_swap_rules", "read_es_swap",
           "cc_output_for", "ws1_follow", "ws1_output", "ensure_undocked_web_rules"}


def read(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def code_only(text):
    """The script without its full-line comments (the shebang stays), so comments can be
    reworded while every code line is still compared."""
    lines = text.split("\n")
    return "\n".join(ln for i, ln in enumerate(lines) if i == 0 or not ln.lstrip().startswith("#"))


def functions(text):
    """name -> exact text from `name() {` to its closing `}` line."""
    out = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{(.*)$", lines[i])
        if m:
            name = m.group(1)
            if m.group(2).rstrip().endswith("}"):          # one-liner
                out[name] = lines[i]
                i += 1
                continue
            j = i + 1
            while j < len(lines) and lines[j] != "}":
                j += 1
            out[name] = "\n".join(lines[i:j + 1])
            i = j + 1
            continue
        i += 1
    return out


def swap_retry_block(text):
    a = text.index('        if [ "$state" = "dual" ]; then\n'
                   '            if role_is source && [ "$sink_settled" = "yes" ]; then\n')
    b = text.index("            settle_to_sink\n        fi\n", a)
    return text[a:b + len("            settle_to_sink\n        fi\n")]


def main_loop(text):
    return text[text.index("\nwhile true; do\n"):]


SW1_LOOP_BLOCK_START = '\n    read_es_swap\n    if [ "$state" = "dual" ]; then\n'


def strip_sw1_loop_block(loop):
    a = loop.index(SW1_LOOP_BLOCK_START)
    b = loop.index('    if [ "$state" = "single" ]; then\n', a)
    return loop[:a + 1] + loop[b:]


# ---------------------------------------------------------------------------
# dash runner
# ---------------------------------------------------------------------------
def wsl():
    return shutil.which("wsl.exe") or shutil.which("wsl")


def to_sh_path(p):
    if LINUX:
        return p
    p = os.path.abspath(p).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = "/mnt/%s%s" % (p[0].lower(), p[2:])
    return p


def have_dash():
    if LINUX:
        return shutil.which("dash") is not None
    w = wsl()
    if not w:
        return False
    try:
        r = subprocess.run([w, "-e", "sh", "-c", "command -v dash && command -v python3"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and "dash" in r.stdout
    except Exception:
        return False


DASH = have_dash()


def run_dash(script, timeout=180):
    if LINUX:
        return subprocess.run(["dash", "-c", script], capture_output=True, text=True,
                              timeout=timeout)
    return subprocess.run([wsl(), "-e", "dash", "-c", script], capture_output=True, text=True,
                          timeout=timeout)


def sh_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def lib_for(text, names, extra=""):
    """A dash prelude: 092's constants + the named functions, verbatim."""
    fns = functions(text)
    parts = ["set -u",
             'EXTERNAL="DP-1"', 'INTERNAL="DSI-1"',
             "log() { echo \"LOG $*\"; }"]
    parts.append(extra)
    for n in names:
        parts.append(fns[n])
    return "\n".join(parts) + "\n"


class TmpDir(unittest.TestCase):
    def mk(self):
        d = tempfile.mkdtemp(prefix="sw1-092-")
        self.addCleanup(shutil.rmtree, d, True)
        return d


# ---------------------------------------------------------------------------
# 1. byte identity
# ---------------------------------------------------------------------------
class TestChargingCodeByteIdentical(unittest.TestCase):
    """The charger session owns the charging/dock code: power_present,
    role_is, try_wake_dp, settle_to_sink, dp_race_detected,
    reset_port_for_dp and the swap retry must stay byte-identical."""

    @classmethod
    def setUpClass(cls):
        cls.raw_old = read(OLD_092)
        cls.old = code_only(cls.raw_old)
        cls.new = code_only(read(NEW_092))
        cls.fo = functions(cls.old)
        cls.fn = functions(cls.new)

    def test_baseline_is_the_pinned_pre_sw1_file(self):
        self.assertEqual(hashlib.sha256(self.raw_old.encode()).hexdigest(), OLD_SHA)

    def test_protected_functions_are_byte_identical(self):
        for name in PROTECTED:
            with self.subTest(name):
                self.assertIn(name, self.fo)
                self.assertIn(name, self.fn)
                self.assertEqual(self.fn[name], self.fo[name], name)

    def test_only_the_three_placement_functions_changed(self):
        changed = {n for n in self.fo if n in self.fn and self.fn[n] != self.fo[n]}
        self.assertEqual(changed, ALLOWED_CHANGED)
        self.assertEqual(set(self.fo) - set(self.fn), set(), "a function was removed")
        self.assertEqual(set(self.fn) - set(self.fo), SW1_NEW)

    def test_swap_retry_block_is_byte_identical(self):
        self.assertEqual(swap_retry_block(self.new), swap_retry_block(self.old))
        self.assertIn("swap_retry=$((swap_retry + 1))", swap_retry_block(self.new))

    def test_main_loop_differs_only_by_the_sw1_block(self):
        self.assertEqual(strip_sw1_loop_block(main_loop(self.new)), main_loop(self.old))

    def test_apply_layout_change_is_the_one_dom_line(self):
        o = self.fo["apply_layout"].split("\n")
        n = self.fn["apply_layout"].split("\n")
        self.assertEqual(len(o), len(n))
        diff = [(a, b) for a, b in zip(o, n) if a != b]
        self.assertEqual(diff, [(
            '    if [ "$dual" = "yes" ]; then dom="$EXTERNAL"; else dom="$INTERNAL"; fi',
            '    if [ "$dual" = "yes" ]; then dom=$(dominant_for dual); else dom="$INTERNAL"; fi')])

    def test_park_es_change_is_two_gated_calls_the_web_workspace_guard_and_comments(self):
        o = self.fo["park_es"].split("\n")
        added = [ln for ln in self.fn["park_es"].split("\n") if ln not in o]
        code = [ln.strip() for ln in added if not ln.strip().startswith("#")]
        gated = '[ "$es_swapped_seen" = "yes" ] && ws1_follow "$target"'
        # the guard lets a web app shown undocked on its own workspace stay on screen
        guard = 'case "$vis" in rp5deck-undocked-*) return 1 ;; esac'
        self.assertEqual(code, [gated, guard, gated])
        # nothing of the old body was removed
        self.assertEqual([ln for ln in self.fn["park_es"].split("\n") if ln in o], o)

    def test_the_whole_file_is_insertions_except_two_lines(self):
        """Line diff of the whole script: every pre-SW1 line survives in
        order, except exactly these two code lines that now consult es_swap
        (comments are left out of the comparison)."""
        import difflib
        o, n = self.old.split("\n"), self.new.split("\n")
        removed = []
        for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, o, n, autojunk=False) \
                .get_opcodes():
            if tag in ("delete", "replace"):
                removed += o[i1:i2]
        self.assertEqual(removed, [
            '    if [ "$dual" = "yes" ]; then dom="$EXTERNAL"; else dom="$INTERNAL"; fi',
            '    if [ "$1" = "dual" ]; then echo "$EXTERNAL"; else echo "$INTERNAL"; fi',
        ])

    def test_prints_the_function_table(self):
        rows = []
        for n in sorted(set(self.fo) | set(self.fn)):
            if n not in self.fo:
                rows.append("  NEW        %s" % n)
            elif self.fo[n] == self.fn[n]:
                rows.append("  identical  %s" % n)
            else:
                rows.append("  CHANGED    %s" % n)
        if os.environ.get("SW1_SHOW_TABLE"):
            print("\n092 function table (old -> new):\n" + "\n".join(rows))
        self.assertTrue(rows)


# ---------------------------------------------------------------------------
# 2. the reader contract, 092 vs config.py
# ---------------------------------------------------------------------------
def contract_cases():
    swapped_full = config.defaults()
    config.set_value(swapped_full, ("screens", "es_screen"), "builtin_bottom")
    for k in ("output", "es_output"):
        swapped_full.pop(k)
    unswapped_full = config.defaults()
    for k in ("output", "es_output"):
        unswapped_full.pop(k)
    return [
        ("missing", None),
        ("empty", ""),
        ("not-json", "{nope"),
        ("list", "[]"),
        ("screens-list", json.dumps({"screens": []})),
        ("swapped", json.dumps({"screens": {"es_screen": "builtin_bottom"}})),
        ("unswapped", json.dumps({"screens": {"es_screen": "addon_top"}})),
        ("upper-case-typo", json.dumps({"screens": {"es_screen": "BUILTIN_BOTTOM"}})),
        ("int", json.dumps({"screens": {"es_screen": 1}})),
        ("null", json.dumps({"screens": {"es_screen": None}})),
        ("cc-only-swapped", json.dumps({"screens": {"command_center_screen": "addon_top"}})),
        ("legacy-flat-swapped", json.dumps({"output": "DP-1", "es_output": "DSI-1"})),
        ("schema-v1-swapped", json.dumps(swapped_full, indent=2, sort_keys=True)),
        ("schema-v1-unswapped", json.dumps(unswapped_full, indent=2, sort_keys=True)),
        ("bom-swapped", "﻿" + json.dumps({"screens": {"es_screen": "builtin_bottom"}})),
        ("trailing-garbage", json.dumps({"screens": {"es_screen": "builtin_bottom"}}) + "x"),
    ]


def config_view(text):
    """What rp5deck makes of the same file: (contract, load()'s es_screen)."""
    d = tempfile.mkdtemp(prefix="sw1-cfg-")
    try:
        p = os.path.join(d, "config.json")
        if text is not None:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        try:
            raw = json.loads(text) if text is not None else None
        except ValueError:
            raw = None
        cfg, _note = config.load(p)
        return config.es_screen_from_raw(raw), cfg["screens"]["es_screen"], \
            cfg["screens"]["command_center_screen"]
    finally:
        shutil.rmtree(d, True)


@unittest.skipUnless(DASH, "dash (native, or in WSL) is needed to run 092's functions")
class TestReaderContract(TmpDir):
    def run_reader(self, text_by_name):
        d = self.mk()
        lines = [lib_for(read(NEW_092), ["rp5_es_screen"])]
        for name, text in text_by_name:
            p = os.path.join(d, name + ".json")
            if text is not None:
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            lines.append("RP5_CONFIG=%s; printf '%%s=%%s\\n' %s \"$(rp5_es_screen)\""
                         % (sh_quote(to_sh_path(p)), sh_quote(name)))
        r = run_dash("\n".join(lines))
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(ln.split("=", 1) for ln in r.stdout.split() if "=" in ln)

    def test_092_and_rp5deck_agree_on_every_file_shape(self):
        cases = contract_cases()
        got = self.run_reader(cases)
        for name, text in cases:
            with self.subTest(name):
                contract, loaded, cc = config_view(text)
                self.assertEqual(got[name], contract, "092 vs es_screen_from_raw")
                self.assertEqual(loaded, contract, "config.load vs contract")
                self.assertNotEqual(cc, loaded, "Command Center screen == ES screen")

    def test_default_is_unswapped_for_everything_but_the_literal_value(self):
        got = self.run_reader(contract_cases())
        swapped = {n for n, v in got.items() if v == "builtin_bottom"}
        self.assertEqual(swapped, {"swapped", "schema-v1-swapped"})


# ---------------------------------------------------------------------------
# 3. functions under dash
# ---------------------------------------------------------------------------
@unittest.skipUnless(DASH, "dash needed")
class TestFunctionsUnderDash(TmpDir):
    def test_dominant_and_cc_outputs(self):
        lib = lib_for(read(NEW_092), ["dominant_for", "cc_output_for"])
        body = []
        for swap in ("no", "yes"):
            for st in ("dual", "single"):
                body.append('es_swap=%s; echo "%s %s $(dominant_for %s) $(cc_output_for %s)"'
                            % (swap, swap, st, st, st))
        r = run_dash(lib + "\n".join(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("\n")[:4], [
            "no dual DP-1 DSI-1",       # the pre-SW1 layout
            "no single DSI-1 DSI-1",
            "yes dual DSI-1 DP-1",      # swapped: ES on the built-in panel
            "yes single DSI-1 DSI-1",   # undocked: the swap is ignored
        ])

    def test_re_read_follows_each_atomic_save(self):
        d = self.mk()
        p = os.path.join(d, "config.json")
        lib = lib_for(read(NEW_092), ["rp5_es_screen", "read_es_swap"],
                      extra="RP5_CONFIG=%s; es_swap=no; rp5_cfg_sig=unread"
                      % sh_quote(to_sh_path(p)))
        # each step: write the file (or not) from dash itself, then read
        steps = [
            ("", "no"),
            ("mv_in builtin_bottom", "yes"),
            ("", "yes"),                                  # cached, unchanged
            ("mv_in addon_top", "no"),
            ("mv_in builtin_bottom", "yes"),
            ("rm -f \"$RP5_CONFIG\"", "no"),
        ]
        body = ["mv_in() { printf '{\"screens\": {\"es_screen\": \"%s\"}}' \"$1\" > \"$RP5_CONFIG.t\";"
                " mv \"$RP5_CONFIG.t\" \"$RP5_CONFIG\"; }"]
        for cmd, _want in steps:
            if cmd:
                body.append(cmd)
            body.append("read_es_swap; echo \"swap=$es_swap\"")
        r = run_dash(lib + "\n".join(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = [ln.split("=")[1] for ln in r.stdout.split() if ln.startswith("swap=")]
        self.assertEqual(got, [w for _c, w in steps])


# ---------------------------------------------------------------------------
# 4. the whole loop against the sway stand-in
# ---------------------------------------------------------------------------
SUBS = [
    ("LOG=/storage/.config/autostart/dual-screen-layout-and-power.log", "LOG=@T@/092.log"),
    ("LOCK=/run/dual-screen-layout-and-power.pid", "LOCK=@T@/092.pid"),
    ("DISABLE=/storage/.disable-dualscreen", "DISABLE=@T@/disable"),
    ("NOOP=/storage/.config/noop-output-monitor", "NOOP=@T@/noop"),
    ("MODEFILE=/storage/dual-screen-mode", "MODEFILE=@T@/mode"),
    ("CHARGER=/sys/class/power_supply/pm8150b-charger/online", "CHARGER=@T@/charger"),
    ("PORT=/sys/class/typec/port0", "PORT=@T@/port0"),
    ("find /run /tmp -name 'sway-ipc", "find @T@ -name 'sway-ipc"),
]
SUB_NEW_ONLY = [("RP5_CONFIG=/storage/rp5deck/config.json", "RP5_CONFIG=@T@/config.json")]

ES_HOME_PAIR = '[app_id="^emulationstation$"] move container to workspace number 1\nworkspace number 1\n'
ROCKNIX_ES_RULE = 'for_window [app_id="emulationstation"] move output DP-1'


def world(docked=True, es_on=None, extra_windows=(), assign_moves=False):
    outs = ["DSI-1", "DP-1"] if docked else ["DSI-1"]
    es_on = es_on or ("DP-1" if docked else "DSI-1")
    other = "DSI-1" if es_on == "DP-1" else "DP-1"
    wss = [{"name": "1", "output": es_on,
            "windows": [{"app_id": "emulationstation", "title": "EmulationStation"}]}]
    vis = {es_on: "1"}
    if docked:
        wss.append({"name": "2", "output": other, "windows": list(extra_windows)})
        vis[other] = "2"
    return {"tick": 0, "outputs": outs, "workspaces": wss, "visible": vis,
            "focused_ws": "1", "focused_win": "emulationstation",
            "assign": {"1": ["DP-1", "DSI-1"]}, "rules": [ROCKNIX_ES_RULE],
            "assign_moves": assign_moves, "timeline": {}, "stop_at": 10, "history": []}


def swapped_cfg():
    return {"schema_version": 1, "screens": {"es_screen": "builtin_bottom",
                                             "command_center_screen": "addon_top"}}


def unswapped_cfg():
    return {"schema_version": 1, "screens": {"es_screen": "addon_top",
                                             "command_center_screen": "builtin_bottom"}}


def prepare_script(src_text, t, new=True, mutate=None):
    s = src_text
    for a, b in SUBS + (SUB_NEW_ONLY if new else []):
        assert s.count(a) == 1, a
        s = s.replace(a, b)
    s = s.replace("@T@", to_sh_path(t))
    s = s.replace("#!/bin/bash", "#!/bin/dash", 1)
    if mutate:
        s = mutate(s)
    return s


class LoopSim(TmpDir):
    def simulate(self, st, script_text, new=True, config_obj=None, mutate=None):
        t = self.mk()
        b = os.path.join(t, "bin")
        os.makedirs(b)
        stubs = {
            "swaymsg": '#!/bin/sh\nexec python3 "$SIM_PY" "$@"\n',
            "sleep": '#!/bin/sh\n[ "$1" = "5" ] && exec python3 "$SIM_PY" --tick\nexit 0\n',
            "pidof": '#!/bin/sh\n[ "$1" = sway ] && { echo 4242; exit 0; }\nexit 1\n',
            "mount": "#!/bin/sh\nexit 1\n",
            "wpctl": "#!/bin/sh\nexit 0\n",
            "pw-cli": "#!/bin/sh\nexit 0\n",
        }
        for name, body in stubs.items():
            p = os.path.join(b, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        open(os.path.join(t, "sway-ipc.4242.sock"), "w").close()
        st = dict(st)
        st["config_path"] = to_sh_path(os.path.join(t, "config.json"))
        st["disable_path"] = to_sh_path(os.path.join(t, "disable"))
        if config_obj is not None:
            with open(os.path.join(t, "config.json"), "w", encoding="utf-8") as f:
                json.dump(config_obj, f)
        with open(os.path.join(t, "state.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        script = os.path.join(t, "092")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(prepare_script(script_text, t, new, mutate))
        os.chmod(script, 0o755)
        T = to_sh_path(t)
        cmd = ("export PATH=%s/bin:$PATH SIM_PY=%s SIM_STATE=%s/state.json SIM_LOG=%s/cmds.log; "
               "chmod +x %s/bin/* %s/092; dash %s/092 --fg; echo rc=$?"
               % (T, sh_quote(to_sh_path(SIM)), T, T, T, T, T))
        r = run_dash(cmd, timeout=300)
        self.assertIn("rc=0", r.stdout, r.stdout + r.stderr)
        with open(os.path.join(t, "state.json"), encoding="utf-8") as f:
            final = json.load(f)
        cmds = read(os.path.join(t, "cmds.log")) if os.path.exists(os.path.join(t, "cmds.log")) \
            else ""
        log = read(os.path.join(t, "092.log")) if os.path.exists(os.path.join(t, "092.log")) \
            else ""
        return final, cmds, log

    def at(self, final, tick, label="tick"):
        for h in final["history"]:
            if h["tick"] == tick and h["label"] == label:
                return h
        self.fail("no %s snapshot at tick %d" % (label, tick))


@unittest.skipUnless(DASH, "dash needed")
class TestLoopUnswappedIsUnchanged(LoopSim):
    """With the swap off, the new 092 must send sway exactly the commands the
    pre-SW1 092 sends - docked, undocked (even with a swapped setting on
    disk, since undocked ignores it), and boot-then-dock."""

    def compare(self, st, config_obj):
        old = self.simulate(st, read(OLD_092), new=False, config_obj=config_obj)
        new = self.simulate(st, read(NEW_092), new=True, config_obj=config_obj)
        self.assertTrue(old[1].strip(), "the simulation sent no commands at all")
        # the two rules that send a newly mapped web window to its own workspace when undocked are
        # the only commands the new script adds with the swap off
        new_cmds = "".join(l for l in new[1].splitlines(True)
                           if "--show-undocked-window" not in l and "--home-es-window" not in l)
        # what that ES rule's handler sends when ES maps: its outputs query (a form nothing else uses), and
        # undocked the move to workspace 1 and the switch to it, as one pair
        new_cmds = new_cmds.replace(ES_HOME_PAIR, "")
        new_cmds = "".join(l for l in new_cmds.splitlines(True) if l != "-r -t get_outputs\n")
        self.assertEqual(new_cmds, old[1])
        self.assertEqual(new[0]["history"], old[0]["history"])
        return old, new

    def test_docked_no_config_with_a_game(self):
        st = world(docked=True)
        st["timeline"] = {"4": [["game_start", None]], "6": [["game_exit", None]]}
        st["stop_at"] = 9
        old, new = self.compare(st, None)
        self.assertEqual(self.at(new[0], 9)["es"], "DP-1")

    def test_docked_explicitly_unswapped(self):
        st = world(docked=True)
        st["stop_at"] = 6
        self.compare(st, unswapped_cfg())

    def test_undocked_ignores_a_swapped_setting(self):
        st = world(docked=False)
        st["timeline"] = {"3": [["game_start", None]], "5": [["game_exit", None]]}
        st["stop_at"] = 8
        old, new = self.compare(st, swapped_cfg())
        self.assertNotIn("swap window rules", new[2])
        self.assertEqual(self.at(new[0], 8)["es"], "DSI-1")

    def test_boot_then_dock(self):
        st = world(docked=False)
        st["assign_moves"] = True
        st["timeline"] = {"3": [["dock", None]]}
        st["stop_at"] = 9
        old, new = self.compare(st, None)
        self.assertEqual(self.at(new[0], 9)["es"], "DP-1")


@unittest.skipUnless(DASH, "dash needed")
class TestLoopSwap(LoopSim):
    """The swap itself, live: ES and workspace 1 go to the built-in panel
    within a poll or two of the setting changing, stay there across a game
    (ES's new window lands on the right panel at once, with focus), and come
    back when the setting is cleared - under both models of `workspace N
    output X`, with no ES ping-pong."""

    TIMELINE = {"3": [["config", swapped_cfg()]],
                "6": [["game_start", None]],
                "8": [["game_exit", None]],
                "11": [["config", unswapped_cfg()]]}

    def run_swap(self, assign_moves, script=None, mutate=None):
        st = world(docked=True, assign_moves=assign_moves)
        st["timeline"] = self.TIMELINE
        st["stop_at"] = 15
        return self.simulate(st, script or read(NEW_092), new=script is None,
                             config_obj=None, mutate=mutate)

    def check_swap(self, final, log):
        h5 = self.at(final, 5)
        self.assertEqual(h5["es"], "DSI-1", h5)
        self.assertEqual(h5["ws1"], "DSI-1", h5)
        self.assertEqual(h5["visible"]["DSI-1"], "1", h5)
        exit_map = self.at(final, 8, "after-es-map")
        self.assertEqual(exit_map["es"], "DSI-1", "ES's new window after the game: %s" % exit_map)
        self.assertEqual(exit_map["focused_win"], "emulationstation", exit_map)
        h10 = self.at(final, 10)
        self.assertEqual(h10["es"], "DSI-1", h10)
        h13 = self.at(final, 13)
        self.assertEqual(h13["es"], "DP-1", h13)
        self.assertEqual(h13["visible"]["DP-1"], "1", h13)
        h15 = self.at(final, 15)
        self.assertEqual(h15["es"], "DP-1", h15)
        moves = log.count("moved EmulationStation")
        self.assertLessEqual(moves, 2, "ES ping-pong:\n" + log)
        self.assertEqual(log.count("swap window rules applied"), 1, log)

    def test_swap_and_back_assignment_does_not_move(self):
        final, cmds, log = self.run_swap(assign_moves=False)
        self.check_swap(final, log)
        self.assertEqual(sum(1 for r in final["rules"] if "emulationstation" in r and "--home-es-window" not in r), 2)

    def test_swap_and_back_assignment_moves(self):
        final, cmds, log = self.run_swap(assign_moves=True)
        self.check_swap(final, log)

    def test_control_the_old_092_never_swaps(self):
        """The same run with the pre-SW1 script: ES never leaves the add-on,
        so check_swap must fail - the simulation can tell the difference."""
        final, cmds, log = self.run_swap(assign_moves=False, script=read(OLD_092))
        self.assertEqual(self.at(final, 5)["es"], "DP-1")
        with self.assertRaises(AssertionError):
            self.check_swap(final, log)


@unittest.skipUnless(DASH, "dash needed")
class TestLoopSwappedDocking(LoopSim):
    def test_swapped_from_boot_then_undock_and_redock(self):
        st = world(docked=True, assign_moves=True)
        st["timeline"] = {"5": [["undock", None]], "8": [["dock", None]]}
        st["stop_at"] = 12
        final, cmds, log = self.simulate(st, read(NEW_092), config_obj=swapped_cfg())
        self.assertEqual(self.at(final, 4)["es"], "DSI-1")
        self.assertEqual(self.at(final, 7)["es"], "DSI-1")      # undocked
        self.assertEqual(self.at(final, 12)["es"], "DSI-1")     # redocked, still swapped
        self.assertEqual(self.at(final, 12)["visible"]["DSI-1"], "1")
        self.assertNotEqual(self.at(final, 12)["visible"]["DP-1"], "1")


@unittest.skipUnless(DASH, "dash needed")
class TestLoopCommandCenterWindows(LoopSim):
    """rp5deck-yt and the emulators' second windows follow the Command
    Center: to the add-on when swapped (moved at the swap, and at once when
    a new one maps, via the exec rule), back when unswapped."""

    def test_windows_follow_the_swap(self):
        yt = {"app_id": "rp5deck-yt", "title": "mpv"}
        st = world(docked=True, extra_windows=[yt])
        st["timeline"] = {"3": [["config", swapped_cfg()]],
                          "7": [["map", {"app_id": "melonDS", "title": "[w2] melonDS"}]],
                          "9": [["config", unswapped_cfg()]]}
        st["stop_at"] = 12
        final, cmds, log = self.simulate(st, read(NEW_092), config_obj=None)
        self.assertEqual(self.at(final, 2)["windows"]["rp5deck-yt"], "DSI-1")
        self.assertEqual(self.at(final, 5)["windows"]["rp5deck-yt"], "DP-1")
        self.assertEqual(self.at(final, 7)["windows"]["melonDS"], "DP-1",
                         "second window must land on the Command Center's (add-on) panel")
        self.assertTrue(any("--place-cc-windows" in e for e in final.get("exec_log", [])))
        h11 = self.at(final, 11)
        self.assertEqual(h11["windows"]["rp5deck-yt"], "DSI-1")
        self.assertEqual(h11["windows"]["melonDS"], "DSI-1")

    def test_helper_does_nothing_unless_docked_and_swapped(self):
        for docked, cfg, want_moves in ((True, unswapped_cfg(), 0), (False, swapped_cfg(), 0),
                                        (True, swapped_cfg(), 2)):
            with self.subTest(docked=docked, cfg=cfg["screens"]["es_screen"]):
                t = self.mk()
                st = world(docked=docked)
                st["config_path"] = to_sh_path(os.path.join(t, "config.json"))
                st["disable_path"] = to_sh_path(os.path.join(t, "disable"))
                with open(os.path.join(t, "state.json"), "w") as f:
                    json.dump(st, f)
                with open(os.path.join(t, "config.json"), "w") as f:
                    json.dump(cfg, f)
                b = os.path.join(t, "bin")
                os.makedirs(b)
                with open(os.path.join(b, "swaymsg"), "w", newline="\n") as f:
                    f.write('#!/bin/sh\nexec python3 "$SIM_PY" "$@"\n')
                script = os.path.join(t, "092")
                with open(script, "w", encoding="utf-8", newline="\n") as f:
                    f.write(prepare_script(read(NEW_092), t, True))
                T = to_sh_path(t)
                r = run_dash("export PATH=%s/bin:$PATH SIM_PY=%s SIM_STATE=%s/state.json "
                             "SIM_LOG=%s/cmds.log; chmod +x %s/bin/*; "
                             "dash %s/092 --place-cc-windows; echo rc=$?; ls %s"
                             % (T, sh_quote(to_sh_path(SIM)), T, T, T, T, T))
                self.assertIn("rc=0", r.stdout, r.stderr)
                self.assertNotIn("092.pid", r.stdout, "the helper took the daemon's lock")
                self.assertNotIn("092.log", r.stdout, "the helper started logging as the daemon")
                cmds = read(os.path.join(t, "cmds.log")) if os.path.exists(
                    os.path.join(t, "cmds.log")) else ""
                self.assertEqual(cmds.count("move container to output"), want_moves, cmds)


if __name__ == "__main__":
    unittest.main()
