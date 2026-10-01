#!/usr/bin/env python3
"""charge_stuck_view.py: ChargeStuckController against a fake host and a fake sampler (no sysfs,
no debugfs), the poll, warning and unplug flow. The fake Home has no banner, so a call to one fails."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import charge_stuck_view as csv  # noqa: E402


class FakeHome:
    pass


class FakeUi:
    def __init__(self):
        self.home = FakeHome()


class Host:
    def __init__(self):
        self.ui = FakeUi()
        self.state_dirty = False
        self.warning_calls = []

    def set_charge_warning(self, on):
        self.warning_calls.append(on)

    @property
    def warning_up(self):
        return bool(self.warning_calls) and self.warning_calls[-1]

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
    def test_stuck_puts_the_big_warning_up(self):
        c = self.make([sample(i * 10.0, True, 700_000, 0x00) for i in range(6)])
        for _ in range(6):
            c.poll()
            self.worker.run()
        self.assertTrue(c.stuck)
        self.assertTrue(self.host.warning_up)

    def test_good_never_warns(self):
        c = self.make([sample(i * 10.0, True, -300_000, 0x0C) for i in range(6)])
        for _ in range(6):
            c.poll()
            self.worker.run()
        self.assertFalse(c.stuck)
        self.assertFalse(self.host.warning_up)

    def test_a_weak_charger_draining_under_load_never_warns(self):
        # a 5 V / 1 A charger while a heavy game runs: the battery still drains, but the
        # input stage is up and reads a real limit, so this is not the stuck charger
        c = self.make([sample(i * 10.0, True, 400_000, 0x14) for i in range(12)])
        for _ in range(12):
            c.poll()
            self.worker.run()
        self.assertFalse(c.stuck)
        self.assertFalse(self.host.warning_up)

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


class TestWarningLatch(Case):
    def stuck_sequence(self, n=6, start=0.0, step=10.0):
        return [sample(start + i * step, True, 700_000, 0x00) for i in range(n)]

    def run_all(self, c, n):
        for _ in range(n):
            c.poll()
            self.worker.run()

    def test_stays_up_after_charging_recovers_while_still_plugged(self):
        good = [sample(60.0 + i * 10.0, True, -1_500_000, 0x28) for i in range(4)]
        c = self.make(self.stuck_sequence() + good)
        self.run_all(c, 10)
        self.assertFalse(c.stuck)             # charging again
        self.assertTrue(self.host.warning_up)  # but it only closes on an unplug

    def test_unplug_closes_it(self):
        c = self.make(self.stuck_sequence() + [sample(60.0, False)])
        self.run_all(c, 7)
        self.assertFalse(self.host.warning_up)

    def test_only_comes_back_on_a_new_stuck_plug(self):
        good_plug = [sample(70.0 + i * 10.0, True, -1_500_000, 0x28) for i in range(8)]
        c = self.make(self.stuck_sequence() + [sample(60.0, False)] + good_plug)
        self.run_all(c, 15)
        self.assertFalse(self.host.warning_up)
        c2 = self.make(self.stuck_sequence() + [sample(60.0, False)] + self.stuck_sequence(n=8, start=70.0))
        self.run_all(c2, 15)
        self.assertTrue(self.host.warning_up)

    def test_the_text_says_the_fix_and_how_it_closes(self):
        self.assertIn("not charging", csv.HEADLINE.lower())
        self.assertTrue(any("unplug the charger and plug it back in" in l for l in csv.LINES))
        self.assertTrue(any("close when you unplug" in l for l in csv.LINES))


class TestState(Case):
    def test_state_is_json_able(self):
        import json
        c = self.make([sample(0.0, True, 700_000, 0x00)])
        c.poll()
        self.worker.run()
        json.dumps(c.state(), default=str)


if __name__ == "__main__":
    unittest.main()
