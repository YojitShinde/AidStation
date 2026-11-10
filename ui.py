"""
AidStation – AI-powered medical emergency response.

Tkinter UI with two modules:
  • MediRoute   – describe a concern, get suitable hospitals (nearest first) and a
               route map to the selected one.
  • MediPilot – source-grounded paramedic assistant backed by WMS documents.

The look is built from a few small, self-contained widgets (rounded panels,
pill buttons, a segmented control, slim scroll areas) rendered with Pillow so
corners and shadows stay crisp and anti-aliased on every platform.
"""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

from agent import run_agent_detailed
from aid_pilot import ask as ask_aid_pilot
from aid_pilot import default_sources, read_source
from location_service import DeviceLocation, get_device_location
from maps_service import directions_url, get_route_map

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Palette (unchanged – matches the styled map)
# ----------------------------------------------------------------------------
CREAM = "#FFF8F2"
NAVY = "#0E425F"
RED = "#CC0000"
BLUSH = "#F3D6D6"
BORDER = "#E3B5AE"
MUTED = "#5B6B76"
WHITE = "#FFFFFF"
OK = "#1B7F3B"
WARN = "#B45309"

# Interaction tones derived from the palette above
RED_DARK = "#A30000"       # pressed / hover of RED (already used before)
NAVY_DARK = "#0A3248"      # pressed / hover of NAVY (already used before)
INK = "#222222"            # body text (already used before)
SELECT_TINT = "#FCEDEA"    # halfway between CREAM and BLUSH
DIVIDER = "#F1E1DB"        # a softer BORDER for hairlines
OK_TINT = "#E8F3EB"
WARN_TINT = "#FBF0E2"
SHADOW_RGB = (14, 66, 95)  # NAVY, used at low alpha for elevation

FONT = "Segoe UI"
EMERGENCY_NOTE = "For a life-threatening situation, call 112 immediately."

QUICK_PROMPTS = [
    ("Chest pain", "Severe chest pain spreading to the left arm, with sweating "
                   "and shortness of breath."),
    ("Breathing trouble", "Sudden difficulty breathing and wheezing; lips are "
                          "turning bluish."),
    ("Stroke signs", "Sudden facial droop, slurred speech and weakness on one "
                     "side of the body."),
    ("Major injury", "Road accident with heavy bleeding and a suspected head injury."),
    ("Pregnancy", "Pregnant woman at 36 weeks with severe abdominal pain and bleeding."),
    ("Child emergency", "Child with a high fever who had a seizure and is now drowsy."),
]

AID_SUGGESTIONS = [
    "Hiker pulled from cold water, shivering violently and confused.",
    "Snakebite on the lower leg 20 minutes ago, swelling is spreading.",
    "Suspected heat stroke during a long trek, patient is not sweating.",
]

# ----------------------------------------------------------------------------
# Scaling & fonts
# ----------------------------------------------------------------------------
_SCALE = 1.0


def px(value: float) -> int:
    """Logical (96-dpi) pixels → device pixels."""
    if not value:
        return 0
    return max(1, int(round(value * _SCALE)))


def F(size: int, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT, size, weight)


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # older Windows or restricted environment
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _pick_font(root: tk.Misc) -> None:
    global FONT
    families = set(tkfont.families(root))
    for candidate in ("Segoe UI", "Inter", "Helvetica Neue", "Arial"):
        if candidate in families:
            FONT = candidate
            return


# ----------------------------------------------------------------------------
# Pillow-rendered shapes and icons (anti-aliased, cached)
# ----------------------------------------------------------------------------
_SS = 4  # supersampling factor
_shape_cache: dict[tuple, ImageTk.PhotoImage] = {}
_icon_cache: dict[tuple, ImageTk.PhotoImage] = {}


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore


def _render_round(w: int, h: int, r: int, fill: str, border: str | None,
                  bw: int, shadow: int) -> Image.Image:
    """Directly render a (small) rounded rectangle with optional soft shadow."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    m = shadow
    if m:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        drop = max(1, m // 3)
        ImageDraw.Draw(sh).rounded_rectangle(
            (m, m + drop, w - m - 1, h - m - 1 + drop), r, fill=SHADOW_RGB + (30,))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(m / 2.2)))
    iw, ih = w - 2 * m, h - 2 * m
    if iw <= 0 or ih <= 0:
        return img
    big = Image.new("RGBA", (iw * _SS, ih * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    rr = min(r * _SS, big.width // 2, big.height // 2)
    box = (0, 0, big.width - 1, big.height - 1)
    if border and bw:
        b = bw * _SS
        d.rounded_rectangle(box, rr, fill=_rgb(border) + (255,))
        d.rounded_rectangle((b, b, big.width - 1 - b, big.height - 1 - b),
                            max(rr - b, 0), fill=_rgb(fill) + (255,))
    else:
        d.rounded_rectangle(box, rr, fill=_rgb(fill) + (255,))
    img.alpha_composite(big.resize((iw, ih), Image.LANCZOS), (m, m))
    return img


def rounded_image(w: int, h: int, r: int, fill: str, border: str | None = None,
                  bw: int = 0, shadow: int = 0) -> ImageTk.PhotoImage:
    """Rounded rectangle of any size, assembled from a small 9-slice render."""
    key = (w, h, r, fill, border, bw, shadow)
    cached = _shape_cache.get(key)
    if cached is not None:
        return cached
    c = r + shadow + max(2, shadow // 2) + 2      # corner block size
    s = 2 * c + 2
    if w <= s or h <= s:
        img = _render_round(w, h, r, fill, border, bw, shadow)
    else:
        base = _render_round(s, s, r, fill, border, bw, shadow)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        mw, mh = w - 2 * c, h - 2 * c
        img.paste(base.crop((0, 0, c, c)), (0, 0))
        img.paste(base.crop((s - c, 0, s, c)), (w - c, 0))
        img.paste(base.crop((0, s - c, c, s)), (0, h - c))
        img.paste(base.crop((s - c, s - c, s, s)), (w - c, h - c))
        img.paste(base.crop((c, 0, s - c, c)).resize((mw, c)), (c, 0))
        img.paste(base.crop((c, s - c, s - c, s)).resize((mw, c)), (c, h - c))
        img.paste(base.crop((0, c, c, s - c)).resize((c, mh)), (0, c))
        img.paste(base.crop((s - c, c, s, s - c)).resize((c, mh)), (w - c, c))
        img.paste(base.crop((c, c, s - c, s - c)).resize((mw, mh)), (c, c))
    photo = ImageTk.PhotoImage(img)
    if len(_shape_cache) > 400:
        for old in list(_shape_cache)[:200]:
            del _shape_cache[old]
    _shape_cache[key] = photo
    return photo


def icon(name: str, size: int, color: str) -> ImageTk.PhotoImage:
    """Small line/solid icons drawn with Pillow."""
    key = (name, size, color)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    s = size * _SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = _rgb(color) + (255,)
    clear = (0, 0, 0, 0)
    lw = max(_SS, int(s * 0.1))

    def P(*pts: float) -> list[tuple[float, float]]:
        return [(pts[i] * s, pts[i + 1] * s) for i in range(0, len(pts), 2)]

    if name == "logo":
        d.rounded_rectangle((0, 0, s - 1, s - 1), int(s * 0.3), fill=c)
        t = s * 0.105
        white = (255, 255, 255, 255)
        d.rounded_rectangle((s / 2 - t, s * 0.22, s / 2 + t, s * 0.78), t * 0.6, fill=white)
        d.rounded_rectangle((s * 0.22, s / 2 - t, s * 0.78, s / 2 + t), t * 0.6, fill=white)
    elif name == "pin":
        d.ellipse(P(0.18, 0.04, 0.82, 0.68), fill=c)
        d.polygon(P(0.24, 0.48, 0.76, 0.48, 0.5, 0.96), fill=c)
        d.ellipse(P(0.37, 0.23, 0.63, 0.49), fill=clear)
    elif name == "phone":
        d.rounded_rectangle(P(0.27, 0.06, 0.73, 0.94), int(s * 0.1), outline=c, width=lw)
        d.line(P(0.43, 0.8, 0.57, 0.8), fill=c, width=lw)
    elif name == "clock":
        d.ellipse(P(0.08, 0.08, 0.92, 0.92), outline=c, width=lw)
        d.line(P(0.5, 0.26, 0.5, 0.52, 0.68, 0.62), fill=c, width=lw, joint="curve")
    elif name == "route":
        d.line(P(0.26, 0.3, 0.26, 0.5, 0.74, 0.5, 0.74, 0.7), fill=c, width=lw, joint="curve")
        d.ellipse(P(0.1, 0.06, 0.42, 0.38), outline=c, width=lw)
        d.ellipse(P(0.58, 0.62, 0.9, 0.94), fill=c)
    elif name == "doc":
        d.line(P(0.62, 0.06, 0.2, 0.06, 0.2, 0.94, 0.8, 0.94, 0.8, 0.24, 0.62, 0.06,
                 0.62, 0.24, 0.8, 0.24), fill=c, width=lw, joint="curve")
        for y in (0.48, 0.64, 0.8):
            d.line(P(0.34, y, 0.66, y), fill=c, width=max(_SS, lw - _SS))
    elif name == "send":
        d.polygon(P(0.5, 0.12, 0.86, 0.5, 0.62, 0.5, 0.62, 0.88, 0.38, 0.88, 0.38, 0.5,
                    0.14, 0.5), fill=c)
    elif name == "arrow":
        d.line(P(0.12, 0.5, 0.84, 0.5), fill=c, width=lw)
        d.line(P(0.56, 0.22, 0.86, 0.5, 0.56, 0.78), fill=c, width=lw, joint="curve")
    elif name == "external":
        d.line(P(0.22, 0.78, 0.76, 0.24), fill=c, width=lw)
        d.line(P(0.36, 0.22, 0.78, 0.22, 0.78, 0.64), fill=c, width=lw, joint="curve")
    elif name == "plus":
        d.line(P(0.5, 0.16, 0.5, 0.84), fill=c, width=lw)
        d.line(P(0.16, 0.5, 0.84, 0.5), fill=c, width=lw)
    elif name == "eye":
        d.ellipse(P(0.06, 0.24, 0.94, 0.76), outline=c, width=lw)
        d.ellipse(P(0.36, 0.36, 0.64, 0.64), fill=c)
    elif name == "trash":
        d.line(P(0.14, 0.24, 0.86, 0.24), fill=c, width=lw)
        d.line(P(0.38, 0.24, 0.38, 0.1, 0.62, 0.1, 0.62, 0.24), fill=c, width=lw)
        d.line(P(0.24, 0.3, 0.3, 0.92, 0.7, 0.92, 0.76, 0.3), fill=c, width=lw, joint="curve")
    elif name == "alert":
        d.polygon(P(0.5, 0.06, 0.96, 0.9, 0.04, 0.9), fill=c)
        d.rounded_rectangle(P(0.45, 0.34, 0.55, 0.62), int(s * 0.04), fill=clear)
        d.ellipse(P(0.44, 0.68, 0.56, 0.8), fill=clear)
    elif name == "copy":
        d.rounded_rectangle(P(0.3, 0.3, 0.9, 0.9), int(s * 0.1), outline=c, width=lw)
        d.line(P(0.1, 0.62, 0.1, 0.1, 0.62, 0.1), fill=c, width=lw, joint="curve")
    elif name == "check":
        d.line(P(0.14, 0.52, 0.4, 0.78, 0.88, 0.24), fill=c, width=lw, joint="curve")
    elif name == "dot":
        d.ellipse(P(0.2, 0.2, 0.8, 0.8), fill=c)
    elif name == "spark":
        d.polygon(P(0.5, 0.04, 0.62, 0.38, 0.96, 0.5, 0.62, 0.62, 0.5, 0.96, 0.38, 0.62,
                    0.04, 0.5, 0.38, 0.38), fill=c)
    photo = ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))
    _icon_cache[key] = photo
    return photo


def recolor(widget: tk.Misc, old: str, new: str) -> None:
    """Swap background ``old`` → ``new`` on a widget subtree."""
    for child in widget.winfo_children():
        try:
            if str(child.cget("bg")).lower() == old.lower():
                child.configure(bg=new)
        except tk.TclError:
            pass
        if not isinstance(child, (PillButton, RoundedPanel)):
            recolor(child, old, new)
        elif isinstance(child, RoundedPanel) and child.fill.lower() == old.lower():
            recolor(child, old, new)


# ----------------------------------------------------------------------------
# Widgets
# ----------------------------------------------------------------------------
class RoundedPanel(tk.Canvas):
    """A rounded, optionally elevated surface. Put children in ``.body``.

    fit="none"   – fills whatever size the geometry manager gives it.
    fit="height" – width from the parent, height hugs the content.
    fit="both"   – hugs the content in both directions (chat bubbles, chips).
    """

    def __init__(self, parent: tk.Misc, *, fill: str = WHITE, border: str | None = BORDER,
                 border_width: int = 1, radius: int = 14, padding: tuple[int, int] = (16, 14),
                 shadow: int = 0, fit: str = "none") -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0,
                         width=1, height=1)
        self.fill, self.border = fill, border
        self.bw = px(border_width) if border_width else 0
        self.radius, self.m = px(radius), px(shadow)
        self.pad_x, self.pad_y = px(padding[0]), px(padding[1])
        self.fit = fit
        self._photo: ImageTk.PhotoImage | None = None
        self._img = self.create_image(0, 0, anchor="nw")
        self.body = tk.Frame(self, bg=fill)
        ix, iy = self._inset()
        # The body is *placed* on the canvas (not embedded as a canvas window) so
        # that it always receives geometry updates, even before it is visible.
        if fit == "none":
            self.body.place(x=ix, y=iy, relwidth=1, relheight=1,
                            width=-2 * ix, height=-2 * iy)
        elif fit == "height":
            self.body.place(x=ix, y=iy, relwidth=1, width=-2 * ix)
        else:
            self.body.place(x=ix, y=iy)
        self.bind("<Configure>", self._on_resize)
        if fit != "none":
            self.body.bind("<Configure>", self._on_body, add="+")

    def _inset(self) -> tuple[int, int]:
        return self.m + self.pad_x, self.m + self.pad_y

    def _on_resize(self, event: tk.Event) -> None:
        self._redraw(event.width, event.height)

    def _on_body(self, _event: tk.Event | None = None) -> None:
        ix, iy = self._inset()
        want_h = self.body.winfo_reqheight() + 2 * iy
        if self.fit == "both":
            want_w = self.body.winfo_reqwidth() + 2 * ix
            if int(float(self.cget("width"))) != want_w:
                self.configure(width=want_w)
        if int(float(self.cget("height"))) != want_h:
            self.configure(height=want_h)

    def _redraw(self, w: int | None = None, h: int | None = None) -> None:
        w = w or self.winfo_width()
        h = h or self.winfo_height()
        if w < 4 or h < 4:
            return
        self._photo = rounded_image(w, h, self.radius, self.fill, self.border,
                                    self.bw, self.m)
        self.itemconfigure(self._img, image=self._photo)

    def set_style(self, fill: str | None = None, border: str | None = None,
                  border_width: int | None = None) -> None:
        if fill and fill != self.fill:
            old = self.fill
            self.fill = fill
            self.body.configure(bg=fill)
            recolor(self.body, old, fill)
        if border is not None:
            self.border = border
        if border_width is not None:
            self.bw = px(border_width)
        self._redraw()


class PillButton(tk.Canvas):
    """Fully rounded button / tag with hover, press and disabled states."""

    VARIANTS = {
        #          fill    hover     text   border
        "primary": (RED, RED_DARK, WHITE, None),
        "navy": (NAVY, NAVY_DARK, WHITE, None),
        "ghost": (WHITE, CREAM, NAVY, BORDER),
        "soft": (CREAM, BLUSH, NAVY, BORDER),
        "chip": (WHITE, SELECT_TINT, NAVY, BORDER),
        "tag": (BLUSH, BLUSH, NAVY, None),
        "sos": (RED, RED, WHITE, None),
    }
    SIZES = {"xs": (8, 22, 9), "sm": (9, 28, 12), "md": (10, 38, 18), "lg": (11, 44, 22)}

    def __init__(self, parent: tk.Misc, text: str = "", command: Callable | None = None,
                 variant: str = "primary", size: str = "md", icon_name: str | None = None,
                 icon_only: bool = False, bold: bool = True,
                 colors: tuple[str, str, str, str | None] | None = None) -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.text, self.command = text, command
        self.variant = variant
        self.colors = colors or self.VARIANTS[variant]
        fsize, h, padx = self.SIZES[size]
        self.font = tkfont.Font(family=FONT, size=fsize, weight="bold" if bold else "normal")
        self.h, self.padx = px(h), px(padx)
        self.icon_name, self.icon_only = icon_name, icon_only
        self.icon_size = px(fsize + 6)
        self.enabled = True
        self._hover = self._pressed = False
        self._refs: list[Any] = []
        if command:
            self.configure(cursor="hand2")
        self.bind("<Enter>", lambda _e: self._interactive() and self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._interactive() and self._set(hover=False,
                                                                          pressed=False))
        self.bind("<ButtonPress-1>", lambda _e: self._interactive() and self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._layout()

    def _interactive(self) -> bool:
        return bool(self.command) and self.enabled

    def set_command(self, command: Callable) -> None:
        self.command = command
        self.configure(cursor="hand2" if self.enabled else "")

    def _layout(self) -> None:
        gap = px(7)
        text_w = 0 if self.icon_only else self.font.measure(self.text)
        icon_w = self.icon_size if self.icon_name else 0
        if self.icon_only:
            self.w = self.h
        else:
            self.w = 2 * self.padx + text_w + (icon_w + gap if icon_w else 0)
        self.configure(width=self.w, height=self.h)
        self._draw()

    def _palette(self) -> tuple[str, str, str | None]:
        fill, hover, fg, border = self.colors
        if not self.enabled:
            if self.variant in ("primary", "navy"):
                return BORDER, WHITE, None
            return fill, BORDER, DIVIDER if border else None
        if self._pressed or self._hover:
            return hover, fg, border
        return fill, fg, border

    def _draw(self) -> None:
        self.delete("all")
        fill, fg, border = self._palette()
        bg = rounded_image(self.w, self.h, self.h // 2, fill, border, px(1) if border else 0)
        self._refs = [bg]
        self.create_image(0, 0, image=bg, anchor="nw")
        cy = self.h // 2 + (1 if self._pressed else 0)
        if self.icon_only and self.icon_name:
            img = icon(self.icon_name, self.icon_size, fg)
            self._refs.append(img)
            self.create_image(self.w // 2, cy, image=img)
            return
        x = self.padx
        if self.icon_name:
            img = icon(self.icon_name, self.icon_size, fg)
            self._refs.append(img)
            self.create_image(x + self.icon_size // 2, cy, image=img)
            x += self.icon_size + px(7)
        self.create_text(x, cy, text=self.text, font=self.font, fill=fg, anchor="w")

    def _set(self, hover: bool | None = None, pressed: bool | None = None) -> None:
        if hover is not None:
            self._hover = hover
        if pressed is not None:
            self._pressed = pressed
        self._draw()

    def _release(self, event: tk.Event) -> None:
        if not self.command:
            return
        inside = 0 <= event.x <= self.w and 0 <= event.y <= self.h
        self._set(pressed=False)
        if inside and self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled and self.command else "")
        self._draw()

    def set_text(self, text: str) -> None:
        self.text = text
        self._layout()

    def set_colors(self, colors: tuple[str, str, str, str | None],
                   icon_name: str | None = None) -> None:
        self.colors = colors
        if icon_name is not None:
            self.icon_name = icon_name
        self._layout()


class SegmentedControl(tk.Canvas):
    """Pill-shaped tab switcher with a sliding thumb."""

    def __init__(self, parent: tk.Misc, options: list[str],
                 command: Callable[[int], None]) -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.options, self.command = options, command
        self.font = tkfont.Font(family=FONT, size=10, weight="bold")
        self.pad = px(4)
        self.h = px(40)
        self.seg_w = max(self.font.measure(o) for o in options) + px(44)
        self.w = self.seg_w * len(options) + 2 * self.pad
        self.configure(width=self.w, height=self.h, cursor="hand2")
        self._track = rounded_image(self.w, self.h, self.h // 2, CREAM, BORDER, px(1))
        th = self.h - 2 * self.pad
        self._thumb = rounded_image(self.seg_w, th, th // 2, WHITE, BORDER, px(1))
        self.create_image(0, 0, image=self._track, anchor="nw")
        self._thumb_id = self.create_image(self.pad, self.pad, image=self._thumb, anchor="nw")
        self._labels = [
            self.create_text(self.pad + self.seg_w * i + self.seg_w // 2, self.h // 2,
                             text=o, font=self.font, fill=MUTED)
            for i, o in enumerate(options)
        ]
        self.index = 0
        self._anim: str | None = None
        self.bind("<Button-1>", self._click)
        self._paint_labels()

    def _click(self, event: tk.Event) -> None:
        i = min(max((event.x - self.pad) // self.seg_w, 0), len(self.options) - 1)
        if i != self.index:
            self.select(int(i))
            self.command(int(i))

    def _paint_labels(self) -> None:
        for i, lid in enumerate(self._labels):
            self.itemconfigure(lid, fill=NAVY if i == self.index else MUTED)

    def select(self, index: int) -> None:
        start = self.coords(self._thumb_id)[0]
        target = self.pad + self.seg_w * index
        self.index = index
        self._paint_labels()
        if self._anim:
            self.after_cancel(self._anim)
        steps = 9

        def step(k: int = 1) -> None:
            t = k / steps
            ease = 1 - (1 - t) ** 3
            self.coords(self._thumb_id, start + (target - start) * ease, self.pad)
            if k < steps:
                self._anim = self.after(14, step, k + 1)
            else:
                self._anim = None

        step()


class ScrollArea(tk.Frame):
    """Vertical scroll container with a slim overlay scrollbar. Use ``.inner``."""

    registry: list["ScrollArea"] = []

    def __init__(self, parent: tk.Misc, bg: str, style: str) -> None:
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                yscrollincrement=px(18))
        self.canvas.pack(fill="both", expand=True)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style=style)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.width_listeners: list[Callable[[int], None]] = []
        ScrollArea.registry.append(self)

    def _on_inner(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                            self.inner.winfo_reqheight()))

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        for listener in self.width_listeners:
            listener(event.width)

    def _on_scroll(self, first: str, last: str) -> None:
        self.bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.place_forget()
        else:
            self.bar.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

    def scroll(self, direction: int) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(direction * 3, "units")

    def to_top(self) -> None:
        self.canvas.yview_moveto(0)

    def to_bottom(self) -> None:
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)


class FlowFrame(tk.Frame):
    """Lays children out left-to-right, wrapping to new rows (chips, tags)."""

    def __init__(self, parent: tk.Misc, gap: int = 6) -> None:
        super().__init__(parent, bg=parent.cget("bg"), height=1)
        self.gap = px(gap)
        self.items: list[tk.Widget] = []
        self.bind("<Configure>", lambda _e: self._relayout())

    def add(self, widget: tk.Widget) -> None:
        self.items.append(widget)
        self.after_idle(self._relayout)

    def clear(self) -> None:
        for w in self.items:
            w.destroy()
        self.items = []
        self.configure(height=1)

    def _relayout(self) -> None:
        width = self.winfo_width()
        if width <= 1:
            return
        x = y = row_h = 0
        for w in self.items:
            rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x > 0 and x + rw > width:
                x, y, row_h = 0, y + row_h + self.gap, 0
            w.place(x=x, y=y)
            x += rw + self.gap
            row_h = max(row_h, rh)
        total = max(1, y + row_h)
        if int(float(self.cget("height"))) != total:
            self.configure(height=total)


class ActivityBar(tk.Canvas):
    """Thin indeterminate progress bar."""

    def __init__(self, parent: tk.Misc, track: str = BLUSH, color: str = RED) -> None:
        super().__init__(parent, bg=track, highlightthickness=0, bd=0, height=px(3))
        self._seg = self.create_rectangle(0, 0, 0, px(3), fill=color, width=0)
        self._job: str | None = None
        self._t = 0.0

    def start(self) -> None:
        if self._job is None:
            self._tick()

    def stop(self) -> None:
        if self._job:
            self.after_cancel(self._job)
            self._job = None

    def _tick(self) -> None:
        w = max(self.winfo_width(), 1)
        self._t = (self._t + 0.012) % 1.0
        t = self._t
        ease = t * t * (3 - 2 * t)
        seg = w * 0.32
        x = -seg + (w + seg) * ease
        self.coords(self._seg, x, 0, x + seg, px(3))
        self._job = self.after(16, self._tick)


class PlaceholderText:
    """Wires a placeholder label on top of a Text widget."""

    def __init__(self, text: tk.Text, placeholder: str, bg: str) -> None:
        self.text = text
        self.label = tk.Label(text.master, text=placeholder, font=text.cget("font"),
                              fg=MUTED, bg=bg, anchor="nw", justify="left", cursor="xterm")
        self.label.bind("<Button-1>", lambda _e: text.focus_set())
        for seq in ("<KeyRelease>", "<<Paste>>", "<FocusIn>", "<FocusOut>"):
            text.bind(seq, lambda _e: text.after_idle(self.refresh), add="+")
        self.refresh()

    def refresh(self) -> None:
        if self.text.get("1.0", "end-1c"):
            self.label.place_forget()
        else:
            self.label.place(x=0, y=0)


# ----------------------------------------------------------------------------
# Lightweight markdown → Text tags (for AidPilot answers)
# ----------------------------------------------------------------------------
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\[Source:[^\]]+\])")


def _clean_source(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    return name.replace("wms_", "").replace("_", " ").strip()


def render_markdown(t: tk.Text, text: str) -> None:
    def inline(content: str, base: tuple[str, ...]) -> None:
        content = re.sub(r"(?<![\*\w])\*(?!\*)([^*\n]+)\*(?!\*)", r"\1", content)
        for part in _INLINE.split(content):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                t.insert("end", part[2:-2], base + ("b",))
            elif part.startswith("[Source:"):
                t.insert("end", " ", base)
                t.insert("end", f"  {_clean_source(part[8:-1])}  ", ("src",))
            else:
                t.insert("end", part, base)

    lines = text.strip().splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        end = "\n" if i < len(lines) - 1 else ""
        if not line.strip():
            t.insert("end", "\n", ("gap",))
            continue
        if m := re.match(r"^\s*#{1,6}\s+(.*)", line):
            inline(m.group(1).strip("* "), ("h",))
        elif m := re.match(r"^(\s*)[\*\-•]\s+(.*)", line):
            tag = "bullet2" if len(m.group(1)) >= 2 else "bullet"
            t.insert("end", "•\t" if tag == "bullet" else "–\t", (tag, "mark"))
            inline(m.group(2), (tag,))
        elif m := re.match(r"^(\s*)(\d+)[\.\)]\s+(.*)", line):
            t.insert("end", f"{m.group(2)}.\t", ("num", "mark"))
            inline(m.group(3), ("num",))
        else:
            inline(line, ("p",))
        t.insert("end", end)


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
class ERouteApp:
    def __init__(self) -> None:
        global _SCALE
        _enable_dpi_awareness()
        self.root = tk.Tk()
        _SCALE = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        _pick_font(self.root)
        self.root.title("AidStation – AI-Powered Medical Emergency Response Application")
        self.root.geometry(f"{px(1360)}x{px(840)}")
        self.root.minsize(px(1100), px(680))
        self.root.configure(bg=CREAM)
        self._setup_styles()

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._origin: DeviceLocation | None = None
        self._hospitals: list[dict[str, Any]] = []
        self._cards: list[RoundedPanel] = []
        self._selected: int | None = None
        self._map_cache: dict[str, bytes | None] = {}
        self._map_photo: ImageTk.PhotoImage | None = None
        self._resize_job: str | None = None
        self._aid_sources = default_sources()
        self._aid_busy = False
        self._source_index: int | None = None
        self._source_rows: list[dict[str, Any]] = []
        self._thinking: tk.Frame | None = None
        self._dots_job: str | None = None
        self._chat_width = px(600)
        self._chat_labels: list[tk.Label] = []
        self._welcome: tk.Frame | None = None

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_pages()
        self._build_footer()
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

        self._set_location_label(None, locating=True)
        self.root.after(100, self._poll_queue)
        threading.Thread(target=self._locate_worker, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------
    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        layout = [("Vertical.Scrollbar.trough", {
            "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
            "sticky": "ns"})]
        for name, bg in (("Cream.Vertical.TScrollbar", CREAM),
                         ("White.Vertical.TScrollbar", WHITE)):
            style.layout(name, layout)
            style.configure(name, troughcolor=bg, background=BORDER, bordercolor=bg,
                            lightcolor=BORDER, darkcolor=BORDER, gripcount=0,
                            arrowsize=px(7), relief="flat")
            style.map(name, background=[("active", MUTED), ("pressed", NAVY)])

    # ------------------------------------------------------------------
    # Header / navigation / footer
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=WHITE)
        header.grid(row=0, column=0, sticky="ew")
        tk.Frame(header, bg=DIVIDER, height=1).pack(side="bottom", fill="x")
        inner = tk.Frame(header, bg=WHITE)
        inner.pack(fill="x", padx=px(24), pady=px(12))

        brand = tk.Frame(inner, bg=WHITE)
        brand.pack(side="left")
        self._logo = icon("logo", px(40), RED)
        tk.Label(brand, image=self._logo, bg=WHITE).pack(side="left")
        words = tk.Frame(brand, bg=WHITE)
        words.pack(side="left", padx=(px(12), 0))
        tk.Label(words, text="AidStation", font=F(17, "bold"), fg=NAVY, bg=WHITE,
                 bd=0).pack(anchor="w")
        tk.Label(words, text="AI-powered medical emergency response", font=F(9),
                 fg=MUTED, bg=WHITE, bd=0).pack(anchor="w")

        self.segments = SegmentedControl(inner, ["MediRoute", "MediPilot"], self._show_page)
        self.segments.pack(side="left", padx=(px(40), 0))
        self._page_hint = tk.Label(inner, text="Find the right hospital, fast",
                                   font=F(9), fg=MUTED, bg=WHITE)
        self._page_hint.pack(side="left", padx=(px(14), 0))

        right = tk.Frame(inner, bg=WHITE)
        right.pack(side="right")
        PillButton(right, "SOS 112", variant="sos", size="sm",
                   icon_name="phone").pack(side="right")
        self.location_pill = PillButton(right, "Locating your device…", size="sm",
                                        bold=False, icon_name="pin",
                                        colors=(CREAM, CREAM, MUTED, BORDER))
        self.location_pill.pack(side="right", padx=(0, px(10)))

    def _build_pages(self) -> None:
        container = tk.Frame(self.root, bg=CREAM)
        container.grid(row=1, column=0, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.eroute_tab = tk.Frame(container, bg=CREAM)
        self.aidpilot_tab = tk.Frame(container, bg=CREAM)
        for page in (self.eroute_tab, self.aidpilot_tab):
            page.grid(row=0, column=0, sticky="nsew")
        self._build_eroute_tab()
        self._build_aidpilot_tab()
        self.eroute_tab.tkraise()

    def _show_page(self, index: int) -> None:
        if index == 0:
            self.eroute_tab.tkraise()
            self._page_hint.configure(text="Find the right hospital, fast")
            self.input.focus_set()
        else:
            self.aidpilot_tab.tkraise()
            self._page_hint.configure(text="Field guidance from WMS sources")
            self.aid_input.focus_set()

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=CREAM)
        footer.grid(row=2, column=0, sticky="ew")
        tk.Frame(footer, bg=DIVIDER, height=1).pack(fill="x")
        row = tk.Frame(footer, bg=CREAM)
        row.pack(fill="x", padx=px(24), pady=px(8))
        self._alert_icon = icon("alert", px(14), RED)
        tk.Label(row, image=self._alert_icon, bg=CREAM).pack(side="left")
        tk.Label(row, text=EMERGENCY_NOTE, font=F(9, "bold"), fg=NAVY,
                 bg=CREAM).pack(side="left", padx=(px(8), 0))
        tk.Label(row, text="AidStation is a routing and guidance aid – not a diagnosis "
                           "or a replacement for emergency services.",
                 font=F(9), fg=MUTED, bg=CREAM).pack(side="right")

    # ------------------------------------------------------------------
    # ERoute
    # ------------------------------------------------------------------
    def _build_eroute_tab(self) -> None:
        body = self.eroute_tab
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = tk.Frame(body, bg=CREAM, width=px(460))
        left.grid(row=0, column=0, sticky="ns", padx=(px(16), 0), pady=(px(10), 0))
        left.pack_propagate(False)
        self._left = left

        # --- Composer --------------------------------------------------
        composer = RoundedPanel(left, radius=18, padding=(20, 18), shadow=10, fit="height")
        composer.pack(fill="x")
        self._composer = composer
        c = composer.body
        tk.Label(c, text="EMERGENCY REQUEST", font=F(8, "bold"), fg=RED,
                 bg=WHITE).pack(anchor="w")
        tk.Label(c, text="Describe the medical concern", font=F(14, "bold"), fg=NAVY,
                 bg=WHITE).pack(anchor="w", pady=(px(2), 0))
        tk.Label(c, text="Symptoms, age and anything notable – your location is automatic.",
                 font=F(9), fg=MUTED, bg=WHITE, justify="left", anchor="w",
                 wraplength=px(380)).pack(fill="x", pady=(px(2), px(12)))

        self._input_box = RoundedPanel(c, fill=CREAM, border=BORDER, radius=12,
                                       padding=(12, 10), fit="height")
        self._input_box.pack(fill="x")
        self.input = tk.Text(self._input_box.body, height=3, wrap="word", font=F(11),
                             relief="flat", bd=0, bg=CREAM, fg=INK,
                             insertbackground=NAVY, selectbackground=BLUSH,
                             selectforeground=NAVY, highlightthickness=0,
                             padx=0, pady=0, width=1)
        self.input.pack(fill="x")
        self._input_ph = PlaceholderText(
            self.input, "e.g. Severe chest pain and difficulty breathing…", CREAM)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda _e: None)  # newline
        self.input.bind("<FocusIn>", lambda _e: self._input_box.set_style(
            border=NAVY, border_width=2), add="+")
        self.input.bind("<FocusOut>", lambda _e: self._input_box.set_style(
            border=BORDER, border_width=1), add="+")
        self.input.focus_set()

        tk.Label(c, text="Quick start", font=F(8, "bold"), fg=MUTED,
                 bg=WHITE).pack(anchor="w", pady=(px(12), px(6)))
        chips = FlowFrame(c, gap=6)
        chips.pack(fill="x")
        for label, prompt in QUICK_PROMPTS:
            chips.add(PillButton(chips, label, variant="chip", size="xs", bold=False,
                                 command=lambda p=prompt: self._use_prompt(p)))

        actions = tk.Frame(c, bg=WHITE)
        actions.pack(fill="x", pady=(px(16), 0))
        hint = tk.Frame(actions, bg=WHITE)
        hint.pack(side="left")
        tk.Label(hint, text="Enter  to search", font=F(8, "bold"), fg=NAVY,
                 bg=WHITE).pack(anchor="w")
        tk.Label(hint, text="Shift + Enter  for a new line", font=F(8), fg=MUTED,
                 bg=WHITE).pack(anchor="w")
        self.submit_btn = PillButton(actions, "Find nearest hospitals", self._submit,
                                     variant="primary", size="lg", icon_name="pin")
        self.submit_btn.pack(side="right")

        # --- Status (shown while working) -----------------------------
        self.status_frame = tk.Frame(left, bg=CREAM)
        self.activity = ActivityBar(self.status_frame)
        self.activity.pack(fill="x", pady=(0, px(8)))
        self.status_label = tk.Label(self.status_frame, text="", font=F(9), fg=MUTED,
                                     bg=CREAM, anchor="w", justify="left",
                                     wraplength=px(420))
        self.status_label.pack(fill="x")

        # --- Results ---------------------------------------------------
        self.results_head = tk.Frame(left, bg=CREAM)
        self.results_head.pack(fill="x", pady=(px(18), px(6)), padx=(px(4), px(4)))
        tk.Label(self.results_head, text="Suitable hospitals", font=F(12, "bold"),
                 fg=NAVY, bg=CREAM).pack(side="left")
        self.count_tag = PillButton(self.results_head, "0", variant="tag", size="xs")
        self.count_tag.pack(side="left", padx=(px(8), 0))
        tk.Label(self.results_head, text="Nearest first · by drive time", font=F(8),
                 fg=MUTED, bg=CREAM).pack(side="right")

        self.cards_area = ScrollArea(left, CREAM, "Cream.Vertical.TScrollbar")
        self.cards_area.pack(fill="both", expand=True, pady=(0, px(8)))
        self.cards_container = self.cards_area
        self.cards_inner = self.cards_area.inner
        self._show_empty_results()

        # --- Map -------------------------------------------------------
        right = tk.Frame(body, bg=CREAM)
        right.grid(row=0, column=1, sticky="nsew", padx=(px(6), px(16)), pady=(px(10), px(6)))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        map_panel = RoundedPanel(right, radius=18, padding=(18, 16), shadow=10)
        map_panel.grid(row=0, column=0, sticky="nsew")
        m = map_panel.body

        bar = tk.Frame(m, bg=WHITE)
        bar.pack(fill="x")
        self.nav_btn = PillButton(bar, "Open in Google Maps", self._open_in_maps,
                                  variant="navy", size="md", icon_name="external")
        self.nav_btn.pack(side="right")
        self.nav_btn.set_enabled(False)
        legend = tk.Frame(bar, bg=WHITE)
        legend.pack(side="right", padx=(0, px(16)))
        self._legend_icons = [icon("dot", px(14), NAVY), icon("dot", px(14), RED)]
        for img, text in zip(self._legend_icons, ("A  You", "B  Hospital")):
            tk.Label(legend, image=img, bg=WHITE).pack(side="left")
            tk.Label(legend, text=text, font=F(9), fg=MUTED, bg=WHITE).pack(
                side="left", padx=(px(3), px(12)))
        titles = tk.Frame(bar, bg=WHITE)
        titles.pack(side="left", fill="x", expand=True)
        self.map_title = tk.Label(titles, text="Route map", font=F(13, "bold"), fg=NAVY,
                                  bg=WHITE, anchor="w")
        self.map_title.pack(fill="x")
        self.map_sub = tk.Label(titles, text="Driving route to the selected hospital.",
                                font=F(9), fg=MUTED, bg=WHITE, anchor="w")
        self.map_sub.pack(fill="x")

        # --- Triage summary (shown with results) ----------------------
        self.summary = tk.Frame(m, bg=WHITE)
        tk.Frame(self.summary, bg=DIVIDER, height=1).pack(fill="x", pady=(px(14), px(12)))
        top = tk.Frame(self.summary, bg=WHITE)
        top.pack(fill="x")
        tk.Label(top, text="TRIAGE", font=F(8, "bold"), fg=RED, bg=WHITE).pack(side="left")
        self.summary_label = tk.Label(top, text="", font=F(11, "bold"), fg=NAVY, bg=WHITE,
                                      anchor="w", justify="left", wraplength=px(560))
        self.summary_label.pack(side="left", padx=(px(10), 0))
        tk.Label(top, text="AI-assisted estimate · not a diagnosis", font=F(8),
                 fg=MUTED, bg=WHITE).pack(side="right")
        tags_row = tk.Frame(self.summary, bg=WHITE)
        tags_row.pack(fill="x", pady=(px(8), 0))
        tk.Label(tags_row, text="Needs", font=F(8, "bold"), fg=MUTED,
                 bg=WHITE).pack(side="left", anchor="n", pady=(px(4), 0), padx=(0, px(10)))
        self.resource_tags = FlowFrame(tags_row, gap=5)
        self.resource_tags.pack(side="left", fill="x", expand=True)

        self.map_area = RoundedPanel(m, fill=CREAM, border=None, border_width=0,
                                     radius=14, padding=(6, 6))
        self.map_area.pack(fill="both", expand=True, pady=(px(14), 0))
        self.map_container = self.map_area.body
        self.map_label = tk.Label(self.map_container, bg=CREAM, bd=0)
        self.map_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.map_container.bind("<Configure>", self._on_map_resize)

        self.map_empty = tk.Frame(self.map_container, bg=CREAM)
        self._map_icon = icon("route", px(34), RED)
        badge = RoundedPanel(self.map_empty, fill=WHITE, border=BORDER, radius=40,
                             padding=(22, 22), fit="both")
        badge.pack()
        tk.Label(badge.body, image=self._map_icon, bg=WHITE).pack()
        self.map_empty_title = tk.Label(self.map_empty, text="Your route will appear here",
                                        font=F(13, "bold"), fg=NAVY, bg=CREAM)
        self.map_empty_title.pack(pady=(px(14), px(4)))
        self.map_empty_sub = tk.Label(
            self.map_empty, font=F(10), fg=MUTED, bg=CREAM, justify="center",
            text="Describe the emergency and AidStation will route you to the\n"
                 "nearest hospital that has every resource you need.")
        self.map_empty_sub.pack()
        self.map_activity = ActivityBar(self.map_empty, track=BLUSH)
        self._show_map_placeholder()

    def _use_prompt(self, prompt: str) -> None:
        self.input.delete("1.0", "end")
        self.input.insert("1.0", prompt)
        self._input_ph.refresh()
        self.input.focus_set()
        self.input.mark_set("insert", "end")

    # ------------------------------------------------------------------
    # AidPilot
    # ------------------------------------------------------------------
    def _build_aidpilot_tab(self) -> None:
        tab = self.aidpilot_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        # --- Sources ---------------------------------------------------
        holder = tk.Frame(tab, bg=CREAM, width=px(340))
        holder.grid(row=0, column=0, sticky="ns", padx=(px(16), 0), pady=(px(10), px(6)))
        holder.pack_propagate(False)
        panel = RoundedPanel(holder, radius=18, padding=(18, 16), shadow=10)
        panel.pack(fill="both", expand=True)
        sp = panel.body

        head = tk.Frame(sp, bg=WHITE)
        head.pack(fill="x")
        tk.Label(head, text="Knowledge sources", font=F(13, "bold"), fg=NAVY,
                 bg=WHITE).pack(side="left")
        self.source_count = PillButton(head, "0", variant="tag", size="xs")
        self.source_count.pack(side="left", padx=(px(8), 0))
        tk.Label(sp, text="WMS documents used to ground every answer.", font=F(9),
                 fg=MUTED, bg=WHITE, anchor="w").pack(fill="x", pady=(px(2), px(12)))
        tk.Frame(sp, bg=DIVIDER, height=1).pack(fill="x")

        actions = tk.Frame(sp, bg=WHITE)
        actions.pack(side="bottom", fill="x", pady=(px(12), 0))
        PillButton(actions, "Add PDF", self._add_source, variant="navy", size="sm",
                   icon_name="plus").pack(side="left")
        self.view_btn = PillButton(actions, "View", self._view_source, variant="ghost",
                                   size="sm", icon_name="eye")
        self.view_btn.pack(side="left", padx=(px(6), 0))
        self.remove_btn = PillButton(actions, "Remove", self._remove_source,
                                     variant="ghost", size="sm", icon_name="trash")
        self.remove_btn.pack(side="left", padx=(px(6), 0))
        tk.Frame(sp, bg=DIVIDER, height=1).pack(side="bottom", fill="x")

        self.sources_area = ScrollArea(sp, WHITE, "White.Vertical.TScrollbar")
        self.sources_area.pack(fill="both", expand=True, pady=px(6))

        # --- Chat ------------------------------------------------------
        chat_panel = RoundedPanel(tab, radius=18, padding=(22, 16), shadow=10)
        chat_panel.grid(row=0, column=1, sticky="nsew", padx=(px(6), px(16)),
                        pady=(px(10), px(6)))
        chat = chat_panel.body
        chat.grid_columnconfigure(0, weight=1)
        chat.grid_rowconfigure(2, weight=1)

        header = tk.Frame(chat, bg=WHITE)
        header.grid(row=0, column=0, sticky="ew")
        self._pilot_logo = icon("logo", px(34), RED)
        tk.Label(header, image=self._pilot_logo, bg=WHITE).pack(side="left")
        self.sources_status = PillButton(header, "", size="sm", bold=False,
                                         icon_name="dot",
                                         colors=(OK_TINT, OK_TINT, OK, None))
        self.sources_status.pack(side="right")
        self._new_chat_btn = PillButton(header, "New chat", self._reset_chat,
                                        variant="ghost", size="sm", icon_name="plus")
        self._new_chat_btn.pack(side="right", padx=(0, px(8)))
        words = tk.Frame(header, bg=WHITE)
        words.pack(side="left", padx=(px(12), 0))
        tk.Label(words, text="MediPilot", font=F(15, "bold"), fg=NAVY, bg=WHITE,
                 bd=0).pack(anchor="w")
        tk.Label(words, text="Field guidance grounded in your WMS "
                             "sources.", font=F(9), fg=MUTED, bg=WHITE, bd=0, anchor="w").pack(anchor="w")
        tk.Frame(chat, bg=DIVIDER, height=1).grid(row=1, column=0, sticky="ew",
                                                  pady=(px(14), 0))

        self.chat_area = ScrollArea(chat, WHITE, "White.Vertical.TScrollbar")
        self.chat_area.grid(row=2, column=0, sticky="nsew")
        self.chat_area.width_listeners.append(self._on_chat_width)
        self.aid_chat = self.chat_area.inner

        composer = RoundedPanel(chat, fill=CREAM, border=BORDER, radius=16,
                                padding=(16, 10), fit="height")
        composer.grid(row=3, column=0, sticky="ew", pady=(px(10), 0))
        self._aid_box = composer
        cb = composer.body
        cb.grid_columnconfigure(0, weight=1)
        text_holder = tk.Frame(cb, bg=CREAM)
        text_holder.grid(row=0, column=0, sticky="ew", pady=px(4))
        self.aid_input = tk.Text(text_holder, height=2, font=F(11), wrap="word",
                                 relief="flat", bd=0, bg=CREAM, fg=INK, width=1,
                                 insertbackground=NAVY, selectbackground=BLUSH,
                                 selectforeground=NAVY, highlightthickness=0,
                                 padx=0, pady=0)
        self.aid_input.pack(fill="x")
        self._aid_ph = PlaceholderText(
            self.aid_input, "Describe the patient, the scene and what happened…", CREAM)
        self.aid_input.bind("<Return>", self._on_aid_enter)
        self.aid_input.bind("<Shift-Return>", lambda _e: None)
        self.aid_input.bind("<FocusIn>", lambda _e: composer.set_style(
            border=NAVY, border_width=2), add="+")
        self.aid_input.bind("<FocusOut>", lambda _e: composer.set_style(
            border=BORDER, border_width=1), add="+")
        self.aid_send = PillButton(cb, "", self._submit_aid, variant="primary",
                                   size="md", icon_name="send", icon_only=True)
        self.aid_send.grid(row=0, column=1, sticky="se", padx=(px(10), 0))

        foot = tk.Frame(chat, bg=WHITE)
        foot.grid(row=4, column=0, sticky="ew", pady=(px(8), 0))
        tk.Label(foot, text="Enter to send · Shift + Enter for a new line", font=F(8),
                 fg=MUTED, bg=WHITE).pack(side="left")
        tk.Label(foot, text="Answers come only from your sources. Always call 112 in "
                            "an emergency.", font=F(8), fg=MUTED, bg=WHITE).pack(side="right")

        self._refresh_sources()
        self._show_chat_welcome()

    # --- Sources ------------------------------------------------------
    def _refresh_sources(self) -> None:
        for child in self.sources_area.inner.winfo_children():
            child.destroy()
        self._source_rows = []
        if self._source_index is not None and self._source_index >= len(self._aid_sources):
            self._source_index = None
        n = len(self._aid_sources)
        self.source_count.set_text(str(n))
        if n:
            self.sources_status.set_colors((OK_TINT, OK_TINT, OK, None))
            self.sources_status.set_text(f"{n} source{'s' if n != 1 else ''} active")
        else:
            self.sources_status.set_colors((WARN_TINT, WARN_TINT, WARN, None))
            self.sources_status.set_text("No sources selected")

        if not n:
            empty = tk.Frame(self.sources_area.inner, bg=WHITE)
            empty.pack(fill="x", pady=px(28))
            self._empty_doc = icon("doc", px(30), BORDER)
            tk.Label(empty, image=self._empty_doc, bg=WHITE).pack()
            tk.Label(empty, text="No sources yet", font=F(10, "bold"), fg=NAVY,
                     bg=WHITE).pack(pady=(px(8), px(2)))
            tk.Label(empty, text="Add a WMS PDF so MediPilot can\nground its answers.",
                     font=F(9), fg=MUTED, bg=WHITE, justify="center").pack()
        self._doc_icon = icon("doc", px(18), RED)
        for i, source in enumerate(self._aid_sources):
            row = tk.Frame(self.sources_area.inner, bg=WHITE, cursor="hand2")
            row.pack(fill="x", pady=(0, px(2)), padx=(0, px(8)))
            accent = tk.Frame(row, bg=WHITE, width=px(3))
            accent.pack(side="left", fill="y")
            tile = tk.Label(row, image=self._doc_icon, bg=BLUSH, width=px(32),
                            height=px(32), cursor="hand2")
            tile.pack(side="left", padx=(px(8), px(10)), pady=px(8))
            text = tk.Frame(row, bg=WHITE, cursor="hand2")
            text.pack(side="left", fill="x", expand=True)
            title = _clean_source(source.name).title() or source.name
            name = tk.Label(text, text=title, font=F(10, "bold"), fg=NAVY, bg=WHITE,
                            anchor="w", justify="left", wraplength=px(220), cursor="hand2")
            name.pack(fill="x")
            file = tk.Label(text, text=source.name, font=F(8), fg=MUTED, bg=WHITE,
                            anchor="w", cursor="hand2")
            file.pack(fill="x")
            parts = [row, text, name, file, tile]
            for w in parts:
                w.bind("<Button-1>", lambda _e, k=i: self._select_source(k))
                w.bind("<Double-Button-1>", lambda _e, k=i: (self._select_source(k),
                                                             self._view_source()))
            self._source_rows.append({"row": row, "accent": accent,
                                      "bg": [row, text, name, file]})
        self._paint_sources()

    def _select_source(self, index: int) -> None:
        self._source_index = index
        self._paint_sources()

    def _paint_sources(self) -> None:
        for i, item in enumerate(self._source_rows):
            sel = i == self._source_index
            for w in item["bg"]:
                w.configure(bg=SELECT_TINT if sel else WHITE)
            item["accent"].configure(bg=RED if sel else WHITE)
        has = self._source_index is not None
        self.view_btn.set_enabled(has)
        self.remove_btn.set_enabled(has)

    def _selected_source(self) -> Path | None:
        if self._source_index is None or self._source_index >= len(self._aid_sources):
            return None
        return self._aid_sources[self._source_index]

    def _view_source(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        window = tk.Toplevel(self.root)
        window.title(f"MediPilot source – {source.name}")
        window.geometry(f"{px(820)}x{px(640)}")
        window.configure(bg=CREAM)
        head = tk.Frame(window, bg=WHITE)
        head.pack(fill="x")
        tk.Frame(head, bg=DIVIDER, height=1).pack(side="bottom", fill="x")
        hi = tk.Frame(head, bg=WHITE)
        hi.pack(fill="x", padx=px(22), pady=px(14))
        tk.Label(hi, text="SOURCE DOCUMENT", font=F(8, "bold"), fg=RED,
                 bg=WHITE).pack(anchor="w")
        tk.Label(hi, text=_clean_source(source.name).title(), font=F(14, "bold"),
                 fg=NAVY, bg=WHITE).pack(anchor="w")
        tk.Label(hi, text=str(source), font=F(8), fg=MUTED, bg=WHITE).pack(anchor="w")
        page = RoundedPanel(window, radius=16, padding=(22, 18), shadow=8)
        page.pack(fill="both", expand=True, padx=px(16), pady=px(16))
        text = tk.Text(page.body, wrap="word", font=F(10), bd=0, relief="flat",
                       bg=WHITE, fg=INK, padx=0, pady=0, highlightthickness=0,
                       spacing1=px(2), spacing3=px(2), selectbackground=BLUSH,
                       selectforeground=NAVY)
        scroll = ttk.Scrollbar(page.body, command=text.yview,
                               style="White.Vertical.TScrollbar")
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        try:
            text.insert("1.0", read_source(source))
        except Exception as exc:
            text.insert("1.0", f"Could not read source: {exc}")
        text.configure(state="disabled")

    def _add_source(self) -> None:
        selected = filedialog.askopenfilename(
            title="Add a WMS source", filetypes=[("PDF documents", "*.pdf")]
        )
        if selected:
            path = Path(selected)
            if path not in self._aid_sources:
                self._aid_sources.append(path)
                self._source_index = len(self._aid_sources) - 1
                self._refresh_sources()

    def _remove_source(self) -> None:
        if self._selected_source() is not None:
            del self._aid_sources[self._source_index]  # type: ignore[arg-type]
            self._source_index = None
            self._refresh_sources()

    # --- Chat ---------------------------------------------------------
    def _on_chat_width(self, width: int) -> None:
        self._chat_width = width
        wrap = max(px(240), int(width * 0.62))
        for label in list(self._chat_labels):
            if label.winfo_exists():
                label.configure(wraplength=wrap)
            else:
                self._chat_labels.remove(label)
        if self._welcome is not None and self._welcome.winfo_exists():
            self._welcome.configure(height=max(px(320), self.chat_area.winfo_height() - px(4)))

    def _show_chat_welcome(self) -> None:
        self._welcome = tk.Frame(self.aid_chat, bg=WHITE)
        self._welcome.pack(fill="both", expand=True)
        box = tk.Frame(self._welcome, bg=WHITE)
        box.place(relx=0.5, rely=0.45, anchor="center")
        badge = RoundedPanel(box, fill=SELECT_TINT, border=None, border_width=0,
                             radius=34, padding=(18, 18), fit="both")
        badge.pack()
        self._welcome_icon = icon("spark", px(30), RED)
        tk.Label(badge.body, image=self._welcome_icon, bg=SELECT_TINT).pack()
        tk.Label(box, text="How can MediPilot help?", font=F(16, "bold"), fg=NAVY,
                 bg=WHITE).pack(pady=(px(14), px(4)))
        tk.Label(box, text="Describe the patient, the environment and what has happened.\n"
                           "Answers cite the WMS sources they come from.",
                 font=F(10), fg=MUTED, bg=WHITE, justify="center").pack()
        tk.Label(box, text="TRY ASKING", font=F(8, "bold"), fg=MUTED,
                 bg=WHITE).pack(pady=(px(20), px(8)))
        for suggestion in AID_SUGGESTIONS:
            PillButton(box, suggestion, variant="soft", size="sm", bold=False,
                       icon_name="arrow",
                       command=lambda q=suggestion: self._ask_suggestion(q)).pack(
                           pady=(0, px(6)))
        self._welcome.configure(height=px(420))
        self._welcome.pack_propagate(False)

    def _ask_suggestion(self, question: str) -> None:
        self.aid_input.delete("1.0", "end")
        self.aid_input.insert("1.0", question)
        self._aid_ph.refresh()
        self._submit_aid()

    def _hide_welcome(self) -> None:
        if self._welcome is not None and self._welcome.winfo_exists():
            self._welcome.destroy()
        self._welcome = None

    def _reset_chat(self) -> None:
        if self._aid_busy:
            return
        for child in self.aid_chat.winfo_children():
            child.destroy()
        self._chat_labels = []
        self._show_chat_welcome()
        self._on_chat_width(self._chat_width)
        self.chat_area.to_top()

    def _append_user(self, text: str) -> None:
        row = tk.Frame(self.aid_chat, bg=WHITE)
        row.pack(fill="x", pady=(px(14), 0), padx=(px(80), px(10)))
        bubble = RoundedPanel(row, fill=NAVY, border=None, border_width=0, radius=16,
                              padding=(16, 11), fit="both")
        bubble.pack(anchor="e")
        label = tk.Label(bubble.body, text=text, font=F(10), fg=WHITE, bg=NAVY,
                         justify="left", anchor="w",
                         wraplength=max(px(240), int(self._chat_width * 0.62)))
        label.pack()
        self._chat_labels.append(label)
        tk.Label(row, text="You", font=F(8), fg=MUTED, bg=WHITE).pack(anchor="e",
                                                                       pady=(px(3), 0))

    def _assistant_shell(self, fill: str = CREAM, border: str = BORDER) -> tuple[tk.Frame, tk.Frame]:
        row = tk.Frame(self.aid_chat, bg=WHITE)
        row.pack(fill="x", pady=(px(14), 0), padx=(0, px(64)))
        avatar = tk.Label(row, image=self._pilot_logo_small(), bg=WHITE)
        avatar.pack(side="left", anchor="n", padx=(px(2), px(10)), pady=(px(2), 0))
        bubble = RoundedPanel(row, fill=fill, border=border, radius=16,
                              padding=(18, 14), fit="height")
        bubble.pack(side="left", fill="x", expand=True)
        return row, bubble.body

    def _pilot_logo_small(self) -> ImageTk.PhotoImage:
        if not hasattr(self, "_logo_small"):
            self._logo_small = icon("logo", px(28), RED)
        return self._logo_small

    def _append_assistant(self, text: str) -> None:
        _row, body = self._assistant_shell()
        head = tk.Frame(body, bg=CREAM)
        head.pack(fill="x", pady=(0, px(6)))
        tk.Label(head, text="MediPilot", font=F(9, "bold"), fg=NAVY,
                 bg=CREAM).pack(side="left")
        tk.Label(head, text="· grounded in WMS sources", font=F(8), fg=MUTED,
                 bg=CREAM).pack(side="left", padx=(px(4), 0))
        copy_btn = PillButton(head, "Copy", variant="ghost", size="xs", icon_name="copy",
                              bold=False)
        copy_btn.set_command(lambda: self._copy(text, copy_btn))
        copy_btn.pack(side="right")

        holder = tk.Frame(body, bg=CREAM, height=px(40))
        holder.pack(fill="x")
        holder.pack_propagate(False)
        t = tk.Text(holder, wrap="word", font=F(10), bd=0, relief="flat", bg=CREAM,
                    fg=INK, padx=0, pady=0, highlightthickness=0, width=1, height=1,
                    cursor="arrow", selectbackground=BLUSH, selectforeground=NAVY,
                    spacing1=px(1), spacing3=px(3), tabs=(px(18),))
        t.pack(fill="both", expand=True)
        t.tag_configure("p", font=F(10))
        t.tag_configure("h", font=F(11, "bold"), foreground=NAVY, spacing1=px(8),
                        spacing3=px(4))
        t.tag_configure("b", font=F(10, "bold"), foreground=NAVY)
        t.tag_configure("bullet", lmargin1=px(4), lmargin2=px(22))
        t.tag_configure("bullet2", lmargin1=px(24), lmargin2=px(42))
        t.tag_configure("num", lmargin1=px(2), lmargin2=px(22))
        t.tag_configure("mark", foreground=RED, font=F(10, "bold"))
        t.tag_configure("gap", font=F(4))
        t.tag_configure("src", font=F(8, "bold"), foreground=NAVY, background=BLUSH)
        render_markdown(t, text)
        t.configure(state="disabled")

        state = {"w": 0}

        def fit(event: tk.Event) -> None:
            w = event.width
            if w <= 1 or w == state["w"]:
                return
            state["w"] = w
            try:
                height = int(t.tk.call(t._w, "count", "-update", "-ypixels", "1.0", "end"))
            except tk.TclError:
                return
            holder.configure(height=height + px(4))

        t.bind("<Configure>", fit)

    def _append_error(self, message: str) -> None:
        _row, body = self._assistant_shell(fill=BLUSH, border=BORDER)
        head = tk.Frame(body, bg=BLUSH)
        head.pack(fill="x", pady=(0, px(4)))
        self._err_icon = icon("alert", px(14), RED)
        tk.Label(head, image=self._err_icon, bg=BLUSH).pack(side="left")
        tk.Label(head, text="MediPilot couldn't answer", font=F(9, "bold"), fg=RED,
                 bg=BLUSH).pack(side="left", padx=(px(6), 0))
        label = tk.Label(body, text=message, font=F(10), fg=INK, bg=BLUSH,
                         justify="left", anchor="w",
                         wraplength=max(px(240), int(self._chat_width * 0.62)))
        label.pack(fill="x")
        self._chat_labels.append(label)

    def _show_thinking(self) -> None:
        row, body = self._assistant_shell()
        self._thinking = row
        line = tk.Frame(body, bg=CREAM)
        line.pack(fill="x")
        tk.Label(line, text="Reviewing the selected WMS sources", font=F(10), fg=MUTED,
                 bg=CREAM).pack(side="left")
        dots = tk.Label(line, text="", font=F(10, "bold"), fg=RED, bg=CREAM, width=4,
                        anchor="w")
        dots.pack(side="left", padx=(px(4), 0))

        def animate(k: int = 0) -> None:
            if not dots.winfo_exists():
                return
            dots.configure(text="•" * (k % 4))
            self._dots_job = self.root.after(320, animate, k + 1)

        animate()

    def _hide_thinking(self) -> None:
        if self._dots_job:
            self.root.after_cancel(self._dots_job)
            self._dots_job = None
        if self._thinking is not None and self._thinking.winfo_exists():
            self._thinking.destroy()
        self._thinking = None

    def _copy(self, text: str, button: PillButton) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        button.set_text("Copied")
        button.icon_name = "check"
        button._layout()

        def reset() -> None:
            if button.winfo_exists():
                button.icon_name = "copy"
                button.set_text("Copy")

        self.root.after(1500, reset)

    def _on_aid_enter(self, _event: tk.Event) -> str:
        self._submit_aid()
        return "break"

    def _submit_aid(self) -> None:
        question = self.aid_input.get("1.0", "end").strip()
        if not question or self._aid_busy:
            return
        self._aid_busy = True
        self.aid_send.set_enabled(False)
        self._new_chat_btn.set_enabled(False)
        self._hide_welcome()
        self._append_user(question)
        self.aid_input.delete("1.0", "end")
        self._aid_ph.refresh()
        self._show_thinking()
        self.chat_area.to_bottom()
        threading.Thread(target=self._aid_worker, args=(question, list(self._aid_sources)),
                         daemon=True).start()

    def _aid_worker(self, question: str, sources: list[Path]) -> None:
        try:
            self._queue.put(("aid_result", ask_aid_pilot(question, sources)))
        except Exception as exc:
            logger.exception("MediPilot failed")
            self._queue.put(("aid_error", str(exc) or exc.__class__.__name__))

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------
    def _on_enter(self, _event: tk.Event) -> str:
        self._submit()
        return "break"

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        path = str(widget)
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        for area in ScrollArea.registry:
            if path.startswith(str(area)) and area.winfo_ismapped():
                area.scroll(direction)
                return

    def _submit(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text or self._busy:
            return
        self._busy = True
        self.submit_btn.set_enabled(False)
        self.submit_btn.set_text("Searching…")
        self._set_status("Understanding the concern, checking live hospital resources "
                         "and ranking routes by drive time…")
        self.summary.pack_forget()
        self._clear_cards()
        self._show_skeletons()
        self._clear_map(loading=True)
        threading.Thread(target=self._agent_worker, args=(text,), daemon=True).start()

    # ------------------------------------------------------------------
    # Background workers (never touch Tk widgets; talk via the queue)
    # ------------------------------------------------------------------
    def _locate_worker(self) -> None:
        self._queue.put(("location", get_device_location()))

    def _agent_worker(self, text: str) -> None:
        try:
            origin = get_device_location()
            self._queue.put(("location", origin))
            self._queue.put(("result", run_agent_detailed(text, origin)))
        except Exception as exc:  # surface any failure to the user
            logger.exception("Agent failed")
            self._queue.put(("error", str(exc) or exc.__class__.__name__))

    def _map_worker(self, key: str, origin: DeviceLocation,
                    hospital: dict[str, Any], width: int, height: int) -> None:
        try:
            data = get_route_map((origin.latitude, origin.longitude),
                                 hospital, width, height)
        except Exception:
            logger.exception("Could not render the route map")
            data = None
        self._queue.put(("map", key, data))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, *payload = self._queue.get_nowait()
                if kind == "location":
                    self._on_location(payload[0])
                elif kind == "result":
                    self._on_result(payload[0])
                elif kind == "error":
                    self._on_error(payload[0])
                elif kind == "map":
                    self._on_map(payload[0], payload[1])
                elif kind == "aid_result":
                    self._on_aid_result(payload[0])
                elif kind == "aid_error":
                    self._on_aid_error(payload[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # Queue handlers
    # ------------------------------------------------------------------
    def _on_location(self, loc: DeviceLocation | None) -> None:
        if loc is not None:
            self._origin = loc
        self._set_location_label(loc)

    def _aid_done(self) -> None:
        self._aid_busy = False
        self.aid_send.set_enabled(True)
        self._new_chat_btn.set_enabled(True)
        self._hide_thinking()

    def _on_aid_result(self, response: str) -> None:
        self._aid_done()
        self._append_assistant(response)
        self.root.after(60, self.chat_area.to_bottom)

    def _on_aid_error(self, message: str) -> None:
        self._aid_done()
        self._append_error(message)
        self.root.after(60, self.chat_area.to_bottom)

    def _on_error(self, message: str) -> None:
        self._finish_request()
        self._set_status("")
        self._show_message("Something went wrong", message)
        self._clear_map()

    def _on_result(self, result: dict[str, Any]) -> None:
        self._finish_request()
        self._set_status("")
        if result["origin"] is not None:
            self._origin = result["origin"]
            self._set_location_label(result["origin"])

        self._show_summary(result.get("concern") or "", result.get("required_resources") or [])

        if result["error"]:
            self._show_message("Couldn't calculate routes", result["error"])
            self._clear_map()
            return
        if not result["hospitals"]:
            self._show_message(
                "No suitable hospital found",
                "No hospital in the database currently has all the required "
                "resources available.")
            self._clear_map()
            return

        self._clear_cards()  # resets self._hospitals, so assign afterwards
        self._hospitals = result["hospitals"]
        self.count_tag.set_text(f"{len(self._hospitals)} found")
        for i, hospital in enumerate(self._hospitals):
            self._build_card(i, hospital)
        self._select(0)

    def _on_map(self, key: str, data: bytes | None) -> None:
        self._map_cache[key] = data
        if self._selected is not None and key == self._map_key(self._hospitals[self._selected]):
            self._display_map()

    # ------------------------------------------------------------------
    # Summary & hospital cards
    # ------------------------------------------------------------------
    def _show_summary(self, concern: str, resources: list[str]) -> None:
        if not concern and not resources:
            return
        self.summary_label.configure(text=concern or "Medical concern")
        self.resource_tags.clear()
        for resource in resources:
            self.resource_tags.add(PillButton(self.resource_tags, resource, variant="tag",
                                              size="xs", bold=False))
        if not resources:
            self.resource_tags.add(PillButton(self.resource_tags, "No specific resources",
                                              variant="tag", size="xs", bold=False))
        self.summary.pack(fill="x", before=self.map_area)

    def _clear_cards(self) -> None:
        for child in self.cards_inner.winfo_children():
            child.destroy()
        self._cards = []
        self._hospitals = []
        self._selected = None
        self.count_tag.set_text("0")
        self.cards_area.to_top()

    def _show_empty_results(self) -> None:
        self._clear_cards()
        box = tk.Frame(self.cards_inner, bg=CREAM)
        box.pack(fill="x", pady=px(26))
        self._empty_icon = icon("pin", px(28), BORDER)
        tk.Label(box, image=self._empty_icon, bg=CREAM).pack()
        tk.Label(box, text="No search yet", font=F(11, "bold"), fg=NAVY,
                 bg=CREAM).pack(pady=(px(8), px(2)))
        tk.Label(box, text="Hospitals that have every resource you need\n"
                           "will appear here, nearest first.",
                 font=F(9), fg=MUTED, bg=CREAM, justify="center").pack()

    def _show_skeletons(self) -> None:
        for k in range(3):
            card = RoundedPanel(self.cards_inner, fill=WHITE, border=DIVIDER, radius=16,
                                padding=(16, 16), fit="height")
            card.pack(fill="x", padx=(px(2), px(10)), pady=(px(2), px(8)))
            for width, height in ((0.55, 14), (0.85, 9), (0.4, 9)):
                bar = tk.Frame(card.body, bg=SELECT_TINT if k else BLUSH,
                               height=px(height), width=int(px(380) * width))
                bar.pack(anchor="w", pady=(0, px(8)))

    def _show_message(self, title: str, text: str) -> None:
        self._clear_cards()
        card = RoundedPanel(self.cards_inner, fill=BLUSH, border=BORDER, radius=16,
                            padding=(18, 16), fit="height")
        card.pack(fill="x", padx=(px(2), px(10)), pady=px(2))
        head = tk.Frame(card.body, bg=BLUSH)
        head.pack(fill="x")
        self._msg_icon = icon("alert", px(16), RED)
        tk.Label(head, image=self._msg_icon, bg=BLUSH).pack(side="left")
        tk.Label(head, text=title, font=F(11, "bold"), fg=RED, bg=BLUSH).pack(
            side="left", padx=(px(8), 0))
        tk.Label(card.body, text=text, font=F(10), fg=INK, bg=BLUSH, justify="left",
                 anchor="w", wraplength=px(370)).pack(fill="x", pady=(px(8), px(10)))
        tk.Label(card.body, text=EMERGENCY_NOTE, font=F(10, "bold"), fg=NAVY, bg=BLUSH,
                 anchor="w").pack(fill="x")

    def _build_card(self, index: int, h: dict[str, Any]) -> None:
        card = RoundedPanel(self.cards_inner, fill=WHITE, border=BORDER, radius=16,
                            padding=(16, 14), fit="height")
        card.pack(fill="x", padx=(px(2), px(10)), pady=(px(2), px(8)))
        b = card.body
        b.grid_columnconfigure(1, weight=1)

        d = px(30)
        badge = (rounded_image(d, d, d // 2, RED) if index == 0
                 else rounded_image(d, d, d // 2, CREAM, BORDER, px(1)))
        rank = tk.Label(b, image=badge, text=str(index + 1), compound="center",
                        font=F(10, "bold"), fg=WHITE if index == 0 else NAVY,
                        bg=WHITE, bd=0)
        rank.image = badge  # keep a reference
        rank.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, px(12)), pady=(px(2), 0))

        info = tk.Frame(b, bg=WHITE)
        info.grid(row=0, column=1, sticky="nsew")
        if index == 0:
            tk.Label(info, text="NEAREST  ·  QUICKEST ROUTE", font=F(7, "bold"), fg=RED,
                     bg=WHITE, anchor="w").pack(fill="x", pady=(0, px(2)))
        tk.Label(info, text=h["name"], font=F(12, "bold"), fg=NAVY, bg=WHITE, anchor="w",
                 justify="left", wraplength=px(230)).pack(fill="x")
        tk.Label(info, text=h.get("address") or "Address not available", font=F(9),
                 fg=MUTED, bg=WHITE, anchor="w", justify="left",
                 wraplength=px(230)).pack(fill="x", pady=(px(3), 0))

        stats = tk.Frame(b, bg=WHITE)
        stats.grid(row=0, column=2, sticky="ne", padx=(px(10), 0))
        tk.Label(stats, text=h["duration"], font=F(17, "bold"), fg=NAVY,
                 bg=WHITE).pack(anchor="e")
        tk.Label(stats, text=f"{h['distance']} drive", font=F(9), fg=MUTED,
                 bg=WHITE).pack(anchor="e")

        tk.Frame(b, bg=DIVIDER, height=1).grid(row=1, column=1, columnspan=2,
                                               sticky="ew", pady=(px(12), px(10)))
        foot = tk.Frame(b, bg=WHITE)
        foot.grid(row=2, column=1, columnspan=2, sticky="ew")
        if not hasattr(self, "_phone_icon"):
            self._phone_icon = icon("phone", px(14), NAVY)
        phone = h.get("phone") or ""
        tk.Label(foot, image=self._phone_icon, bg=WHITE).pack(side="left")
        tk.Label(foot, text=phone or "Phone not available", font=F(9, "bold"),
                 fg=NAVY if phone else MUTED, bg=WHITE).pack(side="left", padx=(px(5), 0))
        PillButton(foot, "Directions", lambda i=index: self._directions(i),
                   variant="ghost", size="xs", icon_name="external").pack(side="right")
        if phone:
            copy = PillButton(foot, "Copy number", variant="ghost", size="xs",
                              icon_name="copy", bold=False)
            copy.set_command(lambda c=copy, p=phone: self._copy(p, c))
            copy.pack(side="right", padx=(0, px(6)))

        self._bind_select(card, index)
        self._cards.append(card)

    def _bind_select(self, widget: tk.Misc, index: int) -> None:
        if isinstance(widget, PillButton):
            return
        widget.bind("<Button-1>", lambda _e, i=index: self._select(i), add="+")
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._bind_select(child, index)

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self._hospitals)):
            return
        if self._selected == index:
            return
        self._selected = index
        for i, card in enumerate(self._cards):
            selected = i == index
            card.set_style(fill=SELECT_TINT if selected else WHITE,
                           border=RED if selected else BORDER,
                           border_width=2 if selected else 1)
        self._show_map(self._hospitals[index])

    def _directions(self, index: int) -> None:
        self._select(index)
        self._open_in_maps()

    # ------------------------------------------------------------------
    # Map pane
    # ------------------------------------------------------------------
    @staticmethod
    def _map_key(hospital: dict[str, Any]) -> str:
        return f"{hospital['name']}|{hospital['latitude']},{hospital['longitude']}"

    def _show_map_placeholder(self, title: str | None = None, sub: str | None = None,
                              loading: bool = False) -> None:
        self._map_photo = None
        self.map_label.configure(image="")
        self.map_empty_title.configure(text=title or "Your route will appear here")
        self.map_empty_sub.configure(
            text=sub or "Describe the emergency and AidStation will route you to the\n"
                        "nearest hospital that has every resource you need.")
        if loading:
            self.map_activity.pack(fill="x", padx=px(60), pady=(px(18), 0))
            self.map_activity.start()
        else:
            self.map_activity.stop()
            self.map_activity.pack_forget()
        self.map_empty.place(relx=0.5, rely=0.5, anchor="center")

    def _clear_map(self, loading: bool = False) -> None:
        self.map_title.configure(text="Route map")
        self.map_sub.configure(text="Driving route to the selected hospital.")
        self.nav_btn.set_enabled(False)
        if loading:
            self._show_map_placeholder("Finding the right hospital…",
                                       "Matching required resources against live\n"
                                       "hospital availability.", loading=True)
        else:
            self._show_map_placeholder()

    def _show_map(self, hospital: dict[str, Any]) -> None:
        self.map_title.configure(text=hospital["name"])
        self.map_sub.configure(
            text=f"{hospital['duration']}  ·  {hospital['distance']} by road")
        self.nav_btn.set_enabled(True)
        key = self._map_key(hospital)
        if key in self._map_cache:
            self._display_map()
            return
        self._show_map_placeholder("Loading route map…", "Drawing the fastest driving "
                                                         "route.", loading=True)
        if self._origin is None:
            return
        width = max(self.map_container.winfo_width(), 300)
        height = max(self.map_container.winfo_height(), 300)
        threading.Thread(
            target=self._map_worker,
            args=(key, self._origin, hospital, width, height),
            daemon=True,
        ).start()

    def _display_map(self) -> None:
        if self._selected is None:
            return
        key = self._map_key(self._hospitals[self._selected])
        if key not in self._map_cache:
            return
        data = self._map_cache[key]
        if data is None:
            self._show_map_placeholder(
                "Map preview unavailable",
                "Use “Open in Google Maps” for turn-by-turn directions.")
            return
        width = max(self.map_container.winfo_width(), 50)
        height = max(self.map_container.winfo_height(), 50)
        image = Image.open(BytesIO(data)).convert("RGBA")
        image = ImageOps.fit(image, (width, height), Image.LANCZOS)
        mask = Image.new("L", (width * 2, height * 2), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width * 2 - 1, height * 2 - 1),
                                               px(10) * 2, fill=255)
        image.putalpha(mask.resize((width, height), Image.LANCZOS))
        self.map_activity.stop()
        self.map_empty.place_forget()
        self._map_photo = ImageTk.PhotoImage(image)
        self.map_label.configure(image=self._map_photo)

    def _on_map_resize(self, _event: tk.Event) -> None:
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self._display_map)

    def _open_in_maps(self) -> None:
        if self._selected is None or self._origin is None:
            return
        origin = (self._origin.latitude, self._origin.longitude)
        webbrowser.open(directions_url(origin, self._hospitals[self._selected]))

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _finish_request(self) -> None:
        self._busy = False
        self.submit_btn.set_text("Find nearest hospitals")
        self.submit_btn.set_enabled(True)

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)
        if text:
            self.status_frame.pack(fill="x", pady=(px(14), 0), padx=px(4),
                                   after=self._composer)
            self.activity.start()
        else:
            self.activity.stop()
            self.status_frame.pack_forget()

    def _set_location_label(self, loc: DeviceLocation | None,
                            locating: bool = False) -> None:
        if locating:
            text, colors = "Locating your device…", (CREAM, CREAM, MUTED, BORDER)
        elif loc is None:
            text, colors = "Location unavailable – enable Location Services", \
                (WARN_TINT, WARN_TINT, WARN, None)
        elif loc.is_precise:
            text, colors = loc.label(), (OK_TINT, OK_TINT, OK, None)
        else:
            text, colors = "Approximate location · city-level", \
                (WARN_TINT, WARN_TINT, WARN, None)
        self.location_pill.set_colors(colors)
        self.location_pill.set_text(text)
