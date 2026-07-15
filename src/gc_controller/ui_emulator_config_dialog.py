"""
UI Emulator Config Dialog

Lets the user install the right controller configs into detected emulators
(RetroArch, Dolphin, …) in one click, so the NSO GameCube pads work without
hand-binding. Existing files are backed up before any change.

All emulator-config logic lives in ``emulator_config.py`` (pure, no Tkinter);
this module only presents it.
"""

import sys
import threading
import tkinter as tk
from typing import Callable, Optional

import customtkinter

from . import ui_theme as T
from . import emulator_config as ec
from .i18n import t


class EmulatorConfigDialog:
    """Modal dialog to detect emulators and apply GC controller configs."""

    def __init__(self, parent,
                 launchbox_path: str = "",
                 on_launchbox_path_changed: Optional[Callable[[str], None]] = None):
        self._parent = parent
        self._launchbox_path = launchbox_path or ""
        self._on_launchbox_path_changed = on_launchbox_path_changed
        self._targets = []
        self._vars = {}          # target.id -> BooleanVar
        self._busy = False

        self._dlg = customtkinter.CTkToplevel(parent)
        self._dlg.title(t("emucfg.title"))
        self._dlg.resizable(False, False)
        self._dlg.transient(parent)
        self._dlg.configure(fg_color=T.GC_PURPLE_DARK)

        outer = customtkinter.CTkFrame(self._dlg, fg_color=T.GC_PURPLE_DARK)
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        customtkinter.CTkLabel(
            outer, text=t("emucfg.title"),
            text_color=T.TEXT_PRIMARY, font=(T.FONT_FAMILY, 18, "bold"),
        ).pack(anchor=tk.W)

        customtkinter.CTkLabel(
            outer, text=t("emucfg.intro"),
            text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 12),
            wraplength=440, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 12))

        self._list_frame = customtkinter.CTkFrame(outer, fg_color="transparent")
        self._list_frame.pack(fill=tk.BOTH, expand=True)

        lb_link = customtkinter.CTkLabel(
            outer, text="📁 " + t("emucfg.locate_launchbox"),
            text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 11, "underline"),
            cursor="hand2",
        )
        lb_link.pack(anchor=tk.W, pady=(6, 0))
        lb_link.bind("<Button-1>", lambda e: self._locate_launchbox())

        self._warning = customtkinter.CTkLabel(
            outer, text="", text_color="#F2B84B",
            font=(T.FONT_FAMILY, 12), wraplength=440, justify=tk.LEFT,
        )
        self._warning.pack(anchor=tk.W, pady=(8, 0))

        self._status = customtkinter.CTkLabel(
            outer, text="", text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 12), wraplength=440, justify=tk.LEFT,
        )
        self._status.pack(anchor=tk.W, pady=(4, 0))

        btn_row = customtkinter.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill=tk.X, pady=(14, 0))

        self._apply_btn = customtkinter.CTkButton(
            btn_row, text=t("emucfg.apply"),
            command=self._on_apply_click,
            fg_color=T.BTN_FG, hover_color=T.BTN_HOVER, text_color=T.BTN_TEXT,
            corner_radius=12, height=36, width=220, font=(T.FONT_FAMILY, 14),
        )
        self._apply_btn.pack(side=tk.LEFT)

        customtkinter.CTkButton(
            btn_row, text=t("btn.cancel"),
            command=self._dlg.destroy,
            fg_color="#463F6F", hover_color="#5A5190", text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=120, font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

        self._detect_and_build()

        self._dlg.protocol("WM_DELETE_WINDOW", self._dlg.destroy)
        self._center_on_parent()
        self._dlg.after(10, self._dlg.grab_set)

    # ── Detection / list building ──────────────────────────────────────

    def _detect_and_build(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._vars.clear()

        try:
            self._targets = ec.detect_emulators(self._launchbox_path or None)
        except Exception as e:
            self._targets = []
            self._status.configure(text=f"{e}")

        if not self._targets:
            customtkinter.CTkLabel(
                self._list_frame, text=t("emucfg.no_emulators"),
                text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 13),
            ).pack(anchor=tk.W, pady=4)
            self._apply_btn.configure(state="disabled")
            return

        for tgt in self._targets:
            row = customtkinter.CTkFrame(self._list_frame, fg_color="transparent")
            row.pack(anchor=tk.W, fill=tk.X, pady=3)

            if tgt.actionable:
                var = tk.BooleanVar(value=True)
                self._vars[tgt.id] = var
                label = tgt.name
                if tgt.detected_from == "launchbox":
                    label += "  (LaunchBox)"
                customtkinter.CTkCheckBox(
                    row, text=label, variable=var,
                    fg_color=T.RADIO_FG, hover_color=T.RADIO_HOVER,
                    checkmark_color=T.BTN_TEXT, border_color=T.RADIO_BORDER,
                    text_color=T.TEXT_PRIMARY, font=(T.FONT_FAMILY, 14),
                ).pack(anchor=tk.W)
                detail = "  ·  ".join(gf.summary for gf in tgt.files if gf.summary)
                if detail:
                    customtkinter.CTkLabel(
                        row, text=detail, text_color=T.TEXT_SECONDARY,
                        font=(T.FONT_FAMILY, 11), wraplength=420, justify=tk.LEFT,
                    ).pack(anchor=tk.W, padx=28)
            else:
                customtkinter.CTkLabel(
                    row, text=f"{tgt.name}  —  {t('emucfg.auto_ok')}",
                    text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 13),
                ).pack(anchor=tk.W)

        self._refresh_running_warning()

    def _refresh_running_warning(self):
        actionable_ids = [t_.id for t_ in self._targets if t_.actionable]
        running = ec.running_emulators(actionable_ids)
        if running:
            names = ", ".join(
                t_.name for t_ in self._targets if t_.id in running)
            self._warning.configure(text=t("emucfg.running_warning", emus=names))
        else:
            self._warning.configure(text="")

    def _locate_launchbox(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(
            parent=self._dlg, title=t("emucfg.locate_launchbox"),
            initialdir=self._launchbox_path or None)
        if not path:
            return
        self._launchbox_path = path
        if self._on_launchbox_path_changed:
            self._on_launchbox_path_changed(path)
        self._detect_and_build()

    # ── Apply ──────────────────────────────────────────────────────────

    def _on_apply_click(self):
        if self._busy:
            return
        selected = [t_ for t_ in self._targets
                    if t_.actionable and self._vars.get(t_.id)
                    and self._vars[t_.id].get()]
        if not selected:
            return

        # Block if any selected emulator is currently running (would clobber).
        running = ec.running_emulators([t_.id for t_ in selected])
        if running:
            names = ", ".join(t_.name for t_ in selected if t_.id in running)
            self._warning.configure(text=t("emucfg.running_warning", emus=names))
            return

        self._busy = True
        self._apply_btn.configure(state="disabled", text=t("emucfg.applying"))
        self._status.configure(text="")

        files = [gf for t_ in selected for gf in t_.files]

        def worker():
            result = ec.apply_files(files, backup=True)
            self._dlg.after(0, lambda: self._on_apply_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_apply_done(self, result):
        self._busy = False
        self._apply_btn.configure(state="normal", text=t("emucfg.apply"))
        if result.errors:
            self._status.configure(
                text=t("emucfg.error", errors="; ".join(result.errors)),
                text_color="#E06C6C")
        else:
            msg = t("emucfg.done", n=len(result.written))
            if result.backups:
                msg += "\n" + t("emucfg.backup_note")
            self._status.configure(text=msg, text_color="#7ED08B")
        # Re-detect so subsequent applies show correct create/modify state.
        self._detect_and_build()

    # ── Window placement ───────────────────────────────────────────────

    def _center_on_parent(self):
        self._dlg.update_idletasks()
        p = self._parent
        try:
            x = p.winfo_x() + (p.winfo_width() - self._dlg.winfo_width()) // 2
            y = p.winfo_y() + (p.winfo_height() - self._dlg.winfo_height()) // 2
            self._dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass
