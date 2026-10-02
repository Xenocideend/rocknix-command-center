"""settings_view: the Settings sheet, built from config.SCHEMA and opened by the gear in the
Command Center.

Touch first at 1920x1080: body text 36-44 px and every control at least ~120 px both ways.
ui has no scroll container, so overflow is handled by paging, with a row of group tabs plus
Prev/Next inside a group when its fields dont fit on one page. media_priority gets its own
reorder pages (up/down buttons) instead of a normal row.

A control only gets built for a SCHEMA field that's in config.WIRED, so every setting on
screen actually does something. A key outside WIRED gets no control, and a group with no WIRED
field gets no tab. Screens has one control, "Swap screens", a Toggle over the
screens.es_screen enum (on = "builtin_bottom", see _TOGGLE_ENUMS), and the derived
screens.command_center_screen never gets one. SettingsSheet.controls maps key_path to its
control, so a test catches a WIRED key with no UI and a control for a key outside WIRED.
Every change (toggle, slider release, cycle tap, reorder) validates through config.set_value()
and then calls on_change(key_path, value). Changing screens.es_screen or
screens.command_center_screen flips the other (they have to stay on opposite outputs) and
fires on_change for both.

Two widgets ui.py doesnt have live here: CycleButton (a tap steps through an enum's values)
and MediaPriorityPage (a reorderable list with up/down per row).
"""
import device
import config
import screen_map
import palettes
import button_colours
import screen_presets
import version
import ui
from ui import THEME, Button, Container, Label, Sheet, Slider, Toggle, Widget

# -- layout constants, every touch target sized for the 1920x1080 panel -----
ROW_H = 120
ROW_GAP = 12
ROWS_PER_PAGE = 5  # normal (non-reorder) fields per page
REORDER_PAGE_SIZE = 4  # media_priority items per reorder page
TAB_H = 120
FOOTER_H = 120
MARGIN = 30
GAP = 16

BOOL_CTRL_W = 160
ENUM_CTRL_W = 420
SLIDER_CTRL_W = 460
READOUT_W = 140
HINT_W = 320  # wide enough for "after restart" without an ellipsis

# A group with no WIRED field gets no tab by construction, not a special case, so a group that
# loses its last consumer disappears and one that gains one comes back.
GROUP_ORDER = [g for g in config.GROUPS
               if g not in screen_map.CURRENT.unsupported_groups()
               and any(f["key_path"] in config.WIRED for f in config.fields_in_group(g))] + ["about"]
GROUP_TITLES = dict(config.GROUP_LABELS)
GROUP_TITLES["about"] = "About"

# The tab strip splits the sheet width by len(GROUP_ORDER) and "Command Center" got cut to
# "Command C...". The full names stay in GROUP_TITLES/config.GROUP_LABELS, only the tab button
# shows the short form.
TAB_TITLES = dict(GROUP_TITLES)
TAB_TITLES["command_center"] = "Command"

_ACRONYMS = {"es": "ES", "pdfjs": "PDF.js", "btn": None, "hud": "HUD"}


def _display(value):
    """"follow_es" -> "Follow ES", "btn_c_paddle" -> "C Paddle" ..."""
    if not isinstance(value, str):
        return str(value)
    words = []
    for w in value.split("_"):
        rep = _ACRONYMS.get(w, w.capitalize())
        if rep:
            words.append(rep)
    return " ".join(words) or value


_IN_GAME_DISPLAY_LABELS = {
    "art": "Game art", "dim": "Dim", "off": "Off (blank)", "manual": "Manual",
    "hud": "HUD (device stats)", "clock": "Clock", "slideshow": "Slideshow (system art)",
}


def _in_game_display_display(value):
    """companion.in_game_display reads better with a few words per choice than the plain
    underscore split gives ("Off (blank)" is what ES-DE Companion does, see
    research/CC3-esde-companion.md).
    """
    return _IN_GAME_DISPLAY_LABELS.get(value, _display(value))


def _hardware_button_display(value):
    """This RP5 has no rear paddles (0 evdev events for either paddle code in 120 s). The paddle
    values stay valid since a saved file might be from another device, but they're labelled so
    picking one here doesnt look like a working choice.
    """
    base = _display(value)
    if value in config.PADDLE_HARDWARE_BUTTONS:
        return base + " (no rear paddles on RP5)"
    return {"btn_back_f1": "Back button", "none": "None (swipe only)"}.get(value, base)


# per-field override of CycleButton's display(), only hardware_button needs one (the paddle
# labels above), the rest use _display()
_ENUM_DISPLAY_OVERRIDES = {
    ("command_center", "hardware_button"): _hardware_button_display,
    ("companion", "in_game_display"): _in_game_display_display,
    ("steam", "in_game_display"): lambda value: ("Same as other games" if value == "same"
                                                  else _in_game_display_display(value)),
}

# A two-valued enum shown as an on/off Toggle instead of a cycle button: key_path -> (value when
# off, value when on). "Swap screens" on puts ES and games on the built-in panel and the
# Command Center on the add-on, off is the normal layout. It's one toggle so the two screens
# cant be picked separately.
_TOGGLE_ENUMS = {
    ("screens", "es_screen"): ("addon_top", "builtin_bottom"),
}

# which corner of the panel gets the small tap target while an emulator's second screen owns
# it, registered separately from the dict above so other additions dont collide with it
_CORNER_HANDLE_LABELS = {
    "off": "Off", "top-left": "Top left", "top-right": "Top right",
    "bottom-left": "Bottom left", "bottom-right": "Bottom right",
}
_ENUM_DISPLAY_OVERRIDES[("command_center", "corner_handle")] = \
    lambda value: _CORNER_HANDLE_LABELS.get(value, _display(value))

# Appearance labels come from palettes.display_label(), the same place `palettes.py --list`
# reads, since splitting the preset name on underscores ("dreamcast" -> "Dreamcast") didnt
# match the descriptive labels ("White/orange swirl").
_ENUM_DISPLAY_OVERRIDES[("appearance", "theme_preset")] = palettes.display_label
_ENUM_DISPLAY_OVERRIDES[("appearance", "button_colours")] = button_colours.label
_ENUM_DISPLAY_OVERRIDES[("screens", "ui_resolution")] = screen_presets.label


# ---------------------------------------------------------------------------
# Widgets ui.py doesnt have
# ---------------------------------------------------------------------------
# About: the version, credits, and that the program is free.
ABOUT_FREE = "Free software under the GNU GPL v2. If you paid for this, who hurt you?"
ABOUT_CREDITS = (
    "Made by Xenocideend, written with Claude (Anthropic)",
    "Runs on ROCKNIX, with SDL3, cairo, poppler and mpv",
    "Source: github.com/Xenocideend/rocknix-command-center",
    "Not an official ROCKNIX project. The ROCKNIX team didnt make it, review it or endorse it.",
    "If something breaks, tell Xenocideend and not the ROCKNIX team.",
)
ABOUT_SUPPORT = (
    "Want to support this project? Dont.",
    "Give your money to Trans Lifeline instead:",
)
ABOUT_SUPPORT_LINK = "translifeline.org/donate"
PRIDE_FLAGS = (
    ("Trans pride", ("#5BCEFA", "#F5A9B8", "#FFFFFF", "#F5A9B8", "#5BCEFA")),
    ("Lesbian pride", ("#D52D00", "#FF9A56", "#FFFFFF", "#D362A4", "#A30262")),
)


def _hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


class PrideFlags(Widget):
    """The flags side by side at 5:3, as tall as the row."""

    def __init__(self, flags=PRIDE_FLAGS, name=None):
        Widget.__init__(self, name=name)
        self.flags = flags
        self.text = " and ".join(n for n, _ in flags) + " flags"

    def draw(self, g):
        x, y, w, h = self.rect
        fw = h * 5 // 3
        for i, (_, stripes) in enumerate(self.flags):
            fx = x + i * (fw + GAP * 2)
            sh = h / float(len(stripes))
            for j, col in enumerate(stripes):
                g.fill_rect((fx, y + j * sh, fw, sh + 1), _hex_rgb(col))
def whats_new_lines():
    return version.whats_new()


def readout_text(field, v):
    """A setting's number the way a person reads it, with its unit (the charge limit shows "85%",
    not "85/80").
    """
    key = field["key_path"][-1]
    if key.endswith("_pct") or key in ("bottom_brightness", "top_brightness"):
        return "%d%%" % round(v)
    if field["key_path"] == ("companion", "background_dim"):
        return "%d%%" % round(v * 100)
    if field["key_path"] == ("lights", "brightness"):
        return "%d%%" % round(v * 100 / 255.0)
    if key.endswith("_ms"):
        return ("%.1f s" % (v / 1000.0)).replace(".0 s", " s")
    if key.endswith("_s"):
        return "%d s" % v
    if field["type"] == "float":
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


class CycleButton(Button):
    """A tap steps to the next allowed value (wraps around). Shows the current value, and
    on_pick(new_value) fires on a finished tap.
    """

    def __init__(self, values, value, rect=(0, 0, 0, 0), on_pick=None, name=None,
                display=None, size=36):
        Button.__init__(self, "", rect, None, name, size)
        self.values = list(values)
        self.value = value
        self.on_pick = on_pick
        self.display = display or _display
        self.text = self.display(self.value)

    def clicked(self):
        i = self.values.index(self.value) if self.value in self.values else -1
        self.value = self.values[(i + 1) % len(self.values)]
        self.text = self.display(self.value)
        self.invalidate()
        if self.on_pick:
            self.on_pick(self.value)

    def set_value(self, v):
        if v != self.value:
            self.value = v
            self.text = self.display(v)
            self.invalidate()


class MediaPriorityPage(Container):
    """One reorder page: `count` ranks in a row of companion.media_priority, each with Up/Down
    buttons that swap against the full list, so a move at a page edge reaches the item on the
    next page.

    Each reorder page has a header naming the setting and which page of it this is. Without one
    a reorder page looked like any other list of rows and nothing said "this is the media order",
    so the cartridge and boxback rows on the last page seemed to be missing.
    """

    def __init__(self, field, offset, count, get_value, commit, name=None):
        Container.__init__(self, name=name or ("settings.media.%d" % offset))
        self.field = field
        self.offset = offset
        self.count = count
        self.get_value = get_value
        self.commit = commit
        self.header = self.add(Label("", size=36, bold=True, color="text",
                                     name="%s.header" % self.name))
        self.rows = []
        for i in range(count):
            lbl = self.add(Label("", size=40, bold=True))
            up = self.add(Button("Up", name="%s.up.%d" % (self.name, i), size=32,
                                 on_click=lambda i=i: self._move(i, -1)))
            down = self.add(Button("Down", name="%s.down.%d" % (self.name, i), size=32,
                                   on_click=lambda i=i: self._move(i, 1)))
            self.rows.append((lbl, up, down))
        self.refresh_from_cfg()

    def _move(self, local_i, d):
        order = list(self.get_value())
        gi = self.offset + local_i
        gj = gi + d
        if 0 <= gi < len(order) and 0 <= gj < len(order):
            order[gi], order[gj] = order[gj], order[gi]
            self.commit(order)
            self.refresh_from_cfg()

    def refresh_from_cfg(self):
        order = self.get_value()
        n = len(order)
        lo = min(self.offset + 1, n)
        hi = min(self.offset + self.count, n)
        self.header.set_text("%s  (%d-%d of %d)" % (self.field["label"], lo, hi, n))
        for local_i, (lbl, up, down) in enumerate(self.rows):
            gi = self.offset + local_i
            if gi < n:
                lbl.set_text("%d. %s" % (gi + 1, _display(order[gi])))
                up.set_enabled(gi > 0)
                down.set_enabled(gi < n - 1)
                lbl.set_visible(True)
                up.set_visible(True)
                down.set_visible(True)
            else:
                lbl.set_visible(False)
                up.set_visible(False)
                down.set_visible(False)

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        self.header.set_rect((x, y, w, ROW_H))
        y0 = y + ROW_H + ROW_GAP
        for i, (lbl, up, down) in enumerate(self.rows):
            ry = y0 + i * (ROW_H + ROW_GAP)
            lbl.set_rect((x, ry, w - 340, ROW_H))
            up.set_rect((x + w - 320, ry, 150, ROW_H))
            down.set_rect((x + w - 160, ry, 150, ROW_H))


class AppearanceExtraRow(Container):
    """The Appearance group's one non-schema row, two plain buttons the same ROW_H as a FieldRow
    so it lays out like one in _layout_current_page(). "Edit custom colours..." can always be
    tapped, not just on Custom, since tapping it is what switches to Custom
    (appearance_view.AppearanceController.open()).
    """

    def __init__(self, on_edit, on_reset, name="settings.appearance.extra"):
        Container.__init__(self, name=name)
        self.edit_btn = self.add(Button("Edit custom colours...", name=name + ".edit",
                                        size=36, on_click=on_edit))
        self.reset_btn = self.add(Button("Reset to default", name=name + ".reset",
                                         size=36, on_click=on_reset))

    def layout(self, rect):
        self.set_rect(rect)
        left, right = ui.hsplit(rect, [None, None], gap=GAP)
        self.edit_btn.set_rect(left)
        self.reset_btn.set_rect(right)


def es_restart_label(pending, game_running, armed, busy=False):
    """(text, tappable) for the ES restart row. ES loads its button icons only when it starts."""
    if busy:
        return "Restarting ES...", False
    if not pending:
        return "ES is showing these buttons", False
    if game_running:
        return "Close the game to restart ES", False
    if armed:
        return "Tap again to restart ES", True
    return "Restart ES to show the new buttons", True


class EsRestartRow(Container):
    """Restarts ES after a button colour change so the new icons show. Same ROW_H as a FieldRow."""

    def __init__(self, on_restart, name="settings.appearance.restart_es"):
        Container.__init__(self, name=name)
        self.button = self.add(Button("", name=name + ".button", size=36, on_click=on_restart))
        self.show(False, False, False)

    def show(self, pending, game_running, armed, busy=False):
        text, tappable = es_restart_label(pending, game_running, armed, busy)
        if text != self.button.text:
            self.button.text = text
            self.button.invalidate()
        self.button.set_enabled(tappable)

    def layout(self, rect):
        self.set_rect(rect)
        self.button.set_rect(rect)


class CommandCenterExtraRow(Container):
    """Home can already reorder and hide tiles in Edit tiles mode, but only a long press got there.
    This row opens that mode from Settings. Not a schema field, same ROW_H as a FieldRow.
    """

    def __init__(self, on_edit_tiles, name="settings.command_center.extra"):
        Container.__init__(self, name=name)
        self.edit_btn = self.add(Button("Edit buttons: reorder, hide or show...",
                                        name=name + ".edit", size=36, on_click=on_edit_tiles))

    def layout(self, rect):
        self.set_rect(rect)
        self.edit_btn.set_rect(rect)


class FieldRow(Container):
    """A label plus one field's control (Toggle / Slider / CycleButton), and a restart hint when the
    field needs one.
    """

    def __init__(self, field, get_value, commit, name=None):
        Container.__init__(self, name=name or ("settings.row.%s" % ".".join(field["key_path"])))
        self.field = field
        self.get_value = get_value
        self.commit = commit
        self.label = self.add(Label(field["label"], size=40, color="text"))
        # The restart hint is also a short-lived note area (set_note()/clear_note()), an unavailable or
        # write failed message replaces it for a bit, same as bar.show_hint() does.
        self._restart_note = "after restart" if field["restart"] else ""
        self.hint = self.add(Label(self._restart_note, size=28, color="warn"))
        self.readout = None
        self.control = self._build_control()
        self.add(self.control)

    def set_note(self, text, warn=True):
        # A bare THEME key, not a resolved THEME["warn"]/THEME["dim"]. Label.set_text() keeps whatever
        # colour it's given until the next call, so a resolved tuple would stop following a theme
        # change from the moment the hint was last set.
        self.hint.set_text(text, "warn" if warn else "dim")

    def clear_note(self):
        self.hint.set_text(self._restart_note, "warn")

    def _cname(self):
        return self.name + ".control"

    def _toggle_values(self):
        return _TOGGLE_ENUMS.get(self.field["key_path"])

    def _is_toggle(self):
        return self.field["type"] == "bool" or self._toggle_values() is not None

    def _commit_toggle_enum(self, state):
        off, on = self._toggle_values()
        self.commit(on if state else off)

    def _build_control(self):
        f = self.field
        v = self.get_value()
        tv = self._toggle_values()
        if tv is not None:
            return Toggle(state=(v == tv[1]), on_toggle=self._commit_toggle_enum,
                          name=self._cname())
        if f["type"] == "bool":
            return Toggle(state=bool(v), on_toggle=self.commit, name=self._cname())
        if f["type"] in ("int", "float"):
            lo, hi = f["range"]
            self.readout = self.add(Label(self._fmt(v), size=36, bold=True, align="right"))
            frac = 0.0 if hi == lo else (float(v) - lo) / (hi - lo)
            return Slider(value=frac, step=0.01, knob_r=40, name=self._cname(),
                         on_change=self._on_slider_change, on_release=self._on_slider_release)
        if f["type"] == "enum":
            display = _ENUM_DISPLAY_OVERRIDES.get(f["key_path"])
            return CycleButton(f["values"], v, on_pick=self.commit, name=self._cname(),
                               display=display)
        raise ValueError("FieldRow cannot render schema type %r" % f["type"])

    def _fmt(self, v):
        # command_center.auto_close_timeout_s: 0 means it never auto-closes, a bare "0" reads like a
        # broken value on a settings screen
        if self.field["key_path"] == ("command_center", "auto_close_timeout_s") and v == 0:
            return "Never"
        return readout_text(self.field, v)

    def _raw_from_frac(self, frac):
        lo, hi = self.field["range"]
        raw = lo + frac * (hi - lo)
        if self.field["type"] == "int":
            raw = int(round(raw))
            step = self.field.get("step")
            if step:
                # battery.safe_charge_end_pct (50-95 step 5), snap to the nearest step instead of a bare 1%
                # slider
                raw = lo + int(round((raw - lo) / float(step))) * step
            return max(lo, min(hi, raw))
        return round(max(lo, min(hi, raw)), 3)

    def _on_slider_change(self, frac):
        self.readout.set_text(self._fmt(self._raw_from_frac(frac)))

    def _on_slider_release(self, frac):
        raw = self._raw_from_frac(frac)
        self.readout.set_text(self._fmt(raw))
        self.commit(raw)

    def refresh_from_cfg(self):
        f, v = self.field, self.get_value()
        tv = self._toggle_values()
        if tv is not None:
            self.control.set_state(v == tv[1])
        elif f["type"] == "bool":
            self.control.set_state(bool(v))
        elif f["type"] in ("int", "float"):
            lo, hi = f["range"]
            frac = 0.0 if hi == lo else (float(v) - lo) / (hi - lo)
            self.control.set_value(frac)
            self.readout.set_text(self._fmt(v))
        elif f["type"] == "enum":
            self.control.set_value(v)

    def layout(self, rect):
        self.set_rect(rect)
        ctrl_w = (BOOL_CTRL_W if self._is_toggle() else
                 SLIDER_CTRL_W if self.field["type"] in ("int", "float") else ENUM_CTRL_W)
        readout_w = READOUT_W if self.readout else 0
        label_r, hint_r, ro_r, ctrl_r = ui.hsplit(rect, [None, HINT_W, readout_w, ctrl_w], gap=GAP)
        self.label.set_rect(label_r)
        self.hint.set_rect(hint_r)
        if self.readout:
            self.readout.set_rect(ro_r)
        self.control.set_rect(ctrl_r)


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------
class SettingsSheet(Sheet):
    """Built entirely from config.SCHEMA. `controls` maps a schema key_path to its widget (a
    FieldRow's Toggle/Slider/CycleButton, or the MediaPriorityPage for the one array field), and
    tests check it covers every SCHEMA entry.
    """

    def __init__(self, cfg, on_change, on_close, device_name=None, app_version="dev", name="settings",
                on_edit_colours=None, on_reset_appearance=None, on_edit_tiles=None, on_restart_es=None):
        Sheet.__init__(self, "Settings", on_close=on_close, name=name)
        self.cfg = cfg
        self.on_change = on_change
        # Appearance's two buttons arent schema fields, the sheet just calls out to main.py. "Edit
        # custom colours..." opens appearance_view.AppearanceSheet and "Reset to default" restores every
        # appearance.* key. Both do nothing if not wired, so tests that dont care about Appearance can
        # leave them out.
        self.on_edit_colours = on_edit_colours or (lambda: None)
        self.on_reset_appearance = on_reset_appearance or (lambda: None)
        self.on_edit_tiles = on_edit_tiles or (lambda: None)
        self.on_restart_es = on_restart_es or (lambda: None)
        self.controls = {}
        self.rows = {}
        self.reorder_pages = {}
        self.tabs = {}
        self.group = config.GROUPS[0]
        self.page_index = 0
        self.pages = {}
        # rows that fit on screen at the current size (layout() sets it), built pages get split into
        # screen pages of at most this many rows
        self._fit = ROWS_PER_PAGE
        self._build_tabs()
        self._build_pages()
        self._build_footer()
        self._build_about(device_name or device.device_name(), app_version)
        self._apply_visibility()

    # -- building ----------------------------------------------------------
    def _build_tabs(self):
        for g in GROUP_ORDER:
            self.tabs[g] = self.body.add(Button(TAB_TITLES[g], name="settings.tab.%s" % g,
                                                size=32, on_click=lambda g=g: self._select_group(g)))

    def _getter(self, field):
        path = field["key_path"]
        return lambda: config.get_value(self.cfg, path)

    def _committer(self, field):
        path = field["key_path"]

        def commit(value):
            if config.set_value(self.cfg, path, value):
                self.on_change(path, config.get_value(self.cfg, path))
                if path[0] == "screens":
                    self._sync_screen_sibling(path)
        return commit

    def _sync_screen_sibling(self, changed_path):
        other = ("screens", "command_center_screen") if changed_path == ("screens", "es_screen") \
            else ("screens", "es_screen")
        row = self.rows.get(other)
        if row is not None:
            row.refresh_from_cfg()
        self.on_change(other, config.get_value(self.cfg, other))

    def _build_pages(self):
        for g in config.GROUPS:
            fields = [f for f in config.fields_in_group(g) if f["key_path"] in config.WIRED]
            normal = [f for f in fields if f["type"] != "array_enum"]
            arrays = [f for f in fields if f["type"] == "array_enum"]
            pages = []
            for i in range(0, len(normal), ROWS_PER_PAGE):
                page = []
                for f in normal[i:i + ROWS_PER_PAGE]:
                    row = self.body.add(FieldRow(f, self._getter(f), self._committer(f)))
                    self.rows[f["key_path"]] = row
                    self.controls[f["key_path"]] = row.control
                    if f["key_path"] == ("appearance", "button_colours") and \
                            not button_colours.supported():
                        # 2, 3 and 6 button devices get it later, with a message for now
                        row.control.set_enabled(False)
                        row.control.text = button_colours.COMING_SOON
                    page.append(row)
                pages.append(page)
            for f in arrays:
                order = config.get_value(self.cfg, f["key_path"])
                offset = 0
                first = True
                subpages = []
                while offset < len(order) or first:
                    first = False
                    page = self.body.add(MediaPriorityPage(
                        f, offset, REORDER_PAGE_SIZE, self._getter(f), self._committer(f),
                        name="settings.media.%s.%d" % (".".join(f["key_path"]), offset)))
                    subpages.append(page)
                    pages.append([page])
                    offset += REORDER_PAGE_SIZE
                # rows/controls point at the first reorder page (a stable widget for coverage checks),
                # reorder_pages holds all of them
                self.rows[f["key_path"]] = subpages[0]
                self.controls[f["key_path"]] = subpages[0]
                self.reorder_pages[f["key_path"]] = subpages
            if g == "appearance":
                # Not a schema field, see AppearanceExtraRow. Goes on the group's last page (right now its only
                # page, theme_preset is the one WIRED field here) so it never ends up on a page nothing reaches.
                self.appearance_extra = self.body.add(
                    AppearanceExtraRow(self.on_edit_colours, self.on_reset_appearance))
                if pages:
                    pages[-1].append(self.appearance_extra)
                else:
                    pages.append([self.appearance_extra])
                self.es_restart = self.body.add(EsRestartRow(lambda: self.on_restart_es()))
                if len(pages[-1]) < ROWS_PER_PAGE:
                    pages[-1].append(self.es_restart)
                else:
                    pages.append([self.es_restart])
            if g == "command_center":
                # Not a schema field, see CommandCenterExtraRow. Gets a new page if the last one is full, never
                # an overflowing one.
                self.command_center_extra = self.body.add(
                    CommandCenterExtraRow(self.on_edit_tiles))
                if pages and len(pages[-1]) < ROWS_PER_PAGE:
                    pages[-1].append(self.command_center_extra)
                else:
                    pages.append([self.command_center_extra])
            self.pages[g] = pages

    def _build_footer(self):
        self.prev_btn = self.body.add(Button("Prev", name="settings.footer.prev", size=36,
                                             on_click=lambda: self._change_page(-1)))
        self.page_label = self.body.add(Label("", size=32, align="center", color="dim"))
        self.next_btn = self.body.add(Button("Next", name="settings.footer.next", size=36,
                                             on_click=lambda: self._change_page(1)))

    def _build_about(self, device_name, app_version):
        """Read-only pages: the device, version and licence, then credits, then support (the flags and
        Trans Lifeline), then what's new in this version (the first lines of its CHANGELOG.md section).
        """
        def lbl(text, size=32, bold=False, color=None, name=None):
            return self.body.add(Label(text, size=size, bold=bold, color=color, name=name))

        first = [
            lbl("Device", color="dim"),
            lbl(device_name, 44, True, name="settings.about.device"),
            lbl("Version", color="dim"),
            lbl(app_version, 44, True, name="settings.about.version"),
            lbl(ABOUT_FREE, 30, True, color="accent", name="settings.about.licence"),
        ]
        credits = [lbl("Credits", color="dim")] + \
            [lbl(t, 30, name="settings.about.credit.%d" % i) for i, t in enumerate(ABOUT_CREDITS)]
        support = [lbl("Support", color="dim"),
                   self.body.add(PrideFlags(name="settings.about.flags"))] + \
            [lbl(t, 30, name="settings.about.support.%d" % i) for i, t in enumerate(ABOUT_SUPPORT)] + \
            [lbl(ABOUT_SUPPORT_LINK, 30, True, color="accent", name="settings.about.support.link")]
        new = whats_new_lines()
        per = ROWS_PER_PAGE - 1                 # a heading row, then the notes
        chunks = [new[i:i + per] for i in range(0, len(new), per)]
        news_pages = []
        for n, chunk in enumerate(chunks):
            head = "What's new in %s" % version.VERSION
            if len(chunks) > 1:
                head += " (%d of %d)" % (n + 1, len(chunks))
            news_pages.append([lbl(head, color="dim")] +
                              [lbl("- " + t, 28, name="settings.about.new.%d" % (n * per + i))
                               for i, t in enumerate(chunk)])
        self.about_rows = first + credits[1:] + support[1:] + [w for pg in news_pages for w in pg[1:]]
        self.pages["about"] = [first, credits, support] + news_pages

    # -- navigation ----------------------------------------------------------
    def _view_pages(self):
        """The group's pages as shown: each built page split into chunks of the rows that fit (a
        smaller screen size leaves room for fewer).
        """
        out = []
        for page in self.pages.get(self.group) or []:
            for i in range(0, len(page), self._fit):
                out.append(page[i:i + self._fit])
        return out

    def _current_page_widgets(self):
        pages = self._view_pages()
        if not pages:
            return []
        idx = min(self.page_index, len(pages) - 1)
        return pages[idx]

    def change_page(self, d):
        """Public paging (Prev/Next and the Settings swipe)."""
        self._change_page(d)

    def _apply_visibility(self):
        self._show_current_rows()
        # only layout() places the current page's widgets, so a page shown by a tab or Prev/Next would
        # sit at rect (0,0,0,0), drawn nowhere and untappable, until something laid the sheet out again
        if self.rect[2] > 0 and self.rect[3] > 0:
            self.layout(self.rect)

    def _show_current_rows(self):
        current = set(id(w) for w in self._current_page_widgets())
        for pages in self.pages.values():
            for page in pages:
                for w in page:
                    visible = id(w) in current
                    w.set_visible(visible)
                    if visible and hasattr(w, "refresh_from_cfg"):
                        w.refresh_from_cfg()
        self._refresh_tab_styles()
        self._refresh_footer()

    def _refresh_tab_styles(self):
        for g, btn in self.tabs.items():
            active = g == self.group
            btn.text = ("> " if active else "") + TAB_TITLES[g]
            btn.invalidate()

    def _refresh_footer(self):
        total = len(self._view_pages()) or 1
        self.page_label.set_text("Page %d of %d" % (self.page_index + 1, total))
        self.prev_btn.set_enabled(self.page_index > 0)
        self.next_btn.set_enabled(self.page_index < total - 1)

    def _select_group(self, g):
        if g != self.group:
            self.group = g
            self.page_index = 0
            self._apply_visibility()

    def _change_page(self, d):
        pages = self._view_pages()
        new = self.page_index + d
        if 0 <= new < len(pages):
            self.page_index = new
            self._apply_visibility()

    # -- layout ----------------------------------------------------------
    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        inner_w = bw - 2 * MARGIN
        tab_rects = ui.hsplit((bx + MARGIN, by, inner_w, TAB_H), [None] * len(GROUP_ORDER), gap=12)
        for g, r in zip(GROUP_ORDER, tab_rects):
            self.tabs[g].set_rect(r)

        content_y = by + TAB_H + GAP
        content_h = max(0, bh - TAB_H - FOOTER_H - 2 * GAP)
        fit = max(1, min(ROWS_PER_PAGE, (content_h + ROW_GAP) // (ROW_H + ROW_GAP)))
        if fit != self._fit:
            self._fit = fit
            self.page_index = min(self.page_index, max(0, len(self._view_pages()) - 1))
            self._show_current_rows()
        self._layout_current_page((bx + MARGIN, content_y, inner_w, content_h))

        footer_rect = (bx + MARGIN, by + bh - FOOTER_H, inner_w, FOOTER_H)
        p, lbl, n = ui.hsplit(footer_rect, [300, None, 300], gap=GAP)
        self.prev_btn.set_rect(p)
        self.page_label.set_rect(lbl)
        self.next_btn.set_rect(n)

    def _layout_current_page(self, rect):
        x, y, w, h = rect
        for i, wdg in enumerate(self._current_page_widgets()):
            r = (x, y + i * (ROW_H + ROW_GAP), w, ROW_H)
            if hasattr(wdg, "layout"):
                wdg.layout(r)
            else:
                wdg.set_rect(r)
