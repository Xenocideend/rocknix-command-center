"""companion_modes - CC2 + CC3.

CC2 (owner request, TASKS.md): "an option setting to decide what the bottom
screen shows during a single-screen game" - i.e. any game whose emulator does
NOT put a real window on DSI-1 (the DS/3DS/Wii U second-screen cases already
put rp5deck into sway_ipc.HIDDEN before any of this runs - see DESIGN.md
"Three display modes"). This module is the pure decision logic behind
`companion.in_game_display`; companion.py's CompanionController is the only
caller and owns all the side effects (timers, submit jobs, view calls).

CC3: research/CC3-esde-companion.md researches the ES-DE Companion Android
app (github.com/RobZombie9043/es-de-companion) and maps its options onto what
ES's HTTP API / gamelists actually expose on this device (es_api.py). Its
"Game Playing Screen Behavior" setting is the direct precedent for
`in_game_display`: On / Dim / Off / Manual / Guide. rp5deck's equivalent
adds two rp5deck-only choices the owner named directly (HUD, clock) and one
more from the owner's own list (a slideshow of the system's art) that ES-DE
Companion does not have:

    ES-DE Companion          rp5deck (in_game_display)      note
    ----------------          --------------------------      ----
    On                        art                              default; unchanged companion behaviour
    Dim                       dim                               art stays, forced darker
    Off                       off                               opaque black, no art decode at all
    Manual                    manual                            falls back to "art" if this game has none
    Guide                     (not implemented)                 no GameFAQs-guide media kind exists in ES's
                                                                  API or gamelists - see the research doc
    (none)                    hud                               owner-requested: device stats (hud.py)
    (none)                    clock                             owner-requested: the idle clock view, reused as-is
    (none)                    slideshow                         owner-requested: a slideshow of the RUNNING
                                                                  game's OWN SYSTEM (not the whole library -
                                                                  that is the existing idle slideshow's job)

Video is never one of the choices: DESIGN.md's media budget rule (no
software-decoded video while a game needs the CPU) applies unconditionally,
before this module is ever consulted - companion.py's video_allowed() already
refuses video whenever self.running is not None, regardless of
in_game_display.
"""

IN_GAME_MODES = ("art", "dim", "off", "manual", "hud", "clock", "slideshow")

# How often the in-game HUD (mini device-stats overlay) re-samples. hud.py's
# sample() shells out (df, iw, ip) and reads a couple dozen sysfs files, so
# this stays well under 1 Hz to keep the "keep CPU low while a game runs"
# rule (DESIGN.md media budget) - the Command Center's own full HUD sheet
# samples at 1 Hz only while that sheet is the visible thing on screen, which
# this is not.
HUD_INTERVAL_S = 3.0


def effective_in_game_mode(mode, has_manual):
    """The mode actually applied this refresh. An unknown/missing mode
    degrades to "art" (never a blank screen because of a typo in a hand-
    edited config.json - see config.py's own validation philosophy).
    "manual" degrades to "art" when this particular game has none, rather
    than opening nothing and leaving the previous view's stale content on
    screen."""
    if mode not in IN_GAME_MODES:
        return "art"
    if mode == "manual" and not has_manual:
        return "art"
    return mode


def in_game_dim(mode, configured_dim):
    """The darken-overlay fraction (0..1) drawn over the game's art.
    "off" never actually reaches this - companion.py shows a plain blank
    view for it, decoding no art at all - but a caller that asks anyway gets
    the same fully-opaque answer a blank view would. "dim" floors the
    configured background_dim at a level that reads as "dimmed", matching
    ES-DE Companion's "Dim" behaviour (a translucent black scrim) rather than
    the subtle veil background_dim normally provides under legible text."""
    if mode == "off":
        return 1.0
    if mode == "dim":
        return max(float(configured_dim), 0.6)
    return float(configured_dim)


# hud.sample()'s keys this module knows how to format, in display order, as
# (key, label, formatter). Only keys that exist AND are not None are shown -
# hud.py's own rule ("never 0, never a fake value") means a None here is a
# genuinely unavailable reading, not a zero to hide specially.
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
    """hud.sample()'s dict (or None, before the first sample arrives) -> a
    short list of "Label value" strings for the in-game HUD-lite overlay -
    the Command Center's own HUD sheet (hud.py + ui.py, not this module) is
    the full version with every field; this is deliberately just enough to
    glance at while playing."""
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
    """es_api.Game.extra's playcount / lastplayed / gametime (all optional
    ES metadata strings) -> a short "Played Nx" style line, "" if nothing
    usable is present. lastplayed/gametime are shown as ES stores them
    (gametime as raw seconds is not reliably present across ES forks, so
    this only ever reports a play COUNT, never a duration it would have to
    guess the units of)."""
    extra = extra or {}
    n = extra.get("playcount")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return "Played once" if n == 1 else "Played %d times" % n
