"""system_sleep - the Command Center's Sleep tile (owner-approved).

`systemctl suspend` off the UI thread (main.App.open_sleep, on the io
worker) - rp5deck runs as root on the device (094-rp5deck), so no polkit/
sudo dance is needed. This call BLOCKS the calling thread for as long as
the device is actually asleep (systemd's own `suspend.target` job is
synchronous) - expected and harmless here since it always runs on a worker
thread, never the UI thread, and the whole process (indeed the whole
kernel scheduler) is frozen alongside it during a real suspend, not spinning.

dp-sleep-guard.service (Before=sleep.target, installed separately - not
this repo) already disables the add-on screen's DP-1 output before suspend
and re-enables it on resume; this module must NEVER touch DP-1 itself,
only ask systemd to suspend and let that guard do its own job - see
rp5-dock-power-plug-loses-dp / rp5-suspend-resets-with-dp-active for the
exact class of bug that already followed from racing it once."""
import logging
import subprocess

log = logging.getLogger("rp5deck.system_sleep")

SUSPEND_CMD = ("systemctl", "suspend")
# Generous on purpose: a normal suspend freezes this call (and everything
# else) along with the rest of the OS, so the timeout only ever "counts"
# real wall-clock time in the brief windows before suspend engages and
# after resume - a hung/inhibited systemctl that never actually suspends is
# what this guards against, not a slow-but-real suspend cycle.
SUSPEND_TIMEOUT_S = 30.0


def suspend(run=None):
    """Ask systemd to suspend. Returns (ok, detail): ok False on a non-zero
    exit, a missing systemctl, a timeout, or any OSError - detail is a
    short string for an error toast. Never raises."""
    run = run or subprocess.run
    try:
        r = run(SUSPEND_CMD, capture_output=True, text=True, timeout=SUSPEND_TIMEOUT_S)
    except FileNotFoundError:
        log.warning("system_sleep: systemctl not found")
        return False, "systemctl not found"
    except subprocess.TimeoutExpired:
        log.warning("system_sleep: systemctl suspend timed out")
        return False, "timed out"
    except OSError as e:
        log.warning("system_sleep: %s", e)
        return False, str(e)
    if r.returncode != 0:
        detail = ((r.stderr or r.stdout or "").strip() or ("rc %d" % r.returncode))[:160]
        log.warning("system_sleep: systemctl suspend failed (rc %s): %s", r.returncode, detail)
        return False, detail
    return True, ""
