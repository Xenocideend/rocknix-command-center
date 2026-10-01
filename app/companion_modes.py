"""companion_modes: what the bottom screen shows during a one-screen game, the pure logic
behind `companion.in_game_display`.

That's any game whose emulator doesnt put a real window on DSI-1. The DS, 3DS and Wii U second
screen cases already put rp5deck in HIDDEN before any of this runs. companion.py's
CompanionController is the only caller and owns every side effect (timers, jobs, view calls).

The ES-DE Companion Android app (github.com/RobZombie9043/es-de-companion, see
research/CC3-esde-companion.md) has a "Game Playing Screen Behavior" setting with On, Dim, Off,
Manual and Guide, which this follows, plus HUD, clock and a system slideshow:

    art        ES-DE's On, the default, normal companion behaviour
    dim        ES-DE's Dim, the art stays but darker
    off        ES-DE's Off, opaque black, no art decoded at all
    manual     ES-DE's Manual, falls back to art if this game has no manual
    hud        device stats (hud.py)
    clock      the idle clock view as is
    slideshow  a slideshow of the running game's own system, not the whole library (that's
               the idle slideshow's job)
ES-DE's Guide isnt here since no guide media kind exists in ES's API or gamelists.

Video is never a choice. No software decoded video while a game needs the CPU, and
companion.py's video_allowed() already refuses video whenever a game runs, whatever
in_game_display says.
"""

IN_GAME_MODES = ("art", "dim", "off", "manual", "hud", "clock", "slideshow")

# How often the in-game HUD resamples. hud.py's sample() shells out (df, iw, ip) and reads a
# couple dozen sysfs files, so this stays well under 1 Hz to keep CPU low while a game runs. The
# Command Center's full HUD sheet samples at 1 Hz, but only while it's on screen.
HUD_INTERVAL_S = 3.0


def effective_in_game_mode(mode, has_manual):
    """The mode actually used this refresh. An unknown or missing mode falls back to "art" so a typo
    in a hand edited config.json never gives a blank screen, and "manual" falls back to "art" when
    this game has none instead of leaving the last view's stale content up.
    """
    if mode not in IN_GAME_MODES:
        return "art"
    if mode == "manual" and not has_manual:
        return "art"
    return mode


def in_game_dim(mode, configured_dim):
    """The darkening fraction (0..1) drawn over the game's art. "off" never gets here (companion.py
    shows a plain blank view with no art decoded), but asking anyway gets the same fully opaque
    answer. "dim" floors background_dim at a level that reads as dimmed, like ES-DE Companion's
    translucent black scrim, instead of the light veil background_dim normally gives under text.
    """
    if mode == "off":
        return 1.0
    if mode == "dim":
        return max(float(configured_dim), 0.6)
    return float(configured_dim)


# hud.sample()'s keys this knows how to format, in display order, as (key, label, formatter).
# Only keys that exist and arent None show, hud.py never fakes a value so None really means
# unavailable.
def _pct(v):
    return "%d%%" % int(round(v))


def _c(v):
    return "%.0f°C" % v


def _mhz_from_clusters(clusters):
    if not clusters:
        return None
    fastest = max((c.get("cur_mhz") for c in clusters if c.get("cur_mhz") is not None), default=None)
    return fastest


def _hottest_temp(temps_c):
    if not temps_c:
        return None
    return max(temps_c.values())


def _cpu_mhz(cl):
    f = _mhz_from_clusters(cl)
    return ("%d MHz" % f) if f is not None else None


def _hottest(temps):
    t = _hottest_temp(temps)
    return _c(t) if t is not None else None


_HUD_FIELDS = (
    ("battery_percent", "Battery", _pct),
    ("temps_c", "Temp", _hottest),
    ("cpu_clusters", "CPU", _cpu_mhz),
    ("gpu_load_percent", "GPU", _pct),
    ("ram_available_mb", "RAM free", lambda mb: "%d MB" % mb),
)


def hud_lines(sample):
    """hud.sample()'s dict (or None before the first sample) -> a short list of "Label value" lines
    for the in-game HUD. The Command Center's HUD sheet is the full version with every field, this
    is just enough to glance at while playing.
    """
    if not sample:
        return []
    out = []
    for key, label, fmt in _HUD_FIELDS:
        raw = sample.get(key)
        if raw is None:
            continue
        try:
            val = fmt(raw)
        except Exception:           # noqa: BLE001 - a malformed sample must not crash the view
            val = None
        if val is not None:
            out.append("%s %s" % (label, val))
    return out


def fmt_playtime(extra):
    """es_api.Game.extra's playcount / lastplayed / gametime (optional ES metadata strings) -> a
    short "Played Nx" line, "" if nothing usable is there. It only ever shows a play count, since
    gametime in raw seconds isnt reliably there across ES forks and its units would be a guess.
    """
    extra = extra or {}
    n = extra.get("playcount")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return "Played once" if n == 1 else "Played %d times" % n
