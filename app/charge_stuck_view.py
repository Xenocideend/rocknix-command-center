"""charge_stuck_view - the "charger connected but not charging" banner.

No sheet, no confirmation, no privileged command - charge_stuck.py only ever
reads sysfs/debugfs, so there is nothing here to confirm before running.
poll() is called every main.CHARGE_STUCK_POLL_S (a plain sysfs read, cheap
even every 10 s) and only ever updates the Home banner
(screens.Home.set_charge_notice); dismiss() is the banner's own Dismiss
button, which re-arms itself the next time the charger is unplugged (the
owner's "maybe a dismiss" - it must not need a fresh look every 10 s once
the owner has seen it and knows the fix, but it must not stay silently
dismissed forever either)."""
import logging

import charge_stuck

log = logging.getLogger("rp5deck.charge_stuck")

# Two steps (device, 25 Sep 04:51): a replug fixed it on some plug-ins, not
# after the APSD loop; restarting on battery and plugging in after boot is
# the path that has always charged (v4, 24 Sep 23:31 / 23:52). Restarting
# WITH the charger in is not offered: the top screen may then stay off.
MESSAGE = ("Not charging: replug the charger (unplug 5 s). Still not? Unplug, restart, "
          "plug in after boot.")
# Old samples are trimmed on every poll so this list never grows unbounded
# over a long play session; a couple of windows' worth is plenty for
# evaluate()'s own rolling WINDOW_S.
KEEP_S = charge_stuck.WINDOW_S * 2


class ChargeStuckController:
    """host: main.App (ui, state_dirty). submit(fn, *args, done=cb) runs fn
    (charge_stuck.Sampler.sample) on a worker and posts cb(result) to the UI
    thread - real sysfs/debugfs I/O, so this must never run on the UI
    thread, even though each read is cheap. sampler defaults to
    charge_stuck.Sampler() (tests inject a fake with canned samples)."""

    def __init__(self, host, submit, sampler=None):
        self.host = host
        self.submit = submit
        self.sampler = sampler or charge_stuck.Sampler()
        self.samples = []
        self.stuck = False
        self.dismissed = False
        self.poll_inflight = False

    def poll(self):
        if self.poll_inflight:
            return
        self.poll_inflight = True
        self.submit(self.sampler.sample, done=self._sampled)

    def _sampled(self, sample):
        self.poll_inflight = False
        self.samples.append(sample)
        cutoff = sample["t"] - KEEP_S
        self.samples = [s for s in self.samples if s["t"] >= cutoff]
        if not sample["plugged"]:
            self.dismissed = False       # re-arm: the owner's own "next plug-in" rule
        was_stuck = self.stuck
        self.stuck, reason = charge_stuck.evaluate(self.samples)
        if self.stuck and not was_stuck:
            log.warning("charge_stuck: %s", reason)
        elif was_stuck and not self.stuck:
            log.info("charge_stuck: cleared (%s)", reason)
        self._apply()
        self.host.state_dirty = True

    def _apply(self):
        home = getattr(self.host.ui, "home", None)
        if home is None:
            return
        home.set_charge_notice(MESSAGE if (self.stuck and not self.dismissed) else "")

    def dismiss(self):
        log.info("charge_stuck: dismissed (re-arms on the next plug-in)")
        self.dismissed = True
        self._apply()
        self.host.state_dirty = True

    def state(self):
        return {"stuck": self.stuck, "dismissed": self.dismissed, "samples": len(self.samples)}
