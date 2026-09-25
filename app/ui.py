"""ui - a small touch-first widget kit for rp5deck.

Pure Python: it imports nothing native, so hit-testing, slider maths and
layout are unit-tested off the device. Widgets draw through a Canvas-like
object `g` (gfx.Canvas on the device) with methods fill_rect, round_rect,
stroke_round_rect, circle, ring, line, polygon, text, and a clip_rect
attribute.

Model:
- A Root holds the widget tree and collects damage. Widgets call
  invalidate() when their appearance changes; the main loop redraws and
  uploads only the union of the damaged rectangles, and sleeps when there is
  none (battery handheld: idle means idle).
- A TouchRouter turns pointer streams (one per finger, plus the mouse) into
  press/move/release/cancel calls on whichever widget was hit at press time.
  That widget keeps the pointer until release (capture), so a drag that
  leaves the slider keeps driving the slider.
- Rects are (x, y, w, h) in surface pixels.

Touch sizing: the bottom panel is ~16 px/mm, so the defaults aim for targets
of at least ~130 px and body text of at least 36 px.
"""


def rgb(hexval, a=1.0):
    return (((hexval >> 16) & 255) / 255.0, ((hexval >> 8) & 255) / 255.0,
            (hexval & 255) / 255.0, a)


# Dark, high-contrast theme. BG is deliberately an exact, unusual colour so a
# screenshot check can count "rp5deck pixels" (see B1-RESULTS.md).
BG_RGB = (18, 20, 28)
THEME = {
    "bg": rgb(0x12141C),
    "bar": rgb(0x1B1E27),
    "panel": rgb(0x1F232D),
    "tile": rgb(0x262B37),
    "tile_pressed": rgb(0x3C4558),
    "line": rgb(0x363C4A),
    "text": rgb(0xF4F5F7),
    "dim": rgb(0xA3A9B5),
    "faint": rgb(0x6B7280),
    "accent": rgb(0x3D8BFD),
    "accent_pressed": rgb(0x6AA6FF),
    "track": rgb(0x3A3F4B),
    "disabled": rgb(0x555B66),
    "danger": rgb(0xE5484D),
    "ok": rgb(0x46A758),
    "warn": rgb(0xF5A524),
}

MIN_TARGET = 130      # px, ~8 mm on the bottom panel
BODY_TEXT = 36        # px


# ---------------------------------------------------------------------------
# Rect helpers
# ---------------------------------------------------------------------------
def rect_contains(rect, x, y, pad=0):
    rx, ry, rw, rh = rect
    return rx - pad <= x < rx + rw + pad and ry - pad <= y < ry + rh + pad


def rect_intersects(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and \
        a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def rect_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def rect_clip(a, bounds):
    x0, y0 = max(a[0], bounds[0]), max(a[1], bounds[1])
    x1 = min(a[0] + a[2], bounds[0] + bounds[2])
    y1 = min(a[1] + a[3], bounds[1] + bounds[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def inset(rect, dx, dy=None):
    dy = dx if dy is None else dy
    return (rect[0] + dx, rect[1] + dy, rect[2] - 2 * dx, rect[3] - 2 * dy)


def hsplit(rect, specs, gap=0):
    """Split rect horizontally. specs: fixed widths (int) or None (flex; the
    remaining width is shared equally). Returns one rect per spec."""
    x, y, w, h = rect
    fixed = sum(s for s in specs if s is not None)
    nflex = sum(1 for s in specs if s is None)
    free = max(0, w - fixed - gap * (len(specs) - 1))
    flex = free // nflex if nflex else 0
    out = []
    for s in specs:
        cw = flex if s is None else s
        out.append((x, y, cw, h))
        x += cw + gap
    return out


def vsplit(rect, specs, gap=0):
    x, y, w, h = rect
    t = hsplit((y, x, h, w), specs, gap)
    return [(r[1], r[0], r[3], r[2]) for r in t]


def grid(rect, cols, rows, gap=0):
    """Row-major cells of a cols x rows grid filling rect."""
    out = []
    for r in vsplit(rect, [None] * rows, gap):
        out.extend(hsplit(r, [None] * cols, gap))
    return out


def clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
class Widget:
    interactive = False

    def __init__(self, rect=(0, 0, 0, 0), name=None):
        self.rect = tuple(rect)
        self.parent = None
        self.visible = True
        self.enabled = True
        self.name = name
        self.hit_pad = 0          # grow the touch target beyond the drawn rect

    # -- tree ------------------------------------------------------------
    def root(self):
        w = self
        while w.parent is not None:
            w = w.parent
        return w

    def shown(self):
        w = self
        while w is not None:
            if not w.visible:
                return False
            w = w.parent
        return True

    def invalidate(self, rect=None):
        r = self.root()
        if isinstance(r, Root) and self.shown():
            r.add_damage(rect or self.rect)

    def set_rect(self, rect):
        rect = tuple(rect)
        if rect != self.rect:
            self.invalidate()
            self.rect = rect
            self.invalidate()

    def set_visible(self, v):
        if bool(v) != self.visible:
            if self.visible:
                self.invalidate()
            self.visible = bool(v)
            if self.visible:
                self.invalidate()

    def set_enabled(self, v):
        if bool(v) != self.enabled:
            self.enabled = bool(v)
            self.invalidate()

    # -- input -----------------------------------------------------------
    def contains(self, x, y):
        return rect_contains(self.rect, x, y, self.hit_pad)

    def hit(self, x, y):
        if self.visible and self.enabled and self.interactive and self.contains(x, y):
            return self
        return None

    def on_press(self, pid, x, y):
        pass

    def on_move(self, pid, x, y):
        pass

    def on_release(self, pid, x, y):
        pass

    def on_cancel(self, pid):
        pass

    # -- drawing ---------------------------------------------------------
    def draw(self, g):
        pass

    def paint(self, g):
        if self.visible and rect_intersects(self.rect, g.clip_rect):
            self.draw(g)

    def walk(self):
        yield self


class Container(Widget):
    def __init__(self, rect=(0, 0, 0, 0), name=None, bg=None):
        Widget.__init__(self, rect, name)
        self.children = []
        # bg may be an rgba tuple (frozen forever) OR a THEME key string,
        # resolved live by bg_color() on every draw - Appearance
        # (palettes.py) needs every already-built screen's background to
        # follow a runtime theme change with no restart, and mutating
        # THEME in place (see palettes.apply_theme()'s docstring) only
        # reaches call sites that read THEME live at draw time. Every
        # Container/Root construction in this app passes a THEME[...]
        # value here, never an unrelated literal colour, so this file's
        # own call sites (and screens.py/app_tabs.py/swap_ui.py's) were
        # migrated to pass the bare key string instead.
        self.bg = bg

    def bg_color(self):
        return THEME[self.bg] if isinstance(self.bg, str) else self.bg

    def add(self, w):
        w.parent = self
        self.children.append(w)
        w.invalidate()
        return w

    def remove(self, w):
        if w in self.children:
            w.invalidate()
            self.children.remove(w)
            w.parent = None

    def clear(self):
        for c in list(self.children):
            self.remove(c)

    def hit(self, x, y):
        if not self.visible or not self.enabled:
            return None
        for c in reversed(self.children):          # topmost first
            h = c.hit(x, y)
            if h is not None:
                return h
        return Widget.hit(self, x, y)

    def draw(self, g):
        bg = self.bg_color()
        if bg is not None:
            g.fill_rect(self.rect, bg)
        for c in self.children:
            c.paint(g)

    def paint(self, g):
        if self.visible and rect_intersects(self.rect, g.clip_rect):
            self.draw(g)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class Root(Container):
    """Top of the tree: owns the surface size and the damage list."""

    def __init__(self, w=0, h=0, bg=None):
        Container.__init__(self, (0, 0, w, h), "root", bg)
        self._damage = None

    def resize(self, w, h):
        self.rect = (0, 0, w, h)
        self.damage_all()

    def add_damage(self, rect):
        r = rect_clip(tuple(int(v) for v in _grow(rect)), self.rect)
        if r is not None:
            self._damage = rect_union(self._damage, r)

    def damage_all(self):
        self._damage = self.rect if self.rect[2] and self.rect[3] else None

    def peek_damage(self):
        return self._damage

    def take_damage(self):
        d, self._damage = self._damage, None
        return d

    def invalidate(self, rect=None):
        self.add_damage(rect or self.rect)


def _grow(rect, by=2):
    # Anti-aliased edges spill a pixel; damage a little more than the rect.
    return (rect[0] - by, rect[1] - by, rect[2] + 2 * by, rect[3] + 2 * by)


class Label(Widget):
    def __init__(self, text="", rect=(0, 0, 0, 0), size=BODY_TEXT, color=None,
                 bold=False, align="left", valign="middle", name=None):
        Widget.__init__(self, rect, name)
        self.text = text
        self.size = size
        # color, like Container.bg above, may be an rgba tuple (frozen) or
        # a THEME key string (resolved live by color_value()). Unstyled
        # text (color=None, the common case) now defaults to the live key
        # "text" instead of eagerly resolving THEME["text"] once - so a
        # runtime theme change (Appearance) recolors it with no restart.
        # A caller that still passes an already-resolved THEME["dim"]-
        # style tuple keeps today's frozen-at-construction behaviour
        # exactly (isinstance check below), so this is purely additive;
        # settings_view.py/appearance_view.py's own Labels were migrated
        # to pass the bare key ("dim", "warn", ...) instead, for the one
        # screen an owner is actually looking at when they change this
        # setting.
        self.color = "text" if color is None else color
        self.bold = bold
        self.align = align
        self.valign = valign

    def color_value(self):
        return THEME[self.color] if isinstance(self.color, str) else self.color

    def set_text(self, text, color=None):
        color = color or self.color
        if text != self.text or color != self.color:
            self.text = text
            self.color = color
            self.invalidate()

    def draw(self, g):
        if self.text:
            g.text(self.text, self.rect, self.size, self.color_value(), self.bold,
                   self.align, self.valign)


class Button(Widget):
    """Press feedback on touch-down; on_click fires on release inside."""
    interactive = True

    def __init__(self, text="", rect=(0, 0, 0, 0), on_click=None, name=None,
                 size=40, icon=None, radius=22):
        Widget.__init__(self, rect, name)
        self.text = text
        self.on_click = on_click
        self.size = size
        self.icon = icon            # callable(g, rect, color) or None
        self.radius = radius
        self.pressed = False
        self._pid = None

    def _set_pressed(self, v):
        if v != self.pressed:
            self.pressed = v
            self.invalidate()

    def on_press(self, pid, x, y):
        self._pid = pid
        self._set_pressed(True)

    def on_move(self, pid, x, y):
        if pid == self._pid:
            self._set_pressed(self.contains(x, y))

    def on_release(self, pid, x, y):
        if pid != self._pid:
            return
        self._pid = None
        fire = self.pressed and self.contains(x, y)
        self._set_pressed(False)
        if fire:
            self.clicked()

    def on_cancel(self, pid):
        if pid == self._pid:
            self._pid = None
            self._set_pressed(False)

    def clicked(self):
        if self.on_click:
            self.on_click()

    def colors(self):
        if not self.enabled:
            return THEME["panel"], THEME["disabled"]
        return (THEME["tile_pressed"] if self.pressed else THEME["tile"]), THEME["text"]

    def draw(self, g):
        bg, fg = self.colors()
        g.round_rect(self.rect, self.radius, bg)
        x, y, w, h = self.rect
        if self.icon and self.text:
            isz = min(h * 0.55, w * 0.4)
            self.icon(g, (x + 24, y + (h - isz) / 2, isz, isz), fg)
            g.text(self.text, (x + 36 + isz, y, w - 48 - isz, h), self.size, fg, True)
        elif self.icon:
            isz = min(w, h) * 0.6
            self.icon(g, (x + (w - isz) / 2, y + (h - isz) / 2, isz, isz), fg)
        else:
            g.text(self.text, self.rect, self.size, fg, True, "center")


class Tile(Button):
    """A big home-screen button: icon above, label below."""

    def __init__(self, text="", rect=(0, 0, 0, 0), on_click=None, name=None,
                 icon=None, subtitle=""):
        Button.__init__(self, text, rect, on_click, name, 48, icon, 28)
        self.subtitle = subtitle
        self.subtitle_size = BODY_TEXT      # I2: screens.Home shrinks it in a 5-column grid

    def draw(self, g):
        bg, fg = self.colors()
        g.round_rect(self.rect, self.radius, bg)
        x, y, w, h = self.rect
        isz = min(w, h) * 0.38
        if self.icon:
            self.icon(g, (x + (w - isz) / 2, y + h * 0.14, isz, isz), fg)
        g.text(self.text, (x + 12, y + h * 0.60, w - 24, 64), self.size, fg, True, "center")
        if self.subtitle:
            g.text(self.subtitle, (x + 12, y + h * 0.60 + 66, w - 24, 44), self.subtitle_size,
                   THEME["dim"], False, "center")


class LitButton(Button):
    """A Button that can be shown "lit" (accent colour) to say a thing it
    controls is on - e.g. the browser strip's Keyboard button while the
    on-screen keyboard is up. Tapping it still just calls on_click; the
    controller decides the lit state (set_lit), so it cannot drift from
    what is really on screen."""

    def __init__(self, text="", rect=(0, 0, 0, 0), on_click=None, name=None, size=40,
                 icon=None, radius=22):
        Button.__init__(self, text, rect, on_click, name, size, icon, radius)
        self.lit = False

    def set_lit(self, v):
        if bool(v) != self.lit:
            self.lit = bool(v)
            self.invalidate()

    def colors(self):
        if self.enabled and self.lit and not self.pressed:
            return THEME["accent"], THEME["text"]
        return Button.colors(self)


class Toggle(Button):
    """Two-state button. on_toggle(new_state) fires on a completed tap.
    set_state() changes it from outside without firing the callback."""

    def __init__(self, rect=(0, 0, 0, 0), state=False, on_toggle=None, name=None,
                 text_on="On", text_off="Off", icon_on=None, icon_off=None,
                 size=40):
        Button.__init__(self, "", rect, None, name, size)
        self.state = bool(state)
        self.on_toggle = on_toggle
        self.text_on, self.text_off = text_on, text_off
        self.icon_on, self.icon_off = icon_on, icon_off

    def set_state(self, s):
        s = bool(s)
        if s != self.state:
            self.state = s
            self.invalidate()

    def clicked(self):
        self.state = not self.state
        self.invalidate()
        if self.on_toggle:
            self.on_toggle(self.state)

    def colors(self):
        if not self.enabled:
            return THEME["panel"], THEME["disabled"]
        if self.pressed:
            return THEME["tile_pressed"], THEME["text"]
        if self.state:
            return THEME["danger"], THEME["text"]
        return Button.colors(self)

    def draw(self, g):
        self.text = self.text_on if self.state else self.text_off
        self.icon = self.icon_on if self.state else self.icon_off
        Button.draw(self, g)


class Slider(Widget):
    """Horizontal slider, value 0..1.

    Touch-down never moves the knob (RV1-M1: a swipe that merely starts on a
    slider must not change the volume). A stroke is judged once, by the first
    axis to travel SLOP px:
      sideways  the drag engages and is RELATIVE: the knob moves by the
                finger's travel since touch-down, from wherever it was
      vertical  the stroke is not for the slider; it changes nothing
      neither   a clean tap: the value jumps to the tap on RELEASE
    on_change(v) fires whenever the (stepped) value changes during a press.
    Every press then ends in exactly one of:
      on_release(v)            a real release that changed the value (commit)
      on_cancel(v0, changed)   anything else: a cancel (the router claimed the
                               stroke as a gesture, a view closed, shutdown) or
                               a release that changed nothing. The value is
                               restored to v0, the one it had at touch-down;
                               nothing is to be committed. Without an
                               on_cancel, a changed value is restored through
                               on_change(v0) so readouts follow.
    The knob sits inside the rect, so 0 and 1 are reachable at the edges.
    External set_value() calls are ignored while a finger is down, so a
    change notification cannot yank the knob from under the finger."""
    interactive = True
    SLOP = 24                   # px (~1.5 mm) of travel before a stroke is judged

    def __init__(self, rect=(0, 0, 0, 0), value=0.0, step=0.01, knob_r=46,
                 on_change=None, on_release=None, name=None, on_cancel=None):
        Widget.__init__(self, rect, name)
        self.value = clamp01(float(value))
        self.step = step
        self.knob_r = knob_r
        self.on_change = on_change
        self.on_release_cb = on_release
        self.on_cancel_cb = on_cancel
        self.dragging = False       # a finger is down on the slider
        self._pid = None
        self._phase = None          # "pending" | "drag" | "ignore"
        self._x0 = self._y0 = 0.0
        self._v0 = self.value
        self._kx0 = 0.0
        self._changed = False

    def track(self):
        x, y, w, h = self.rect
        return x + self.knob_r, x + w - self.knob_r

    def value_at(self, px):
        x0, x1 = self.track()
        if x1 <= x0:
            return 0.0
        f = clamp01((px - x0) / float(x1 - x0))
        if self.step:
            f = clamp01(round(f / self.step) * self.step)
        return round(f, 6)

    def x_for(self, v):
        x0, x1 = self.track()
        return x0 + clamp01(v) * (x1 - x0)

    def _set(self, v):
        if v != self.value:
            self.value = v
            self._changed = True
            self.invalidate()
            if self.on_change:
                self.on_change(v)

    def _drag_to(self, x):
        # relative: the knob keeps the offset it had from the finger at touch-down
        self._set(self.value_at(self._kx0 + (x - self._x0)))

    def _judge(self, x, y):
        if self._phase != "pending":
            return
        dx, dy = abs(x - self._x0), abs(y - self._y0)
        if dx >= self.SLOP and dx > dy:
            self._phase = "drag"
        elif dy >= self.SLOP and dy >= dx:
            self._phase = "ignore"
            self.invalidate()           # the knob drops its pressed look

    def on_press(self, pid, x, y):
        if self.dragging:
            return                  # a second finger does not steal the knob
        self.dragging = True
        self._pid = pid
        self._phase = "pending"
        self._x0, self._y0 = x, y
        self._v0 = self.value
        self._kx0 = self.x_for(self.value)
        self._changed = False
        self.invalidate()

    def on_move(self, pid, x, y):
        if self.dragging and pid == self._pid:
            self._judge(x, y)
            if self._phase == "drag":
                self._drag_to(x)

    def _end(self):
        self.dragging = False
        self._pid = None
        self._phase = None
        self.invalidate()

    def on_release(self, pid, x, y):
        if not (self.dragging and pid == self._pid):
            return
        self._judge(x, y)
        if self._phase == "drag":
            self._drag_to(x)
        elif self._phase == "pending":
            self._set(self.value_at(x))         # a clean tap: set on release
        self._end()
        if self._changed:
            if self.on_release_cb:
                self.on_release_cb(self.value)
        else:
            self._restore()

    def on_cancel(self, pid):
        if self.dragging and pid == self._pid:
            self._end()
            self._restore()

    def _restore(self):
        v0, changed = self._v0, self._changed
        self._changed = False
        if changed and self.value != v0:
            self.value = v0
            self.invalidate()
        if self.on_cancel_cb:
            self.on_cancel_cb(v0, changed)
        elif changed and self.on_change:
            self.on_change(v0)

    def set_value(self, v):
        if self.dragging:
            return False
        v = clamp01(float(v))
        if v != self.value:
            self.value = v
            self.invalidate()
        return True

    def draw(self, g):
        x, y, w, h = self.rect
        x0, x1 = self.track()
        cy = y + h / 2.0
        th = 26
        on = self.enabled
        g.round_rect((x0 - th / 2, cy - th / 2, x1 - x0 + th, th), th / 2, THEME["track"])
        kx = self.x_for(self.value)
        if on:
            g.round_rect((x0 - th / 2, cy - th / 2, kx - x0 + th, th), th / 2, THEME["accent"])
        pressed = self.dragging and self._phase != "ignore"
        r = self.knob_r * (0.92 if pressed else 0.8)
        g.circle(kx, cy, r, THEME["text"] if on else THEME["disabled"])
        if pressed:
            g.ring(kx, cy, r + 6, THEME["accent_pressed"], 5)


class Sheet(Container):
    """Full-size panel with a header: a big Back control and a title."""
    HEADER_H = 140

    def __init__(self, title="", on_close=None, name=None):
        Container.__init__(self, name=name, bg="bg")
        self.back = self.add(Button("Back", on_click=on_close,
                                    name=(name or "sheet") + ".back", size=44,
                                    icon=icon_back))
        self.title = self.add(Label(title, size=52, bold=True, align="center"))
        self.body = self.add(Container(name=(name or "sheet") + ".body"))

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        self.back.set_rect((x + 20, y + 10, 280, self.HEADER_H - 20))
        self.title.set_rect((x + 320, y, w - 640, self.HEADER_H))
        self.body.set_rect((x, y + self.HEADER_H, w, h - self.HEADER_H))

    def draw(self, g):
        Container.draw(self, g)
        x, y, w, h = self.rect
        g.fill_rect((x, y + self.HEADER_H - 2, w, 2), THEME["line"])


def icon_back(g, r, color):
    x, y, w, h = r
    t = max(4, w * 0.12)
    g.line(x + w * 0.65, y + h * 0.15, x + w * 0.3, y + h * 0.5, color, t)
    g.line(x + w * 0.3, y + h * 0.5, x + w * 0.65, y + h * 0.85, color, t)


# ---------------------------------------------------------------------------
# Text entry inside rp5deck's own panel (HF1): a field and a built-in
# keyboard. Both are ordinary widgets on the layer surface, so typing here
# never involves keyboard focus at all - the surface keeps keyboard
# interactivity NONE and the keys are just buttons.
# ---------------------------------------------------------------------------
class TextField(Button):
    """A one-line text box. Tapping it calls on_focus (the owner of the
    field shows a Keyboard); the text is edited through insert/backspace/
    clear, never by a real keyboard. `focused` only draws the caret."""

    def __init__(self, placeholder="", on_focus=None, name=None, size=44, max_len=200):
        Button.__init__(self, "", on_click=on_focus, name=name, size=size, radius=18)
        self.placeholder = placeholder
        self.max_len = max_len
        self.focused = False

    def set_text(self, text):
        text = (text or "")[:self.max_len]
        if text != self.text:
            self.text = text
            self.invalidate()

    def insert(self, s):
        self.set_text(self.text + s)

    def backspace(self):
        if self.text:
            self.set_text(self.text[:-1])

    def clear(self):
        self.set_text("")

    def set_focused(self, v):
        if bool(v) != self.focused:
            self.focused = bool(v)
            self.invalidate()

    def draw(self, g):
        x, y, w, h = self.rect
        g.round_rect(self.rect, self.radius, THEME["panel"])
        g.stroke_round_rect(self.rect, self.radius,
                            THEME["accent"] if self.focused else THEME["line"], 4)
        inner = (x + 28, y, w - 56, h)
        if self.text:
            # show the END of a long entry: that is where the caret is
            shown = self.text if len(self.text) <= 48 else "…" + self.text[-47:]
            g.text(shown + ("|" if self.focused else ""), inner, self.size, THEME["text"])
        else:
            g.text(("|" if self.focused else "") + self.placeholder, inner, self.size,
                   THEME["faint"])


# Key specs: a plain string is a character key of width 1; a tuple is
# (action, width units). Every row adds up to 10 units.
KB_LAYERS = {
    "letters": [
        list("qwertyuiop"),
        list("asdfghjkl'"),
        [("shift", 1.5)] + list("zxcvbnm") + [("bksp", 1.5)],
        [("sym", 1.5), ",", ("space", 5), ".", ("enter", 1.5)],
    ],
    "sym": [
        list("1234567890"),
        list("@#&-_()/:;"),
        [("clear", 1.5)] + list("!?+=*%\"") + [("bksp", 1.5)],
        [("letters", 1.5), ",", ("space", 5), ".", ("enter", 1.5)],
    ],
}
KB_LABELS = {"shift": "Shift", "bksp": "Delete", "sym": "?123", "letters": "abc",
             "space": "", "enter": "Enter", "clear": "Clear"}


class Keyboard(Container):
    """A built-in on-screen keyboard for rp5deck's own text fields.

    on_text(s) gets each typed character (a space is " "); on_backspace(),
    on_clear() and on_enter() the edit keys (key ids are unique per layer).
    Shift is one-shot: it uppercases the next letter and then turns itself
    off. press(key) is the whole key logic, public so tests drive it
    without geometry."""

    ROWS = 4
    GAP = 14

    def __init__(self, on_text=None, on_backspace=None, on_enter=None, enter_label="Enter",
                 name="kb", on_clear=None):
        Container.__init__(self, name=name, bg="bar")
        self.on_text, self.on_backspace, self.on_enter = on_text, on_backspace, on_enter
        self.on_clear = on_clear
        self.enter_label = enter_label
        self.layer = "letters"
        self.shift = False
        self.keys = {}              # key id -> Button
        self._build()

    @staticmethod
    def _spec(k):
        return (k, 1) if isinstance(k, str) else k

    def key_id(self, k):
        kid, _ = self._spec(k)
        return kid

    def _label(self, kid):
        if kid == "enter":
            return self.enter_label
        if kid in KB_LABELS:
            return KB_LABELS[kid]
        return kid.upper() if (self.shift and self.layer == "letters") else kid

    def _build(self):
        self.clear()
        self.keys = {}
        for row in KB_LAYERS[self.layer]:
            for k in row:
                kid = self.key_id(k)
                btn = (LitButton if kid == "shift" else Button)(
                    self._label(kid), on_click=lambda kid=kid: self.press(kid),
                    name="%s.key.%s" % (self.name, kid), size=48, radius=16)
                if kid == "shift":
                    btn.set_lit(self.shift)
                self.keys[kid] = self.add(btn)
        if self.rect[2] and self.rect[3]:
            self.layout(self.rect)

    def _relabel(self):
        for kid, btn in self.keys.items():
            btn.text = self._label(kid)
            btn.invalidate()
        if "shift" in self.keys:
            self.keys["shift"].set_lit(self.shift)

    def press(self, kid):
        if kid == "shift":
            self.shift = not self.shift
            self._relabel()
        elif kid in ("sym", "letters"):
            self.layer, self.shift = kid, False
            self._build()
        elif kid == "bksp":
            if self.on_backspace:
                self.on_backspace()
        elif kid == "clear":
            if self.on_clear:
                self.on_clear()
        elif kid == "enter":
            if self.on_enter:
                self.on_enter()
        else:
            ch = " " if kid == "space" else kid
            if self.shift and self.layer == "letters":
                ch = ch.upper()
                self.shift = False
                self._relabel()
            if self.on_text:
                self.on_text(ch)

    def layout(self, rect):
        self.set_rect(rect)
        x, y, w, h = rect
        rows = vsplit(inset(rect, self.GAP), [None] * self.ROWS, self.GAP)
        for row, (rx, ry, rw, rh) in zip(KB_LAYERS[self.layer], rows):
            unit = (rw - self.GAP * 9) / 10.0     # ten 1-unit cells and nine gaps
            cx = rx
            for k in row:
                kid, units = self._spec(k)
                kw = unit * units + self.GAP * (units - 1)
                self.keys[kid].set_rect((int(round(cx)), ry, int(round(kw)), rh))
                cx += kw + self.GAP


class SwipeRecognizer:
    """Watches every pointer stroke alongside the widgets.

    A stroke becomes a gesture once it has travelled `distance` px and is
    mostly vertical (|dy| >= 2|dx|). Names:
      swipe_down_from_top    started within `edge` px of the top
      swipe_down             started anywhere else
      swipe_up_from_bottom   started within `edge` px of the bottom
      swipe_up               started anywhere else
    Each stroke is judged once: a stroke that goes `distance` px sideways
    first (a slider drag) is never a gesture. If wants(name) is true the
    router takes the pointer away from its widget (on_cancel) and calls
    on_gesture(name); otherwise the widget keeps it."""

    def __init__(self, height_fn, edge=60, distance=120, wants=None, on_gesture=None):
        self.height_fn = height_fn
        self.edge = edge
        self.distance = distance
        self.wants = wants or (lambda name: False)
        self.on_gesture = on_gesture
        self.strokes = {}           # pid -> [x0, y0, decided]

    def start(self, pid, x, y):
        self.strokes[pid] = [x, y, False]

    def classify(self, x0, y0, x, y):
        dx, dy = x - x0, y - y0
        if abs(dx) >= self.distance and abs(dx) > abs(dy) / 2.0:
            return False            # horizontal: never a swipe
        if abs(dy) < self.distance or abs(dy) < 2 * abs(dx):
            return None             # undecided yet
        if dy > 0:
            return "swipe_down_from_top" if y0 < self.edge else "swipe_down"
        return "swipe_up_from_bottom" if y0 >= self.height_fn() - self.edge else "swipe_up"

    def move(self, pid, x, y):
        """Returns a gesture name if this move completes a wanted gesture."""
        st = self.strokes.get(pid)
        if st is None or st[2]:
            return None
        g = self.classify(st[0], st[1], x, y)
        if g is None:
            return None
        st[2] = True
        return g if g and self.wants(g) else None

    def end(self, pid):
        self.strokes.pop(pid, None)


class TouchRouter:
    """Routes pointer streams to widgets with capture, and to an optional
    SwipeRecognizer.

    pid identifies a pointer: ('f', finger_id) for touch, ('m', 0) for the
    mouse / seat pointer. The widget hit on press receives every later event
    of that pointer until release or cancel, even outside its rect - unless
    the recogniser claims the stroke as a gesture, in which case the widget
    gets on_cancel and the rest of the stroke goes nowhere."""

    def __init__(self, root, log=None, gestures=None):
        self.root = root
        self.captures = {}
        self.claimed = set()
        self.log = log
        self.gestures = gestures

    def down(self, pid, x, y):
        if pid in self.captures or pid in self.claimed:   # a lost release; close it first
            self.cancel(pid)
        if self.gestures:
            self.gestures.start(pid, x, y)
        w = self.root.hit(x, y)
        if self.log:
            self.log("input down %s (%.0f,%.0f) -> %s"
                     % (_pid_str(pid), x, y, (w.name or type(w).__name__) if w else "nothing"))
        if w is not None:
            self.captures[pid] = w
            w.on_press(pid, x, y)
        return w

    def move(self, pid, x, y):
        if pid in self.claimed:
            return
        if self.gestures:
            g = self.gestures.move(pid, x, y)
            if g:
                self.claimed.add(pid)
                w = self.captures.pop(pid, None)
                if w is not None:
                    w.on_cancel(pid)
                if self.log:
                    self.log("gesture %s by %s" % (g, _pid_str(pid)))
                if self.gestures.on_gesture:
                    self.gestures.on_gesture(g)
                return
        w = self.captures.get(pid)
        if w is not None:
            w.on_move(pid, x, y)

    def up(self, pid, x, y):
        if self.gestures:
            self.gestures.end(pid)
        if pid in self.claimed:
            self.claimed.discard(pid)
            return None
        w = self.captures.pop(pid, None)
        if self.log:
            self.log("input up   %s (%.0f,%.0f) -> %s"
                     % (_pid_str(pid), x, y, (w.name or type(w).__name__) if w else "nothing"))
        if w is not None:
            w.on_release(pid, x, y)
        return w

    def cancel(self, pid):
        if self.gestures:
            self.gestures.end(pid)
        self.claimed.discard(pid)
        w = self.captures.pop(pid, None)
        if w is not None:
            w.on_cancel(pid)

    def cancel_all(self):
        for pid in list(self.captures) + list(self.claimed):
            self.cancel(pid)


def _pid_str(pid):
    return "%s:%s" % pid if isinstance(pid, tuple) else str(pid)
