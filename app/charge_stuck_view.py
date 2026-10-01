"""charge_stuck_view - the big "charger connected but not charging" warning.

poll() runs every main.CHARGE_STUCK_POLL_S and only reads sysfs/debugfs. Once charge_stuck says
its stuck the warning comes up full screen and stays until the charger is unplugged, even if
charging recovers first. It only comes back on a new stuck plug. A weak charger never trips it
since the input stage still reads a real limit (ICL not 0)."""
import logging

import charge_stuck

log = logging.getLogger("rp5deck.charge_stuck")

# Unplugging and replugging the charger is the fix that works. A restart with the charger still
# in came up stuck again, so it's never offered. While stuck, sleep_guard.py blocks suspend,
# since a suspend in this state reset the RP5.
HEADLINE = "Charger connected but not charging"
LINES = ("The best fix is to unplug the charger and plug it back in.",
         "This message will close when you unplug the charger.")
MESSAGE = HEADLINE + ". " + " ".join(LINES)
# old samples get trimmed every poll so this never grows over a long session, a couple of
# windows is plenty for evaluate()'s rolling WINDOW_S
KEEP_S = charge_stuck.WINDOW_S * 2


class ChargeStuckController:
    """host is main.App (ui, state_dirty). submit(fn, *args, done=cb) runs fn
    (charge_stuck.Sampler.sample) on a worker and posts cb(result) to the UI thread, since it's real
    sysfs/debugfs I/O even if each read is cheap. sampler defaults to charge_stuck.Sampler() (tests
    pass a fake with canned samples).
    """

    def __init__(self, host, submit, sampler=None):
        self.host = host
        self.submit = submit
        self.sampler = sampler or charge_stuck.Sampler()
        self.samples = []
        self.stuck = False
        self.warning = False            # latched on a stuck plug, cleared only by an unplug
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
        was_stuck = self.stuck
        self.stuck, reason = charge_stuck.evaluate(self.samples)
        if self.stuck and not was_stuck:
            log.warning("charge_stuck: %s", reason)
        elif was_stuck and not self.stuck:
            log.info("charge_stuck: cleared (%s)", reason)
        if self.stuck and not self.warning:
            self.warning = True
            log.warning("charge_stuck: warning up until the charger is unplugged")
        elif self.warning and not sample["plugged"]:
            self.warning = False
            log.info("charge_stuck: charger unplugged, warning closed")
        self._apply()
        self.host.state_dirty = True

    def _apply(self):
        home = getattr(self.host.ui, "home", None)
        if home is None:
            return
        show = getattr(self.host, "set_charge_warning", None)
        if show is not None:
            show(self.warning)

    def state(self):
        return {"stuck": self.stuck, "warning": self.warning, "samples": len(self.samples)}
