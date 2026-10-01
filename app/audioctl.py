"""audioctl: the UI's view of audio.py, a dry run switch and a throttle.

audio.py is used as is. With RP5DECK_AUDIO_DRYRUN=1 every setter logs exactly what it would
have run (same clamping and argument formatting as audio.py) instead of calling wpctl or
/usr/bin/volume. Readers (get_master, list_streams, Subscriber) always run for real.

Throttle turns a slider's stream of drag values into at most one send per interval (the latest
value wins) and drops whatever's pending when the drag ends, since the release is committed
separately, exactly once.
"""
import collections
import logging
import os

import audio

log = logging.getLogger("rp5deck.audio")


def dryrun_from_env(env=None):
    env = os.environ if env is None else env
    return env.get("RP5DECK_AUDIO_DRYRUN", "") == "1"


class Backend:
    def __init__(self, dryrun=None):
        self.dryrun = dryrun_from_env() if dryrun is None else bool(dryrun)
        # (name, argument) of recent setter calls, for state.json and tests
        self.calls = collections.deque(maxlen=200)

    # -- readers: always real -------------------------------------------
    def get_master(self):
        return audio.get_master()

    def list_streams(self):
        return audio.list_streams()

    def subscriber(self, callback):
        return audio.Subscriber(callback)

    # -- setters --------------------------------------------------------
    def _dry(self, name, arg, cmd):
        self.calls.append((name, arg))
        log.info("DRYRUN %s(%s) -> would run: %s", name, arg, " ".join(cmd))
        return True

    def set_master(self, v):
        v = audio._clamp(float(v), 0.0, audio.MAX_LIVE_VOLUME)
        if self.dryrun:
            return self._dry("set_master", "%.4f" % v,
                             ["wpctl", "set-volume", audio.DEFAULT_SINK_ALIAS, "%.4f" % v])
        self.calls.append(("set_master", "%.4f" % v))
        return audio.set_master(v)

    def commit_master(self, v):
        v = audio._clamp(float(v), 0.0, audio.MAX_COMMIT_VOLUME)
        percent = int(round(v * 100))
        if self.dryrun:
            return self._dry("commit_master", "%.4f" % v, [audio.VOLUME_SCRIPT, str(percent)])
        self.calls.append(("commit_master", "%.4f" % v))
        ok = audio.commit_master(v)
        log.info("commit_master(%.4f) -> /usr/bin/volume %d: %s", v, percent, ok)
        return ok

    def toggle_master_mute(self):
        if self.dryrun:
            return self._dry("toggle_master_mute", "",
                             ["wpctl", "set-mute", audio.DEFAULT_SINK_ALIAS, "toggle"])
        self.calls.append(("toggle_master_mute", ""))
        return audio.toggle_master_mute()

    def set_stream_volume(self, stream_id, v):
        v = audio._clamp(float(v), 0.0, audio.MAX_LIVE_VOLUME)
        if self.dryrun:
            return self._dry("set_stream_volume", "%s, %.4f" % (stream_id, v),
                             ["wpctl", "set-volume", str(stream_id), "%.4f" % v])
        self.calls.append(("set_stream_volume", "%s, %.4f" % (stream_id, v)))
        return audio.set_stream_volume(stream_id, v)

    def toggle_stream_mute(self, stream_id):
        if self.dryrun:
            return self._dry("toggle_stream_mute", str(stream_id),
                             ["wpctl", "set-mute", str(stream_id), "toggle"])
        self.calls.append(("toggle_stream_mute", str(stream_id)))
        return audio.toggle_stream_mute(stream_id)


class Throttle:
    """Rate limits a stream of values to one send per `interval` seconds.

    push(v, now) sends right away if the interval has passed, otherwise keeps v pending and returns
    when due(now) should be called. finish() drops any pending value (the caller commits the final
    one itself). The clock gets passed in so tests dont sleep.
    """

    def __init__(self, interval, send):
        self.interval = interval
        self.send = send
        self.last_sent = None
        self.pending = None
        self.sent = 0

    def push(self, v, now):
        if self.last_sent is None or now >= self.last_sent + self.interval - 1e-9:
            self._do(v, now)
            return None
        self.pending = v
        return self.last_sent + self.interval

    def due(self, now):
        """Sends the pending value if it's due. Returns the next due time if something's still waiting,
        else None.
        """
        if self.pending is None:
            return None
        # the same expression as the due time handed out, so a timer that fires right on time is never
        # judged early by float rounding and rearmed
        if now >= self.last_sent + self.interval - 1e-9:
            v, self.pending = self.pending, None
            self._do(v, now)
            return None
        return self.last_sent + self.interval

    def finish(self):
        self.pending = None
        self.last_sent = None

    def _do(self, v, now):
        self.last_sent = now
        self.sent += 1
        self.send(v)
