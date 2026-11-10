"""
ERoute - ER Assistant
Tkinter UI. Left: concern input + hospital cards (nearest first).
Right: map with the driving route to the selected hospital.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from PIL import Image, ImageOps, ImageTk

from agent import run_agent_detailed
from aid_pilot import ask as ask_aid_pilot
from aid_pilot import default_sources, read_source
from location_service import DeviceLocation, get_device_location
from maps_service import directions_url, get_route_map

logger = logging.getLogger(__name__)

# Palette (matches the styled map)
CREAM = "#FFF8F2"
NAVY = "#0E425F"
RED = "#CC0000"
BLUSH = "#F3D6D6"
BORDER = "#E3B5AE"
MUTED = "#5B6B76"
WHITE = "#FFFFFF"
OK = "#1B7F3B"
WARN = "#B45309"
FONT = "Segoe UI"

EMERGENCY_NOTE = "For a life-threatening situation, call 112 immediately."


class ERouteApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AidStation – AI-Powered Medical Emergency Response Application")
        self.root.geometry("1320x800")
        self.root.minsize(1060, 640)
        self.root.configure(bg=CREAM)

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._origin: DeviceLocation | None = None
        self._hospitals: list[dict[str, Any]] = []
        self._card_widgets: list[list[tk.Misc]] = []
        self._selected: int | None = None
        self._map_cache: dict[str, bytes | None] = {}
        self._map_photo: ImageTk.PhotoImage | None = None
        self._resize_job: str | None = None
        self._aid_sources = default_sources()
        self._aid_busy = False

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_tabs()
        self._build_footer()

        self._set_location_label(None, locating=True)
        self.root.after(100, self._poll_queue)
        threading.Thread(target=self._locate_worker, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=WHITE, highlightthickness=1,
                          highlightbackground=BORDER)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="🏥 AidStation", font=(FONT, 22, "bold"),
                 fg=RED, bg=WHITE).pack(side="left", padx=20, pady=10)

        right = tk.Frame(header, bg=WHITE)
        right.pack(side="right", padx=20)
        tk.Label(right, text="AI-powered medical emergency response",
                 font=(FONT, 11, "bold"), fg=MUTED, bg=WHITE).pack(anchor="e")
        self.location_label = tk.Label(right, text="", font=(FONT, 10),
                                       bg=WHITE, fg=MUTED)
        self.location_label.pack(anchor="e")

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(10, 0))
        self.eroute_tab = tk.Frame(self.notebook, bg=CREAM)
        self.aidpilot_tab = tk.Frame(self.notebook, bg=CREAM)
        self.notebook.add(self.eroute_tab, text="  ERoute  ")
        self.notebook.add(self.aidpilot_tab, text="  AidPilot  ")
        self._build_eroute_tab()
        self._build_aidpilot_tab()

    def _build_eroute_tab(self) -> None:
        body = tk.Frame(self.eroute_tab, bg=CREAM)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        # ---------------- Left panel ----------------
        left = tk.Frame(body, bg=CREAM, width=500)
        left.grid(row=0, column=0, sticky="ns")
        left.pack_propagate(False)

        tk.Label(left, text="Describe the medical concern",
                 font=(FONT, 12, "bold"), fg=NAVY, bg=CREAM,
                 anchor="w").pack(fill="x", padx=14, pady=(14, 4))

        self.input = tk.Text(left, height=4, wrap="word", font=(FONT, 11),
                             relief="flat", bg=WHITE, fg="black",
                             highlightthickness=1, highlightbackground=BORDER,
                             highlightcolor=NAVY, padx=8, pady=6)
        self.input.pack(fill="x", padx=14)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda _e: None)  # newline
        self.input.focus_set()

        row = tk.Frame(left, bg=CREAM)
        row.pack(fill="x", padx=14, pady=8)
        self.submit_btn = tk.Button(
            row, text="Find nearest hospitals", command=self._submit,
            font=(FONT, 11, "bold"), bg=RED, fg=WHITE, activebackground="#A30000",
            activeforeground=WHITE, relief="flat", padx=14, pady=6, cursor="hand2",
        )
        self.submit_btn.pack(side="left")
        # tk.Label(row, text="Enter to send · Shift+Enter for new line",
        #          font=(FONT, 9), fg=MUTED, bg=CREAM).pack(side="left", padx=10)

        self.status_label = tk.Label(left, text="", font=(FONT, 10), fg=MUTED,
                                     bg=CREAM, anchor="w", justify="left",
                                     wraplength=470)
        self.status_label.pack(fill="x", padx=14)

        self.summary_label = tk.Label(left, text="", font=(FONT, 10), fg=NAVY,
                                      bg=CREAM, anchor="w", justify="left",
                                      wraplength=470)
        self.summary_label.pack(fill="x", padx=14, pady=(2, 6))

        # Scrollable card list
        self.cards_container = tk.Frame(left, bg=CREAM)
        self.cards_container.pack(fill="both", expand=True, pady=(0, 8))
        self.cards_canvas = tk.Canvas(self.cards_container, bg=CREAM,
                                      highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.cards_container, orient="vertical",
                                  command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.cards_canvas.pack(side="left", fill="both", expand=True)

        self.cards_inner = tk.Frame(self.cards_canvas, bg=CREAM)
        self._inner_id = self.cards_canvas.create_window(
            (0, 0), window=self.cards_inner, anchor="nw")
        self.cards_inner.bind(
            "<Configure>",
            lambda _e: self.cards_canvas.configure(
                scrollregion=self.cards_canvas.bbox("all")))
        self.cards_canvas.bind(
            "<Configure>",
            lambda e: self.cards_canvas.itemconfigure(self._inner_id, width=e.width))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        # self._show_message("Hospitals that have everything you need will appear here, "
        #                    "nearest first.", color=MUTED)

        # ---------------- Right panel ----------------
        right = tk.Frame(body, bg=CREAM)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 14), pady=14)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        bar = tk.Frame(right, bg=CREAM)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.map_title = tk.Label(bar, text="Route map", font=(FONT, 12, "bold"),
                                  fg=NAVY, bg=CREAM)
        self.map_title.pack(side="left")
        self.nav_btn = tk.Button(
            bar, text="Open in Google Maps ↗", command=self._open_in_maps,
            font=(FONT, 10), bg=NAVY, fg=WHITE, activebackground="#0A3248",
            activeforeground=WHITE, relief="flat", padx=10, pady=3,
            cursor="hand2", state="disabled")
        self.nav_btn.pack(side="right")
        tk.Label(bar, text="A = your location   B = hospital", font=(FONT, 9),
                 fg=MUTED, bg=CREAM).pack(side="right", padx=12)

        self.map_container = tk.Frame(right, bg=WHITE, highlightthickness=1,
                                      highlightbackground=BORDER)
        self.map_container.grid(row=1, column=0, sticky="nsew")
        self.map_label = tk.Label(
            self.map_container, text="",
            font=(FONT, 11), fg=MUTED, bg=WHITE)
        self.map_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.map_container.bind("<Configure>", self._on_map_resize)

    def _build_aidpilot_tab(self) -> None:
        tab = self.aidpilot_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        sources = tk.Frame(tab, bg=WHITE, highlightthickness=1,
                           highlightbackground=BORDER, width=320)
        sources.grid(row=0, column=0, sticky="ns", padx=(2, 8), pady=12)
        sources.grid_propagate(False)
        tk.Label(sources, text="Knowledge sources", font=(FONT, 13, "bold"),
                 fg=NAVY, bg=WHITE, anchor="w").pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(sources, text="WMS documents used to ground responses",
                 font=(FONT, 9), fg=MUTED, bg=WHITE, anchor="w").pack(
                     fill="x", padx=14, pady=(0, 10))
        source_frame = tk.Frame(sources, bg=WHITE)
        source_frame.pack(fill="both", expand=True, padx=10)
        self.source_list = tk.Listbox(source_frame, font=(FONT, 10), relief="flat",
                                      activestyle="none", selectbackground=BLUSH,
                                      selectforeground=NAVY, bg=CREAM,
                                      highlightthickness=0)
        source_scroll = ttk.Scrollbar(source_frame, command=self.source_list.yview)
        self.source_list.configure(yscrollcommand=source_scroll.set)
        self.source_list.pack(side="left", fill="both", expand=True)
        source_scroll.pack(side="right", fill="y")
        self.source_list.bind("<Double-Button-1>", lambda _e: self._view_source())
        actions = tk.Frame(sources, bg=WHITE)
        actions.pack(fill="x", padx=12, pady=12)
        for text, command in (("View", self._view_source),
                              ("Add", self._add_source),
                              ("Remove", self._remove_source)):
            tk.Button(actions, text=text, command=command, font=(FONT, 9),
                      bg=NAVY if text == "Add" else CREAM, fg=WHITE if text == "Add" else NAVY,
                      activebackground=BLUSH, relief="flat", padx=8, pady=4,
                      cursor="hand2").pack(side="left", padx=(0, 5))

        chat = tk.Frame(tab, bg=CREAM)
        chat.grid(row=0, column=1, sticky="nsew", pady=12)
        chat.grid_columnconfigure(0, weight=1)
        chat.grid_rowconfigure(1, weight=1)
        tk.Label(chat, text="AidPilot", font=(FONT, 18, "bold"),
                 fg=NAVY, bg=CREAM, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(chat, text="Describe what is happening. Responses are grounded in the selected WMS sources.",
                 font=(FONT, 10), fg=MUTED, bg=CREAM, anchor="w").grid(row=0, column=0, sticky="e")
        self.aid_chat = tk.Text(chat, font=(FONT, 11), wrap="word", state="disabled",
                                relief="flat", bg=WHITE, fg="#222222",
                                highlightthickness=1, highlightbackground=BORDER,
                                padx=14, pady=12)
        self.aid_chat.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        composer = tk.Frame(chat, bg=CREAM)
        composer.grid(row=2, column=0, sticky="ew")
        composer.grid_columnconfigure(0, weight=1)
        self.aid_input = tk.Text(composer, height=3, font=(FONT, 11), wrap="word",
                                 relief="flat", bg=WHITE, fg="black",
                                 highlightthickness=1, highlightbackground=BORDER,
                                 padx=8, pady=6)
        self.aid_input.grid(row=0, column=0, sticky="ew")
        self.aid_input.bind("<Return>", self._on_aid_enter)
        tk.Button(composer, text="Ask AidPilot", command=self._submit_aid,
                  font=(FONT, 10, "bold"), bg=RED, fg=WHITE, relief="flat",
                  activebackground="#A30000", activeforeground=WHITE,
                  padx=12, pady=7, cursor="hand2").grid(row=0, column=1, padx=(8, 0))
        self._refresh_sources()

    def _refresh_sources(self) -> None:
        self.source_list.delete(0, "end")
        for source in self._aid_sources:
            self.source_list.insert("end", source.name.replace("wms_", "").replace("_", " "))

    def _selected_source(self) -> Path | None:
        selection = self.source_list.curselection()
        return self._aid_sources[selection[0]] if selection else None

    def _view_source(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        window = tk.Toplevel(self.root)
        window.title(f"AidPilot source – {source.name}")
        window.geometry("760x600")
        text = tk.Text(window, wrap="word", font=(FONT, 10), padx=12, pady=12)
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
                self._refresh_sources()

    def _remove_source(self) -> None:
        selection = self.source_list.curselection()
        if selection:
            del self._aid_sources[selection[0]]
            self._refresh_sources()

    def _on_aid_enter(self, _event: tk.Event) -> str:
        self._submit_aid()
        return "break"

    def _submit_aid(self) -> None:
        question = self.aid_input.get("1.0", "end").strip()
        if not question or getattr(self, "_aid_busy", False):
            return
        self._aid_busy = True
        self._append_aid("You", question)
        self.aid_input.delete("1.0", "end")
        self._append_aid("AidPilot", "Thinking from the selected WMS sources…")
        threading.Thread(target=self._aid_worker, args=(question, list(self._aid_sources)),
                         daemon=True).start()

    def _aid_worker(self, question: str, sources: list[Path]) -> None:
        try:
            self._queue.put(("aid_result", ask_aid_pilot(question, sources)))
        except Exception as exc:
            logger.exception("AidPilot failed")
            self._queue.put(("aid_error", str(exc) or exc.__class__.__name__))

    def _append_aid(self, speaker: str, text: str) -> None:
        self.aid_chat.configure(state="normal")
        self.aid_chat.insert("end", f"{speaker}\n{text}\n\n")
        self.aid_chat.configure(state="disabled")
        self.aid_chat.see("end")

    def _build_footer(self) -> None:
        tk.Label(
            self.root,
            text=EMERGENCY_NOTE,
            font=(FONT, 9), fg=MUTED, bg=CREAM,
        ).grid(row=2, column=0, pady=(0, 6))

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------
    def _on_enter(self, _event: tk.Event) -> str:
        self._submit()
        return "break"

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None or not str(widget).startswith(str(self.cards_container)):
            return
        if self.cards_canvas.yview() == (0.0, 1.0):
            return
        self.cards_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _submit(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text or self._busy:
            return
        self._busy = True
        self.submit_btn.configure(state="disabled")
        self._set_status("Analyzing your concern and finding the nearest suitable hospitals…")
        self.summary_label.configure(text="")
        self._clear_cards()
        self._clear_map()
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

    def _on_aid_result(self, response: str) -> None:
        self._aid_busy = False
        self._remove_last_aid_thinking()
        self._append_aid("AidPilot", response)

    def _on_aid_error(self, message: str) -> None:
        self._aid_busy = False
        self._remove_last_aid_thinking()
        self._append_aid("AidPilot", f"⚠️ {message}")

    def _remove_last_aid_thinking(self) -> None:
        self.aid_chat.configure(state="normal")
        content = self.aid_chat.get("1.0", "end-1c")
        marker = "AidPilot\nThinking from the selected WMS sources…\n\n"
        if content.endswith(marker):
            self.aid_chat.delete(f"end-{len(marker)}c", "end-1c")
        self.aid_chat.configure(state="disabled")

    def _on_error(self, message: str) -> None:
        self._finish_request()
        self._set_status("")
        self._show_message(f"⚠️ {message}\n\n{EMERGENCY_NOTE}", color=RED)

    def _on_result(self, result: dict[str, Any]) -> None:
        self._finish_request()
        self._set_status("")
        if result["origin"] is not None:
            self._origin = result["origin"]
            self._set_location_label(result["origin"])

        resources = ", ".join(result["required_resources"])
        self.summary_label.configure(
            text=f"Concern: {result['concern']}\nRequired: {resources}")

        if result["error"]:
            self._show_message(f"⚠️ {result['error']}\n\n{EMERGENCY_NOTE}", color=RED)
            return
        if not result["hospitals"]:
            self._show_message(
                "⚠️ No hospital in the database currently has all the required "
                f"resources available.\n\n{EMERGENCY_NOTE}", color=RED)
            return

        self._clear_cards()  # resets self._hospitals, so assign afterwards
        self._hospitals = result["hospitals"]
        for i, hospital in enumerate(self._hospitals):
            self._build_card(i, hospital)
        self._select(0)

    def _on_map(self, key: str, data: bytes | None) -> None:
        self._map_cache[key] = data
        if self._selected is not None and key == self._map_key(self._hospitals[self._selected]):
            self._display_map()

    # ------------------------------------------------------------------
    # Hospital cards
    # ------------------------------------------------------------------
    def _clear_cards(self) -> None:
        for child in self.cards_inner.winfo_children():
            child.destroy()
        self._card_widgets = []
        self._hospitals = []
        self._selected = None
        self.cards_canvas.yview_moveto(0)

    def _show_message(self, text: str, color: str = MUTED) -> None:
        self._clear_cards()
        tk.Label(self.cards_inner, text=text, font=(FONT, 11), fg=color, bg=CREAM,
                 justify="left", anchor="w", wraplength=450).pack(
                     fill="x", padx=14, pady=10)

    def _build_card(self, index: int, h: dict[str, Any]) -> None:
        outer = tk.Frame(self.cards_inner, bg=WHITE, highlightthickness=2,
                         highlightbackground=BORDER, cursor="hand2")
        outer.pack(fill="x", padx=(10, 6), pady=6)
        widgets: list[tk.Misc] = [outer]

        body = tk.Frame(outer, bg=WHITE)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        info = tk.Frame(body, bg=WHITE)
        info.pack(side="left", fill="both", expand=True)
        stats = tk.Frame(body, bg=WHITE)
        stats.pack(side="right", anchor="n", padx=(12, 0))
        widgets += [body, info, stats]

        def label(parent: tk.Misc, **kw: Any) -> tk.Label:
            lbl = tk.Label(parent, bg=WHITE, cursor="hand2", **kw)
            widgets.append(lbl)
            return lbl

        if index == 0:
            label(info, text="★ NEAREST", font=(FONT, 9, "bold"), fg=RED,
                  anchor="w").pack(fill="x")
        label(info, text=h["name"], font=(FONT, 13, "bold"), fg=NAVY,
              anchor="w", justify="left", wraplength=260).pack(fill="x")
        label(info, text=h.get("address") or "Address not available",
              font=(FONT, 10), fg="#222222", anchor="w", justify="left",
              wraplength=260).pack(fill="x", pady=(4, 0))
        label(info, text=f"📞 {h.get('phone') or 'Phone not available'}",
              font=(FONT, 10), fg="#222222", anchor="w").pack(fill="x", pady=(2, 0))

        label(stats, text="Est. travel time", font=(FONT, 9), fg=MUTED).pack(anchor="e")
        label(stats, text=h["duration"], font=(FONT, 17, "bold"), fg=NAVY).pack(anchor="e")
        label(stats, text="Distance", font=(FONT, 9), fg=MUTED).pack(anchor="e", pady=(8, 0))
        label(stats, text=h["distance"], font=(FONT, 12, "bold"), fg="#222222").pack(anchor="e")

        for w in widgets:
            w.bind("<Button-1>", lambda _e, i=index: self._select(i))
        self._card_widgets.append(widgets)

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self._hospitals)):
            return
        self._selected = index
        for i, widgets in enumerate(self._card_widgets):
            selected = i == index
            bg = BLUSH if selected else WHITE
            for w in widgets:
                w.configure(bg=bg)
            widgets[0].configure(highlightbackground=RED if selected else BORDER)
        self._show_map(self._hospitals[index])

    # ------------------------------------------------------------------
    # Map pane
    # ------------------------------------------------------------------
    @staticmethod
    def _map_key(hospital: dict[str, Any]) -> str:
        return f"{hospital['name']}|{hospital['latitude']},{hospital['longitude']}"

    def _clear_map(self) -> None:
        self._map_photo = None
        self.map_label.configure(image="", text="Your route will appear here.")
        self.map_title.configure(text="Route map")
        self.nav_btn.configure(state="disabled")

    def _show_map(self, hospital: dict[str, Any]) -> None:
        self.map_title.configure(text=f"Route to {hospital['name']}")
        self.nav_btn.configure(state="normal")
        key = self._map_key(hospital)
        if key in self._map_cache:
            self._display_map()
            return
        self._map_photo = None
        self.map_label.configure(image="", text="Loading route map…")
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
            self._map_photo = None
            self.map_label.configure(
                image="", text="Map preview unavailable.\nUse “Open in Google Maps” for directions.")
            return
        width = max(self.map_container.winfo_width() - 2, 50)
        height = max(self.map_container.winfo_height() - 2, 50)
        image = ImageOps.contain(Image.open(BytesIO(data)), (width, height),
                                 Image.LANCZOS)
        self._map_photo = ImageTk.PhotoImage(image)
        self.map_label.configure(image=self._map_photo, text="")

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
        self.submit_btn.configure(state="normal")

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _set_location_label(self, loc: DeviceLocation | None,
                            locating: bool = False) -> None:
        if locating:
            text, color = "📍 Locating your device…", MUTED
        elif loc is None:
            text, color = "📍 Location unavailable – enable Location Services", WARN
        elif loc.is_precise:
            text, color = f"📍 {loc.label()}", OK
        else:
            text, color = f"📍 {loc.label()}", WARN
        self.location_label.configure(text=text, fg=color)
