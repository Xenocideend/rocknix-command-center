"""system_sleep: the Command Center's Sleep tile.

`systemctl suspend` off the UI thread (main.App.open_sleep, on the io worker). rp5deck runs as
root on the device, so theres no polkit or sudo step. The call blocks its thread for as long as
the device sleeps (systemd's suspend.target job is synchronous), which is fine on a worker since
the whole system is frozen during a real suspend anyway.

dp-sleep-guard.service (Before=sleep.target, installed separately) already turns off the
add-on's DP-1 before suspend and back on at resume. This must never touch DP-1 itself, only ask
systemd to suspend and let that guard do its job, racing it caused resets before.
"""
import logging
import subprocess

log = logging.getLogger("rp5deck.system_sleep")

SUSPEND_CMD = ("systemctl", "suspend")
# Long on purpose. A normal suspend freezes this call along with everything else, so the timeout
# only counts the moments before suspend starts and after resume. It's for a hung or inhibited
# systemctl that never suspends, not a slow real one.
SUSPEND_TIMEOUT_S = 30.0


def suspend(run=None):
    """Asks systemd to suspend. Returns (ok, detail), ok False on a bad exit, a missing systemctl, a
    timeout or any OSError, and detail is a short string for an error toast. Never raises.
    """
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
