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
