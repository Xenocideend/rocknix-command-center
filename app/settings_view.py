"""settings_view - the Settings sheet (B12a), generated from config.SCHEMA.

Opened by the gear icon in the Command Center (wiring happens in I1 - this
module only builds the sheet and does not touch main.py/screens.py/ui.py).

Touch-first, 1920x1080 landscape: body text 36-44 px, every interactive
control at least ~120 px in both dimensions. `ui` has no scroll container,
so overflow is handled by PAGING: a row of group tabs (Companion view,
Manuals, Command Center, Screens, Audio, About - R6 §2's six groups) plus
Prev/Next within a group when its fields don't fit on one page. The
`media_priority` array<enum> gets its own reorder sub-pages (up/down
buttons) instead of a normal row.

One control is built per SCHEMA field THAT IS IN config.WIRED (RV4-M2:
four settings had a control and no effect at all - see config.py's WIRED
set). A schema key outside WIRED gets no tab/page/control at all; if a
group ends up with no WIRED field, its tab is not built either. SW1: the
Screens group has exactly one control, "Swap screens" - a Toggle over the
screens.es_screen enum (on = "builtin_bottom"), see _TOGGLE_ENUMS. The
derived screens.command_center_screen never gets a control (the old
RV1-M4 "dangerous combo"). `SettingsSheet.controls` maps key_path -> that control widget, so
a WIRED key with no UI is a coverage gap a test can catch, and so is a
visible control for a key that ISN'T in WIRED. Every value change (toggle, slider release, cycle
button tap, reorder) validates through config.set_value() and then calls
`on_change(key_path, value)`. Changing one of screens.es_screen /
screens.command_center_screen flips the other (they must stay opposite
outputs) and fires on_change for BOTH keys, since both actually changed.

Widgets this file needed that ui.py does not provide: CycleButton (tap
advances through an enum's allowed values) and MediaPriorityPage (a
reorderable list with per-row up/down buttons).
"""
import device
import config
import palettes
import ui
from ui import THEME, Button, Container, Label, Sheet, Slider, Toggle

# -- layout constants (all touch targets sized for the 1920x1080 panel) -----
ROW_H = 120
ROW_GAP = 12
ROWS_PER_PAGE = 5          # normal (non-reorder) fields per page
REORDER_PAGE_SIZE = 4      # media_priority items per reorder sub-page
TAB_H = 120
FOOTER_H = 120
MARGIN = 30
GAP = 16

BOOL_CTRL_W = 160
ENUM_CTRL_W = 420
SLIDER_CTRL_W = 460
READOUT_W = 140
HINT_W = 320       # was 260: "applies after restart" ellipsized to a sliver;
                   # widened AND (below) shortened to "after restart"

# A group with no WIRED field gets no tab at all, by construction rather
# than a special case: any group that loses its last consumer disappears,
# and any group that gains one reappears (Screens did, with SW1).
GROUP_ORDER = [g for g in config.GROUPS
               if any(f["key_path"] in config.WIRED for f in config.fields_in_group(g))] + ["about"]
GROUP_TITLES = dict(config.GROUP_LABELS)
GROUP_TITLES["about"] = "About"

# The tab strip divides the sheet width by len(GROUP_ORDER); "Command
# Center" ellipsized to "Command C..." (I1 polish finding). Full names stay
# in GROUP_TITLES/config.GROUP_LABELS for anywhere else they're used; only
# the tab button itself shows the shorter form.
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
    """CC2's companion.in_game_display reads better with a few words per
    choice than the generic underscore-splitting _display() gives ("off" ->
    "Off (blank)" is the ES-DE Companion precedent for what "Off" actually
    does - see research/CC3-esde-companion.md)."""
    return _IN_GAME_DISPLAY_LABELS.get(value, _display(value))


def _hardware_button_display(value):
    """Device-verified 23 Sep: this RP5 has no rear paddles (0 evdev events
    for either paddle code in 120 s) - the paddle enum values stay valid
    (a stored file might be for a different device) but are labeled so
    picking one on THIS device doesn't look like a safe, working choice."""
    base = _display(value)
    if value in config.PADDLE_HARDWARE_BUTTONS:
        return base + " (no rear paddles on RP5)"
    return base


# Per-field override of CycleButton's display() - only hardware_button
# needs one today (the paddle-labeling above); everything else uses the
# generic _display().
_ENUM_DISPLAY_OVERRIDES = {
    ("command_center", "hardware_button"): _hardware_button_display,
    ("companion", "in_game_display"): _in_game_display_display,
}

# SW1: a two-valued enum shown as an on/off Toggle instead of a cycle
# button: key_path -> (value when OFF, value when ON). "Swap screens" on
# means ES and games move to the built-in panel and the Command Center to
# the add-on; off is the shipped layout. One toggle, so there is no way to
# pick the two screens independently.
_TOGGLE_ENUMS = {
    ("screens", "es_screen"): ("addon_top", "builtin_bottom"),
}

# CC5 (hidden_overlay.py): which corner of the panel gets the small tap
# target while an emulator's second screen owns it. Registered here, apart
# from the dict literal above, so this patch and others that add their own
# override line there do not collide.
_CORNER_HANDLE_LABELS = {
    "off": "Off", "top-left": "Top left", "top-right": "Top right",
    "bottom-left": "Bottom left", "bottom-right": "Bottom right",
}
_ENUM_DISPLAY_OVERRIDES[("command_center", "corner_handle")] = \
    lambda value: _CORNER_HANDLE_LABELS.get(value, _display(value))

# Appearance: without this, the generic _display() splits the raw preset
# NAME on underscores ("dreamcast" -> "Dreamcast"), which disagreed with
# `palettes.py --list`'s descriptive PRESET_LABELS ("White/orange swirl")
# for every classic-console preset - found in the device field test 25 Sep.
# palettes.display_label() is the one place both now read from.
_ENUM_DISPLAY_OVERRIDES[("appearance", "theme_preset")] = palettes.display_label


# ---------------------------------------------------------------------------
# Widgets ui.py lacks
# ---------------------------------------------------------------------------
class CycleButton(Button):
    """Tap advances to the next allowed value (wraps around). Shows the
    current value; on_pick(new_value) fires on a completed tap."""

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
    """One reorder sub-page: `count` consecutive ranks of the
    companion.media_priority list, each with Up/Down buttons that swap
    against the FULL underlying list (so a move at a page edge correctly
    reaches into the neighbouring sub-page's item).

    CM bug 2 (test day): the owner paged Next x7 through Settings ->
    Companion, never tapped a settings.media.* control, and reported
    "cartridge/boxback buttons are not there". A PC render of every
    Companion page (screenshots/cm-pc-render-media-page-*.png) showed the
    two reorder sub-pages ARE built correctly - page 5 of 6 has
    video/titleshot/mix/image, page 6 of 6 has marquee/fanart/cartridge/
    boxback, all with working Up/Down - so this was pure discoverability:
    every page in the sheet looks like a plain list of rows, and unlike a
    FieldRow (which shows field["label"]), a reorder sub-page never showed
    ANY label at all - nothing on screen said "this is the media order
    setting", let alone "page 1 of 2 of it". The header below fixes that in
    the same modest, no-new-navigation way FieldRow already does it."""

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
    """The Appearance group's one non-schema row: two plain buttons, same
    ROW_H as every FieldRow so it lays out identically to one in
    _layout_current_page(). "Edit custom colours..." is always tappable
    (not just when theme_preset == "custom") - tapping it is what SWITCHES
    to Custom (appearance_view.AppearanceController.open() does that), so
    there is no separate greyed-out state to explain."""

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


class FieldRow(Container):
    """A label plus one field's control (Toggle / Slider / CycleButton),
    and a restart hint when the field needs one."""

    def __init__(self, field, get_value, commit, name=None):
        Container.__init__(self, name=name or ("settings.row.%s" % ".".join(field["key_path"])))
        self.field = field
        self.get_value = get_value
        self.commit = commit
        self.label = self.add(Label(field["label"], size=40, color="text"))
        # YT4: the restart hint doubles as a transient note area
        # (set_note()/clear_note()) - an "unavailable"/write-failed message
        # temporarily replaces it, same idea as bar.show_hint()'s own
        # temporary-message-then-revert pattern elsewhere in the app.
        self._restart_note = "after restart" if field["restart"] else ""
        self.hint = self.add(Label(self._restart_note, size=28, color="warn"))
        self.readout = None
        self.control = self._build_control()
        self.add(self.control)

    def set_note(self, text, warn=True):
        # A bare THEME key, not THEME["warn"]/THEME["dim"] resolved here -
        # Label.set_text() freezes whatever colour it is given until the
        # next call (ui.py's own docstring), so passing an already-
        # resolved tuple would stop following a runtime theme change from
        # the moment this hint was last set. Appearance field test on the
        # device (25 Sep) found exactly this on the volume readout and
        # this hint - both were still passing resolved THEME[...] tuples.
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
        # command_center.auto_close_timeout_s: 0 means "never auto-closes",
        # not the literal number - a bare "0" reads like a broken/unset
        # value on a touch settings screen (I1 polish finding).
        if self.field["key_path"] == ("command_center", "auto_close_timeout_s") and v == 0:
            return "Never"
        # YT4: battery.safe_charge_end_pct - the owner's own phrasing was
        # "Stops at 85% - resumes at 80%"; the readout column here is a
        # fixed READOUT_W (140 px, shared by every int/float row), too
        # narrow for that whole sentence, so this is a compact stand-in
        # ("85/80") rather than widening one row's column at every other
        # row's expense.
        if self.field["key_path"] == ("battery", "safe_charge_end_pct"):
            return "%d/%d" % (v, v - 5)
        return ("%.2f" % v) if self.field["type"] == "float" else str(v)

    def _raw_from_frac(self, frac):
        lo, hi = self.field["range"]
        raw = lo + frac * (hi - lo)
        if self.field["type"] == "int":
            raw = int(round(raw))
            step = self.field.get("step")
            if step:
                # YT4: battery.safe_charge_end_pct (50-95 step 5) - snap to
                # the nearest reviewed increment instead of a bare 1% slider.
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
    """Generated wholly from config.SCHEMA. `controls` maps a schema
    key_path -> its interactive widget (a FieldRow's Toggle/Slider/
    CycleButton, or the MediaPriorityPage for the one array field) -
    tests assert this covers every SCHEMA entry."""

    def __init__(self, cfg, on_change, on_close, device_name=None, app_version="dev", name="settings",
                on_edit_colours=None, on_reset_appearance=None):
        Sheet.__init__(self, "Settings", on_close=on_close, name=name)
        self.cfg = cfg
        self.on_change = on_change
        # Appearance's two buttons are not schema fields (nothing to
        # validate/persist here - the sheet just calls out to main.py):
        # "Edit custom colours..." opens appearance_view.AppearanceSheet,
        # "Reset to default" restores every appearance.* key. Both no-ops
        # if not wired (tests that don't care about Appearance can omit
        # them; a tap then does nothing rather than raising).
        self.on_edit_colours = on_edit_colours or (lambda: None)
        self.on_reset_appearance = on_reset_appearance or (lambda: None)
        self.controls = {}
        self.rows = {}
        self.reorder_pages = {}
        self.tabs = {}
        self.group = config.GROUPS[0]
        self.page_index = 0
        self.pages = {}
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
                # rows/controls point at the FIRST sub-page (a stable, findable
                # widget for coverage checks); reorder_pages holds all of them.
                self.rows[f["key_path"]] = subpages[0]
                self.controls[f["key_path"]] = subpages[0]
                self.reorder_pages[f["key_path"]] = subpages
            if g == "appearance":
                # Not a schema field - see AppearanceExtraRow's own
                # docstring. Lands on the group's LAST page (today, its
                # only page: theme_preset is the one WIRED field here) so
                # it is never left stranded on a page nothing else reaches.
                self.appearance_extra = self.body.add(
                    AppearanceExtraRow(self.on_edit_colours, self.on_reset_appearance))
                if pages:
                    pages[-1].append(self.appearance_extra)
                else:
                    pages.append([self.appearance_extra])
            self.pages[g] = pages

    def _build_footer(self):
        self.prev_btn = self.body.add(Button("Prev", name="settings.footer.prev", size=36,
                                             on_click=lambda: self._change_page(-1)))
        self.page_label = self.body.add(Label("", size=32, align="center", color="dim"))
        self.next_btn = self.body.add(Button("Next", name="settings.footer.next", size=36,
                                             on_click=lambda: self._change_page(1)))

    def _build_about(self, device_name, app_version):
        self.about_rows = [
            self.body.add(Label("Device", size=32, color="dim")),
            self.body.add(Label(device_name, size=44, bold=True, name="settings.about.device")),
            self.body.add(Label("App version", size=32, color="dim")),
            self.body.add(Label(app_version, size=44, bold=True, name="settings.about.version")),
        ]
        self.pages["about"] = [self.about_rows]

    # -- navigation ----------------------------------------------------------
    def _current_page_widgets(self):
        pages = self.pages.get(self.group) or []
        if not pages:
            return []
        idx = min(self.page_index, len(pages) - 1)
        return pages[idx]

    def _apply_visibility(self):
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
        # I1 fix: only layout() places the current page's widgets, so a page
        # made visible by a tab or Prev/Next tap kept rect (0,0,0,0) - drawn
        # nowhere and untappable - until something re-laid the sheet out.
        if self.rect[2] > 0 and self.rect[3] > 0:
            self.layout(self.rect)

    def _refresh_tab_styles(self):
        for g, btn in self.tabs.items():
            active = g == self.group
            btn.text = ("> " if active else "") + TAB_TITLES[g]
            btn.invalidate()

    def _refresh_footer(self):
        total = len(self.pages.get(self.group) or []) or 1
        self.page_label.set_text("Page %d of %d" % (self.page_index + 1, total))
        self.prev_btn.set_enabled(self.page_index > 0)
        self.next_btn.set_enabled(self.page_index < total - 1)

    def _select_group(self, g):
        if g != self.group:
            self.group = g
            self.page_index = 0
            self._apply_visibility()

    def _change_page(self, d):
        pages = self.pages.get(self.group) or []
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
