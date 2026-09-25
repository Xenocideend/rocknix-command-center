"""screens - rp5deck's screens, built from ui.py widgets.

DeckUI owns the widget tree (Navigation v2, DESIGN.md):
  companion  the default view (companion.CompanionView), when given
  cc         the Command Center, pulled down over it:
    bar      volume strip: mute, master volume slider, % readout, battery
             (tap -> HUD sheet), clock. In FULL mode it sits at the TOP of
             the Command Center ("volume first"); in BAR mode it is the
             whole 140 px surface.
    home     tile grid: Mixer, HUD, Browser, Discord, YouTube App, Hotkeys
             (CC7), Clean state (CC1), Settings, Swap screens (SW1) - 5 x 2
             once there are more than 8 (I2, Home.grid_cols) - plus a Close
             control (back to the companion). CC5 OVERLAY (over
             an emulator's second screen, hidden_overlay.py): only Mixer,
             HUD and a big Close, under the same volume strip
    sheets   hud, mixer (paged, 5 rows a page), launch (status while Firefox
             starts / why it failed), settings (settings_view.SettingsSheet,
             full panel: it needs every row, and has its own Back)

BAR mode (HF1): while an rp5deck window (Firefox rp5deck-web) fills DSI-1
above it, the strip keeps mute + volume and swaps battery / clock for that
app's controls: Back, Reload, Home, Keyboard, Close for the browser; Home,
the leanback D-pad, Close for the YouTube App (YT3 - no Tabs button on this
row, see Bar.tv_controls()). Every control is a tap on the layer surface,
so none of them moves focus.

Every view covers the whole panel in FULL mode (a tap on bare DSI-1 steals
the game's focus, DESIGN.md).

Callbacks go to a `handlers` object (the controller in main.py). Pure
formatting (hud_rows, fmt_*) is unit-tested; drawing is not.
"""
import math
import time

import swap_ui
import ui
from ui import (THEME, Button, Container, Keyboard, Label, LitButton, Sheet, Slider, TextField,
                Tile, Toggle)

BAR_H = 140
# AH: the YouTube TV app's auto-hidden strip - main.py's geometry()/
# bar_autohide.py shrink the WHOLE layer surface (not just the drawn strip)
# to this height instead of BAR_H, with the exclusive zone at 0, so the
# leanback window gets the full 1080 and never reflows; a thin always-
# touchable grab handle is all that is drawn or laid out at this height
# (Bar.layout/_apply_app_visibility/draw, CommandCenter.layout). Every other
# BAR app (Browser/Discord/mpv) never sees this - they keep the fixed BAR_H
# reserved strip exactly as before.
HANDLE_H = 24
# CC5: the home tiles shown over an emulator's second screen (hidden_overlay.py):
# per-app volume and device info. Browser / YouTube / Discord would open a
# window on the screen the game owns; Settings is a whole-panel sheet.
OVERLAY_TILES = ("home.mixer", "home.hud")
# I2: the home grid is HOME_ROWS rows of at least HOME_MIN_COLS columns
# (Home.grid_cols); tiles narrower than the 4-column width use a smaller label.
HOME_ROWS = 2
HOME_MIN_COLS = 4
TILE_TEXT = 48
TILE_TEXT_NARROW = 42
TILE_SUB_NARROW = 28

# Tile customisation ("Edit tiles" mode, owner: "add a customisation mode
# with drag and drop tiles"): a long-press on any home tile (held in place
# past EDIT_LONG_PRESS_S) enters it - see _make_tile_editable() below. A
# small persistent "Edit" button was the other option; long-press won out
# because it matches the mobile home-screen rearrange gesture the owner is
# almost certainly picturing, and it reuses the app's own call_at/
# call_later timer (main.App, already relied on for the auto-close
# countdown, the RGB keeper poll, etc.) rather than adding new permanent
# header chrome for a rarely-used mode. TILE_BADGE is the per-tile hide/
# show control's touch target (>= the 120 px floor).
EDIT_LONG_PRESS_S = 0.5
TILE_BADGE = 120

# DS (dualscreen_keys_view.py): the "dual-screen settings missing" banner's
# own band height, reserved above the tile grid only while it is shown -
# tall enough that its Restore button clears the app's own >= 120 px touch
# target floor (ui.MIN_TARGET), same as every tile/badge/button elsewhere.
DS_BANNER_H = 130


# ---------------------------------------------------------------------------
# Icons: callables (g, rect, color)
# ---------------------------------------------------------------------------
def icon_speaker(g, r, color, muted=False, level=1.0):
    x, y, w, h = r
    g.polygon([(x, y + h * 0.35), (x + w * 0.22, y + h * 0.35), (x + w * 0.5, y + h * 0.1),
               (x + w * 0.5, y + h * 0.9), (x + w * 0.22, y + h * 0.65), (x, y + h * 0.65)], color)
    t = max(4, w * 0.08)
    if muted:
        g.line(x + w * 0.64, y + h * 0.32, x + w * 0.98, y + h * 0.68, color, t)
        g.line(x + w * 0.98, y + h * 0.32, x + w * 0.64, y + h * 0.68, color, t)
    else:
        g.ring(x + w * 0.5, y + h * 0.5, w * 0.22, color, t, -0.8, 0.8)
        g.ring(x + w * 0.5, y + h * 0.5, w * 0.42, color, t, -0.8, 0.8)


def icon_speaker_on(g, r, color):
    icon_speaker(g, r, color, False)


def icon_speaker_muted(g, r, color):
    icon_speaker(g, r, color, True)


def icon_mixer(g, r, color):
    x, y, w, h = r
    t = max(5, w * 0.07)
    for i, k in enumerate((0.3, 0.7, 0.45)):
        cx = x + w * (0.2 + 0.3 * i)
        g.line(cx, y + h * 0.05, cx, y + h * 0.95, THEME["faint"], t)
        g.round_rect((cx - w * 0.1, y + h * k - h * 0.07, w * 0.2, h * 0.14), 6, color)


def icon_gauge(g, r, color):
    x, y, w, h = r
    cx, cy, rad = x + w / 2, y + h * 0.62, w * 0.46
    g.ring(cx, cy, rad, color, max(6, w * 0.08), 3.4, 6.03)
    g.line(cx, cy, cx + rad * 0.55, cy - rad * 0.55, color, max(6, w * 0.07))
    g.circle(cx, cy, w * 0.07, color)


def icon_globe(g, r, color):
    x, y, w, h = r
    cx, cy, rad = x + w / 2, y + h / 2, w * 0.45
    t = max(4, w * 0.05)
    g.ring(cx, cy, rad, color, t)
    g.line(cx - rad, cy, cx + rad, cy, color, t)
    g.line(cx, cy - rad, cx, cy + rad, color, t)
    g.ring(cx, cy + rad * 1.35, rad * 1.1, color, t, 4.05, 5.37)
    g.ring(cx, cy - rad * 1.35, rad * 1.1, color, t, 0.9, 2.24)


def icon_gear(g, r, color):
    x, y, w, h = r
    cx, cy, rad = x + w / 2, y + h / 2, w * 0.3
    t = max(6, w * 0.12)
    for k in range(8):
        a = k * math.pi / 4
        g.line(cx + math.cos(a) * rad * 0.8, cy + math.sin(a) * rad * 0.8,
               cx + math.cos(a) * rad * 1.45, cy + math.sin(a) * rad * 1.45, color, t)
    g.ring(cx, cy, rad, color, max(6, w * 0.11))


def icon_chat(g, r, color):
    x, y, w, h = r
    t = max(5, w * 0.07)
    g.stroke_round_rect((x + w * 0.05, y + h * 0.12, w * 0.9, h * 0.6), h * 0.18, color, t)
    g.polygon([(x + w * 0.25, y + h * 0.66), (x + w * 0.25, y + h * 0.92),
               (x + w * 0.5, y + h * 0.7)], color)


def icon_close_up(g, r, color):
    x, y, w, h = r
    t = max(4, w * 0.12)
    g.line(x + w * 0.15, y + h * 0.65, x + w * 0.5, y + h * 0.3, color, t)
    g.line(x + w * 0.5, y + h * 0.3, x + w * 0.85, y + h * 0.65, color, t)


def icon_reload(g, r, color):
    x, y, w, h = r
    t = max(5, w * 0.1)
    cx, cy, rad = x + w / 2, y + h / 2, w * 0.36
    g.ring(cx, cy, rad, color, t, -1.2, 4.1)
    ax, ay = cx + rad * math.cos(-1.2), cy + rad * math.sin(-1.2)
    g.polygon([(ax - w * 0.2, ay - h * 0.02), (ax + w * 0.1, ay - h * 0.2),
               (ax + w * 0.08, ay + h * 0.14)], color)


def icon_home(g, r, color):
    x, y, w, h = r
    g.polygon([(x + w * 0.5, y + h * 0.08), (x + w * 0.95, y + h * 0.5), (x + w * 0.05, y + h * 0.5)],
              color)
    g.fill_rect((x + w * 0.2, y + h * 0.48, w * 0.6, h * 0.44), color)
    g.fill_rect((x + w * 0.42, y + h * 0.64, w * 0.16, h * 0.28), THEME["tile"])


def icon_keyboard(g, r, color):
    x, y, w, h = r
    t = max(4, w * 0.06)
    g.stroke_round_rect((x + w * 0.02, y + h * 0.2, w * 0.96, h * 0.6), h * 0.1, color, t)
    for row in range(2):
        for col in range(5):
            g.round_rect((x + w * (0.13 + col * 0.16), y + h * (0.32 + row * 0.16),
                          w * 0.09, h * 0.09), 2, color)
    g.round_rect((x + w * 0.26, y + h * 0.64, w * 0.48, h * 0.07), 2, color)


def icon_close(g, r, color):
    x, y, w, h = r
    t = max(5, w * 0.12)
    g.line(x + w * 0.2, y + h * 0.2, x + w * 0.8, y + h * 0.8, color, t)
    g.line(x + w * 0.8, y + h * 0.2, x + w * 0.2, y + h * 0.8, color, t)


def icon_tabs(g, r, color):
    """CC6: three tabs over a panel (the BAR strip's Tabs button)."""
    x, y, w, h = r
    for i in range(3):
        g.round_rect((x + w * (0.05 + 0.31 * i), y + h * 0.12, w * 0.27, h * 0.22), 4, color)
    g.round_rect((x + w * 0.05, y + h * 0.40, w * 0.9, h * 0.5), 6, color)


def icon_ytapp(g, r, color):
    """W2b: a TV screen (leanback)."""
    x, y, w, h = r
    t = max(4, w * 0.07)
    g.stroke_round_rect((x + w * 0.1, y + h * 0.08, w * 0.8, h * 0.58), 6, color, t)
    g.fill_rect((x + w * 0.42, y + h * 0.7, w * 0.16, h * 0.14), color)
    g.fill_rect((x + w * 0.28, y + h * 0.86, w * 0.44, h * 0.08), color)


def icon_stick_lights(g, r, color):
    """RG: an analog stick cap with an RGB glow ring around it."""
    x, y, w, h = r
    cx, cy = x + w / 2, y + h / 2
    g.circle(cx, cy, w * 0.22, color)
    g.ring(cx, cy, w * 0.42, color, max(4, w * 0.08))


def icon_osk(g, r, color):
    """The ROCKNIX on-screen keyboard toggle - like icon_keyboard's cheat
    sheet glyph, but with a small pop-up caret above it so it reads as a
    toggle rather than a reference card."""
    x, y, w, h = r
    t = max(4, w * 0.06)
    g.stroke_round_rect((x + w * 0.02, y + h * 0.32, w * 0.96, h * 0.56), h * 0.1, color, t)
    for row in range(2):
        for col in range(5):
            g.round_rect((x + w * (0.13 + col * 0.16), y + h * (0.44 + row * 0.16),
                          w * 0.09, h * 0.09), 2, color)
    g.round_rect((x + w * 0.26, y + h * 0.76, w * 0.48, h * 0.07), 2, color)
    g.polygon([(x + w * 0.42, y), (x + w * 0.58, y), (x + w * 0.5, y + h * 0.16)], color)


def icon_sleep(g, r, color):
    """YT4: a crescent moon (Sleep tile) - a solid circle with a smaller
    circle "cut out" of it in the tile's own background colour, the same
    trick icon_home uses for its door cutout."""
    x, y, w, h = r
    cx, cy, rad = x + w * 0.5, y + h * 0.5, w * 0.34
    g.circle(cx, cy, rad, color)
    g.circle(cx + rad * 0.6, cy - rad * 0.5, rad * 0.85, THEME["tile"])


def icon_eye(g, r, color):
    """Tile customisation: a tile is currently shown."""
    x, y, w, h = r
    cx, cy = x + w / 2, y + h / 2
    g.stroke_round_rect((x + w * 0.06, y + h * 0.34, w * 0.88, h * 0.32), h * 0.16, color,
                        max(3, w * 0.07))
    g.circle(cx, cy, w * 0.13, color)


def icon_eye_off(g, r, color):
    """Tile customisation: a tile is currently hidden - the same eye, with
    a diagonal bar through it."""
    icon_eye(g, r, color)
    x, y, w, h = r
    t = max(4, w * 0.09)
    g.polygon([(x + w * 0.16, y + h * 0.16 - t / 2), (x + w * 0.16 + t, y + h * 0.16 + t / 2),
              (x + w * 0.84 + t, y + h * 0.84 - t / 2), (x + w * 0.84, y + h * 0.84 + t / 2)],
              color)


def draw_battery(g, r, percent, charging, color):
    """Battery glyph with fill level; r is the body rect incl. the nub."""
    x, y, w, h = r
    nub = w * 0.08
    body = (x, y, w - nub, h)
    g.stroke_round_rect(body, 8, color, 5)
    g.round_rect((x + w - nub, y + h * 0.3, nub, h * 0.4), 3, color)
    if percent is not None:
        p = max(0, min(100, percent)) / 100.0
        fillc = THEME["ok"] if percent > 30 else THEME["warn"] if percent > 15 else THEME["danger"]
        g.round_rect((x + 8, y + 8, max(0.0, (w - nub - 16) * p), h - 16), 4, fillc)
    if charging:
        cx, cy = x + (w - nub) / 2, y + h / 2
        g.polygon([(cx + 4, y + 4), (cx - 14, cy + 3), (cx - 1, cy + 3), (cx - 5, y + h - 4),
                   (cx + 14, cy - 3), (cx + 1, cy - 3)], THEME["text"])


# ---------------------------------------------------------------------------
# Formatting (pure)
# ---------------------------------------------------------------------------
NA = "—"       # em dash: "unknown" - never rendered as 0


def fmt_num(v, fmt, suffix=""):
    if v is None:
        return NA
    try:
        return (fmt % v) + suffix
    except (TypeError, ValueError):
        return NA


def fmt_uptime(s):
    if s is None:
        return NA
    d, rem = divmod(int(s), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%d d %d h %d min" % (d, h, m)
    if h:
        return "%d h %d min" % (h, m)
    return "%d min" % m


def volume_readout(master):
    """Text for the bar's readout from an audio.get_master() dict."""
    if master is None:
        return "…"
    st = master.get("state")
    if st == "restoring":
        return "Restoring"
    if st != "ok" or master.get("volume") is None:
        return "Audio error"
    return "%d%%" % int(round(master["volume"] * 100))


def _max_temp(temps, prefix):
    if not temps:
        return None
    vals = [v for k, v in temps.items() if k.startswith(prefix) and v is not None]
    return max(vals) if vals else None


def _cluster_row(c):
    cpus = c.get("cpus") or []
    title = "CPU %d" % cpus[0] if len(cpus) == 1 else \
        "CPU %d-%d" % (cpus[0], cpus[-1]) if cpus else "CPU"
    cur, mx, gov = c.get("cur_mhz"), c.get("max_mhz"), c.get("governor")
    val = "%s / %s MHz" % (fmt_num(cur, "%d"), fmt_num(mx, "%d"))
    if gov:
        val += "  " + gov
    return title, val


def hud_rows(s):
    """Three columns of (title, value) from a hud.sample() dict. Every
    top-level hud field appears; None renders as an em dash, never 0."""
    s = s or {}
    status = s.get("battery_status")
    batt = fmt_num(s.get("battery_percent"), "%d", "%")
    charger = s.get("charger_online")
    power = [
        ("Battery", batt),
        ("Status", status or NA),
        ("Power", fmt_num(s.get("battery_power_w"), "%.2f", " W")),
        ("Voltage", fmt_num(s.get("battery_voltage_v"), "%.2f", " V")),
        ("Current", fmt_num(s.get("battery_current_a"), "%.2f", " A")),
        ("Battery temp", fmt_num(s.get("battery_temp_c"), "%.1f", " °C")),
        ("Charger", NA if charger is None else ("Connected" if charger else "Not connected")),
    ]
    clusters = s.get("cpu_clusters") or []
    perf = [_cluster_row(c) for c in clusters[:3]]
    while len(perf) < 3:
        perf.append(("CPU", NA))
    load = s.get("gpu_load_percent")
    perf.append(("GPU", "%s MHz  load %s" % (fmt_num(s.get("gpu_mhz"), "%d"),
                                            fmt_num(load, "%.0f", "%"))))
    temps = s.get("temps_c")
    perf.append(("CPU temp (max)", fmt_num(_max_temp(temps, "cpu"), "%.1f", " °C")))
    perf.append(("GPU temp (max)", fmt_num(_max_temp(temps, "gpu"), "%.1f", " °C")))
    perf.append(("Fan", "%s rpm  PWM %s" % (fmt_num(s.get("fan_rpm"), "%d"),
                                          fmt_num(s.get("fan_pwm"), "%d"))))
    tot, avail = s.get("ram_total_mb"), s.get("ram_available_mb")
    ram = NA if tot is None or avail is None else \
        "%.1f / %.1f GB used" % ((tot - avail) / 1024.0, tot / 1024.0)
    stot, sfree = s.get("storage_total_gb"), s.get("storage_free_gb")
    sto = NA if stot is None or sfree is None else "%.0f GB free of %.0f" % (sfree, stot)
    system = [
        ("RAM", ram),
        ("Storage", sto),
        ("Wi-Fi", s.get("wifi_ssid") or NA),
        ("Signal", fmt_num(s.get("wifi_signal_dbm"), "%d", " dBm")),
        ("IP address", s.get("ip_address") or NA),
        ("Uptime", fmt_uptime(s.get("uptime_s"))),
        ("Running game", s.get("running_game") or NA),
    ]
    return [("Power", power), ("Performance", perf), ("System", system)]


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------
class BatteryButton(Button):
    def __init__(self, on_click, name):
        Button.__init__(self, "", on_click=on_click, name=name)
        self.percent = None
        self.charging = False

    def set_battery(self, percent, charging):
        if (percent, charging) != (self.percent, self.charging):
            self.percent, self.charging = percent, charging
            self.invalidate()

    def draw(self, g):
        bg, fg = self.colors()
        g.round_rect(self.rect, self.radius, bg)
        x, y, w, h = self.rect
        draw_battery(g, (x + 20, y + (h - 50) / 2, 96, 50), self.percent, self.charging, fg)
        g.text(fmt_num(self.percent, "%d", "%"), (x + 126, y, w - 136, h), 48, fg, True, "center")


class Bar(Container):
    """The volume strip. In FULL mode (top row of the Command Center) and in
    BAR mode without an app: mute, slider, readout, battery, clock. In BAR
    mode with an app (HF1, set_app()): mute, slider, readout, then that
    app's controls instead of battery and clock."""

    APP_W = 150                 # an app control, px (the bar's targets are 120 px tall)
    TABS_W = 110                # CC6: the Tabs button
    TV_W = 120                  # W2: the leanback D-pad - 6 of them, so kept at the 120 px floor
    TV_ACTION_W = 260           # YT3: Close / Home - icon + label, wider than the bare D-pad keys

    def __init__(self, h):
        Container.__init__(self, name="bar", bg="bar")
        self.mute = self.add(Toggle(name="bar.mute", on_toggle=h.on_mute_toggle,
                                    text_on="", text_off="",
                                    icon_on=icon_speaker_muted, icon_off=icon_speaker_on))
        # on_cancel: a stroke that became a swipe (or any cancel) puts the
        # volume back and commits nothing (RV1-M1).
        self.slider = self.add(Slider(name="bar.slider", on_change=h.on_volume_drag,
                                      on_release=h.on_volume_release, knob_r=48,
                                      on_cancel=getattr(h, "on_volume_cancel", None)))
        self.readout = self.add(Label("…", size=56, bold=True, align="center",
                                      name="bar.readout"))
        self.battery = self.add(BatteryButton(h.on_battery, "bar.battery"))
        self.clock = self.add(Label("", size=56, bold=True, align="center", name="bar.clock"))
        self.slider.hit_pad = 10
        # HF1 app controls; each tap is handlers.on_app_action("<name>")
        act = getattr(h, "on_app_action", None)

        def ctl(key, text="", icon=None, cls=Button):
            return self.add(cls(text, on_click=(lambda k=key: act(k)) if act else None,
                                name="bar.%s" % key, size=40, icon=icon))
        self.web_back = ctl("web.back", icon=ui.icon_back)
        self.web_reload = ctl("web.reload", icon=icon_reload)
        self.web_home = ctl("web.home", icon=icon_home)
        self.web_keys = ctl("web.keys", icon=icon_keyboard, cls=LitButton)
        self.web_close = ctl("web.close", icon=icon_close)
        # W2: the YouTube TV (leanback) tile's D-pad - text labels, not icons,
        # same minimal pattern the old "yt" mpv strip's -10 s/+10 s used (that
        # strip is gone, YT3 - the old mpv-based "YouTube" tile). Sent as key
        # presses (Browser.send_key(), WebDriver:PerformActions), never a
        # page-specific control: leanback only takes arrow/Enter/Back.
        self.tv_left = ctl("tv.left", "←")
        self.tv_up = ctl("tv.up", "↑")
        self.tv_down = ctl("tv.down", "↓")
        self.tv_right = ctl("tv.right", "→")
        self.tv_ok = ctl("tv.ok", "OK")
        self.tv_back = ctl("tv.back", "Back")
        self._tv_btns = (self.tv_left, self.tv_up, self.tv_down, self.tv_right, self.tv_ok,
                         self.tv_back)
        # YT3 (owner feedback: "no way to kill the app or tab out of it"):
        # Home parks the tile (same park-not-close app_tabs.py already does
        # for Browser/Discord) and shows the Command Center directly - a
        # one-tap action, not the multi-app Tabs picker (bar.tabs, below)
        # the owner never recognised; Close actually ends the session. Both
        # get an icon AND a label (Button draws both when given both) so
        # neither reads as a mystery icon the way the bare Tabs button did.
        self.tv_home = ctl("tv.home", "Home", icon=icon_home)
        self.tv_close = ctl("tv.close", "Close", icon=icon_close)
        # CC6 (app_tabs.py): the tab strip does not fit in 140 px, so the strip
        # gets one Tabs button: park this app's window, open the Command Center
        tabs = getattr(h, "show_app_tabs", None)
        self.tabs_btn = self.add(Button("", on_click=tabs, name="bar.tabs", size=40,
                                        icon=icon_tabs)) if tabs else None
        self.hint = self.add(Label("", size=48, bold=True, align="center", color="warn",
                                   name="bar.hint"))
        self.app = None             # None | "web" | "tv" (shown only when compact)
        self.compact = False        # set by the Command Center: BAR mode
        self.keys_offered = True    # osk mode "off" drops the Keyboard button
        self.hinting = False
        # AH: the YouTube TV app's auto-hidden strip - CommandCenter.layout()
        # sets this (from the rect it hands down: shorter than BAR_H can only
        # mean the surface itself shrank to screens.HANDLE_H, main.py's
        # geometry()) and every control - including mute/slider/readout,
        # never just the per-app ones - disappears in favour of one drawn
        # grab handle (draw()). A tap anywhere in that handle still reaches
        # main.App (there is nothing else on the surface to hit), which is
        # all bar_autohide.BarAutoHide.touch() needs to bring the strip back.
        self.handle_only = False
        self._apply_app_visibility()

    def _tabs_ctl(self):
        return [self.tabs_btn] if self.tabs_btn is not None else []

    def _all_controls(self):
        """AH: every child this Bar can show, for the handle-only state
        (_apply_app_visibility) where none of them are - not even mute/
        slider/readout, unlike every other path through this class."""
        out = [self.mute, self.slider, self.readout, self.battery, self.clock, self.hint,
               self.web_back, self.web_reload, self.web_home, self.web_keys, self.web_close,
               self.tv_home, self.tv_close]
        out.extend(self._tv_btns)
        if self.tabs_btn is not None:
            out.append(self.tabs_btn)
        return out

    def _cell_w(self, c):
        """CC6: the Tabs button is narrower, so the volume slider keeps
        >= 600 px next to the browser's controls (HF1's floor)."""
        if c in (self.tv_home, self.tv_close):
            return self.TV_ACTION_W
        if c in self._tv_btns:
            return self.TV_W
        return self.TABS_W if c is self.tabs_btn else self.APP_W

    def web_controls(self):
        out = self._tabs_ctl() + [self.web_back, self.web_reload, self.web_home]
        if self.keys_offered:
            out.append(self.web_keys)
        return out + [self.web_close]

    def tv_controls(self):
        """YT3: Home first (same "escape hatch first" convention the Tabs
        button uses for web/yt), then the D-pad, then Close last - no Tabs
        button on this row at all (see set_app()'s own note on why)."""
        return [self.tv_home] + list(self._tv_btns) + [self.tv_close]

    def shown_app(self):
        return self.app if self.compact else None

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        # AH: h < BAR_H can only happen while CommandCenter.layout() has
        # shrunk the whole surface to screens.HANDLE_H for the auto-hidden
        # YouTube TV strip - every normal BAR app always gets exactly BAR_H.
        self.handle_only = self.compact and h < BAR_H
        if self.handle_only:
            self._apply_app_visibility()
            return
        inner = (x + 10, y + 10, w - 20, h - 20)
        app = self.shown_app()
        if app == "web":
            ctrls = self.web_controls()
            cells = ui.hsplit(inner, [150, None, 160] + [self._cell_w(c) for c in ctrls], gap=16)
            for c, r in zip(ctrls, cells[3:]):
                c.set_rect(r)
        elif app == "tv":
            # YT3: no % readout cell here (0 px, still gapped like every
            # other cell) - Close/Home plus the 6-button D-pad need the
            # width instead; see set_app()'s note on the resulting slider
            # width and _apply_app_visibility() for hiding the label itself.
            ctrls = self.tv_controls()
            cells = ui.hsplit(inner, [150, None, 0] + [self._cell_w(c) for c in ctrls], gap=16)
            for c, r in zip(ctrls, cells[3:]):
                c.set_rect(r)
        else:
            cells = ui.hsplit(inner, [150, None, 200, 280, 180], gap=20)
            self.battery.set_rect(cells[3])
            self.clock.set_rect(cells[4])
        m, sl, ro = cells[:3]
        self.mute.set_rect(m)
        self.slider.set_rect(sl)
        self.readout.set_rect(ro)
        self.hint.set_rect(ui.rect_union(sl, ro))
        self._apply_app_visibility()

    def _apply_app_visibility(self):
        if self.handle_only:
            for c in self._all_controls():
                c.set_visible(False)
            return
        app = self.shown_app()
        self.battery.set_visible(app is None)
        self.clock.set_visible(app is None)
        web = set(map(id, self.web_controls())) if app == "web" else set()
        for c in (self.web_back, self.web_reload, self.web_home, self.web_keys, self.web_close):
            c.set_visible(id(c) in web)
        for c in self._tv_btns:
            c.set_visible(app == "tv")
        self.tv_home.set_visible(app == "tv")
        self.tv_close.set_visible(app == "tv")
        if self.tabs_btn is not None:
            self.tabs_btn.set_visible(app == "web")
        self.hint.set_visible(self.hinting)
        self.slider.set_visible(not self.hinting)
        # YT3: the tv row drops the % readout entirely (see layout()'s own
        # note) to make room for Close/Home alongside the 6-button D-pad.
        self.readout.set_visible(not self.hinting and app != "tv")

    def set_app(self, app, keys_offered=None):
        if keys_offered is not None:
            self.keys_offered = bool(keys_offered)
        self.app = app
        if app != "web":
            self.web_keys.set_lit(False)
        if self.rect[2]:
            self.layout(self.rect)
        else:
            self._apply_app_visibility()

    def set_keys_lit(self, on):
        self.web_keys.set_lit(on)

    def show_hint(self, text):
        """A short message over the volume area (e.g. "Tap the page first").
        Never while a finger is on the slider: that would cancel its drag."""
        if self.slider.dragging:
            return False
        self.hint.set_text(text)
        self.hinting = True
        self._apply_app_visibility()
        return True

    def clear_hint(self):
        if self.hinting:
            self.hinting = False
            self._apply_app_visibility()

    def draw(self, g):
        if self.handle_only:
            bg = self.bg_color()
            if bg is not None:
                g.fill_rect(self.rect, bg)
            x, y, w, h = self.rect
            pill_w = min(160, max(60, w // 4))
            pill_h = max(6, min(10, h - 8))
            g.round_rect((x + (w - pill_w) / 2.0, y + (h - pill_h) / 2.0, pill_w, pill_h),
                        pill_h / 2.0, THEME["line"])
            return
        Container.draw(self, g)
        x, y, w, h = self.rect
        g.fill_rect((x, y, w, 2), THEME["line"])

    def set_master(self, master, force=False):
        """Apply an audio.get_master() dict. Returns False if the slider was
        being dragged and kept its value."""
        ok = master is not None and master.get("state") == "ok" and master.get("volume") is not None
        # A bare THEME key ("text"/"warn"), not the resolved tuple: this
        # runs on every audio poll REGARDLESS of whether the volume itself
        # changed while `ok` stays the same, so on a device the readout
        # can sit for a long time between calls that would otherwise
        # refresh a frozen colour - a runtime theme change must not have
        # to wait for a volume change to be picked up here (found on the
        # device: the readout stayed white on RP5 White, a light theme,
        # because it happened to be set once under the dark Default theme
        # and then never again while the volume held steady).
        self.readout.set_text(volume_readout(master), "text" if ok else "warn")
        self.slider.set_enabled(ok)
        self.mute.set_enabled(ok and master.get("muted") is not None)
        moved = True
        if ok:
            moved = self.slider.set_value(min(1.0, master["volume"]))
            if master.get("muted") is not None:
                self.mute.set_state(master["muted"])
        return moved

    def show_percent(self, v):
        self.readout.set_text("%d%%" % int(round(v * 100)), "text")

    def tick_clock(self):
        self.clock.set_text(time.strftime("%H:%M"))


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
def _make_tile_editable(t, grid):
    """Wrap ONE Tile's on_press/on_move/on_release/on_cancel so it takes
    part in Home's tile-customisation mode, without changing the Tile
    class itself (swap_ui.swap_tile()'s returned Tile gets exactly the
    same wrapping as one built here - no subclass, no swap_ui.py edit).

    NORMAL mode (grid.edit_mode False): behaves exactly like a plain Tile
    (the wrapped functions call through to the ones it starts with), plus
    a long-press timer armed on press via grid.h.call_later() - the same
    timer idiom main.App already uses for its own auto-close countdown and
    the RGB keeper poll. The timer is cancelled the moment the finger
    leaves the tile or is released/cancelled, so a normal tap is
    unaffected; if it fires, grid.enter_edit() runs and the pending
    release is swallowed (no navigation on the same touch that just
    entered edit mode).

    EDIT mode (grid.edit_mode True): a fresh press-and-drag (a NEW touch,
    not a continuation of the one that triggered the long-press) picks the
    tile up and moves it with the finger; grid._drag_move() reflows every
    OTHER tile live as the dragged one crosses into a new cell, and
    release/cancel calls grid._drag_end() to snap it into place and
    persist. A second finger touching a different tile while one is
    already being dragged is ignored, the same "a second finger does not
    steal" rule ui.Slider already uses."""
    orig_press, orig_move = t.on_press, t.on_move
    orig_release, orig_cancel = t.on_release, t.on_cancel
    st = {"pid": None, "timer": None, "long_fired": False, "dx": 0.0, "dy": 0.0}

    def _cancel_timer():
        if st["timer"] is not None:
            grid.h.cancel(st["timer"])
            st["timer"] = None

    def _fire_long_press(pid):
        if st["pid"] != pid or grid.edit_mode or not t.pressed:
            return
        st["timer"] = None
        st["long_fired"] = True
        grid.enter_edit()

    def on_press(pid, x, y):
        if grid.edit_mode:
            if grid.dragging_tile not in (None, t):
                return                              # a second finger does not steal it
            st["pid"] = pid
            st["dx"], st["dy"] = x - t.rect[0], y - t.rect[1]
            grid.dragging_tile = t
            grid._raise_tile(t)
            t._set_pressed(True)
            return
        orig_press(pid, x, y)
        st["pid"] = pid
        st["long_fired"] = False
        st["timer"] = grid.h.call_later(EDIT_LONG_PRESS_S, lambda: _fire_long_press(pid))

    def on_move(pid, x, y):
        if grid.edit_mode:
            if grid.dragging_tile is t and pid == st["pid"]:
                t.set_rect((x - st["dx"], y - st["dy"], t.rect[2], t.rect[3]))
                grid._drag_move(t)
            return
        orig_move(pid, x, y)
        if st["timer"] is not None and not t.pressed:
            _cancel_timer()

    def on_release(pid, x, y):
        if grid.edit_mode:
            if grid.dragging_tile is t and pid == st["pid"]:
                st["pid"] = None
                grid._drag_end(t)
            return
        _cancel_timer()
        if st["long_fired"]:
            st["long_fired"] = False
            t._set_pressed(False)
            st["pid"] = None
            return
        orig_release(pid, x, y)
        st["pid"] = None

    def on_cancel(pid):
        if grid.edit_mode:
            if grid.dragging_tile is t and pid == st["pid"]:
                st["pid"] = None
                grid._drag_end(t)
            return
        _cancel_timer()
        st["long_fired"] = False
        orig_cancel(pid)
        st["pid"] = None

    t.on_press, t.on_move = on_press, on_move
    t.on_release, t.on_cancel = on_release, on_cancel


class Home(Container):
    def __init__(self, h, title="Command Center"):
        Container.__init__(self, name="home", bg="bg")
        self.h = h                      # tile customisation: call_later/cancel + on_tile_layout_changed
        self._title_text = title
        self.close = self.add(Button("Close", on_click=getattr(h, "close_command_center", None),
                                     name="home.close", size=40, icon=icon_close_up))
        self.title = self.add(Label(title, size=52, bold=True, name="home.title"))
        self.sub = self.add(Label("", size=40, color="dim", align="right",
                                  name="home.sink"))
        # Tile customisation ("Edit tiles" mode): Done / Reset replace
        # Close / the sink label in the header row while active.
        self.edit_mode = False
        self.dragging_tile = None
        self.edit_done = self.add(Button("Done", on_click=self.exit_edit,
                                         name="home.edit.done", size=44))
        self.edit_reset = self.add(Button("Reset tile layout", on_click=self.reset_layout,
                                          name="home.edit.reset", size=32))
        self.edit_done.visible = False
        self.edit_reset.visible = False
        # CC5 (hidden_overlay.py): over an emulator's second screen the home
        # grid shrinks to OVERLAY_TILES plus a big Close and a one-line note.
        self.overlay = False
        self.overlay_tiles = OVERLAY_TILES
        self.overlay_close = self.add(Tile("Close", on_click=getattr(h, "close_command_center",
                                                                     None),
                                           name="home.overlay_close", icon=icon_close_up,
                                           subtitle="Back to the game"))
        self.overlay_note = self.add(Label(
            "The game keeps running. Close gives its touch screen back.", size=40,
            color="dim", align="center", name="home.overlay_note"))
        self.overlay_close.visible = self.overlay_note.visible = False
        # Banner strip above the tile grid - a persistent-until-fixed
        # warning, never a tile itself (it must not change grid_cols()/tile
        # order/hidden-tiles persistence, and it must show without the
        # owner hunting for it). One shared Label + one shared Button:
        # _refresh_banner() decides which registered notice (if any) is
        # showing right now - CHG (charge_stuck_view.py) always outranks DS
        # (dualscreen_keys_view.py), the owner's own rule for when both are
        # active at once. Hidden by default; every setter's "" always hides
        # it again, so an idle Home (every test that never raises a notice)
        # lays out exactly as before either feature existed.
        self._notice_text = {"charge": "", "dualscreen": ""}
        self.banner_label = self.add(Label("", size=32, color="warn", align="left",
                                           name="home.banner_label"))
        self.banner_action = self.add(Button("", name="home.banner_action", size=34))
        self.banner_label.visible = False
        self.banner_action.visible = False
        self.tiles = [
            self.add(Tile("Mixer", on_click=h.open_mixer, name="home.mixer", icon=icon_mixer,
                          subtitle="Per-app volume")),
            self.add(Tile("HUD", on_click=h.open_hud, name="home.hud", icon=icon_gauge,
                          subtitle="Device info")),
            self.add(Tile("Browser", on_click=h.open_browser, name="home.browser",
                          icon=icon_globe, subtitle="Firefox, on this screen")),
            self.add(Tile("Discord", on_click=getattr(h, "open_discord", None),
                          name="home.discord", icon=icon_chat, subtitle="Web app, QR login")),
            # W2b: its own Firefox profile/process (never Discord's/Browser's -
            # see web_tiles.YtAppSession's own doc) - a QR code on the tile's
            # own page signs the owner in on their phone.
            self.add(Tile("YouTube App", on_click=getattr(h, "open_ytapp", None),
                          name="home.ytapp", icon=icon_ytapp, subtitle="Sign in with a QR code")),
            # CC7: reads combos from ROCKNIX source + this device's own
            # retroarch.cfg/system.cfg where readable (hotkeys.py) - not a
            # remote control, so it stays out of OVERLAY_TILES like Settings.
            self.add(Tile("Hotkeys", on_click=getattr(h, "open_hotkeys", None),
                          name="home.hotkeys", icon=icon_keyboard, subtitle="Button cheat sheet")),
            # CC1 (cleanstate_view.py): stop the game / Browser / YouTube and
            # check ES; everything it stops is listed in its confirm first.
            # Not in OVERLAY_TILES: over a running game it would close that game.
            self.add(Tile("Clean state", on_click=getattr(h, "open_clean_state", None),
                          name="home.clean", icon=icon_reload,
                          subtitle="Close apps, check ES")),
            self.add(Tile("Settings", on_click=getattr(h, "open_settings", None),
                          name="home.settings", icon=icon_gear, subtitle="Companion, screens")),
            self.add(swap_ui.swap_tile(h)),                 # SW1
            # RG: rgb_view.RGBController.open() - the 10th tile.
            self.add(Tile("Stick lights", on_click=getattr(h, "open_lights", None),
                          name="home.lights", icon=icon_stick_lights,
                          subtitle="Thumb stick RGB")),
            # Keyboard: an immediate toggle (rocknix_keyboard.py), not a
            # sheet - tapping it signals ROCKNIX's own touchkeyboard.service
            # instance of wvkbd-mobintl directly, then closes the Command
            # Center so the keys reach the emulator/ES underneath. Part of
            # the tile-customisation set like every other tile here.
            self.add(Tile("Keyboard", on_click=getattr(h, "toggle_keyboard", None),
                          name="home.keyboard", icon=icon_osk,
                          subtitle="Type into emulator settings")),
            # YT4 (owner-approved): main.App.open_sleep() shows a brief
            # "Sleeping..." hint, then runs `systemctl suspend` off the UI
            # thread (system_sleep.py) after a short delay so this tap's own
            # release animation gets to paint first.
            self.add(Tile("Sleep", on_click=getattr(h, "open_sleep", None),
                          name="home.sleep", icon=icon_sleep,
                          subtitle="Suspend the device")),
        ]
        self._tiles_by_name = {t.name: t for t in self.tiles}
        # Order and hidden set: main.py pushes the real, persisted values in
        # right after building the UI (config.py's "command_center.tile_order"
        # / "hidden_tiles" - see set_order()/set_hidden()); construction order
        # is the default/fallback until then. "home.settings" can never be
        # hidden - enforced again here even though config.py's schema already
        # refuses to store it, so a bug in the persistence path cannot lock
        # the owner out either.
        self.order = [t.name for t in self.tiles]
        self.hidden = set()
        # One hide/show badge per hideable tile, added AFTER every tile so
        # Container.hit()'s topmost-first search finds the badge before the
        # tile underneath it (tapping the badge must never also start a
        # drag). Hidden outside edit mode.
        self.badges = {}
        for t in self.tiles:
            if t.name == "home.settings":
                continue
            b = self.add(Button("", on_click=(lambda n=t.name: self._toggle_hidden(n)),
                                name=t.name + ".hide", size=1, icon=icon_eye, radius=60))
            b.visible = False
            self.badges[t.name] = b
        for t in self.tiles:
            _make_tile_editable(t, self)

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        edit = self.edit_mode
        self.close.set_rect((x + 30, y + 10, 240, 120))
        self.close.set_visible(not edit)
        self.edit_done.set_rect((x + 30, y + 10, 240, 120))
        self.edit_done.set_visible(edit)
        self.title.set_rect((x + 300, y + 10, w - 780, 120))
        self.title.set_text("Edit tiles" if edit else self._title_text)
        self.sub.set_rect((x + w - 460, y + 10, 420, 120))
        self.sub.set_visible(not edit)
        self.edit_reset.set_rect((x + w - 460, y + 10, 420, 120))
        self.edit_reset.set_visible(edit)
        if self.overlay:
            self._layout_overlay(rect)
            return
        if self.banner_label.visible:
            by = y + 150
            if self.banner_action.visible:
                self.banner_action.set_rect((x + w - 400, by, 370, DS_BANNER_H))
                self.banner_label.set_rect((x + 30, by, w - 440, DS_BANNER_H))
            else:
                self.banner_label.set_rect((x + 30, by, w - 60, DS_BANNER_H))
        self._relayout_tiles()

    def visible_tiles(self):
        """Every tile in EDIT mode (a hidden one must still be reachable to
        be shown again), or just the non-hidden ones in NORMAL mode - both
        in the current persisted/live order."""
        names = self.order if self.edit_mode else [n for n in self.order if n not in self.hidden]
        return [self._tiles_by_name[n] for n in names if n in self._tiles_by_name]

    def grid_cols(self):
        """I2: two rows, at least four columns, one more column per two tiles
        past eight (now of whatever is actually shown right now - hiding
        tiles in NORMAL mode gives the rest more room, same math)."""
        return max(HOME_MIN_COLS, -(-len(self.visible_tiles()) // HOME_ROWS))

    def _grid_rect(self):
        x, y, w, h = self.rect
        # The banner (when shown) takes its own band just above the grid,
        # on top of the fixed 150 px header - the grid never overlaps it,
        # and it costs nothing when the banner is hidden (the default).
        top = 150 + (DS_BANNER_H + 20 if self.banner_label.visible else 0)
        return (x + 30, y + top, w - 60, h - top - 30)

    def _relayout_tiles(self):
        if not self.rect[2]:
            return
        cols = self.grid_cols()
        shown = self.visible_tiles()
        cells = ui.grid(self._grid_rect(), cols, HOME_ROWS, 30)
        narrow = cols > HOME_MIN_COLS
        shown_set = set(shown)
        for t in self.tiles:
            t.set_visible(t in shown_set)
        for t, c in zip(shown, cells):
            t.size = TILE_TEXT_NARROW if narrow else TILE_TEXT
            t.subtitle_size = TILE_SUB_NARROW if narrow else ui.BODY_TEXT
            if t is not self.dragging_tile:          # a drag owns its own rect live
                t.set_rect(c)
            self._layout_badge(t, t.rect if t is self.dragging_tile else c)
        self.invalidate()

    def _layout_badge(self, t, rect):
        b = self.badges.get(t.name)
        if b is None:
            return
        x, y, w, h = rect
        b.set_rect((x + w - TILE_BADGE - 8, y + 8, TILE_BADGE, TILE_BADGE))

    def _update_badge(self, name):
        b = self.badges.get(name)
        if b is not None:
            b.icon = icon_eye_off if name in self.hidden else icon_eye
            b.invalidate()

    # -- entering / leaving edit mode ---------------------------------------
    def enter_edit(self):
        if self.edit_mode or self.overlay:
            return
        self.edit_mode = True
        self.dragging_tile = None
        for name in self.badges:
            self._update_badge(name)
        for b in self.badges.values():
            b.set_visible(True)
        if self.rect[2]:
            self.layout(self.rect)

    def exit_edit(self):
        if not self.edit_mode:
            return
        self.edit_mode = False
        self.dragging_tile = None
        for b in self.badges.values():
            b.set_visible(False)
        if self.rect[2]:
            self.layout(self.rect)
        self._notify_layout_changed()

    def reset_layout(self):
        """Back to construction order, nothing hidden."""
        self.order = [t.name for t in self.tiles]
        self.hidden = set()
        for name in self.badges:
            self._update_badge(name)
        self._relayout_tiles()
        self._notify_layout_changed()

    def _toggle_hidden(self, name):
        if name == "home.settings":            # can never be hidden - see __init__
            return
        if name in self.hidden:
            self.hidden.discard(name)
        else:
            self.hidden.add(name)
        self._update_badge(name)
        self._relayout_tiles()
        self._notify_layout_changed()

    def _notify_layout_changed(self):
        cb = getattr(self.h, "on_tile_layout_changed", None)
        if cb is not None:
            cb(list(self.order), sorted(self.hidden))

    # -- persisted state in, from main.py at startup -------------------------
    def set_order(self, order):
        known = set(self._tiles_by_name)
        cleaned = [n for n in order if n in known]
        cleaned += [n for n in self.order if n not in cleaned]   # a new/unknown tile is appended
        self.order = cleaned
        self._relayout_tiles()

    def set_hidden(self, hidden):
        self.hidden = set(n for n in hidden if n in self._tiles_by_name and n != "home.settings")
        for name in self.badges:
            self._update_badge(name)
        self._relayout_tiles()

    # -- the shared banner: CHG always outranks DS ----------------------------
    # key -> (button text, main.App callback attribute, shown during CC5's
    # OVERLAY too). Order is priority order: the first key in _NOTICE_ORDER
    # with non-empty text wins the one shared banner slot - the owner's own
    # rule for "the charge one takes priority if both are active".
    _NOTICE_ORDER = ("charge", "dualscreen")
    _NOTICE_ACTION = {
        "charge": ("Dismiss", "on_charge_notice_dismiss", True),
        "dualscreen": ("Restore", "on_dualscreen_restore", False),
    }

    def _set_notice(self, key, text):
        text = text or ""
        if text == self._notice_text[key]:
            return
        self._notice_text[key] = text
        self._refresh_banner()

    def set_charge_notice(self, text):
        """charge_stuck_view.ChargeStuckController: "" hides it; any other
        text names the "charger connected but not charging" condition, with
        a Dismiss button - re-arming itself (on the next plug-in) is the
        controller's own job, not this widget's. Shown over CC5's OVERLAY
        too: a battery draining mid-game matters more than the emulator's
        own minimal tile set, unlike DS below."""
        self._set_notice("charge", text)

    def set_dualscreen_notice(self, text):
        """dualscreen_keys_view.DualScreenKeysController: "" hides it; any
        other text names the missing system.cfg keys, with a Restore
        button - never as a tile. Never shown during CC5's OVERLAY (it
        would invite a system.cfg edit while a game/emulator owns essway's
        cgroup) and never shown while an active charge notice outranks it."""
        self._set_notice("dualscreen", text)

    def _refresh_banner(self):
        text = action = cb = None
        in_overlay = False
        for key in self._NOTICE_ORDER:
            if self._notice_text[key]:
                text = self._notice_text[key]
                action, attr, in_overlay = self._NOTICE_ACTION[key]
                cb = getattr(self.h, attr, None)
                break
        text = text or ""
        self.banner_label.set_text(text)
        self.banner_action.text = action or ""
        self.banner_action.on_click = cb
        self.banner_action.invalidate()
        show = bool(text) and (in_overlay or not self.overlay)
        self.banner_label.set_visible(show)
        self.banner_action.set_visible(show and bool(action))
        if self.rect[2]:
            self.layout(self.rect)

    # -- dragging (EDIT mode only - see _make_tile_editable()) ---------------
    def _raise_tile(self, t):
        if t in self.children:
            self.children.remove(t)
            self.children.append(t)
        b = self.badges.get(t.name)
        if b is not None:
            b.set_visible(False)          # its own badge steps aside while it moves
        self.invalidate()

    def _drag_move(self, t):
        cols = self.grid_cols()
        cells = ui.grid(self._grid_rect(), cols, HOME_ROWS, 30)
        if not cells:
            return
        cx, cy, cw, ch = t.rect
        center = (cx + cw / 2.0, cy + ch / 2.0)

        def dist(c):
            ccx, ccy = c[0] + c[2] / 2.0, c[1] + c[3] / 2.0
            return (ccx - center[0]) ** 2 + (ccy - center[1]) ** 2

        idx = min(range(len(cells)), key=lambda i: dist(cells[i]))
        names = list(self.order)
        idx = min(idx, len(names) - 1)
        cur = names.index(t.name)
        if idx != cur:
            names.pop(cur)
            names.insert(idx, t.name)
            self.order = names
            self._relayout_tiles()

    def _drag_end(self, t):
        self.dragging_tile = None
        t._set_pressed(False)
        b = self.badges.get(t.name)
        if b is not None and self.edit_mode:
            b.set_visible(True)
        self._relayout_tiles()             # snaps t into its final cell too
        self._notify_layout_changed()

    # -- CC5 --------------------------------------------------------------
    def overlay_shown_tiles(self):
        return [t for t in self.tiles if t.name in self.overlay_tiles] + [self.overlay_close]

    def _layout_overlay(self, rect):
        x, y, w, h = rect
        row_h = max(0, min(420, h - 300))
        shown = self.overlay_shown_tiles()
        for t, c in zip(shown, ui.grid((x + 30, y + 150, w - 60, row_h), len(shown), 1, 30)):
            t.set_rect(c)
        self.overlay_note.set_rect((x + 30, y + 150 + row_h + 30, w - 60, 70))

    def set_overlay(self, on):
        """CC5: only the tiles that belong over a running game (per-app
        volume, device info) and a big Close; everything back when off."""
        on = bool(on)
        if on == self.overlay:
            return
        self.overlay = on
        for t in self.tiles:
            t.set_visible(t.name in self.overlay_tiles if on else True)
        self.overlay_close.set_visible(on)
        self.overlay_note.set_visible(on)
        self._refresh_banner()      # CHG may still show over OVERLAY; DS never does
        if self.rect[2]:
            self.layout(self.rect)
        self.invalidate()


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------
class HudSheet(Sheet):
    def __init__(self, h):
        Sheet.__init__(self, "Device", on_close=h.close_sheet, name="hud")
        self.cols = []          # [(heading Label, [(title Label, value Label)])]
        for heading, rows in hud_rows({}):
            hl = self.body.add(Label(heading, size=40, bold=True, color="accent"))
            cells = []
            for title, value in rows:
                tl = self.body.add(Label(title, size=36, color="dim"))
                vl = self.body.add(Label(value, size=44, bold=True))
                cells.append((tl, vl))
            self.cols.append((hl, cells))
        self.samples = 0

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        colrects = ui.hsplit((bx + 40, by + 10, bw - 80, bh - 20), [None] * 3, gap=40)
        for (hl, cells), (cx, cy, cw, ch) in zip(self.cols, colrects):
            hl.set_rect((cx, cy, cw, 56))
            rowh = (ch - 60) // max(1, len(cells))
            for i, (tl, vl) in enumerate(cells):
                ry = cy + 60 + i * rowh
                tl.set_rect((cx, ry, cw, 40))
                vl.set_rect((cx, ry + 40, cw, rowh - 42))

    def set_sample(self, s):
        self.samples += 1
        for (hl, cells), (heading, rows) in zip(self.cols, hud_rows(s)):
            for (tl, vl), (title, value) in zip(cells, rows):
                tl.set_text(title)
                # A bare THEME key (see Bar.set_master()'s comment above
                # for why a resolved tuple would freeze here too) - the
                # HUD only calls this on its own poll tick, independent of
                # any theme change.
                vl.set_text(value, "dim" if value == NA else "text")


class StreamRow(Container):
    def __init__(self, stream, h):
        Container.__init__(self, name="mixer.row.%s" % stream["id"], bg="panel")
        self.stream_id = stream["id"]
        sid = self.stream_id
        self.label = self.add(Label(stream.get("display_name") or "?", size=44, bold=True))
        self.slider = self.add(Slider(
            name="mixer.slider.%s" % sid, knob_r=44,
            on_change=lambda v: (self._pct(v), h.on_stream_drag(sid, v)),
            on_release=lambda v: h.on_stream_release(sid, v),
            on_cancel=lambda v0, changed: self._cancelled(h, v0, changed)))
        self.pct = self.add(Label("", size=44, bold=True, align="center"))
        self.mute = self.add(Toggle(name="mixer.mute.%s" % sid,
                                    on_toggle=lambda st: h.on_stream_mute(sid, st),
                                    text_on="", text_off="",
                                    icon_on=icon_speaker_muted, icon_off=icon_speaker_on))
        self.update(stream)

    def _pct(self, v):
        self.pct.set_text("%d%%" % int(round(v * 100)))

    def _cancelled(self, h, v0, changed):
        """The press ended without a commit (RV1-M1): put the readout back
        and let the controller undo any live change."""
        if changed:
            self._pct(v0)
        cb = getattr(h, "on_stream_cancel", None)
        if cb is not None:
            cb(self.stream_id, v0, changed)

    def update(self, stream):
        self.label.set_text(stream.get("display_name") or "?")
        vol, muted = stream.get("volume"), stream.get("muted")
        self.slider.set_enabled(vol is not None)
        if vol is not None:
            self.slider.set_value(min(1.0, vol))
            self.pct.set_text("%d%%" % int(round(vol * 100)))
        else:
            self.pct.set_text(NA)
        self.mute.set_enabled(muted is not None)
        if muted is not None:
            self.mute.set_state(muted)

    def layout(self, rect):
        self.set_rect(rect)
        n, s, p, m = ui.hsplit(ui.inset(rect, 16, 8), [480, None, 150, 150], gap=20)
        self.label.set_rect(n)
        self.slider.set_rect(s)
        self.pct.set_rect(p)
        self.mute.set_rect(m)

    def draw(self, g):
        g.round_rect(self.rect, 20, THEME["panel"])
        for c in self.children:
            c.paint(g)


class MixerSheet(Sheet):
    """Per-app streams, MAX_ROWS a page. More streams than that get Prev /
    Next in the header (I1: B1 showed only "and N more")."""
    ROW_H = 150
    MAX_ROWS = 5

    def __init__(self, h):
        Sheet.__init__(self, "Mixer", on_close=h.close_sheet, name="mixer")
        self.h = h
        self.msg = self.body.add(Label("Reading audio streams…", size=48, bold=True,
                                       align="center", name="mixer.message"))
        self.msg2 = self.body.add(Label("", size=36, color="dim", align="center"))
        self.more = self.body.add(Label("", size=36, color="dim", align="center"))
        self.prev = self.add(Button("Prev", on_click=lambda: self.turn(-1), name="mixer.prev",
                                    size=40))
        self.next = self.add(Button("Next", on_click=lambda: self.turn(1), name="mixer.next",
                                    size=40))
        self.prev.visible = self.next.visible = False
        self.rows = {}
        self.streams = "loading"
        self.page = 0

    def dragging(self):
        return any(r.slider.dragging for r in self.rows.values())

    def pages(self):
        n = len(self.streams) if isinstance(self.streams, list) else 0
        return max(1, (n + self.MAX_ROWS - 1) // self.MAX_ROWS)

    def turn(self, d):
        new = self.page + d
        if 0 <= new < self.pages() and not self.dragging():
            self.page = new
            self.set_streams(self.streams)

    def set_streams(self, streams):
        """streams: list from audio.list_streams(), None if pw-dump failed."""
        self.streams = streams
        if streams is None:
            self.msg.set_text("Could not read audio streams")
            self.msg2.set_text("pw-dump failed; will retry on the next audio event.")
        elif not streams:
            self.msg.set_text("No app is playing audio right now")
            self.msg2.set_text("Games and videos appear here while they make sound.")
        else:
            self.msg.set_text("")
            self.msg2.set_text("")
        self.page = max(0, min(self.page, self.pages() - 1))
        first = self.page * self.MAX_ROWS
        want = {s["id"]: s for s in (streams or [])[first:first + self.MAX_ROWS]}
        for sid in list(self.rows):
            if sid not in want:
                self.body.remove(self.rows.pop(sid))
        for sid, s in want.items():
            if sid in self.rows:
                self.rows[sid].update(s)
            else:
                self.rows[sid] = self.body.add(StreamRow(s, self.h))
        n = self.pages()
        self.title.set_text("Mixer" if n == 1 else "Mixer  %d / %d" % (self.page + 1, n))
        self.prev.set_visible(n > 1)
        self.next.set_visible(n > 1)
        self.prev.set_enabled(self.page > 0)
        self.next.set_enabled(self.page < n - 1)
        self.more.set_text("")
        self.layout(self.rect)

    def layout(self, rect):
        Sheet.layout(self, rect)
        x, y, w, h = rect
        self.title.set_rect((x + 320, y, max(0, w - 980), self.HEADER_H))
        self.prev.set_rect((x + w - 640, y + 10, 300, self.HEADER_H - 20))
        self.next.set_rect((x + w - 320, y + 10, 300, self.HEADER_H - 20))
        bx, by, bw, bh = self.body.rect
        self.msg.set_rect((bx, by + bh * 0.3, bw, 80))
        self.msg2.set_rect((bx, by + bh * 0.3 + 90, bw, 60))
        for i, row in enumerate(self.rows.values()):
            row.layout((bx + 30, by + 10 + i * self.ROW_H, bw - 60, self.ROW_H - 16))
        self.more.set_rect((bx, by + 10 + self.MAX_ROWS * self.ROW_H, bw, 50))


class LaunchSheet(Sheet):
    """What the Command Center shows while an app starts (Firefox takes a few
    seconds to open a window) or why it did not: a headline, two lines, and
    one action button (Cancel / Close), which goes to
    handlers.on_app_action("launch.action")."""

    def __init__(self, h):
        Sheet.__init__(self, "", on_close=h.close_sheet, name="launch")
        act = getattr(h, "on_app_action", None)
        self.big = self.body.add(Label("", size=64, bold=True, align="center",
                                       name="launch.big"))
        self.small = self.body.add(Label("", size=40, color="dim", align="center",
                                         name="launch.small"))
        self.small2 = self.body.add(Label("", size=36, color="faint", align="center",
                                          name="launch.small2"))
        self.action = self.body.add(Button("Cancel", name="launch.action", size=44,
                                           on_click=(lambda: act("launch.action")) if act else None))
        self.action.visible = False

    def show(self, title, big, small="", small2="", action=None):
        self.title.set_text(title)
        self.big.set_text(big)
        self.small.set_text(small)
        self.small2.set_text(small2)
        if action:
            self.action.text = action
            self.action.invalidate()
        self.action.set_visible(bool(action))

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        top = by + int(bh * 0.18)
        self.big.set_rect((bx, top, bw, 100))
        self.small.set_rect((bx + 60, top + 120, bw - 120, 60))
        self.small2.set_rect((bx + 60, top + 190, bw - 120, 54))
        self.action.set_rect((bx + (bw - 420) // 2, top + 290, 420, 130))


# ---------------------------------------------------------------------------
# The whole UI
# ---------------------------------------------------------------------------
class CommandCenter(Container):
    """The Command Center: volume strip, HOME tile grid and its sheets, as
    ONE self-contained view, pulled down over the companion view.

    FULL mode: the volume strip is at the TOP (volume is the owner's top
    priority, and it is the first thing under the finger after a pull-down),
    home / hud / mixer / launch / youtube fill the rest, and the settings
    sheet covers the whole panel. BAR mode (compact): only the strip, filling
    the surface, with the running app's controls (set_bar_app)."""

    def __init__(self, handlers, title="Command Center", settings=None, hotkeys=None):
        Container.__init__(self, name="cc")
        self.title = title
        self.home = self.add(Home(handlers, title))
        self.hud = self.add(HudSheet(handlers))
        self.mixer = self.add(MixerSheet(handlers))
        self.launch = self.add(LaunchSheet(handlers))
        self.confirm = self.add(swap_ui.ConfirmSheet(handlers))     # SW1 (and CC1)
        self.bar = self.add(Bar(handlers))
        # W: BAR mode's Tabs button shows the app strip inline in the bar's
        # own row instead of always parking + opening Home. app_tabs.py owns
        # the tab list shown there (a Home pill is appended - the escape
        # hatch to Settings / Mixer, replacing the old park-and-open-Home
        # single tap) and calls close_bar_tabs() once a tap starts a switch.
        self.bar_tabs_open = False
        if self.bar.tabs_btn is not None:
            self.bar.tabs_btn.on_click = self.toggle_bar_tabs
        self.sheets = {"hud": self.hud, "mixer": self.mixer, "launch": self.launch,
                       swap_ui.CONFIRM_SHEET: self.confirm}
        self.settings = None
        if settings is not None:
            self.settings = self.add(settings)
            self.sheets["settings"] = settings
        # CC7: hotkeys_view.HotkeysSheet - same optional-extra-sheet shape as
        # `settings` just above (owned by CC7, not this file).
        self.hotkeys = None
        if hotkeys is not None:
            self.hotkeys = self.add(hotkeys)
            self.sheets["hotkeys"] = hotkeys
        self.sheet = None           # name of the open sheet, or None for home
        self.compact = False        # BAR mode: only the strip fits

    @property
    def screen(self):
        return self.sheet or "home"

    tabs = None                     # CC6: app_tabs.TabStrip, under the volume strip
    # CC6: the sheets that keep the tab strip above them. The others (mixer,
    # launch, youtube, ...) have fixed row / keyboard heights and use the
    # full area under the volume strip, as before.
    TABBED = ("hud",)

    def set_tabs(self, strip):
        """CC6: the app tab strip (app_tabs.py) - between the volume strip
        and home / the sheets in FULL mode; hidden in BAR mode and under the
        full-panel settings sheet."""
        if self.tabs is not None:
            self.remove(self.tabs)
        self.tabs = self.add(strip)
        if self.rect[2]:
            self.layout(self.rect)
        return strip

    def set_bar_app(self, app, keys_offered=None):
        """What the BAR strip controls: None, "web" or "yt" (HF1)."""
        self.bar.set_app(app, keys_offered)

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        self.compact = h <= BAR_H + 10
        if not self.compact:
            self.bar_tabs_open = False      # W: the inline picker only exists in BAR mode
        self.bar.compact = self.compact
        # AH: h < BAR_H while compact can only be the YouTube TV app's
        # auto-hidden strip (main.py's geometry() shrinks the whole surface
        # to screens.HANDLE_H, exclusive zone 0) - the bar IS the (already
        # small) rect handed down, not a BAR_H-tall strip pinned to its
        # bottom. Every other BAR app always arrives here with h >= BAR_H.
        if self.compact and h < BAR_H:
            bar_rect = (x, y, w, h)
        else:
            bar_rect = (x, y + h - BAR_H, w, BAR_H) if self.compact else (x, y, w, BAR_H)
        self.bar.layout(bar_rect)
        content = (x, y + BAR_H, w, max(0, h - BAR_H))
        tabbed = content                # CC6: home and TABBED sheets sit under the tab strip
        if self.tabs is not None:
            if self.compact and self.bar_tabs_open:
                self.tabs.layout(bar_rect)              # W: inline, the bar's own row
            elif not self.compact:
                th = self.tabs.HEIGHT
                self.tabs.layout((x, y + BAR_H, w, th))
                tabbed = (x, y + BAR_H + th, w, max(0, h - BAR_H - th))
        self.home.layout(tabbed)
        for name, s in self.sheets.items():
            s.layout(rect if name == "settings" else (tabbed if name in self.TABBED else content))
        self._apply_visibility()

    def _apply_visibility(self):
        if self.tabs is not None:
            self.tabs.set_visible((not self.compact and (self.sheet is None
                                                         or self.sheet in self.TABBED))
                                  or (self.compact and self.bar_tabs_open))
        self.home.set_visible(not self.compact and self.sheet is None)
        for name, s in self.sheets.items():
            s.set_visible(not self.compact and self.sheet == name)
        # the full-panel settings sheet hides the strip; everything else keeps it
        # (W: in BAR mode the inline picker takes the bar's own place instead)
        self.bar.set_visible((self.compact and not self.bar_tabs_open)
                             or (not self.compact and self.sheet != "settings"))

    def toggle_bar_tabs(self):
        """W: BAR mode's Tabs button - shows the app tab strip inline in the
        bar's own 140 px row, in place of the volume/app controls, so
        Browser / Discord / YouTube can jump straight to another running app
        (or tap the strip's Home pill for Settings / Mixer) without first
        parking to the full Command Center. The button itself is part of the
        bar it replaces, so it disappears once shown; a tap on any tab there
        (app_tabs.TabsController.select) is what closes it again, via
        close_bar_tabs()."""
        if not self.compact or self.tabs is None:
            return
        self.bar_tabs_open = not self.bar_tabs_open
        self._tabs_reopened()
        if self.rect[2]:
            self.layout(self.rect)

    def close_bar_tabs(self):
        """W: app_tabs.TabsController.select() calls this once a tap starts
        a real switch, so picking e.g. Browser while Discord is showing (mode
        stays BAR throughout) does not leave the inline strip stuck open."""
        if self.bar_tabs_open:
            self.bar_tabs_open = False
            self._tabs_reopened()
            if self.rect[2]:
                self.layout(self.rect)

    def _tabs_reopened(self):
        """W: let app_tabs.py's TabStrip.on_open (if wired) refresh its tab
        list (the Home pill) right after bar_tabs_open flips, before the
        strip is laid out inline / restored."""
        on_open = getattr(self.tabs, "on_open", None)
        if on_open is not None:
            on_open()

    def add_sheet(self, name, sheet):
        """CC1: a sheet owned by another module (cleanstate_view), laid out
        and shown like hud / mixer (the content area under the strip)."""
        if name in self.sheets:
            raise ValueError("sheet %r exists" % name)
        self.add(sheet)
        self.sheets[name] = sheet
        if self.rect[2]:
            self.layout(self.rect)
        return sheet

    def open(self, sheet):
        if sheet not in self.sheets:
            raise ValueError(sheet)
        self.sheet = sheet
        self._apply_visibility()

    def close(self):
        self.sheet = None
        self._apply_visibility()

    def set_overlay(self, on):
        """CC5: the Command Center over an emulator's second screen - the
        strip (volume first) stays, home shows only OVERLAY_TILES + Close."""
        self.home.set_overlay(on)

    @property
    def overlay(self):
        return self.home.overlay


class DeckUI:
    """The widget tree root: the companion view (the default, when given)
    with the Command Center above it. Without a companion (B1's shape, and
    the tests that predate Navigation v2) the Command Center is the only view.

    view: "companion" or "cc" - which one FULL mode shows. BAR mode (compact)
    always shows just the Command Center's strip."""

    def __init__(self, handlers, w, h, title="Command Center", companion=None,
                 settings=None, hotkeys=None):
        self.root = ui.Root(w, h, bg="bg")
        self.companion = self.root.add(companion) if companion is not None else None
        self.cc = self.root.add(CommandCenter(handlers, title, settings, hotkeys=hotkeys))
        self.view = "cc"
        self.set_size(w, h)

    # the controller talks to the Command Center's parts directly
    title = property(lambda self: self.cc.title)
    home = property(lambda self: self.cc.home)
    hud = property(lambda self: self.cc.hud)
    mixer = property(lambda self: self.cc.mixer)
    launch = property(lambda self: self.cc.launch)
    confirm = property(lambda self: self.cc.confirm)
    bar = property(lambda self: self.cc.bar)
    sheets = property(lambda self: self.cc.sheets)
    sheet = property(lambda self: self.cc.sheet)
    screen = property(lambda self: self.cc.screen)
    compact = property(lambda self: self.cc.compact)

    def open(self, sheet):
        self.cc.open(sheet)

    def close(self):
        self.cc.close()

    settings = property(lambda self: self.cc.settings)
    hotkeys = property(lambda self: self.cc.hotkeys)

    def add_sheet(self, name, sheet):
        """CC1: see CommandCenter.add_sheet."""
        self.cc.add_sheet(name, sheet)
        self.root.damage_all()
        return sheet

    def set_size(self, w, h):
        self.root.resize(w, h)
        self.cc.layout((0, 0, w, h))
        if self.companion is not None:
            self.companion.layout((0, 0, w, h))
        self._apply_view()
        self.root.damage_all()

    def set_view(self, view):
        """"companion" or "cc" (what FULL mode shows)."""
        if view not in ("companion", "cc"):
            raise ValueError(view)
        if view == "companion" and self.companion is None:
            view = "cc"
        self.view = view
        self._apply_view()

    def _apply_view(self):
        comp = self.companion is not None and self.view == "companion" and not self.cc.compact
        if self.companion is not None:
            self.companion.set_visible(comp)
        self.cc.set_visible(not comp)

    @property
    def showing(self):
        """What is on screen: "bar", "companion" or "cc"."""
        if self.cc.compact:
            return "bar"
        return "companion" if (self.companion is not None and self.view == "companion") else "cc"

    def show_command_center(self, visible):
        self.cc.set_visible(visible)

    def set_overlay(self, on):
        """CC5 (hidden_overlay.py): see CommandCenter.set_overlay."""
        self.cc.set_overlay(on)

    def targets(self):
        """Visible interactive widgets by name -> [x, y, w, h] (for
        state.json, so a shell test can click what it means to click)."""
        out = {}
        for wdg in self.root.walk():
            if wdg.name and wdg.interactive and wdg.shown() and wdg.enabled:
                out[wdg.name] = [int(v) for v in wdg.rect]
                if isinstance(wdg, Slider):
                    x0, x1 = wdg.track()
                    out[wdg.name + ".track"] = [int(round(x0)), int(round(x1)),
                                                int(wdg.rect[1] + wdg.rect[3] // 2)]
        return out
