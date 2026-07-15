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
        self._closed = False
        self._detect_seq = 0     # guards against stale background detections
        self._nintendo_var = tk.BooleanVar(value=True)
        self._ps_var = tk.BooleanVar(value=False)

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

        # ── Nintendo layout toggle ──
        customtkinter.CTkCheckBox(
            outer, text=t("emucfg.nintendo_layout"),
            variable=self._nintendo_var, command=self._on_layout_toggle,
            fg_color=T.RADIO_FG, hover_color=T.RADIO_HOVER,
            checkmark_color=T.BTN_TEXT, border_color=T.RADIO_BORDER,
            text_color=T.TEXT_PRIMARY, font=(T.FONT_FAMILY, 13),
        ).pack(anchor=tk.W, pady=(0, 2))
        customtkinter.CTkLabel(
            outer, text=t("emucfg.nintendo_layout_hint"),
            text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 11),
            wraplength=440, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=28, pady=(0, 6))

        # ── PlayStation A/B fix (opt-in; only meaningful with Nintendo layout) ──
        self._ps_check = customtkinter.CTkCheckBox(
            outer, text=t("emucfg.playstation_fix"),
            variable=self._ps_var, command=self._on_layout_toggle,
            fg_color=T.RADIO_FG, hover_color=T.RADIO_HOVER,
            checkmark_color=T.BTN_TEXT, border_color=T.RADIO_BORDER,
            text_color=T.TEXT_PRIMARY, font=(T.FONT_FAMILY, 13),
        )
        self._ps_check.pack(anchor=tk.W, padx=28, pady=(0, 2))
        customtkinter.CTkLabel(
            outer, text=t("emucfg.playstation_fix_hint"),
            text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 11),
            wraplength=420, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=52, pady=(0, 10))

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
            command=self._on_close,
            fg_color="#463F6F", hover_color="#5A5190", text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=120, font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

        self._start_detection()

        self._dlg.protocol("WM_DELETE_WINDOW", self._on_close)
        self._center_on_parent()
        self._dlg.after(10, self._dlg.grab_set)

    def _on_close(self):
        self._closed = True
        self._dlg.destroy()

    # ── Detection / list building ──────────────────────────────────────

    def _start_detection(self):
        """Detect + generate off the UI thread so the window opens instantly.

        Detection reads/processes a large retroarch.cfg and runs `tasklist`
        (a subprocess); doing that on the Tk main thread froze the window on
        open. Here we show a placeholder immediately and compute in the
        background, then repaint via after().
        """
        # PS fix only applies on top of the Nintendo layout (instant, UI thread).
        nintendo = self._nintendo_var.get()
        try:
            self._ps_check.configure(state="normal" if nintendo else "disabled")
        except Exception:
            pass

        for w in self._list_frame.winfo_children():
            w.destroy()
        self._vars.clear()
        customtkinter.CTkLabel(
            self._list_frame, text=t("emucfg.detecting"),
            text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 13),
        ).pack(anchor=tk.W, pady=4)
        self._apply_btn.configure(state="disabled")

        self._detect_seq += 1
        seq = self._detect_seq
        lb = self._launchbox_path or None
        ps = nintendo and self._ps_var.get()

        def worker():
            err = None
            try:
                targets = ec.detect_emulators(lb, nintendo_layout=nintendo,
                                              playstation_fix=ps)
                running = ec.running_emulators(
                    [t_.id for t_ in targets if t_.actionable])
            except Exception as e:
                targets, running, err = [], [], str(e)
            try:
                self._dlg.after(
                    0, lambda: self._populate(seq, targets, running, err))
            except Exception:
                pass  # dialog already gone

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, seq, targets, running, err):
        # Ignore results from a superseded/late detection or a closed dialog.
        if self._closed or seq != self._detect_seq:
            return
        try:
            if not self._dlg.winfo_exists():
                return
        except Exception:
            return

        for w in self._list_frame.winfo_children():
            w.destroy()
        self._vars.clear()
        self._targets = targets

        if err:
            self._status.configure(text=err, text_color="#E06C6C")

        if not targets:
            customtkinter.CTkLabel(
                self._list_frame, text=t("emucfg.no_emulators"),
                text_color=T.TEXT_SECONDARY, font=(T.FONT_FAMILY, 13),
            ).pack(anchor=tk.W, pady=4)
            self._apply_btn.configure(state="disabled")
            self._warning.configure(text="")
            return

        for tgt in targets:
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

        self._apply_btn.configure(state="normal")
        if running:
            names = ", ".join(t_.name for t_ in targets if t_.id in running)
            self._warning.configure(text=t("emucfg.running_warning", emus=names))
        else:
            self._warning.configure(text="")

    def _on_layout_toggle(self):
        # Regenerate configs with the chosen A/B / PlayStation options.
        self._start_detection()

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
        self._start_detection()

    # ── Apply ──────────────────────────────────────────────────────────

    def _on_apply_click(self):
        if self._busy:
            return
        selected = [t_ for t_ in self._targets
                    if t_.actionable and self._vars.get(t_.id)
                    and self._vars[t_.id].get()]
        if not selected:
            return

        self._busy = True
        self._apply_btn.configure(state="disabled", text=t("emucfg.applying"))
        self._status.configure(text="")

        ids = [t_.id for t_ in selected]
        names_by_id = {t_.id: t_.name for t_ in selected}
        files = [gf for t_ in selected for gf in t_.files]

        def worker():
            # Both the running-check (tasklist) and the writes run off-thread.
            running = ec.running_emulators(ids)
            if running:
                try:
                    self._dlg.after(
                        0, lambda: self._on_apply_blocked(running, names_by_id))
                except Exception:
                    pass
                return
            result = ec.apply_files(files, backup=True)
            try:
                self._dlg.after(0, lambda: self._on_apply_done(result))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_apply_blocked(self, running, names_by_id):
        if self._closed:
            return
        self._busy = False
        self._apply_btn.configure(state="normal", text=t("emucfg.apply"))
        names = ", ".join(names_by_id[i] for i in running if i in names_by_id)
        self._warning.configure(text=t("emucfg.running_warning", emus=names))

    def _on_apply_done(self, result):
        if self._closed:
            return
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
        self._start_detection()

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
