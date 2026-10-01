"""rgb_view: the Stick lights sheet and its controller.

RGBSheet (a screens.Sheet like hotkeys_view.HotkeysSheet, Back plus a title) is widgets only.
RGBController drives it, talks to rgb_leds.Controller (the device side), and saves through
config.py's "lights" group, the same split as cleanstate_view.py.

Layout at 1920x1080, every row you touch at least 120 px tall:
  mode               Follow ROCKNIX / Off / Colour (3 LitButtons, one at a time)
  same colour toggle plus a Left/Right stick picker (only when not linked)
  preset swatches    9 named colours (rgb_leds.PRESETS), tap to apply
  hue slider         custom colour at full saturation (rgb_leds.hue_to_rgb)
  brightness slider  the single 0-255 ledcontrol brightness argument
  live preview       two circles, each stick's actual colour

Slider drags are rate limited by rgb_leds.Controller.drag() (at most every 80 ms) and always
apply the last value on release (drag_flush()). Every commit (mode, linked, a preset tap, a
slider release) gets saved through RGBController.save_fn (config.save_changes by default), and
a value that was only dragged and never released is never written.
"""
import ui
from ui import THEME, Button, Label, LitButton, Sheet, Slider, Toggle, Widget

import rgb_leds

ROW_H = 120
GAP = 16
PAD = 20
SLIDER_LABEL_H = 30

MODE_LABELS = (("rocknix", "Follow ROCKNIX"), ("off", "Off"), ("colour", "Colour"))


# ---------------------------------------------------------------------------
# Small local widgets (Button/Widget subclasses)
# ---------------------------------------------------------------------------
class Swatch(Button):
    """A preset colour tile, filled with its colour and ringed when it's the stick's current colour."""

    def __init__(self, rgb_val, rect=(0, 0, 0, 0), on_click=None, name=None):
        Button.__init__(self, "", rect, on_click, name, 0, None, 24)
        self.rgb_val = rgb_val
        self.selected = False

    def _fill(self):
        r, g, b = self.rgb_val
        return (r / 255.0, g / 255.0, b / 255.0, 1.0)

    def set_selected(self, v):
        v = bool(v)
        if v != self.selected:
            self.selected = v
            self.invalidate()

    def draw(self, g):
        g.round_rect(self.rect, self.radius, self._fill())
        if self.selected:
            g.stroke_round_rect(self.rect, self.radius, THEME["text"], 6)
        elif self.pressed:
            g.stroke_round_rect(self.rect, self.radius, THEME["accent"], 4)


class PreviewCircle(Widget):
    """A circle showing one stick's actual colour, not tappable, just a live readout."""

    def __init__(self, rect=(0, 0, 0, 0), name=None):
        Widget.__init__(self, rect, name)
        self.rgb_val = (0, 0, 0)
        self.on = True

    def set_colour(self, rgb_val, on=True):
        if (rgb_val, bool(on)) != (self.rgb_val, self.on):
            self.rgb_val, self.on = rgb_val, bool(on)
            self.invalidate()

    def draw(self, g):
        x, y, w, h = self.rect
        cx, cy = x + w / 2.0, y + h / 2.0
        r = max(0.0, min(w, h) / 2.0 - 6)
        rr, gg, bb = self.rgb_val
        colour = (rr / 255.0, gg / 255.0, bb / 255.0, 1.0) if self.on else THEME["disabled"]
        g.circle(cx, cy, r, colour)
        g.ring(cx, cy, r, THEME["line"], 4)


# ---------------------------------------------------------------------------
# Sheet
# ---------------------------------------------------------------------------
class RGBSheet(Sheet):
    """Widgets only. RGBController fills and reads them and gets every on_action(name, payload):
      "lights.mode"                payload: "rocknix" | "off" | "colour"
      "lights.linked"              payload: bool
      "lights.stick"               payload: "left" | "right" (unlinked only)
      "lights.preset"              payload: a rgb_leds.PRESETS name
      "lights.hue" / ".commit"     payload: int hue 0-359 (drag / release)
      "lights.brightness" / ".commit"  payload: int 0-255 (drag / release)
      "lights.close"               payload: None
    """

    def __init__(self, on_action):
        Sheet.__init__(self, "Stick lights", on_close=lambda: on_action("lights.close", None),
                       name="lights")
        self.on_action = on_action

        self.mode_buttons = {}
        for value, label in MODE_LABELS:
            self.mode_buttons[value] = self.body.add(LitButton(
                label, name="lights.mode.%s" % value, size=40,
                on_click=lambda v=value: on_action("lights.mode", v)))

        self.linked = self.body.add(Toggle(
            name="lights.linked", size=36,
            text_on="Same colour on both sticks", text_off="Same colour on both sticks",
            on_toggle=lambda st: on_action("lights.linked", st)))

        self.stick_buttons = {}
        for stick in ("left", "right"):
            self.stick_buttons[stick] = self.body.add(LitButton(
                stick.capitalize(), name="lights.stick.%s" % stick, size=36,
                on_click=lambda s=stick: on_action("lights.stick", s)))

        self.swatches = []
        for pname, rgb_val in rgb_leds.PRESETS:
            sw = self.body.add(Swatch(rgb_val, name="lights.preset.%s" % pname,
                                      on_click=lambda p=pname: on_action("lights.preset", p)))
            self.swatches.append((pname, sw))

        self.hue_label = self.body.add(Label("Hue", size=32, color="dim"))
        self.hue = self.body.add(Slider(
            name="lights.hue", knob_r=44, step=1.0 / 359,
            on_change=lambda v: on_action("lights.hue", int(round(v * 359))),
            on_release=lambda v: on_action("lights.hue.commit", int(round(v * 359)))))

        self.brightness_label = self.body.add(Label("Brightness", size=32, color="dim"))
        self.brightness = self.body.add(Slider(
            name="lights.brightness", knob_r=44, step=1.0 / 255,
            on_change=lambda v: on_action("lights.brightness", int(round(v * 255))),
            on_release=lambda v: on_action("lights.brightness.commit", int(round(v * 255)))))

        self.preview_left = self.body.add(PreviewCircle(name="lights.preview.left"))
        self.preview_right = self.body.add(PreviewCircle(name="lights.preview.right"))
        self.preview_left_label = self.body.add(Label("Left", size=30, align="center",
                                                       color="dim"))
        self.preview_right_label = self.body.add(Label("Right", size=30, align="center",
                                                        color="dim"))

    def layout(self, rect):
        Sheet.layout(self, rect)
        bx, by, bw, bh = self.body.rect
        x0 = bx + PAD
        w0 = bw - 2 * PAD
        y = by + PAD

        cells = ui.hsplit((x0, y, w0, ROW_H), [None] * len(MODE_LABELS), gap=GAP)
        for (value, _label), cell in zip(MODE_LABELS, cells):
            self.mode_buttons[value].set_rect(cell)
        y += ROW_H + GAP

        linked_w = int(w0 * 0.6)
        self.linked.set_rect((x0, y, linked_w, ROW_H))
        stick_cells = ui.hsplit((x0 + linked_w + GAP, y, w0 - linked_w - GAP, ROW_H),
                                [None, None], gap=GAP)
        for stick, cell in zip(("left", "right"), stick_cells):
            self.stick_buttons[stick].set_rect(cell)
        y += ROW_H + GAP

        swatch_cells = ui.hsplit((x0, y, w0, ROW_H), [None] * len(self.swatches), gap=GAP // 2)
        for (_pname, sw), cell in zip(self.swatches, swatch_cells):
            sw.set_rect(cell)
        y += ROW_H + GAP

        self.hue_label.set_rect((x0, y, w0, SLIDER_LABEL_H))
        y += SLIDER_LABEL_H
        self.hue.set_rect((x0, y, w0, ROW_H))
        y += ROW_H + GAP

        self.brightness_label.set_rect((x0, y, w0, SLIDER_LABEL_H))
        y += SLIDER_LABEL_H
        self.brightness.set_rect((x0, y, w0, ROW_H))
        y += ROW_H + GAP

        preview_h = max(120, by + bh - y - PAD)
        label_h = 40
        pairs = ((self.preview_left, self.preview_left_label),
                (self.preview_right, self.preview_right_label))
        for (circle, label), cell in zip(pairs, ui.hsplit((x0, y, w0, preview_h), [None, None],
                                                          gap=GAP * 2)):
            cx, cy, cw, ch = cell
            circle.set_rect((cx, cy, cw, max(0, ch - label_h)))
            label.set_rect((cx, cy + max(0, ch - label_h), cw, label_h))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class RGBController:
    """host is main.App-like (`close_sheet`, `state_dirty`, `ui.open`). lights is an
    rgb_leds.Controller, the real device state and apply logic, and this class only turns sheet
    events into calls on it, plus saving. save_fn(changes) defaults to config.save_changes, tests
    pass a fake so no real file gets touched (the config round-trip test uses the real one against
    a temp RP5DECK_CONFIG).
    """

    def __init__(self, host, lights, save_fn=None):
        self.host = host
        self.lights = lights
        self.save_fn = save_fn or self._default_save
        self.sheet = RGBSheet(self.action)
        self.active_stick = "left"
        self.refresh()

    @staticmethod
    def _default_save(changes):
        import config
        return config.save_changes(changes)

    # -- host-facing ----------------------------------------------------------
    def open(self):
        self.refresh()
        self.host.ui.open("lights")
        self._dirty()

    # -- state -> widgets -------------------------------------------------
    def refresh(self):
        st = self.lights.state
        for value, btn in self.sheet.mode_buttons.items():
            btn.set_lit(value == st.mode)
        self.sheet.linked.set_state(st.linked)
        for stick, btn in self.sheet.stick_buttons.items():
            btn.set_visible(not st.linked)
            btn.set_lit(stick == self.active_stick)
        edit_stick = "left" if st.linked else self.active_stick
        edit_rgb = st.colour_for(edit_stick)
        for pname, sw in self.sheet.swatches:
            sw.set_selected(rgb_leds.PRESET_BY_NAME.get(pname) == edit_rgb)
        hue, _sat = rgb_leds.rgb_to_hue(edit_rgb)
        self.sheet.hue.set_value(hue / 359.0)
        self.sheet.brightness.set_value(st.brightness / 255.0)
        lit_on = st.mode != rgb_leds.MODE_OFF
        self.sheet.preview_left.set_colour(st.left, on=lit_on)
        self.sheet.preview_right.set_colour(st.right, on=lit_on)
        if self.sheet.rect[2]:
            self.sheet.layout(self.sheet.rect)

    def _persist(self):
        self.save_fn(rgb_leds.config_changes_for(self.lights.state))

    def _dirty(self):
        if hasattr(self.host, "state_dirty"):
            self.host.state_dirty = True

    def _edit_stick(self):
        return "left" if self.lights.state.linked else self.active_stick

    def _set_colour(self, rgb_val, commit):
        new = self.lights.state.with_colour(self._edit_stick(), rgb_val)
        if commit:
            self.lights.set_state(new)
            self._persist()
        else:
            self.lights.drag(new)

    # -- events --------------------------------------------------------------
    def action(self, name, payload):
        if name == "lights.close":
            self.host.close_sheet()
            return
        if name == "lights.mode":
            self.lights.set_state(self.lights.state.replace(mode=payload))
            self._persist()
        elif name == "lights.linked":
            self.lights.set_state(self.lights.state.replace(linked=payload))
            self._persist()
        elif name == "lights.stick":
            self.active_stick = payload
        elif name == "lights.preset":
            self._set_colour(rgb_leds.PRESET_BY_NAME[payload], commit=True)
        elif name == "lights.hue":
            self._set_colour(rgb_leds.hue_to_rgb(payload), commit=False)
        elif name == "lights.hue.commit":
            self.lights.drag_flush()
            self._set_colour(rgb_leds.hue_to_rgb(payload), commit=True)
        elif name == "lights.brightness":
            self.lights.drag(self.lights.state.replace(brightness=payload))
        elif name == "lights.brightness.commit":
            self.lights.drag_flush()
            self.lights.set_state(self.lights.state.replace(brightness=payload))
            self._persist()
        self.refresh()
        self._dirty()
