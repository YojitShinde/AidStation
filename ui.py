"""
ERoute - ER Assistant
Minimal, chat-style Tkinter UI.

Layout
------
  header      logo + name, "New chat"
  stage       welcome screen (first visit) or conversation
  composer    rounded input with a send button (Enter = send, Shift+Enter = new line)
"""

from __future__ import annotations

import base64
import math
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from contextlib import contextmanager
from tkinter import ttk

from agent import run_agent_detailed

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
CREAM = "#FFF8F2"      # app background
GREY = "#EBEBEB"       # borders, user bubble, hover
RED = "#CC0000"        # brand / emergency accent (used sparingly)
SKY = "#A3C1DA"        # focus ring, text selection
NAVY = "#0E425F"       # text + primary action
WHITE = "#FFFFFF"      # input surface
MUTED = "#5E7787"      # secondary text (navy, softened)
SKY_SOFT = "#E6EEF5"   # hover tint (SKY, lightened)
IDLE = "#CFCFCF"       # disabled send button


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------
def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Draw a rounded rectangle on a canvas."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def draw_logo(canvas: tk.Canvas, size: int):
    """Red disc with a white cross."""
    c, t, arm = size / 2, size * 0.075, size * 0.26
    canvas.create_oval(1, 1, size - 1, size - 1, fill=RED, outline=RED)
    canvas.create_rectangle(c - t, c - arm, c + t, c + arm, fill=WHITE, width=0)
    canvas.create_rectangle(c - arm, c - t, c + arm, c + t, fill=WHITE, width=0)


class Bubble(tk.Canvas):
    """A rounded message bubble sized to its (wrapped) text."""

    def __init__(self, master, text, font, fg, fill, bg,
                 max_width=520, pad_x=18, pad_y=12, radius=18):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0)
        item = self.create_text(
            0, 0, text=text, font=font, fill=fg, anchor="nw",
            width=max_width - 2 * pad_x,
        )
        x1, y1, x2, y2 = self.bbox(item)
        w = (x2 - x1) + 2 * pad_x
        h = (y2 - y1) + 2 * pad_y
        self.configure(width=w, height=h)
        rounded_rect(self, 1, 1, w - 1, h - 1, radius, fill=fill, outline=fill)
        self.coords(item, pad_x, pad_y)
        self.tag_raise(item)


class MapCard(tk.Canvas):
    """
    A rounded map card: the route image, a caption pill with the hospital
    name / travel time, and an "Open in Maps" button.
    """

    def __init__(self, master, png_bytes, title, subtitle, url,
                 font, font_bold, font_small, bg=CREAM, radius=16):
        self.photo = tk.PhotoImage(data=base64.b64encode(png_bytes))
        w, h = self.photo.width(), self.photo.height()
        super().__init__(master, width=w, height=h, bg=bg,
                         highlightthickness=0, bd=0)
        self.create_image(0, 0, image=self.photo, anchor="nw")

        # Round the image corners by masking them with the background colour
        r = radius
        for cx, cy, sx, sy in ((r, r, -1, -1), (w - r, r, 1, -1),
                               (r, h - r, -1, 1), (w - r, h - r, 1, 1)):
            pts = [cx + sx * r, cy + sy * r]
            for deg in range(0, 91, 10):
                a = math.radians(deg)
                pts += [cx + sx * r * math.cos(a), cy + sy * r * math.sin(a)]
            self.create_polygon(pts, fill=bg, outline=bg)
        rounded_rect(self, 0, 0, w - 1, h - 1, r, fill="", outline=GREY, width=1)

        # Caption pill (bottom-left)
        pad, gap = 14, 14
        btn_label = "Open in Maps"
        btn_w = font_bold.measure(btn_label) + 32
        max_text = w - 2 * gap - btn_w - 2 * pad - 16
        while font_bold.measure(title) > max_text and len(title) > 4:
            title = title[:-2].rstrip() + "…"
        tw = max(font_bold.measure(title), font_small.measure(subtitle))
        ph = font_bold.metrics("linespace") + font_small.metrics("linespace") + 2 * pad - 4
        pw = tw + 2 * pad
        px, py = gap, h - gap - ph
        rounded_rect(self, px, py, px + pw, py + ph, 12, fill=WHITE, outline=GREY)
        self.create_text(px + pad, py + pad - 2, text=title, anchor="nw",
                         font=font_bold, fill=NAVY)
        self.create_text(px + pad, py + ph - pad + 2, text=subtitle, anchor="sw",
                         font=font_small, fill=MUTED)

        # "Open in Maps" button (bottom-right)
        bh = font_bold.metrics("linespace") + 18
        bx, by = w - gap - btn_w, h - gap - bh
        rounded_rect(self, bx, by, bx + btn_w, by + bh, 12,
                     fill=NAVY, outline=NAVY, tags="open")
        self.create_text(bx + btn_w / 2, by + bh / 2, text=btn_label,
                         font=font_bold, fill=WHITE, tags="open")
        self.tag_bind("open", "<Button-1>", lambda _e: webbrowser.open(url))
        self.tag_bind("open", "<Enter>", lambda _e: self.config(cursor="hand2"))
        self.tag_bind("open", "<Leave>", lambda _e: self.config(cursor=""))


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class ERouteApp:
    """ERoute ER Assistant – chat-style Tkinter UI."""

    MAX_COLUMN = 760
    MAX_LINES = 6

    SUGGESTIONS = [
        "I have severe chest pain and trouble breathing. I'm near Andheri West, Mumbai.",
        "My father fell and hit his head and is drowsy. We're in Powai, Mumbai.",
        "I'm pregnant and have heavy bleeding. I'm in Thane West.",
    ]
    PLACEHOLDER = "Describe what's happening and where you are"

    # ------------------------------------------------------------------ init
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ERoute")
        self.root.geometry("960x760")
        self.root.minsize(640, 560)
        self.root.configure(bg=CREAM)

        self._busy = False
        self._req_id = 0
        self._results: queue.Queue = queue.Queue()
        self._dots = 0
        self._dots_job = None

        self._init_fonts()
        self._init_styles()
        self._build_ui()
        self._configure_tags()

        self.root.after(100, self._poll_results)
        self.root.after(150, self.input.focus_set)

    def _init_fonts(self):
        available = set(tkfont.families(self.root))
        family = next(
            (f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
                         "Ubuntu", "DejaVu Sans") if f in available),
            "TkDefaultFont",
        )
        F = tkfont.Font
        self.f_body = F(family=family, size=12)
        self.f_bold = F(family=family, size=12, weight="bold")
        self.f_italic = F(family=family, size=12, slant="italic")
        self.f_head = F(family=family, size=13, weight="bold")
        self.f_gap = F(family=family, size=5)
        self.f_ui = F(family=family, size=10)
        self.f_small = F(family=family, size=9)
        self.f_small_bold = F(family=family, size=9, weight="bold")
        self.f_brand = F(family=family, size=14, weight="bold")
        self.f_title = F(family=family, size=22, weight="bold")
        self.f_sub = F(family=family, size=12)

    def _init_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        name = "ERoute.Vertical.TScrollbar"
        style.layout(name, [
            ("Vertical.Scrollbar.trough", {
                "sticky": "ns",
                "children": [("Vertical.Scrollbar.thumb",
                              {"expand": "1", "sticky": "nswe"})],
            })
        ])
        style.configure(
            name, background=GREY, troughcolor=CREAM, bordercolor=CREAM,
            lightcolor=GREY, darkcolor=GREY, relief="flat", width=8, gripcount=0,
        )
        style.map(name, background=[("active", SKY), ("pressed", SKY)])
        # Same scrollbar, blended into the background (used when nothing scrolls)
        style.layout("ERouteHidden.Vertical.TScrollbar", style.layout(name))
        style.configure(
            "ERouteHidden.Vertical.TScrollbar", background=CREAM, troughcolor=CREAM,
            bordercolor=CREAM, lightcolor=CREAM, darkcolor=CREAM, relief="flat", width=8,
        )

    # -------------------------------------------------------------- build UI
    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=CREAM, height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        logo = tk.Canvas(header, width=28, height=28, bg=CREAM, highlightthickness=0)
        draw_logo(logo, 28)
        logo.pack(side=tk.LEFT, padx=(24, 10), pady=14)
        tk.Label(header, text="ERoute", font=self.f_brand, bg=CREAM, fg=NAVY
                 ).pack(side=tk.LEFT)
        self._ghost_button(header, "New chat", self._on_new_chat
                           ).pack(side=tk.RIGHT, padx=20)

        tk.Frame(self.root, bg=GREY, height=1).pack(fill=tk.X)

        # Centered column
        body = tk.Frame(self.root, bg=CREAM)
        body.pack(fill=tk.BOTH, expand=True)
        self.col = tk.Frame(body, bg=CREAM)
        self.col.place(relx=0.5, rely=0, anchor="n", relheight=1, width=self.MAX_COLUMN)
        body.bind(
            "<Configure>",
            lambda e: self.col.place_configure(
                width=min(self.MAX_COLUMN, max(300, e.width - 48))),
        )

        # Bottom: emergency note, then composer (packed bottom-up)
        note = tk.Frame(self.col, bg=CREAM)
        note.pack(side=tk.BOTTOM, pady=(0, 14))
        tk.Label(note, text="In a life-threatening emergency, call ",
                 font=self.f_small, bg=CREAM, fg=MUTED).pack(side=tk.LEFT)
        tk.Label(note, text="112", font=self.f_small_bold, bg=CREAM, fg=RED
                 ).pack(side=tk.LEFT)
        tk.Label(note, text=" immediately.", font=self.f_small, bg=CREAM, fg=MUTED
                 ).pack(side=tk.LEFT)

        self._build_composer()

        # Stage: welcome <-> chat
        self.stage = tk.Frame(self.col, bg=CREAM)
        self.stage.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_welcome()
        self._build_chat()
        self._show_welcome()

    def _ghost_button(self, parent, text, command):
        b = tk.Label(parent, text=text, font=self.f_ui, bg=CREAM, fg=NAVY,
                     padx=12, pady=6, cursor="hand2")
        b.bind("<Enter>", lambda _e: b.config(bg=GREY))
        b.bind("<Leave>", lambda _e: b.config(bg=CREAM))
        b.bind("<Button-1>", lambda _e: command())
        return b

    # -- welcome ------------------------------------------------------------
    def _build_welcome(self):
        w = tk.Frame(self.stage, bg=CREAM)
        self.welcome = w

        logo = tk.Canvas(w, width=56, height=56, bg=CREAM, highlightthickness=0)
        draw_logo(logo, 56)
        logo.pack(pady=(0, 18))
        tk.Label(w, text="Need a hospital, fast?", font=self.f_title,
                 bg=CREAM, fg=NAVY).pack()
        tk.Label(
            w,
            text="Tell me what's happening and where you are.\n"
                 "I'll find the nearest hospital equipped to treat you.",
            font=self.f_sub, bg=CREAM, fg=MUTED, justify="center",
        ).pack(pady=(8, 26))

        for text in self.SUGGESTIONS:
            chip = tk.Label(
                w, text=text, font=self.f_body, bg=WHITE, fg=NAVY, anchor="w",
                padx=18, pady=12, cursor="hand2",
                highlightthickness=1, highlightbackground=GREY, highlightcolor=GREY,
            )
            chip.pack(fill=tk.X, pady=4)
            chip.bind("<Enter>", lambda _e, c=chip: c.config(bg=SKY_SOFT))
            chip.bind("<Leave>", lambda _e, c=chip: c.config(bg=WHITE))
            chip.bind("<Button-1>", lambda _e, t=text: self._submit(t))

    def _show_welcome(self):
        self.chat_wrap.pack_forget()
        self.welcome.place(relx=0.5, rely=0.45, anchor="center")

    def _show_chat(self):
        self.welcome.place_forget()
        if not self.chat_wrap.winfo_ismapped():
            self.chat_wrap.pack(fill=tk.BOTH, expand=True)

    # -- chat ---------------------------------------------------------------
    def _build_chat(self):
        self.chat_wrap = tk.Frame(self.stage, bg=CREAM)
        self.chat = tk.Text(
            self.chat_wrap, wrap=tk.WORD, bg=CREAM, fg=NAVY, font=self.f_body,
            bd=0, highlightthickness=0, padx=6, pady=16, cursor="arrow",
            state=tk.DISABLED, insertwidth=0,
            selectbackground=SKY, selectforeground=NAVY,
        )
        sb = ttk.Scrollbar(self.chat_wrap, orient=tk.VERTICAL,
                           style="ERouteHidden.Vertical.TScrollbar",
                           command=self.chat.yview)

        def on_scroll(lo, hi):
            sb.set(lo, hi)
            fits = float(lo) <= 0.0 and float(hi) >= 1.0
            sb.configure(style="ERouteHidden.Vertical.TScrollbar" if fits
                         else "ERoute.Vertical.TScrollbar")

        self.chat.configure(yscrollcommand=on_scroll)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.chat.bind("<Button-1>", lambda _e: self.chat.focus_set())

    def _configure_tags(self):
        c = self.chat
        c.tag_configure("body", font=self.f_body, foreground=NAVY, spacing1=2, spacing3=2)
        c.tag_configure("bold", font=self.f_bold)
        c.tag_configure("italic", font=self.f_italic)
        c.tag_configure("bullet", lmargin1=8, lmargin2=26)
        c.tag_configure("indent", lmargin1=26, lmargin2=26)
        c.tag_configure("h", font=self.f_head, spacing1=14, spacing3=4)
        c.tag_configure("gap", font=self.f_gap)
        c.tag_configure("who", font=self.f_small_bold, foreground=RED,
                        spacing1=22, spacing3=4)
        c.tag_configure("muted", font=self.f_body, foreground=MUTED)
        c.tag_configure("user_line", justify="right", spacing1=18, spacing3=2)
        c.tag_configure("map_line", spacing1=10, spacing3=16)

    @contextmanager
    def _writable(self):
        self.chat.configure(state=tk.NORMAL)
        try:
            yield
        finally:
            self.chat.configure(state=tk.DISABLED)

    # -- composer -----------------------------------------------------------
    def _build_composer(self):
        self.composer = tk.Canvas(self.col, height=61, bg=CREAM, highlightthickness=0)
        self.composer.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 8))

        self.input = tk.Text(
            self.composer, height=1, wrap=tk.WORD, font=self.f_body,
            bg=WHITE, fg=NAVY, insertbackground=NAVY, bd=0,
            highlightthickness=0, padx=0, pady=0, undo=True,
            selectbackground=SKY, selectforeground=NAVY,
        )
        self.placeholder = tk.Label(
            self.input, text=self.PLACEHOLDER, font=self.f_body,
            bg=WHITE, fg=MUTED, anchor="w", cursor="xterm",
        )
        self.placeholder.place(x=0, y=0)
        self.placeholder.bind("<Button-1>", lambda _e: self.input.focus_set())

        self.input.bind("<Return>", self._on_send)
        self.input.bind("<Shift-Return>", lambda _e: None)  # default: newline
        self.input.bind("<KeyRelease>", lambda _e: self._on_input_change())
        self.input.bind("<<Paste>>", lambda _e: self.root.after_idle(self._on_input_change))
        self.input.bind("<<Cut>>", lambda _e: self.root.after_idle(self._on_input_change))
        self.input.bind("<FocusIn>", lambda _e: self._layout_composer())
        self.input.bind("<FocusOut>", lambda _e: self._layout_composer())
        self.composer.bind("<Configure>", lambda _e: self._layout_composer())

    def _input_lines(self) -> int:
        try:
            n = self.input.count("1.0", "end", "displaylines")
        except tk.TclError:
            n = 1
        if isinstance(n, (tuple, list)):
            n = n[0] if n else 1
        return max(1, min(self.MAX_LINES, int(n or 1)))

    def _has_text(self) -> bool:
        return bool(self.input.get("1.0", "end-1c").strip())

    def _layout_composer(self):
        c = self.composer
        w = c.winfo_width()
        if w < 120:
            return
        lh = self.f_body.metrics("linespace")
        lines = self._input_lines()
        h = lines * lh + 40
        if int(float(c.cget("height"))) != h:
            c.configure(height=h)

        c.delete("all")
        focused = self.root.focus_get() is self.input
        rounded_rect(c, 1, 1, w - 1, h - 1, min(26, h // 2),
                     fill=WHITE, outline=SKY if focused else GREY, width=1.5)

        self.input.configure(height=lines)
        c.create_window(24, 20, anchor="nw", window=self.input,
                        width=w - 24 - 72, height=lines * lh)

        # Send button
        enabled = self._has_text() and not self._busy
        cx, cy, r = w - 34, h - 30, 17
        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                      fill=NAVY if enabled else IDLE, outline="", tags="send")
        c.create_line(cx, cy + 6, cx, cy - 6, fill=WHITE, width=2.2,
                      capstyle="round", tags="send")
        c.create_line(cx - 5.5, cy - 1, cx, cy - 6.5, cx + 5.5, cy - 1,
                      fill=WHITE, width=2.2, capstyle="round", joinstyle="round",
                      tags="send")
        c.tag_bind("send", "<Button-1>", lambda _e: self._on_send())
        c.tag_bind("send", "<Enter>",
                   lambda _e: c.config(cursor="hand2" if enabled else "arrow"))
        c.tag_bind("send", "<Leave>", lambda _e: c.config(cursor=""))

    def _on_input_change(self):
        if self._has_text():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=0, y=0)
        self._layout_composer()

    # ------------------------------------------------------------- Messages
    def _add_user_message(self, text: str):
        max_w = int(min(560, max(240, self.col.winfo_width() * 0.78)))
        bubble = Bubble(self.chat, text, self.f_body, NAVY, GREY, CREAM, max_width=max_w)
        with self._writable():
            start = self.chat.index("end-1c")
            self.chat.window_create("end", window=bubble)
            self.chat.insert("end", "\n")
            self.chat.tag_add("user_line", start, "end-1c")
        self.chat.see("end")

    def _add_assistant_message(self, text: str, map_args=None):
        with self._writable():
            self.chat.mark_set("answer_start", "end-1c")
            self.chat.mark_gravity("answer_start", "left")
            self.chat.insert("end", "ERoute\n", "who")
            self._render_markdown(text, map_args)
            self.chat.insert("end", "\n", "gap")
        # Show the answer from its first line (not its last) so nothing is cut off
        self.chat.update_idletasks()
        self.chat.yview("answer_start")

    # Minimal Markdown: #/##/### headings, **bold**, *italic*, bullets, ---
    _INLINE = re.compile(r"(\*\*.+?\*\*|(?<!\*)\*[^\s*][^*]*?\*(?!\*))")
    _HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
    _BULLET = re.compile(r"^\s*[*\-•]\s+(.*)$")
    _RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")

    # The map goes right above the "Alternative…" heading
    _ALT_HEADING = re.compile(r"^\s*(#{1,6}\s+|\*\*)[^\w]*alternativ", re.I)

    def _render_markdown(self, text: str, map_args=None):
        ins = self.chat.insert
        last_blank = True
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip() or self._RULE.match(line):
                if not last_blank:
                    ins("end", "\n", "gap")
                last_blank = True
                continue
            last_blank = False

            if map_args and self._ALT_HEADING.match(line):
                self._insert_map(map_args)
                map_args = None

            m = self._HEADING.match(line)
            if m:
                self._inline(m.group(1), ("body", "h"))
                ins("end", "\n", ("body", "h"))
                continue

            m = self._BULLET.match(line)
            if m:
                ins("end", "•  ", ("body", "bullet"))
                self._inline(m.group(1), ("body", "bullet"))
                ins("end", "\n", ("body", "bullet"))
                continue

            base = ("body", "indent") if raw[:2] == "  " else ("body",)
            self._inline(line.strip(), base)
            ins("end", "\n", base)

        if map_args:  # no "Alternative" section in this answer: map goes last
            self._insert_map(map_args)

    def _insert_map(self, map_args):
        png, hospital, origin = map_args
        parts = [hospital.get("duration"), hospital.get("distance")]
        subtitle = "  •  ".join(p for p in parts if p) or "Recommended hospital"
        if hospital.get("duration"):
            subtitle = subtitle.replace(hospital["duration"],
                                        f"{hospital['duration']} by car", 1)
        # No origin: Google Maps starts from the person's live location.
        url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&destination={hospital['latitude']},{hospital['longitude']}"
            "&travelmode=driving"
        )
        try:
            card = MapCard(self.chat, png, hospital.get("name", "Hospital"),
                           subtitle, url, self.f_body, self.f_small_bold, self.f_small)
        except tk.TclError:
            return  # unreadable image data: skip the map, keep the answer
        start = self.chat.index("end-1c")
        self.chat.window_create("end", window=card)
        self.chat.insert("end", "\n")
        self.chat.tag_add("map_line", start, "end-1c")

    def _inline(self, text: str, base: tuple):
        ins = self.chat.insert
        for part in self._INLINE.split(text):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                ins("end", part[2:-2], base + ("bold",))
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                ins("end", part[1:-1], base + ("italic",))
            else:
                ins("end", part, base)

    # -- "thinking" indicator ----------------------------------------------
    def _start_thinking(self):
        with self._writable():
            self.chat.mark_set("think_head", "end-1c")
            self.chat.mark_gravity("think_head", "left")
            self.chat.insert("end", "ERoute\n", "who")
            self.chat.mark_set("think_text", "end-1c")
            self.chat.mark_gravity("think_text", "left")
            self.chat.insert("end", "Finding the nearest suitable hospital", "muted")
        self.chat.see("end")
        self._dots = 0
        self._dots_job = self.root.after(400, self._animate_thinking)

    def _animate_thinking(self):
        self._dots = (self._dots + 1) % 4
        with self._writable():
            self.chat.delete("think_text", "end-1c")
            self.chat.insert("think_text",
                             "Finding the nearest suitable hospital" + "." * self._dots,
                             "muted")
        self._dots_job = self.root.after(400, self._animate_thinking)

    def _stop_thinking(self):
        if self._dots_job is not None:
            self.root.after_cancel(self._dots_job)
            self._dots_job = None
        try:
            with self._writable():
                self.chat.delete("think_head", "end-1c")
        except tk.TclError:
            pass

    # ------------------------------------------------------------- Actions
    def _on_send(self, _event=None):
        text = self.input.get("1.0", "end-1c").strip()
        if text and not self._busy:
            self._submit(text)
        return "break"

    def _submit(self, text: str):
        if self._busy:
            return
        self._show_chat()
        self._add_user_message(text)
        self.input.delete("1.0", "end")
        self._busy = True
        self._on_input_change()
        self._start_thinking()

        self._req_id += 1
        threading.Thread(target=self._worker, args=(self._req_id, text),
                         daemon=True).start()

    def _worker(self, req_id: int, text: str):
        """Runs the agent off the UI thread; hands the result back via a queue."""
        try:
            self._results.put((req_id, run_agent_detailed(text)))
        except Exception as exc:  # noqa: BLE001 – surface any failure to the user
            self._results.put((req_id, {"text": f"⚠️ Something went wrong.\n\n{exc}"}))

    def _poll_results(self):
        try:
            while True:
                req_id, result = self._results.get_nowait()
                if req_id == self._req_id and self._busy:
                    self._stop_thinking()
                    map_args = None
                    if result.get("map_png") and result.get("hospital"):
                        map_args = (result["map_png"], result["hospital"],
                                    result.get("origin", ""))
                    self._add_assistant_message(result["text"], map_args)
                    self._busy = False
                    self._on_input_change()
                    self.input.focus_set()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    def _on_new_chat(self):
        self._req_id += 1  # ignore any in-flight result
        self._busy = False
        self._stop_thinking()
        with self._writable():
            self.chat.delete("1.0", "end")
        self.input.delete("1.0", "end")
        self._on_input_change()
        self._show_welcome()
        self.input.focus_set()

    # ---------------------------------------------------------------- Run
    def run(self):
        self.root.mainloop()
