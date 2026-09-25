#!/usr/bin/env python3
"""charge_stuck_view.py (CHG): ChargeStuckController against a fake host and
a fake sampler (no sysfs, no debugfs) - the poll/banner/dismiss/re-arm flow.
The real screens.Home banner sharing (CHG outranks DS) is covered by
tests/test_screens.py's TestBannerPriority."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import charge_stuck_view as csv  # noqa: E402


class FakeHome:
    def __init__(self):
        self.charge_notice = None

    def set_charge_notice(self, text):
        self.charge_notice = text


class FakeUi:
    def __init__(self):
        self.home = FakeHome()


class Host:
    def __init__(self):
        self.ui = FakeUi()
        self.state_dirty = False

    def post(self, fn, *a):
        fn(*a)


class Deferred:
    def __init__(self, host):
        self.host = host
        self.q = []

    def __call__(self, fn, *args, done=None):
        self.q.append((fn, args, done))

    def run(self):
        fn, args, done = self.q.pop(0)
        res = fn(*args)
        if done:
            self.host.post(done, res)


def sample(t, plugged, current_ua=None, icl=None):
    return {"t": t, "plugged": plugged, "current_ua": current_ua, "icl": icl}


class FakeSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.samples.pop(0)


class Case(unittest.TestCase):
    def make(self, samples):
        self.host = Host()
        self.worker = Deferred(self.host)
        self.sampler = FakeSampler(samples)
        self.c = csv.ChargeStuckController(self.host, self.worker, sampler=self.sampler)
        return self.c


class TestPoll(Case):
    def test_stuck_sets_the_banner(self):
        c = self.make([sample(i * 10.0, True, 700_000, 0x00) for i in range(6)])
        for _ in range(6):
            c.poll()
            self.worker.run()
        self.assertTrue(c.stuck)
        self.assertEqual(self.host.ui.home.charge_notice, csv.MESSAGE)

    def test_good_never_sets_the_banner(self):
        c = self.make([sample(i * 10.0, True, -300_000, 0x0C) for i in range(6)])
        for _ in range(6):
            c.poll()
            self.worker.run()
        self.assertFalse(c.stuck)
        self.assertEqual(self.host.ui.home.charge_notice, "")

    def test_a_poll_already_inflight_is_not_duplicated(self):
        c = self.make([sample(0.0, False), sample(10.0, False)])
        c.poll()
        c.poll()
        self.assertEqual(len(self.worker.q), 1)
        self.worker.run()
        self.assertEqual(self.sampler.calls, 1)

    def test_old_samples_are_trimmed(self):
        c = self.make([sample(0.0, True, 700_000, 0x00),
                       sample(csv.KEEP_S + 100.0, True, 700_000, 0x00)])
        c.poll()
        self.worker.run()
        c.poll()
        self.worker.run()
        self.assertEqual(len(c.samples), 1)


class TestDismissAndRearm(Case):
    def stuck_sequence(self, n=6, start=0.0, step=10.0):
        return [sample(start + i * step, True, 700_000, 0x00) for i in range(n)]

    def test_dismiss_hides_the_banner_even_though_still_stuck(self):
        c = self.make(self.stuck_sequence())
        for _ in range(6):
            c.poll()
            self.worker.run()
        self.assertEqual(self.host.ui.home.charge_notice, csv.MESSAGE)
        c.dismiss()
        self.assertEqual(self.host.ui.home.charge_notice, "")
        self.assertTrue(c.stuck)          # the underlying condition is unchanged

    def test_dismissed_stays_hidden_across_polls_while_still_plugged(self):
        c = self.make(self.stuck_sequence(n=8))
        for _ in range(6):
            c.poll()
            self.worker.run()
        c.dismiss()
        for _ in range(2):
            c.poll()
            self.worker.run()
        self.assertEqual(self.host.ui.home.charge_notice, "")

    def test_unplug_rearms_even_while_still_dismissed(self):
        # after the replug at t=60, the 60 s rolling window must be entirely
        # past that unplugged sample before "stuck" can fire again - 7 more
        # samples (70..130) clears it (130 - 60 = 70 > window_s).
        samples = self.stuck_sequence(n=6) + [sample(60.0, False)] + self.stuck_sequence(
            n=7, start=70.0)
        c = self.make(samples)
        for _ in range(6):
            c.poll()
            self.worker.run()
        c.dismiss()
        self.assertEqual(self.host.ui.home.charge_notice, "")
        c.poll()                          # the unplugged sample
        self.worker.run()
        self.assertFalse(c.dismissed)
        for _ in range(7):
            c.poll()
            self.worker.run()
        self.assertEqual(self.host.ui.home.charge_notice, csv.MESSAGE)

    def test_dismiss_before_ever_stuck_is_harmless(self):
        c = self.make([sample(0.0, False)])
        c.dismiss()
        c.poll()
        self.worker.run()
        self.assertEqual(self.host.ui.home.charge_notice, "")


class TestState(Case):
    def test_state_is_json_able(self):
        import json
        c = self.make([sample(0.0, True, 700_000, 0x00)])
        c.poll()
        self.worker.run()
        json.dumps(c.state(), default=str)


if __name__ == "__main__":
    unittest.main()
