"""
UI BLE Controller Scan — Live streaming scan dialog for BLE controller discovery.

Starts scanning immediately, shows Nintendo controllers in real-time as they
are detected, and auto-connects after a grace period if exactly one is found.
"""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

import customtkinter

from . import ui_theme as T
from .i18n import t

logger = logging.getLogger(__name__)

_NINTENDO_COMPANY_IDS = frozenset({
    '894',   # 0x037E — Nintendo (classic controllers)
    '1363',  # 0x0553 — Nintendo (Switch 2 controllers)
})

_NINTENDO_SERVICE_UUIDS = frozenset({
    '00c5af5d-1964-4e30-8f51-1956f96bd280',
    'ab7de9be-89fe-49ad-828f-118f09df7fd0',
})

_NINTENDO_OUI = frozenset({
    '04:03:D6', '58:2F:40', '7C:BB:8A', '98:B6:E9',
    'A4:C0:E1', 'B8:AE:6E', 'CC:FB:65', 'D4:F0:57',
    'DC:68:EB', 'E0:E7:51', 'E8:4E:CE', '40:F4:07',
    '2C:10:C1', '34:AF:2C', '48:A5:E7', '64:B5:C6',
    '8C:56:C5', '9C:E6:35', 'A0:AB:1B', 'B0:9F:BA',
    'BC:83:85', 'E4:17:D8', 'E8:65:D4', 'EC:10:7B',
})

_CONTROLLER_NAME_PATTERNS = (
    'pro controller', 'joy-con', 'gamecube', 'gc controller',
    'nso', 'nintendo', 'hvc', 'snes', 'n64', 'sega',
    'devicename',
)

_AUTO_CONNECT_DELAY_MS = 3000


def _is_likely_controller(dev: dict) -> bool:
    """Detect Nintendo controllers using BLE advertisement data."""
    mfg = dev.get('manufacturer_data', {})
    if mfg and _NINTENDO_COMPANY_IDS & set(mfg.keys()):
        return True
    svc = dev.get('service_uuids', [])
    if svc and _NINTENDO_SERVICE_UUIDS & set(s.lower() for s in svc):
        return True
    name = (dev.get('name', '') or '').lower()
    if name and any(pat in name for pat in _CONTROLLER_NAME_PATTERNS):
        return True
    addr = dev.get('address', '').upper()
    if len(addr) >= 8 and addr[:8] in _NINTENDO_OUI:
        return True
    return False


class BLEControllerScanDialog:
    """Live streaming scan dialog for discovering Nintendo BLE controllers.

    Scanning starts as soon as the dialog opens. Controllers appear in
    the picker as they are detected. If exactly one controller is found
    and no new controllers appear within the grace period, it is
    auto-selected.

    Args:
        parent: The parent window (CTk or Tk).
        on_start_scan: Called to begin streaming BLE scan.
        on_stop_scan: Called to stop the streaming scan.
        exclude_addresses: Set of uppercase MAC addresses already connected.
    """

    def __init__(self, parent,
                 on_start_scan: Callable[[], None],
                 on_stop_scan: Callable[[], None],
                 exclude_addresses: set[str] | None = None):
        self._result: Optional[str] = None
        self._on_start_scan = on_start_scan
        self._on_stop_scan = on_stop_scan
        self._exclude = {a.upper() for a in (exclude_addresses or ())}
        self._controllers: list[dict] = []
        self._other_devices: list[dict] = []
        self._seen_addresses: set[str] = set()
        self._auto_connect_timer: Optional[str] = None
        self._scanning = False
        self._closed = False

        self._dlg = customtkinter.CTkToplevel(parent)
        self._dlg.title(t("scan.title"))
        self._dlg.resizable(False, False)
        self._dlg.transient(parent)
        self._dlg.configure(fg_color=T.GC_PURPLE_DARK)

        self._outer = customtkinter.CTkFrame(
            self._dlg, fg_color=T.GC_PURPLE_DARK)
        self._outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        self._content = customtkinter.CTkFrame(
            self._outer, fg_color="transparent")
        self._content.pack(fill=tk.BOTH, expand=True)

        self._btn_bar = customtkinter.CTkFrame(
            self._outer, fg_color="transparent")
        self._btn_bar.pack(fill=tk.X, pady=(16, 0))

        self._build_scanning_ui()
        self._start_scan()

        self._dlg.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._dlg.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_x()
        py = parent.winfo_y()
        dw = self._dlg.winfo_width()
        dh = self._dlg.winfo_height()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        self._dlg.geometry(f"+{x}+{y}")

        self._dlg.after(10, self._dlg.grab_set)

    def _clear_content(self):
        for w in self._content.winfo_children():
            w.destroy()
        for w in self._btn_bar.winfo_children():
            w.destroy()

    def _build_scanning_ui(self):
        """Build the initial scanning view with progress and instruction."""
        self._clear_content()

        customtkinter.CTkLabel(
            self._content, text=t("scan.heading"),
            text_color=T.TEXT_PRIMARY,
            font=(T.FONT_FAMILY, 16, "bold"),
        ).pack(anchor=tk.W, pady=(0, 8))

        customtkinter.CTkLabel(
            self._content,
            text=t("scan.live_instructions"),
            text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 13),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 12))

        self._progress = customtkinter.CTkProgressBar(
            self._content,
            fg_color=T.SURFACE_DARK,
            progress_color=T.GC_PURPLE_LIGHT,
            width=400,
        )
        self._progress.pack(pady=(0, 8))
        self._progress.configure(mode="indeterminate")
        self._progress.start()

        self._status_label = customtkinter.CTkLabel(
            self._content, text=t("scan.scanning"),
            text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 12),
        )
        self._status_label.pack(anchor=tk.W, pady=(0, 8))

        self._tree_frame = customtkinter.CTkFrame(
            self._content, fg_color="transparent")
        self._tree_frame.pack(fill=tk.BOTH, expand=True)
        self._tree: Optional[ttk.Treeview] = None
        self._tree_initialized = False

        # Buttons
        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.cancel"),
            command=self._on_cancel,
            fg_color=T.GC_PURPLE_SURFACE,
            hover_color=T.GC_PURPLE_LIGHT,
            text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=100,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

    def _start_scan(self):
        """Request the backend to start streaming scan."""
        self._scanning = True
        self._on_start_scan()

    def _stop_scan(self):
        """Request the backend to stop scanning."""
        if self._scanning:
            self._scanning = False
            try:
                self._on_stop_scan()
            except Exception:
                pass

    # ── Device callbacks (called from app on main thread) ─────────

    def add_device(self, dev: dict):
        """Called by the app when a new BLE device is detected during scan."""
        if self._closed:
            return

        addr = dev.get('address', '').upper()
        if not addr or addr in self._exclude or addr in self._seen_addresses:
            return

        self._seen_addresses.add(addr)
        is_ctrl = _is_likely_controller(dev)

        if is_ctrl:
            self._controllers.append(dev)
            logger.info("Scan: Nintendo controller detected: %s  name=%r  "
                        "rssi=%s  mfg=%s",
                        addr, dev.get('name'),
                        dev.get('rssi'), dev.get('manufacturer_data', {}))
        else:
            self._other_devices.append(dev)

        if is_ctrl:
            self._update_tree()
            self._reset_auto_connect_timer()

    def _update_tree(self):
        """Rebuild or update the treeview with current controllers."""
        if not self._controllers:
            return

        n = len(self._controllers)
        if self._status_label and self._status_label.winfo_exists():
            self._status_label.configure(
                text=t("scan.found_n", n=n))

        if not self._tree_initialized:
            self._init_tree()

        # Clear and re-insert
        for item in self._tree.get_children():
            self._tree.delete(item)

        sorted_ctrls = sorted(self._controllers,
                              key=lambda d: (d.get('rssi', -999) * -1))
        first_iid = None
        for dev in sorted_ctrls:
            rssi = dev.get('rssi', -999)
            signal = f"{rssi} dBm" if rssi > -999 else "?"
            label = dev.get('name') or t("scan.nintendo_controller")
            iid = self._tree.insert("", tk.END, values=(
                label, dev['address'], signal))
            if first_iid is None:
                first_iid = iid

        if first_iid:
            self._tree.selection_set(first_iid)

        self._ensure_connect_btn()

    def _init_tree(self):
        """Create the treeview widget."""
        style = ttk.Style()
        style.theme_use('default')
        style.configure('WizBLE.Treeview',
                        background=T.SURFACE_DARK,
                        foreground=T.TEXT_PRIMARY,
                        fieldbackground=T.SURFACE_DARK,
                        borderwidth=0,
                        font=("", 11))
        style.configure('WizBLE.Treeview.Heading',
                        background=T.GC_PURPLE_MID,
                        foreground=T.TEXT_PRIMARY,
                        borderwidth=0,
                        font=("", 11, "bold"))
        style.map('WizBLE.Treeview',
                  background=[('selected', T.GC_PURPLE_LIGHT)],
                  foreground=[('selected', T.TEXT_PRIMARY)])

        cols = ("type", "address", "signal")
        self._tree = ttk.Treeview(
            self._tree_frame, columns=cols, show="headings",
            height=4,
            style='WizBLE.Treeview')
        self._tree.heading("type", text=t("scan.col_type"))
        self._tree.heading("address", text=t("scan.col_address"))
        self._tree.heading("signal", text=t("scan.col_signal"))
        self._tree.column("type", width=180)
        self._tree.column("address", width=160)
        self._tree.column("signal", width=60, anchor=tk.CENTER)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<Double-1>", lambda _: self._on_connect())
        self._tree_initialized = True

    def _ensure_connect_btn(self):
        """Add the Connect button to the button bar if not already present."""
        if hasattr(self, '_connect_btn') and self._connect_btn:
            return
        self._connect_btn = customtkinter.CTkButton(
            self._btn_bar, text=t("btn.connect"),
            command=self._on_connect,
            fg_color=T.BTN_FG,
            hover_color=T.BTN_HOVER,
            text_color=T.BTN_TEXT,
            corner_radius=12, height=36, width=120,
            font=(T.FONT_FAMILY, 14),
        )
        self._connect_btn.pack(side=tk.RIGHT, padx=(8, 0))

    # ── Auto-connect timer ────────────────────────────────────────

    def _reset_auto_connect_timer(self):
        """Reset the auto-connect countdown. If exactly 1 controller and no
        new controllers appear within the grace period, auto-connect."""
        self._cancel_auto_connect_timer()
        if len(self._controllers) == 1:
            self._auto_connect_timer = self._dlg.after(
                _AUTO_CONNECT_DELAY_MS, self._try_auto_connect)

    def _cancel_auto_connect_timer(self):
        if self._auto_connect_timer is not None:
            try:
                self._dlg.after_cancel(self._auto_connect_timer)
            except Exception:
                pass
            self._auto_connect_timer = None

    def _try_auto_connect(self):
        """Auto-connect if exactly one controller was found."""
        self._auto_connect_timer = None
        if len(self._controllers) == 1 and not self._closed:
            logger.info("Auto-connecting to single controller: %s",
                        self._controllers[0].get('address'))
            self._result = self._controllers[0]['address']
            self._stop_scan()
            self._dlg.destroy()

    # ── Actions ───────────────────────────────────────────────────

    def _on_connect(self):
        """User clicked Connect or double-clicked a row."""
        if not self._tree:
            return
        sel = self._tree.selection()
        if sel:
            values = self._tree.item(sel[0], "values")
            self._result = values[1]  # address column
            logger.info("User selected controller: %s", self._result)
            self._stop_scan()
            self._closed = True
            self._cancel_auto_connect_timer()
            self._dlg.destroy()

    def _on_cancel(self):
        self._result = None
        self._stop_scan()
        self._closed = True
        self._cancel_auto_connect_timer()
        self._dlg.destroy()

    def show(self) -> Optional[str]:
        """Show the dialog and block until closed. Returns address or None."""
        self._dlg.wait_window()
        return self._result
