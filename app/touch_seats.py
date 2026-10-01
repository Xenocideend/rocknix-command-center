"""touch_seats: which of SDL's touch devices the app listens to.

sway can have a second seat (ROCKNIX's sway config creates seat1 when EmulationStation starts) and puts every touch device
on both. SDL binds each seat's wl_seat and shows one "Virtual core touch (seatN)" device per seat, so a single finger on the
panel arrives twice within a millisecond with the same finger id: once from seat0 at the right place and once from seat1 at
a spot that is the same touch without the panel's rotation. The router keys a touch by that finger id, so the second down
replaced the first and the tap acted in a random place until the app was restarted.

wanted_ids() keeps the default seat's device (seat0) when there are several seats and leaves every other case alone.
"""
import re

SEAT = re.compile(r"\(seat(\d+)\)\s*$")
DEFAULT_SEAT = 0


def seat_of(name):
    """The seat number in a device name like "Virtual core touch (seat1)", or None for any other name."""
    m = SEAT.search(name or "")
    return int(m.group(1)) if m else None


def wanted_ids(devices):
    """devices: {touch id: device name}. The set of ids whose touches are used. Devices on a non-default seat are dropped
    only when the default seat's device is there too; with one seat, or no seat names, every device is kept."""
    seats = {i: seat_of(n) for i, n in devices.items()}
    if DEFAULT_SEAT not in seats.values():
        return set(devices)
    return {i for i, s in seats.items() if s is None or s == DEFAULT_SEAT}


class TwinFilter:
    """For a device whose touch can arrive through either of two seats. A finger id belongs to the first touch device
    that reports it, and the same id from another device is its twin and is dropped, until the finger lifts or goes
    quiet for IDLE_NS. accept() says whether to use an event."""
    IDLE_NS = 500_000_000
    MAX_OWNED = 64

    def __init__(self, idle_ns=IDLE_NS):
        self.idle_ns = idle_ns
        self.owner = {}                     # finger id -> [touch id, time of its last event]

    def accept(self, touch_id, finger_id, timestamp, ends=False):
        cur = self.owner.get(finger_id)
        if cur is not None and cur[0] != touch_id and timestamp - cur[1] <= self.idle_ns:
            return False
        if ends:
            self.owner.pop(finger_id, None)
        else:
            self.owner[finger_id] = [touch_id, timestamp]
            if len(self.owner) > self.MAX_OWNED:      # a finger whose lift was never seen
                for k in sorted(self.owner, key=lambda k: self.owner[k][1])[:len(self.owner) - self.MAX_OWNED]:
                    del self.owner[k]
        return True
