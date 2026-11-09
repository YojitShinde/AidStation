"""
ERoute - ER Assistant
Tkinter UI with a modern emergency-themed design.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import scrolledtext

from agent import run_agent


# ---------------------------------------------------------------------------
# Placeholder text box
# ---------------------------------------------------------------------------
class PlaceholderText(tk.Text):
    """A Text widget that shows greyed-out placeholder text when empty."""

    def __init__(self, master, placeholder: str = "", **kwargs):
        super().__init__(master, **kwargs)
        self.placeholder = placeholder
        self._placeholder_active = False

        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

        # Show placeholder initially
        self._show_placeholder()

    def _show_placeholder(self):
        self.delete("1.0", tk.END)
        self.insert("1.0", self.placeholder)
        self.config(fg="#888888")
        self._placeholder_active = True

    def _on_focus_in(self, _event):
        if self._placeholder_active:
            self.delete("1.0", tk.END)
            self.config(fg="#FFFFFF")
            self._placeholder_active = False

    def _on_focus_out(self, _event):
        content = self.get("1.0", tk.END).strip()
        if not content:
            self._show_placeholder()

    def get_text(self) -> str:
        """Return the user's text, ignoring placeholder."""
        if self._placeholder_active:
            return ""
        return self.get("1.0", tk.END).strip()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class ERouteApp:
    """ERoute ER Assistant – Tkinter UI."""

    SAMPLE_PROMPT = (
        "I'm experiencing severe chest pain and difficulty breathing. "
        "I'm currently near Andheri West, Mumbai. "
        "Find the nearest hospital with the required services."
    )

    # Colors
    BG_DARK = "#1a1a2e"
    BG_PANEL = "#16213e"
    BG_INPUT = "#0f3460"
    ACCENT_RED = "#e94560"
    ACCENT_GREEN = "#00b894"
    TEXT_WHITE = "#FFFFFF"
    TEXT_GREY = "#b0b0b0"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ERoute – ER Assistant")
        self.root.geometry("900x700")
        self.root.minsize(750, 600)
        self.root.configure(bg=self.BG_DARK)
        self.root.resizable(True, True)

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # --- Header ---
        header = tk.Frame(self.root, bg=self.ACCENT_RED, height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(
            header,
            text="🏥  ERoute – ER Assistant",
            font=("Segoe UI", 18, "bold"),
            bg=self.ACCENT_RED,
            fg=self.TEXT_WHITE,
        ).pack(side=tk.LEFT, padx=20, pady=10)

        tk.Label(
            header,
            text="Guiding you to the right hospital, fast.",
            font=("Segoe UI", 10),
            bg=self.ACCENT_RED,
            fg="#ffd5dc",
        ).pack(side=tk.RIGHT, padx=20, pady=10)

        # --- Body container ---
        body = tk.Frame(self.root, bg=self.BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        # --- Input Section ---
        input_frame = tk.LabelFrame(
            body,
            text="  Describe your medical situation  ",
            font=("Segoe UI", 11, "bold"),
            bg=self.BG_PANEL,
            fg=self.TEXT_GREY,
            bd=1,
            relief=tk.GROOVE,
            padx=10,
            pady=10,
        )
        input_frame.pack(fill=tk.X, pady=(0, 10))

        self.input_text = PlaceholderText(
            input_frame,
            placeholder=self.SAMPLE_PROMPT,
            height=5,
            font=("Segoe UI", 11),
            bg=self.BG_INPUT,
            fg=self.TEXT_WHITE,
            insertbackground=self.TEXT_WHITE,
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=10,
            pady=10,
            bd=0,
        )
        self.input_text.pack(fill=tk.X, pady=(0, 10))

        # --- Buttons ---
        btn_frame = tk.Frame(input_frame, bg=self.BG_PANEL)
        btn_frame.pack(fill=tk.X)

        self.find_btn = tk.Button(
            btn_frame,
            text="🔍  Find Hospital",
            font=("Segoe UI", 12, "bold"),
            bg=self.ACCENT_RED,
            fg=self.TEXT_WHITE,
            activebackground="#c0392b",
            activeforeground=self.TEXT_WHITE,
            relief=tk.FLAT,
            cursor="hand2",
            padx=20,
            pady=8,
            command=self._on_find,
        )
        self.find_btn.pack(side=tk.LEFT)

        self.clear_btn = tk.Button(
            btn_frame,
            text="🗑  Clear",
            font=("Segoe UI", 10),
            bg="#2c3e50",
            fg=self.TEXT_WHITE,
            activebackground="#34495e",
            activeforeground=self.TEXT_WHITE,
            relief=tk.FLAT,
            cursor="hand2",
            padx=15,
            pady=8,
            command=self._on_clear,
        )
        self.clear_btn.pack(side=tk.LEFT, padx=(10, 0))

        # --- Status ---
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = tk.Label(
            btn_frame,
            textvariable=self.status_var,
            font=("Segoe UI", 10),
            bg=self.BG_PANEL,
            fg=self.ACCENT_GREEN,
        )
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # --- Output Section ---
        output_frame = tk.LabelFrame(
            body,
            text="  Results  ",
            font=("Segoe UI", 11, "bold"),
            bg=self.BG_PANEL,
            fg=self.TEXT_GREY,
            bd=1,
            relief=tk.GROOVE,
            padx=10,
            pady=10,
        )
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output_text = scrolledtext.ScrolledText(
            output_frame,
            font=("Segoe UI", 11),
            bg=self.BG_INPUT,
            fg=self.TEXT_WHITE,
            insertbackground=self.TEXT_WHITE,
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=10,
            pady=10,
            state=tk.DISABLED,
            bd=0,
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)

        # --- Footer ---
        tk.Label(
            self.root,
            text="⚠️  In a life-threatening emergency, call 112 immediately.",
            font=("Segoe UI", 9),
            bg=self.BG_DARK,
            fg="#e74c3c",
        ).pack(pady=(0, 8))

    # ----------------------------------------------------------- Callbacks
    def _on_find(self):
        user_input = self.input_text.get_text()
        if not user_input:
            self._show_output("Please describe your medical situation first.")
            return

        # Disable button and show loading
        self.find_btn.config(state=tk.DISABLED)
        self.status_var.set("⏳ Analyzing your situation...")
        self.status_label.config(fg="#f39c12")
        self._show_output("Processing your request, please wait...\n")

        # Run the agent in a background thread to keep UI responsive
        thread = threading.Thread(target=self._run_agent_thread, args=(user_input,))
        thread.daemon = True
        thread.start()

    def _run_agent_thread(self, user_input: str):
        """Execute the agent pipeline on a background thread."""
        try:
            response = run_agent(user_input)
            self.root.after(0, self._on_agent_done, response)
        except Exception as exc:
            error_msg = f"❌ An error occurred:\n{exc}"
            self.root.after(0, self._on_agent_done, error_msg)

    def _on_agent_done(self, response: str):
        """Called on the main thread when the agent finishes."""
        self._show_output(response)
        self.find_btn.config(state=tk.NORMAL)
        self.status_var.set("✅ Done")
        self.status_label.config(fg=self.ACCENT_GREEN)

    def _on_clear(self):
        self.input_text._show_placeholder()
        self._show_output("")
        self.status_var.set("Ready")
        self.status_label.config(fg=self.ACCENT_GREEN)

    def _show_output(self, text: str):
        self.output_text.config(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", text)
        self.output_text.config(state=tk.DISABLED)

    # ---------------------------------------------------------------- Run
    def run(self):
        self.root.mainloop()
