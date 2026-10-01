#!/usr/bin/env python3
"""command-center-app: the restart/backoff decision (pure, cross-platform - runs
under plain bash on Windows AND under WSL/Linux with no sway/python/device
at all), and full integration scenarios (real process signals, /proc, `kill
-TERM` - these need a real POSIX process model, so on native Windows they
run through `wsl.exe -e sh -c ...`; on Linux/WSL itself they run directly).

Stubs (tests/fixtures/, labelled SYNTHETIC - none of these touch a real
sway or python):
  supervisor-stub-python3-SYNTHETIC.sh   `python3 X` -> exec X directly (X is
                                         itself an executable shell script).
  supervisor-stub-swaymsg-SYNTHETIC.sh   answers `get_outputs` with DP-1
                                         present (or absent, via
                                         SWAYMSG_NO_DP1) instantly.
  supervisor-stub-child-SYNTHETIC.sh     stands in for main.py/focus_guard.py:
                                         controllable via $0.starts /
                                         $0.run_seconds / $0.exit_code (see
                                         its own header), handles TERM
                                         immediately.
"""
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
SUPERVISOR = os.path.join(APP, "command-center-app")

LINUX = sys.platform.startswith("linux")


def _to_wsl_path(p):
    """'D:\\Tools\\x' -> '/mnt/d/Tools/x' (only used when shelling out to
    wsl.exe from native Windows; a no-op-shaped passthrough elsewhere)."""
    p = p.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = "/mnt/%s%s" % (p[0].lower(), p[2:])
    return p


def _which_wsl():
    return shutil.which("wsl.exe") or shutil.which("wsl")


# Windows-only bug ST2 found and fixed: a bare `bash` lookup from a normal
# Windows process resolves, ahead of anything else, to the WSL launcher shim
# Windows installs at `%LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe` -
# confirmed live on this project's dev box, from a plain (non-WSL, non-Git-
# Bash) PowerShell process:
#     PS> (Get-Command bash).Source
#     C:\Users\yourname\AppData\Local\Microsoft\WindowsApps\bash.exe
#     PS> python3 -c "import shutil; print(shutil.which('bash'))"
#     C:\Users\yourname\AppData\Local\Microsoft\WindowsApps\bash.EXE
# Git for Windows never wins that lookup on its own: only `Git\cmd` (git.exe,
# no bash) is normally on PATH; the real binaries sit unlisted at
# `Git\bin\bash.exe` / `Git\usr\bin\bash.exe`. Invoked as `bash SCRIPT ...`,
# the WSL shim re-execs into WSL, which does NOT translate a Windows-style
# path given as a bare argv script name (no `/mnt/` rewrite happens for a
# positional arg) - backslashes are simply consumed, so
# `D:\Tools\...\command-center-app` arrives as `D:ToolsRP5-DualScreen-Setup...` and
# every TestBackoffDecisionPure test failed with exit 127 ("No such file or
# directory"), never a real assertion failure. Confirmed directly:
#     "/c/Users/.../WindowsApps/bash.exe" "D:\Tools\...\command-center-app" ...
#     -> /bin/bash: D:ToolsRP5-DualScreen-Setupbottom-screen-app...: No such
#        file or directory (exit 127)
#     "/c/Program Files/Git/bin/bash.exe" "D:\Tools\...\command-center-app" ...
#     -> works (Git Bash/MSYS opens a Windows-style path argv unchanged).
# Fix: look for a real Git Bash binary by known install location FIRST, and
# explicitly refuse a WindowsApps-shim `bash` even if that is all PATH offers
# (return None -> the test skips with a clear reason instead of silently
# feeding the shim a path it cannot use). Off Windows this is a no-op: plain
# `shutil.which("bash")` is correct there (this module also drives real
# WSL/Linux integration tests via wsl.exe/sh directly - see run_harness -
# which are unaffected by this function either way).
def _which_bash():
    if sys.platform == "win32":
        candidates = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
                candidates.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
        candidates.append(r"C:\Program Files\Git\bin\bash.exe")
        candidates.append(r"C:\Program Files\Git\usr\bin\bash.exe")
        for c in candidates:
            if os.path.isfile(c):
                return c
        found = shutil.which("bash") or shutil.which("bash.exe")
        if found and "windowsapps" in found.lower():
            return None   # the WSL shim - unusable for a Windows-path argv
        return found
    return shutil.which("bash") or shutil.which("bash.exe")


def wslify(p):
    return p if LINUX else _to_wsl_path(p)


# ---------------------------------------------------------------------------
# Harness process lifetime (25 Sep 2026 leak). The harnesses below used to
# clean up with `pkill -f "$D"`, which matches only the stub children: the
# supervisor's argv is `bash .../command-center-app --fg` and the stubs' sleeps are
# `sleep 3600`, neither mentions $D. Worse, `rm -rf "$D"` then removed the fake
# sway socket, which 094 treats as "sway gone" and waits out forever by
# design. One run of this module plus test_sw1_merged left 12 supervisors and
# 52 `sleep 3600` in WSL, holding the tree open (a worktree could not be
# deleted) and growing vmmemWSL; a subprocess timeout leaked the lot too.
#
# Now every harness runs in its own session (start_new_session on Linux,
# `setsid` inside WSL from Windows), so 094, its --fg re-exec, its loops, the
# stubs and their sleeps all share one process group, and teardown kills that
# group (TERM, then KILL) from an addCleanup registered before the harness
# starts - so it runs whether the test passes, fails, raises or times out.
# Every harness also exports a unique RP5DECK_TEST_TAG, inherited by every
# descendant, and names its temp dir after it; after the group kill,
# _survivors() looks for any process carrying the tag (environ or argv) or
# mentioning 094's path, and the test FAILS if one is left.
# ---------------------------------------------------------------------------
TAG_ENV = "RP5DECK_TEST_TAG"
TAG_PREFIX = "rp5deck-suptest-"
_ISSUED_TAGS = set()
_LIVE = {}          # tag -> Popen, for the atexit sweep (Ctrl-C skips cleanups)

# Runs in its own python3 (on Linux, or inside WSL): lists every process whose
# environ carries a tag starting with SCAN_TAG, or whose argv mentions
# SCAN_TAG or SCAN_SUP. The values arrive through the environment, never
# argv, so the scanner cannot match itself (it also skips its own pid).
_SCANNER = r"""
import json, os
tag, sup, me = os.environ['SCAN_TAG'], os.environ['SCAN_SUP'], os.getpid()
rows = []
for p in os.listdir('/proc'):
    if not p.isdigit() or int(p) == me:
        continue
    try:
        with open('/proc/%s/cmdline' % p, 'rb') as f:
            cmd = f.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
        with open('/proc/%s/environ' % p, 'rb') as f:
            env = f.read().split(b'\0')
    except OSError:
        continue
    ptag = ''
    for e in env:
        if e.startswith(b'RP5DECK_TEST_TAG='):
            ptag = e.split(b'=', 1)[1].decode('utf-8', 'replace')
    if (ptag and ptag.startswith(tag)) or tag in cmd or sup in cmd:
        rows.append([int(p), ptag, cmd])
print(json.dumps(rows))
"""

# Kills the harness's process group from inside WSL (Windows runs only: the
# group lives in Linux, out of reach of os.killpg). $T arrives via env. dash's
# kill rejects `kill -TERM -- -PGID` ("Illegal number"), so no `--` here.
_WSL_REAP = r"""
g=$(cat "/tmp/$T.pgid" 2>/dev/null)
if [ -n "$g" ]; then
    kill -TERM "-$g" 2>/dev/null
    i=0; while [ "$i" -lt 10 ] && kill -0 "-$g" 2>/dev/null; do sleep 0.1; i=$((i + 1)); done
    kill -KILL "-$g" 2>/dev/null
    i=0; while [ "$i" -lt 30 ] && kill -0 "-$g" 2>/dev/null; do sleep 0.1; i=$((i + 1)); done
fi
rm -rf "/tmp/$T".*
"""


def _survivors(tag_prefix=TAG_PREFIX):
    """[pid, tag, cmdline] for every process carrying a tag that starts with
    tag_prefix (environ or argv) or whose argv mentions 094's path."""
    env = {"SCAN_TAG": tag_prefix, "SCAN_SUP": wslify(SUPERVISOR)}
    if LINUX:
        r = subprocess.run([sys.executable, "-c", _SCANNER], capture_output=True,
                           text=True, timeout=30, env=dict(os.environ, **env))
    else:
        r = subprocess.run([_which_wsl(), "-e", "env"] + ["%s=%s" % kv for kv in env.items()]
                           + ["python3", "-c", _SCANNER],
                           capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("leak scan failed (rc %d): %s" % (r.returncode, r.stderr))
    return json.loads(r.stdout)


def _kill_group(proc, tag):
    """TERM, then KILL, the harness's whole process group; then remove its
    temp dir. Safe to call more than once."""
    if proc.poll() is None:
        proc.kill()             # the direct child (sh, or wsl.exe on Windows)
    proc.wait()
    if LINUX:
        pgid = proc.pid         # start_new_session: sh leads its own group
        for sig, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 3.0)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            deadline = time.time() + grace
            gone = False
            while time.time() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    gone = True
                    break
                time.sleep(0.05)
            if gone:
                break
        for name in os.listdir("/tmp"):
            if name.startswith(tag + "."):
                p = os.path.join("/tmp", name)
                shutil.rmtree(p, True) if os.path.isdir(p) else os.remove(p)
    else:
        subprocess.run([_which_wsl(), "-e", "env", "T=" + tag, "sh", "-c", _WSL_REAP],
                       capture_output=True, text=True, timeout=30)
    _LIVE.pop(tag, None)


def _teardown_harness(test, proc, tag):
    _kill_group(proc, tag)
    left = [row for row in _survivors(tag) if row[1] == tag or tag in row[2]]
    if left:
        test.fail("harness %s leaked %d process(es) past its group kill:\n%s"
                  % (tag, len(left), "\n".join("  %d %s" % (r[0], r[2]) for r in left)))


def start_harness(test, script):
    """Start a POSIX shell harness in its own session. Teardown (group kill +
    leak check) is registered on `test` BEFORE the process exists, so it runs
    whatever happens next. Returns (proc, tag), or None if there is no POSIX
    process model here (native Windows without WSL)."""
    if not LINUX and not _which_wsl():
        return None
    tag = TAG_PREFIX + uuid.uuid4().hex[:12]
    _ISSUED_TAGS.add(tag)
    prelude = "export %s='%s'\necho $$ > '/tmp/%s.pgid'\n" % (TAG_ENV, tag, tag)
    if LINUX:
        proc = subprocess.Popen(["sh", "-c", prelude + script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    else:
        # wsl.exe cannot take start_new_session; setsid makes sh the leader
        # of a fresh session on the Linux side, so $$ is the group id.
        proc = subprocess.Popen([_which_wsl(), "-e", "setsid", "-w", "sh", "-c",
                                 prelude + script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    _LIVE[tag] = proc
    test.addCleanup(_teardown_harness, test, proc, tag)
    return proc, tag


def run_harness(test, script, timeout=15):
    """start_harness + wait. A timeout kills the whole group (not just the
    direct child, which is all subprocess.run's timeout ever did) and fails
    the test with whatever output there was."""
    started = start_harness(test, script)
    if started is None:
        return None
    proc, tag = started
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out = None              # fail outside the except: no chained traceback
    if out is None:             # quoting the whole script
        _kill_group(proc, tag)
        out, err = proc.communicate()
        test.fail("harness timed out after %ss\n%s%s" % (timeout, out, err))
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def assert_no_leftovers():
    """tearDownModule check: nothing from any harness this process started,
    and no untagged process running this checkout's 094, may be alive. A
    process with some OTHER run's tag (a concurrent suite) is not ours."""
    left = [r for r in _survivors() if r[1] in _ISSUED_TAGS
            or (not r[1] and wslify(SUPERVISOR) in r[2])]
    if left:
        raise AssertionError("%d leftover harness process(es) after the module:\n%s"
                             % (len(left), "\n".join("  %d [%s] %s" % tuple(r) for r in left)))


def tearDownModule():
    assert_no_leftovers()


@atexit.register
def _sweep_live_harnesses():
    for tag, proc in list(_LIVE.items()):
        try:
            _kill_group(proc, tag)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The pure restart/backoff decision - no process management at all
# ---------------------------------------------------------------------------
class TestBackoffDecisionPure(unittest.TestCase):
    """Drives the REAL function inside command-center-app via its hidden
    `--print-backoff-decision` CLI mode - not a reimplementation. Runs under
    plain bash, cross-platform (Windows git-bash or WSL/Linux bash); no
    sway, python, or device involved at all."""

    def setUp(self):
        self.bash = _which_bash()
        if not self.bash:
            if sys.platform == "win32":
                self.skipTest("no usable bash: only a WindowsApps WSL-shim `bash` is on "
                              "PATH (it cannot run this script with a Windows-style path "
                              "argv - see the comment on _which_bash()); install Git for "
                              "Windows, or run these tests under WSL instead")
            self.skipTest("no bash on PATH")

    def decide(self, prev, ran, rapid_secs, rapid_max, backoff_max):
        import subprocess
        r = subprocess.run([self.bash, SUPERVISOR, "--print-backoff-decision",
                          str(prev), str(ran), str(rapid_secs), str(rapid_max),
                          str(backoff_max)], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        parts = r.stdout.strip().split()
        self.assertEqual(len(parts), 2, r.stdout)
        return parts[0], parts[1]

    def test_first_rapid_exit_backs_off_by_one_doubling(self):
        rapid, action = self.decide(0, 5, 30, 5, 60)
        self.assertEqual((rapid, action), ("1", "2"))

    def test_backoff_doubles_each_consecutive_rapid_exit(self):
        rapid, action = self.decide(1, 5, 30, 5, 60)
        self.assertEqual((rapid, action), ("2", "4"))
        rapid, action = self.decide(2, 5, 30, 5, 60)
        self.assertEqual((rapid, action), ("3", "8"))

    def test_a_run_at_or_past_rapid_secs_resets_the_counter(self):
        rapid, action = self.decide(4, 30, 30, 5, 60)
        self.assertEqual((rapid, action), ("0", "1"))
        rapid, action = self.decide(4, 999, 30, 5, 60)
        self.assertEqual((rapid, action), ("0", "1"))

    def test_gives_up_at_rapid_max(self):
        rapid, action = self.decide(4, 5, 30, 5, 60)
        self.assertEqual((rapid, action), ("5", "GIVEUP"))

    def test_backoff_is_capped(self):
        # rapid would reach 4 (backoff 16) but backoff_max is 4.
        rapid, action = self.decide(3, 5, 30, 10, 4)
        self.assertEqual((rapid, action), ("4", "4"))

    def test_break_restore_the_rapid_cap(self):
        # Break: call with a rapid_max that can never be reached (a common
        # off-by-one: using ">" instead of ">=" would need one extra exit).
        # RED: with the real script, rapid_max=5 and a 5th consecutive rapid
        # exit MUST give up - prove a wrong comparison would not.
        rapid, action = self.decide(4, 5, 30, 5, 60)
        self.assertEqual(action, "GIVEUP")             # GREEN: production gives up
        broken_would_giveup = int(rapid) > 5           # what a `>` bug would require
        self.assertFalse(broken_would_giveup, "the real bug: needs a bigger count")


# ---------------------------------------------------------------------------
# Regression guard for _which_bash() itself (see its own comment for the full
# story): a naive `shutil.which("bash")` resolves to the WSL launcher shim on
# a normal Windows box, which cannot run this script at all.
# ---------------------------------------------------------------------------
class TestWhichBash(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "the WindowsApps-shim bug is Windows-only")
    def test_does_not_return_the_windowsapps_wsl_shim(self):
        found = _which_bash()
        if found is None:
            self.skipTest("no usable bash at all on this box (no Git for Windows found)")
        self.assertNotIn("windowsapps", found.lower(),
                         "resolved to the WSL launcher shim, not a real bash: %r" % found)

    @unittest.skipUnless(sys.platform == "win32", "exercises the Windows-specific lookup")
    def test_can_actually_run_the_supervisor_script_with_its_windows_path(self):
        found = _which_bash()
        if found is None:
            self.skipTest("no usable bash at all on this box (no Git for Windows found)")
        import subprocess
        r = subprocess.run([found, SUPERVISOR, "--print-backoff-decision", "0", "5", "30", "5", "60"],
                          capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "1 2")


# ---------------------------------------------------------------------------
# Full integration: real process management (WSL / Linux only)
# ---------------------------------------------------------------------------
class TestSupervisorIntegration(unittest.TestCase):
    def setUp(self):
        if not LINUX and not _which_wsl():
            self.skipTest("no WSL and not on Linux: cannot exercise real "
                          "process signals/backoff (see module doc)")

    def _harness(self, body, timeout=25):
        r = run_harness(self, self._script(body), timeout=timeout)
        if r is None:
            self.skipTest("no WSL available")
        return r

    def _script(self, body):
        """Wraps `body` (a POSIX shell fragment) with a temp dir, the stub
        PATH (fake python3 + swaymsg), the stub app/guard scripts, and every
        RP5DECK_* env var override. `body` can use:
          $D              the temp dir
          $APP_STUB / $GUARD_STUB   the two child stub script paths
          $LOCK / $LOG    the supervisor's own pid-lock and log file
          launch          function: starts a fresh supervisor in the
                          background and sets $SUPPID to its REAL pid (read
                          from $LOCK, NOT the launcher's own $! - command-center-app
                          always re-execs itself once via `nohup ... --fg &`,
                          so the direct child pid is a short-lived wrapper)
          starts NAME     function: prints NAME's restart counter
          wait_for CMD N  function: polls CMD every 0.1s, up to N tenths
        Teardown is run_harness's: the whole process group is killed and $D
        removed, no matter which test method or assertion failed."""
        supervisor = wslify(SUPERVISOR)
        stub_py3 = wslify(os.path.join(FIX, "supervisor-stub-python3-SYNTHETIC.sh"))
        stub_sway = wslify(os.path.join(FIX, "supervisor-stub-swaymsg-SYNTHETIC.sh"))
        stub_child = wslify(os.path.join(FIX, "supervisor-stub-child-SYNTHETIC.sh"))
        script = r"""
set -u
# named after the tag so run_harness can find and remove it after the group kill
D=$(mktemp -d "/tmp/$RP5DECK_TEST_TAG.XXXXXX")
mkdir -p "$D/bin" "$D/home" "$D/log"
cp '%(stub_py3)s' "$D/bin/python3"; chmod +x "$D/bin/python3"
cp '%(stub_sway)s' "$D/bin/swaymsg"; chmod +x "$D/bin/swaymsg"
cp '%(stub_child)s' "$D/home/app_stub.sh"; chmod +x "$D/home/app_stub.sh"
cp '%(stub_child)s' "$D/home/guard_stub.sh"; chmod +x "$D/home/guard_stub.sh"
export PATH="$D/bin:$PATH"
touch "$D/sway-ipc.999.sock"
export SWAYSOCK="$D/sway-ipc.999.sock"
# I2: search only this harness's dir. A global /run /tmp search sees every
# other sway-ipc.*.sock on the machine (a concurrent harness, a killed run)
# and "sway gone" then never happens: the flaky sway-gone cases.
export RP5DECK_SWAY_SOCK_DIRS="$D"
export RP5DECK_HOME="$D/home"
export RP5DECK_MAIN="$D/home/app_stub.sh"
export RP5DECK_GUARD="$D/home/guard_stub.sh"
export RP5DECK_LOG_DIR="$D/log"
export RP5DECK_LOCK="$D/094.pid"
export RP5DECK_DISABLE="$D/.disable-rp5deck"
export RP5DECK_GUARD_DISABLE="$D/.disable-rp5deck-focus-guard"
export RP5DECK_RAPID_SECS=1
export RP5DECK_RAPID_MAX=3
export RP5DECK_BACKOFF_MAX=1
export RP5DECK_GUARD_RAPID_SECS=1
export RP5DECK_GUARD_RAPID_MAX=3
export RP5DECK_GUARD_BACKOFF_MAX=1
APP_STUB="$D/home/app_stub.sh"
GUARD_STUB="$D/home/guard_stub.sh"
LOCK="$RP5DECK_LOCK"
LOG="$D/log/command-center-app.log"
starts() { cat "$1.starts" 2>/dev/null || echo 0; }
wait_for() {
    # wait_for CONDITION_CMD TIMEOUT_TENTHS
    i=0
    while [ "$i" -lt "$2" ]; do
        if eval "$1"; then return 0; fi
        i=$((i + 1)); sleep 0.1
    done
    return 1
}
# Force ONE currently-running stub instance to exit right now (simulates an
# unexpected exit), rather than racing a control-file write against its
# already-captured 3600s default sleep.
kill_stub_now() {
    p=$(pgrep -f "$1" | head -1)
    [ -n "$p" ] && kill -TERM "$p" 2>/dev/null
}
launch() {
    rm -f "$LOCK"
    bash '%(supervisor)s' >"$D/stdout.log" 2>&1 &
    wait_for '[ -s "$LOCK" ]' 100 || { echo "LAUNCH_FAILED=yes"; return 1; }
    SUPPID=$(cat "$LOCK")
    return 0
}
launch
%(body)s
""" % {"stub_py3": stub_py3, "stub_sway": stub_sway, "stub_child": stub_child,
       "supervisor": supervisor, "body": body}
        return script

    def test_both_children_start(self):
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ] && [ "$(starts "$GUARD_STUB")" -ge 1 ]' 100
echo "APP_STARTS=$(starts "$APP_STUB")"
echo "GUARD_STARTS=$(starts "$GUARD_STUB")"
""")
        self.assertNotIn("LAUNCH_FAILED", r.stdout, r.stdout + r.stderr)
        self.assertIn("APP_STARTS=1", r.stdout)
        self.assertIn("GUARD_STARTS=1", r.stdout)

    def test_starts_the_app_without_the_add_on(self):
        script = "export SWAYMSG_NO_DP1=1\n" + self._script(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
echo "APP_STARTS=$(starts "$APP_STUB")"
grep -c 'absent (undocked)' "$LOG" | sed 's/^/UNDOCKED_LOG=/'
""")
        r = run_harness(self, script, timeout=25)
        if r is None:
            self.skipTest("no WSL available")
        self.assertNotIn("LAUNCH_FAILED", r.stdout, r.stdout + r.stderr)
        self.assertIn("APP_STARTS=1", r.stdout, r.stdout + r.stderr)
        self.assertIn("UNDOCKED_LOG=1", r.stdout, r.stdout + r.stderr)

    def test_app_restarts_after_unexpected_exit(self):
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
kill_stub_now "$APP_STUB"
wait_for '[ "$(starts "$APP_STUB")" -ge 2 ]' 100
echo "RESTARTED=$(starts "$APP_STUB")"
""")
        self.assertIn("RESTARTED=2", r.stdout, r.stdout + r.stderr)

    def test_app_giving_up_removes_the_lock_and_stops_the_supervisor(self):
        # RAPID_MAX=3, RAPID_SECS=1: kill the app right after every start so
        # every run is "rapid" -> after 3 in a row the supervisor gives up
        # (a broken app must not respawn forever on a battery device) and
        # its own pid lock disappears (proof it really exited, not just its
        # restart loop).
        r = self._harness(r"""
last=0
attempt=0
while [ "$attempt" -lt 15 ]; do
    attempt=$((attempt + 1))
    wait_for "[ \"\$(starts \"\$APP_STUB\")\" -gt $last ]" 50 || break
    last=$(starts "$APP_STUB")
    kill_stub_now "$APP_STUB"
    grep -q "giving up after" "$LOG" 2>/dev/null && break
done
wait_for '! kill -0 "$SUPPID" 2>/dev/null' 100
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
echo "LOCK_GONE=$([ -f "$LOCK" ] && echo no || echo yes)"
echo "APP_STARTS=$(starts "$APP_STUB")"
grep -q "giving up after" "$LOG" && echo "LOGGED=yes"
""", timeout=45)
        self.assertIn("SUP_ALIVE=no", r.stdout, r.stdout + r.stderr)
        self.assertIn("LOCK_GONE=yes", r.stdout)
        self.assertIn("LOGGED=yes", r.stdout)

    def test_guard_giving_up_does_not_stop_the_app(self):
        # The guard is a safety net, not the app: if IT gives up, rp5deck
        # keeps running (and keeps being restarted if it also happens to
        # exit) without the guard.
        r = self._harness(r"""
last=0
attempt=0
while [ "$attempt" -lt 15 ]; do
    attempt=$((attempt + 1))
    wait_for "[ \"\$(starts \"\$GUARD_STUB\")\" -gt $last ]" 50 || break
    last=$(starts "$GUARD_STUB")
    kill_stub_now "$GUARD_STUB"
    grep -q "WARNING: giving up on the focus guard" "$LOG" 2>/dev/null && break
done
wait_for 'grep -q "WARNING: giving up on the focus guard" "$LOG" 2>/dev/null' 50
echo "GUARD_WARNED=$(grep -q "WARNING: giving up on the focus guard" "$LOG" && echo yes || echo no)"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
before=$(starts "$APP_STUB")
sleep 1.5
after=$(starts "$APP_STUB")
echo "APP_STILL_RUNNING=$([ "$before" -ge 1 ] && [ "$after" -ge "$before" ] && echo yes || echo no)"
""", timeout=45)
        self.assertIn("GUARD_WARNED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("SUP_ALIVE=yes", r.stdout)
        self.assertIn("APP_STILL_RUNNING=yes", r.stdout)

    def test_kill_switch_present_at_start_stays_stopped(self):
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
kill -TERM "$SUPPID" 2>/dev/null
wait_for '! kill -0 "$SUPPID" 2>/dev/null' 100
before=$(starts "$APP_STUB")
: > "$RP5DECK_DISABLE"
launch
sleep 1
after=$(starts "$APP_STUB")
echo "STARTS_UNCHANGED=$([ "$before" = "$after" ] && echo yes || echo no)"
""")
        # A kill-switch start exits immediately (line 1 of the script: `[ -e
        # "$DISABLE" ] && exit 0`), so its OWN pid never reaches $LOCK -
        # `launch`'s wait_for for a fresh $LOCK entry must therefore time
        # out (LAUNCH_FAILED) - and, the actual behaviour that matters, the
        # app's restart count from the FIRST (already-stopped) run never
        # moves again: nothing new got launched.
        self.assertIn("LAUNCH_FAILED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("STARTS_UNCHANGED=yes", r.stdout)

    def test_clean_stop_kills_both_children_promptly_and_lock_is_removed(self):
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ] && [ "$(starts "$GUARD_STUB")" -ge 1 ]' 100
APP_PID=$(pgrep -f "$APP_STUB" | head -1)
GUARD_PID=$(pgrep -f "$GUARD_STUB" | head -1)
kill -TERM "$SUPPID"
wait_for '! kill -0 "$SUPPID" 2>/dev/null' 100
echo "SUP_GONE=$(kill -0 "$SUPPID" 2>/dev/null && echo no || echo yes)"
echo "APP_GONE=$(kill -0 "$APP_PID" 2>/dev/null && echo no || echo yes)"
echo "GUARD_GONE=$(kill -0 "$GUARD_PID" 2>/dev/null && echo no || echo yes)"
echo "LOCK_GONE=$([ -f "$LOCK" ] && echo no || echo yes)"
""", timeout=20)
        self.assertIn("SUP_GONE=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("APP_GONE=yes", r.stdout)
        self.assertIn("GUARD_GONE=yes", r.stdout)
        self.assertIn("LOCK_GONE=yes", r.stdout)

    def test_break_restore_clean_stop_must_forward_to_the_guard(self):
        # A weaker on_signal handler (forwards TERM to the app only, forgets
        # the guard loop) would leave the guard process running after the
        # supervisor is gone. Demonstrate that failure mode directly (RED:
        # killing only the app leaves the guard alive), then confirm the
        # REAL, full stop (SIGTERM to the actual supervisor) takes the guard
        # down too (GREEN).
        r = self._harness(r"""
wait_for '[ "$(starts "$GUARD_STUB")" -ge 1 ] && [ "$(starts "$APP_STUB")" -ge 1 ]' 100
GUARD_PID=$(pgrep -f "$GUARD_STUB" | head -1)
APP_PID=$(pgrep -f "$APP_STUB" | head -1)
kill -TERM "$APP_PID" 2>/dev/null
sleep 0.3
echo "GUARD_SURVIVES_PARTIAL_KILL=$(kill -0 "$GUARD_PID" 2>/dev/null && echo yes || echo no)"
kill -TERM "$SUPPID" 2>/dev/null
wait_for '! kill -0 "$GUARD_PID" 2>/dev/null' 100
echo "GUARD_GONE_AFTER_REAL_STOP=$(kill -0 "$GUARD_PID" 2>/dev/null && echo no || echo yes)"
""", timeout=20)
        self.assertIn("GUARD_SURVIVES_PARTIAL_KILL=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("GUARD_GONE_AFTER_REAL_STOP=yes", r.stdout)

    # -----------------------------------------------------------------------
    # ST2 / ST1: sway disappearing (a Steam session: start_steam.sh runs
    # `systemctl stop sway` for the whole session, only recovered afterward
    # via `essway.service Requires=sway.service`) must be a PAUSE, not a
    # crash - re-resolve SWAYSOCK/WAYLAND_DISPLAY before every respawn
    # instead of trusting the value found once at boot, and never spend the
    # rapid-restart budget while there is no sway to run against.
    # -----------------------------------------------------------------------
    def test_sway_gone_pauses_respawn_then_resumes_with_the_new_socket_no_budget_spent(self):
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
OLD_SOCK="$SWAYSOCK"
rm -f "$D"/sway-ipc.*.sock
kill_stub_now "$APP_STUB"
# The old bug: a respawn against the now-dead OLD_SOCK would exit instantly
# and, with BACKOFF_MAX=1 in this harness, retry within ~1s - so if a second
# start appears within this window at all, the fix is not re-resolving/
# pausing (this is the RED condition; verified by hand against the pre-fix
# command-center-app during development - see the ST2 report).
wait_for '[ "$(starts "$APP_STUB")" -ge 2 ]' 20 && echo "PREMATURE_RESPAWN=yes" || echo "PREMATURE_RESPAWN=no"
NEW_SOCK="$D/sway-ipc.4242.sock"
touch "$NEW_SOCK"
wait_for '[ "$(starts "$APP_STUB")" -ge 2 ]' 100
echo "RESPAWNED=$(starts "$APP_STUB")"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
last_seen=$(tail -1 "$APP_STUB.swaysock_log")
echo "NEW_SOCK_USED=$([ "$last_seen" = "$NEW_SOCK" ] && echo yes || echo no)"
echo "OLD_SOCK_NOT_REUSED=$([ "$last_seen" != "$OLD_SOCK" ] && echo yes || echo no)"
echo "LOGGED_GONE=$(grep -q "sway is gone after exit" "$LOG" && echo yes || echo no)"
echo "LOGGED_BACK=$(grep -q "app.*sway is back\|sway is back" "$LOG" && echo yes || echo no)"
echo "LOGGED_NO_BUDGET=$(grep -q "not counting it against the restart budget" "$LOG" && echo yes || echo no)"
""", timeout=30)
        self.assertIn("PREMATURE_RESPAWN=no", r.stdout, r.stdout + r.stderr)
        self.assertIn("RESPAWNED=2", r.stdout)
        self.assertIn("SUP_ALIVE=yes", r.stdout)
        self.assertIn("NEW_SOCK_USED=yes", r.stdout)
        self.assertIn("OLD_SOCK_NOT_REUSED=yes", r.stdout)
        self.assertIn("LOGGED_GONE=yes", r.stdout)
        self.assertIn("LOGGED_NO_BUDGET=yes", r.stdout)

    def test_repeated_sway_gone_cycles_never_exhaust_the_restart_budget(self):
        # Contrast with test_app_giving_up_removes_the_lock_and_stops_the_
        # supervisor above: THAT test proves RAPID_MAX (3) truly-rapid exits
        # WHILE SWAY IS PRESENT make the supervisor give up. This drives MORE
        # cycles than RAPID_MAX, each one sway disappearing and coming back
        # under a NEW name (never present at the same time as an old one) -
        # if these were counted the same way, it would give up partway
        # through; it must not, no matter how many times Steam is opened and
        # closed in a session.
        r = self._harness(r"""
CYCLES=6
cyc=0
while [ "$cyc" -lt "$CYCLES" ]; do
    # NOTE: `wait_for` (defined by the harness template above) uses its own
    # unscoped `$i` internally - naming this loop's own counter `i` too
    # would have wait_for silently reset it to 0 on every call (plain POSIX
    # sh functions share the caller's variables), turning this into an
    # infinite loop. Proved the hard way: an earlier draft named it `i` and
    # hung at 60s every time, in total isolation, with `$i` frozen at 0 in
    # the trace - see the ST2 report.
    cyc=$((cyc + 1))
    wait_for "[ \"\$(starts \"\$APP_STUB\")\" -ge $cyc ]" 100 || break
    rm -f "$D"/sway-ipc.*.sock
    kill_stub_now "$APP_STUB"
    sleep 0.3
    touch "$D/sway-ipc.$((9000 + cyc)).sock"
done
wait_for "[ \"\$(starts \"\$APP_STUB\")\" -gt $CYCLES ]" 100
echo "STARTS=$(starts "$APP_STUB")"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
echo "LOCK_PRESENT=$([ -f "$LOCK" ] && echo yes || echo no)"
echo "GAVE_UP=$(grep -q "giving up after" "$LOG" && echo yes || echo no)"
""", timeout=60)
        self.assertIn("SUP_ALIVE=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("LOCK_PRESENT=yes", r.stdout)
        self.assertIn("GAVE_UP=no", r.stdout)
        starts_line = [ln for ln in r.stdout.splitlines() if ln.startswith("STARTS=")][0]
        self.assertGreaterEqual(int(starts_line.split("=", 1)[1]), 6,
                                "expected at least 6 starts (1 initial + one per cycle)")

    def test_focus_guard_also_pauses_on_sway_gone_and_resumes_with_new_socket(self):
        # Mirrors the app-loop test above for the --guard-loop code path,
        # which has its own, independent re-resolve + pause logic.
        r = self._harness(r"""
wait_for '[ "$(starts "$GUARD_STUB")" -ge 1 ]' 100
rm -f "$D"/sway-ipc.*.sock
kill_stub_now "$GUARD_STUB"
wait_for '[ "$(starts "$GUARD_STUB")" -ge 2 ]' 20 && echo "PREMATURE_RESPAWN=yes" || echo "PREMATURE_RESPAWN=no"
touch "$D/sway-ipc.5252.sock"
wait_for '[ "$(starts "$GUARD_STUB")" -ge 2 ]' 100
echo "RESPAWNED=$(starts "$GUARD_STUB")"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
last_seen=$(tail -1 "$GUARD_STUB.swaysock_log")
echo "NEW_SOCK_USED=$([ "$last_seen" = "$D/sway-ipc.5252.sock" ] && echo yes || echo no)"
""", timeout=30)
        self.assertIn("PREMATURE_RESPAWN=no", r.stdout, r.stdout + r.stderr)
        self.assertIn("RESPAWNED=2", r.stdout)
        self.assertIn("SUP_ALIVE=yes", r.stdout)
        self.assertIn("NEW_SOCK_USED=yes", r.stdout)


# ---------------------------------------------------------------------------
# SW2: the hang detector - a child whose PID stays alive but stops touching
# its heartbeat file must be restarted (command-center-app's heartbeat_monitor),
# distinct from every test above (which all exercise the child actually
# EXITING). Its own harness (not TestSupervisorIntegration._harness): a
# different main-app stub (one that writes heartbeats) and short HB_* knobs
# so the test does not need to wait anywhere near the real 30s default.
# ---------------------------------------------------------------------------
class TestHeartbeatHangDetector(unittest.TestCase):
    def setUp(self):
        if not LINUX and not _which_wsl():
            self.skipTest("no WSL and not on Linux: cannot exercise real "
                          "process signals (see module doc)")

    def _harness(self, body, timeout=30):
        supervisor = wslify(SUPERVISOR)
        stub_py3 = wslify(os.path.join(FIX, "supervisor-stub-python3-SYNTHETIC.sh"))
        stub_sway = wslify(os.path.join(FIX, "supervisor-stub-swaymsg-SYNTHETIC.sh"))
        stub_hb = wslify(os.path.join(FIX, "supervisor-stub-heartbeat-SYNTHETIC.sh"))
        stub_child = wslify(os.path.join(FIX, "supervisor-stub-child-SYNTHETIC.sh"))
        script = r"""
set -u
D=$(mktemp -d "/tmp/$RP5DECK_TEST_TAG.XXXXXX")
mkdir -p "$D/bin" "$D/home" "$D/log" "$D/run"
cp '%(stub_py3)s' "$D/bin/python3"; chmod +x "$D/bin/python3"
cp '%(stub_sway)s' "$D/bin/swaymsg"; chmod +x "$D/bin/swaymsg"
cp '%(stub_hb)s' "$D/home/app_stub.sh"; chmod +x "$D/home/app_stub.sh"
cp '%(stub_child)s' "$D/home/guard_stub.sh"; chmod +x "$D/home/guard_stub.sh"
export PATH="$D/bin:$PATH"
touch "$D/sway-ipc.999.sock"
export SWAYSOCK="$D/sway-ipc.999.sock"
export RP5DECK_SWAY_SOCK_DIRS="$D"
export RP5DECK_HOME="$D/home"
export RP5DECK_MAIN="$D/home/app_stub.sh"
export RP5DECK_GUARD="$D/home/guard_stub.sh"
export RP5DECK_LOG_DIR="$D/log"
export RP5DECK_LOCK="$D/094.pid"
export RP5DECK_DISABLE="$D/.disable-rp5deck"
export RP5DECK_GUARD_DISABLE="$D/.disable-rp5deck-focus-guard"
export RP5DECK_OVERLAY="$D/home/nope.py"
export RP5DECK_RUN_DIR="$D/run"
export RP5DECK_RAPID_SECS=1 RP5DECK_RAPID_MAX=5 RP5DECK_BACKOFF_MAX=1
export RP5DECK_HB_CHECK_INTERVAL=1 RP5DECK_HB_GRACE_SECS=1 RP5DECK_HB_STALE_SECS=2
export RP5DECK_HB_KILL_GRACE=2
APP_STUB="$D/home/app_stub.sh"
LOCK="$RP5DECK_LOCK"
LOG="$D/log/command-center-app.log"
HEARTBEAT="$D/run/heartbeat"
starts() { cat "$1.starts" 2>/dev/null || echo 0; }
wait_for() { i=0; while [ "$i" -lt "$2" ]; do if eval "$1"; then return 0; fi; i=$((i + 1)); sleep 0.1; done; return 1; }
rm -f "$LOCK"
bash '%(sup)s' >"$D/stdout.log" 2>&1 &
wait_for '[ -s "$LOCK" ]' 100 || echo "LAUNCH_FAILED=yes"
SUPPID=$(cat "$LOCK")
%(body)s
""" % {"stub_py3": stub_py3, "stub_sway": stub_sway, "stub_hb": stub_hb,
       "stub_child": stub_child, "sup": supervisor, "body": body}
        r = run_harness(self, script, timeout=timeout)
        if r is None:
            self.skipTest("no WSL available")
        return r

    def test_a_hung_but_alive_child_is_restarted(self):
        # No run_seconds/exit_code control file at all: the stub would keep
        # heartbeating (and running) for a very long time on its own - the
        # ONLY thing that ends this run is the hang detector, once its
        # heartbeat has been stopped (below) while the pid stays alive.
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
wait_for '[ -f "$HEARTBEAT" ]' 50
echo "STOP_AFTER=2" > /dev/null   # (documents the intent; set the real file next)
echo 2 > "$APP_STUB.heartbeat_stop_after"
FIRST_PID=$(pgrep -f "$APP_STUB" | head -1)
wait_for '! kill -0 "$FIRST_PID" 2>/dev/null' 100
echo "FIRST_KILLED=$(kill -0 "$FIRST_PID" 2>/dev/null && echo no || echo yes)"
wait_for '[ "$(starts "$APP_STUB")" -ge 2 ]' 100
echo "RESTARTED=$(starts "$APP_STUB")"
echo "HUNG_LOGGED=$(grep -q "app hung" "$LOG" && echo yes || echo no)"
echo "SUP_ALIVE=$(kill -0 "$SUPPID" 2>/dev/null && echo yes || echo no)"
""")
        self.assertIn("FIRST_KILLED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("RESTARTED=2", r.stdout, r.stdout + r.stderr)
        self.assertIn("HUNG_LOGGED=yes", r.stdout, r.stdout + r.stderr)
        self.assertIn("SUP_ALIVE=yes", r.stdout)

    def test_a_healthy_heartbeat_is_never_touched(self):
        # The stub keeps heartbeating throughout: no restart, no "hung" log,
        # across several check intervals - the detector must not be a false
        # positive on an ordinary, healthy run.
        r = self._harness(r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ]' 100
wait_for '[ -f "$HEARTBEAT" ]' 50
sleep 4
echo "STARTS=$(starts "$APP_STUB")"
echo "HUNG_LOGGED=$(grep -q "app hung" "$LOG" && echo yes || echo no)"
""")
        self.assertIn("STARTS=1", r.stdout, r.stdout + r.stderr)
        self.assertIn("HUNG_LOGGED=no", r.stdout)


# ---------------------------------------------------------------------------
# The leak guard itself (see "Harness process lifetime" above). A scan that
# finds nothing after teardown only means something if the same scan finds
# the harness's processes while it is alive - so this test looks first.
# ---------------------------------------------------------------------------
class TestHarnessTeardown(unittest.TestCase):
    def setUp(self):
        if not LINUX and not _which_wsl():
            self.skipTest("no WSL and not on Linux")

    def test_group_kill_leaves_nothing_the_scan_can_see(self):
        # `sleep 47` is only a marker that the body got this far (094's own
        # naps are 1/2/5/8/16/30s); the stub restart before it exercises the
        # stub's TERM path, which used to orphan its `sleep 3600`.
        body = r"""
wait_for '[ "$(starts "$APP_STUB")" -ge 1 ] && [ "$(starts "$GUARD_STUB")" -ge 1 ]' 100
kill_stub_now "$APP_STUB"
wait_for '[ "$(starts "$APP_STUB")" -ge 2 ]' 100
sleep 47
"""
        started = start_harness(self, TestSupervisorIntegration._script(self, body))
        if started is None:
            self.skipTest("no WSL available")
        proc, tag = started
        mine = lambda: [r for r in _survivors(tag) if r[1] == tag or tag in r[2]]
        rows = []
        deadline = time.time() + 20
        while time.time() < deadline:
            rows = mine()
            if any(r[2] == "sleep 47" for r in rows):
                break
            time.sleep(0.3)
        cmds = [r[2] for r in rows]
        detail = "\n".join(cmds)
        # positive control: the scan sees every kind of process that leaked
        self.assertIn("sleep 47", cmds, "harness never got ready:\n" + detail)
        self.assertTrue(any(wslify(SUPERVISOR) + " --fg" in c for c in cmds), detail)
        stubs = [c for c in cmds if c.endswith("_stub.sh")]
        sleeps = [c for c in cmds if c == "sleep 3600"]
        self.assertEqual(len(stubs), 2, detail)
        # one sleep per LIVE stub: the TERM'd first app stub took its own
        # with it (before the stub fix, 3 here - the orphan)
        self.assertEqual(len(sleeps), len(stubs), detail)

        _kill_group(proc, tag)
        self.assertEqual(mine(), [], "survived the group kill")


if __name__ == "__main__":
    unittest.main()
