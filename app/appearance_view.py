"""appearance_view - the "Custom colours" Command Center sheet + controller
for the Appearance settings page's colour theme (palettes.py).

Reuses rgb_view.py's colour-picker widgets (Swatch, PreviewCircle) instead
of writing a new picker: the owner's brief specifically asked for this.
What's genuinely new here, and could not just be RGBSheet pointed at a
different config key:
  - THREE colours to edit (accent/background/text), not one - a row of
    LitButtons (SLOT_LABELS) picks which one the sliders/swatches below
    are currently editing, the same "which one am I editing" shape
    rgb_view.RGBSheet already uses for its own left/right stick buttons.
  - rgb_leds.hue_to_rgb()/rgb_to_hue() are fixed at full value (correct
    for an LED, which is always at full brightness some colour or other)
    and so can never reach a near-black or near-white pick - most of what
    a background or text colour actually needs. palettes.hsl_to_rgb()/
    rgb_to_hsl() add the missing lightness (and saturation) axis; this
    file is the only caller.
  - an inline low-contrast warning (palettes.custom_contrast_warning()) -
    item 3 of the brief: shown, never blocking.

AppearanceController's shape mirrors rgb_view.RGBController closely (same
split: Sheet is widgets only, Controller drives it + persistence via
save_fn, defaulting to config.save_changes so tests can inject a fake) -
see that class's own docstring for the reasoning this borrows wholesale.

Live apply: every slider drag and swatch tap calls palettes.apply_theme()
immediately (mutates ui.THEME in place - see that function's docstring for
why not a plain reassignment), same "always live, no restart" contract as
the rest of Appearance; only *persistence* (config.save_changes) is
deferred to slider release / a completed tap, matching RGBController's
own drag()/drag_flush() split (there is no device I/O here to rate-limit,
so unlike RGBController this file does not need a keeper/rate limiter at
all - a plain dict update is cheap enough to do on every drag tick)."""
import ui
from ui import THEME, Label, LitButton, Sheet, Slider

import config
import palettes
import rgb_leds
import rgb_view

ROW_H = 120
GAP = 16
PAD = 20
SLIDER_LABEL_H = 30

SLOT_LABELS = (("accent", "Accent"), ("bg", "Background"), ("text", "Text"))
SLOT_KEY_PATHS = {
    "accent": ("appearance", "custom_accent"),
    "bg": ("appearance", "custom_bg"),
    "text": ("appearance", "custom_text"),
}


# ---------------------------------------------------------------------------
# Sheet
# ---------------------------------------------------------------------------
class AppearanceSheet(Sheet):
    """Widgets only; AppearanceController fills/reads them and receives
    every on_action(name, payload):
      "appearance.slot"                  payload: "accent" | "bg" | "text"
      "appearance.preset"                payload: an (r, g, b) quick pick
      "appearance.hue" / ".commit"       payload: int hue 0-359 (drag / release)
      "appearance.sat" / ".commit"       payload: float 0-1 (drag / release)
      "appearance.light" / ".commit"     payload: float 0-1 (drag / release)
      "appearance.close"                 payload: None
    """

    def __init__(self, on_action):
        Sheet.__init__(self, "Custom colours",
                       on_close=lambda: on_action("appearance.close", None),
                       name="appearance_custom")
        self.on_action = on_action

        self.slot_buttons = {}
        for value, label in SLOT_LABELS:
            self.slot_buttons[value] = self.body.add(LitButton(
                label, name="appearance.slot.%s" % value, size=40,
                on_click=lambda v=value: on_action("appearance.slot", v)))

        # Quick picks: the same named palette the stick-light sheet offers
        # (rgb_leds.PRESETS) - full-saturation hues are a reasonable start
        # for an accent colour, less so for background/text, but a tap
        # here is just a starting point; the sliders below reach anywhere
        # the swatches don't.
        self.swatches = []
        for pname, rgb_val in rgb_leds.PRESETS:
            sw = self.body.add(rgb_view.Swatch(
                rgb_val, name="appearance.preset.%s" % pname,
                on_click=lambda v=rgb_val: on_action("appearance.preset", v)))
            self.swatches.append((pname, sw))

        self.hue_label = self.body.add(Label("Hue", size=32, color="dim"))
        self.hue = self.body.add(Slider(
            name="appearance.hue", knob_r=44, step=1.0 / 359,
            on_change=lambda v: on_action("appearance.hue", int(round(v * 359))),
            on_release=lambda v: on_action("appearance.hue.commit", int(round(v * 359)))))

        self.sat_label = self.body.add(Label("Saturation", size=32, color="dim"))
        self.sat = self.body.add(Slider(
            name="appearance.sat", knob_r=44, step=0.01,
            on_change=lambda v: on_action("appearance.sat", v),
            on_release=lambda v: on_action("appearance.sat.commit", v)))

        self.light_label = self.body.add(Label("Lightness", size=32, color="dim"))
        self.light = self.body.add(Slider(
            name="appearance.light", knob_r=44, step=0.01,
            on_change=lambda v: on_action("appearance.light", v),
            on_release=lambda v: on_action("appearance.light.commit", v)))

        self.warning = self.body.add(Label("", size=28, color="warn", align="center"))

        self.previews = {}
        self.preview_labels = {}
        for value, label in SLOT_LABELS:
            self.previews[value] = self.body.add(
                rgb_view.PreviewCircle(name="appearance.preview.%s" % value))
            self.preview_labels[value] = self.body.add(
                Label(label, size=30, align="center", color="dim"))

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        x0 = bx + PAD
        w0 = bw - 2 * PAD
        y = by + PAD

        cells = ui.hsplit((x0, y, w0, ROW_H), [None] * len(SLOT_LABELS), gap=GAP)
        for (value, _label), cell in zip(SLOT_LABELS, cells):
            self.slot_buttons[value].set_rect(cell)
        y += ROW_H + GAP

        swatch_cells = ui.hsplit((x0, y, w0, ROW_H), [None] * len(self.swatches), gap=GAP // 2)
        for (_pname, sw), cell in zip(self.swatches, swatch_cells):
            sw.set_rect(cell)
        y += ROW_H + GAP

        for lbl, sld in ((self.hue_label, self.hue), (self.sat_label, self.sat),
                        (self.light_label, self.light)):
            lbl.set_rect((x0, y, w0, SLIDER_LABEL_H))
            y += SLIDER_LABEL_H
            sld.set_rect((x0, y, w0, ROW_H))
            y += ROW_H + GAP

        self.warning.set_rect((x0, y, w0, 40))
        y += 40 + GAP

        preview_h = max(120, by + bh - y - PAD)
        label_h = 40
        cells = ui.hsplit((x0, y, w0, preview_h), [None] * len(SLOT_LABELS), gap=GAP * 2)
        for (value, _label), cell in zip(SLOT_LABELS, cells):
            cx, cy, cw, ch = cell
            self.previews[value].set_rect((cx, cy, cw, max(0, ch - label_h)))
            self.preview_labels[value].set_rect((cx, cy + max(0, ch - label_h), cw, label_h))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class AppearanceController:
    """host: main.App-like object (`close_appearance`, `state_dirty`,
    `ui.open`/`ui.settings`). cfg: the SAME dict main.App holds as
    self.cfg (mutated in place by config.set_value(), exactly like
    RGBController's `lights` argument does for the "lights" group).
    save_fn(changes) defaults to config.save_changes; tests inject a fake
    so no real file is touched."""

    def __init__(self, host, cfg, save_fn=None):
        self.host = host
        self.cfg = cfg
        self.save_fn = save_fn or self._default_save
        self.sheet = AppearanceSheet(self.action)
        self.active_slot = "accent"
        self.refresh()

    @staticmethod
    def _default_save(changes):
        return config.save_changes(changes)

    # -- state -------------------------------------------------------------
    def _hexval(self, slot):
        stored = config.get_value(self.cfg, SLOT_KEY_PATHS[slot])
        return palettes.hex_str_to_int(stored, palettes.PRESETS[palettes.DEFAULT_PRESET][slot])

    # -- host-facing ---------------------------------------------------------
    def open(self):
        """Opening the editor always switches appearance.theme_preset to
        "custom" - there is deliberately no separate "turn on Custom mode"
        step, so the sheet is never open while some OTHER preset is what's
        actually on screen."""
        if config.get_value(self.cfg, ("appearance", "theme_preset")) != palettes.CUSTOM_PRESET:
            config.set_value(self.cfg, ("appearance", "theme_preset"), palettes.CUSTOM_PRESET)
            self.save_fn({("appearance", "theme_preset"): palettes.CUSTOM_PRESET})
        self._apply_live()
        self.refresh()
        self.host.ui.open("appearance_custom")
        self._dirty()

    # -- state -> widgets ----------------------------------------------------
    def refresh(self):
        vals = {slot: self._hexval(slot) for slot, _ in SLOT_LABELS}
        for slot, btn in self.sheet.slot_buttons.items():
            btn.set_lit(slot == self.active_slot)
        h, s, l = palettes.rgb_to_hsl(vals[self.active_slot])
        self.sheet.hue.set_value(h / 359.0)
        self.sheet.sat.set_value(s)
        self.sheet.light.set_value(l)
        cur_bytes = palettes.hex_to_bytes(vals[self.active_slot])
        for pname, sw in self.sheet.swatches:
            sw.set_selected(rgb_leds.PRESET_BY_NAME.get(pname) == cur_bytes)
        for slot, _label in SLOT_LABELS:
            self.sheet.previews[slot].set_colour(palettes.hex_to_bytes(vals[slot]), on=True)
        warning = palettes.custom_contrast_warning(vals["accent"], vals["bg"], vals["text"])
        self.sheet.warning.set_text(warning or "")
        if self.sheet.rect[2]:
            self.sheet.layout(self.sheet.rect)

    def _apply_live(self):
        palettes.apply_theme(self.cfg)
        # Mutating ui.THEME alone is not enough - see main.App.on_setting()'s
        # comment on the "appearance" branch for the full story (device
        # field test 25 Sep): only a widget that is itself marked damaged
        # gets repainted, so the WHOLE surface must be marked damaged here
        # too, every drag tick and every commit, or the sheet's own
        # background/swatches/previews (everything except whatever widget
        # happened to invalidate itself) go stale exactly like the Settings
        # page did.
        ui_obj = getattr(self.host, "ui", None)
        root = getattr(ui_obj, "root", None)
        if root is not None:
            root.damage_all()

    def _commit_slot(self, hexval, commit):
        hex_str = "#%06x" % hexval
        config.set_value(self.cfg, SLOT_KEY_PATHS[self.active_slot], hex_str)
        self._apply_live()
        if commit:
            self.save_fn({SLOT_KEY_PATHS[self.active_slot]: hex_str})

    def _dirty(self):
        if hasattr(self.host, "state_dirty"):
            self.host.state_dirty = True

    def _slot_hsl(self):
        return palettes.rgb_to_hsl(self._hexval(self.active_slot))

    # -- events --------------------------------------------------------------
    def action(self, name, payload):
        if name == "appearance.close":
            self.host.close_appearance()
            return
        if name == "appearance.slot":
            self.active_slot = payload
        elif name == "appearance.preset":
            self._commit_slot(palettes.bytes_to_hex(payload), commit=True)
        elif name == "appearance.hue":
            _h, s, l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(payload, s, l), commit=False)
        elif name == "appearance.hue.commit":
            _h, s, l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(payload, s, l), commit=True)
        elif name == "appearance.sat":
            h, _s, l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(h, payload, l), commit=False)
        elif name == "appearance.sat.commit":
            h, _s, l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(h, payload, l), commit=True)
        elif name == "appearance.light":
            h, s, _l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(h, s, payload), commit=False)
        elif name == "appearance.light.commit":
            h, s, _l = self._slot_hsl()
            self._commit_slot(palettes.hsl_to_rgb(h, s, payload), commit=True)
        self.refresh()
        self._dirty()
