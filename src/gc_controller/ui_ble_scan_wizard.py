"""
UI BLE Controller Scan — Single-scan dialog for BLE controller discovery.

Performs one scan, filters results for Nintendo controllers using
manufacturer_data / service UUIDs / name / OUI, and always shows
a picker for user confirmation.
"""

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

import customtkinter

from . import ui_theme as T
from .i18n import t

logger = logging.getLogger(__name__)

# Nintendo BLE manufacturer company IDs (string keys as serialized from backend)
_NINTENDO_COMPANY_IDS = frozenset({
    '894',   # 0x037E — Nintendo (classic controllers)
    '1363',  # 0x0553 — Nintendo (Switch 2 controllers)
})

# Known Nintendo GATT service UUIDs (SW2 protocol)
_NINTENDO_SERVICE_UUIDS = frozenset({
    '00c5af5d-1964-4e30-8f51-1956f96bd280',
    'ab7de9be-89fe-49ad-828f-118f09df7fd0',
})

# Known Nintendo OUI prefixes (uppercase, colon-separated)
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


def _is_likely_controller(dev: dict) -> bool:
    """Detect Nintendo controllers using BLE advertisement data.

    Priority:
    1. manufacturer_data contains a Nintendo company ID (definitive)
    2. service_uuids contain known Nintendo GATT services (definitive)
    3. Device name matches known controller patterns (strong)
    4. MAC address OUI prefix matches Nintendo (reasonable)
    """
    # 1. Manufacturer data — most reliable signal
    mfg = dev.get('manufacturer_data', {})
    if mfg and _NINTENDO_COMPANY_IDS & set(mfg.keys()):
        return True

    # 2. Service UUIDs
    svc = dev.get('service_uuids', [])
    if svc and _NINTENDO_SERVICE_UUIDS & set(s.lower() for s in svc):
        return True

    # 3. Name patterns
    name = (dev.get('name', '') or '').lower()
    if name and any(pat in name for pat in _CONTROLLER_NAME_PATTERNS):
        return True

    # 4. OUI prefix
    addr = dev.get('address', '').upper()
    if len(addr) >= 8 and addr[:8] in _NINTENDO_OUI:
        return True

    return False


class BLEControllerScanDialog:
    """Modal single-scan dialog for discovering Nintendo BLE controllers.

    Performs one scan, filters for likely controllers, and always shows
    a picker so the user can confirm which device to connect.

    Args:
        parent: The parent window (CTk or Tk).
        on_scan: Callback to trigger a BLE scan.  Accepts a completion
                 callback that receives list[dict] with address/name/rssi/
                 manufacturer_data/service_uuids keys.
        exclude_addresses: Set of uppercase MAC addresses already connected
                           (these are hidden from the results).
    """

    def __init__(self, parent,
                 on_scan: Callable[[Callable[[list[dict]], None]], None],
                 exclude_addresses: set[str] | None = None):
        self._result: Optional[str] = None
        self._on_scan = on_scan
        self._exclude = {a.upper() for a in (exclude_addresses or ())}
        self._all_devices: list[dict] = []

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

        self._show_scan_prompt()

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

    # ── Scan prompt ───────────────────────────────────────────────

    def _show_scan_prompt(self):
        self._clear_content()

        customtkinter.CTkLabel(
            self._content, text=t("scan.heading"),
            text_color=T.TEXT_PRIMARY,
            font=(T.FONT_FAMILY, 16, "bold"),
        ).pack(anchor=tk.W, pady=(0, 8))

        customtkinter.CTkLabel(
            self._content,
            text=t("scan.instructions"),
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
        self._progress.set(0)
        self._progress.pack_forget()

        self._status_label = customtkinter.CTkLabel(
            self._content, text="",
            text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 12),
        )
        self._status_label.pack(anchor=tk.W)

        self._scan_btn = customtkinter.CTkButton(
            self._btn_bar, text=t("btn.scan"),
            command=self._do_scan,
            fg_color=T.BTN_FG,
            hover_color=T.BTN_HOVER,
            text_color=T.BTN_TEXT,
            corner_radius=12, height=36, width=120,
            font=(T.FONT_FAMILY, 14),
        )
        self._scan_btn.pack(side=tk.RIGHT, padx=(8, 0))

        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.cancel"),
            command=self._on_cancel,
            fg_color=T.GC_PURPLE_SURFACE,
            hover_color=T.GC_PURPLE_LIGHT,
            text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=100,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

    def _do_scan(self):
        self._scan_btn.configure(state="disabled")
        self._progress.pack(pady=(0, 8))
        self._progress.configure(mode="indeterminate")
        self._progress.start()
        self._status_label.configure(text=t("scan.scanning"))

        self._on_scan(self._on_scan_complete)

    def _on_scan_complete(self, devices: list[dict]):
        self._progress.stop()
        self._progress.pack_forget()

        visible = [d for d in devices
                   if d['address'].upper() not in self._exclude]
        self._all_devices = visible

        controllers = [d for d in visible if _is_likely_controller(d)]
        others = [d for d in visible if not _is_likely_controller(d)]

        logger.debug("Scan complete: %d total, %d after exclude, "
                     "%d controller(s), %d other(s)",
                     len(devices), len(visible),
                     len(controllers), len(others))
        for d in controllers:
            logger.debug("  [controller] %s  name=%r  rssi=%s  mfg=%s",
                         d.get('address'), d.get('name'),
                         d.get('rssi'), d.get('manufacturer_data', {}))

        if controllers:
            self._show_picker(controllers, all_devices=visible)
        else:
            self._show_no_results()

    # ── No controllers found ──────────────────────────────────────

    def _show_no_results(self):
        self._clear_content()

        customtkinter.CTkLabel(
            self._content, text=t("scan.no_controllers"),
            text_color=T.TEXT_PRIMARY,
            font=(T.FONT_FAMILY, 16, "bold"),
        ).pack(anchor=tk.W, pady=(0, 8))

        customtkinter.CTkLabel(
            self._content,
            text=t("scan.no_controllers_detail"),
            text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 13),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 12))

        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.retry"),
            command=self._show_scan_prompt,
            fg_color=T.BTN_FG,
            hover_color=T.BTN_HOVER,
            text_color=T.BTN_TEXT,
            corner_radius=12, height=36, width=120,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT, padx=(8, 0))

        if self._all_devices:
            customtkinter.CTkButton(
                self._btn_bar, text=t("btn.show_all_devices"),
                command=lambda: self._show_picker(self._all_devices),
                fg_color=T.GC_PURPLE_SURFACE,
                hover_color=T.GC_PURPLE_LIGHT,
                text_color=T.TEXT_PRIMARY,
                corner_radius=12, height=36, width=120,
                font=(T.FONT_FAMILY, 14),
            ).pack(side=tk.RIGHT, padx=(8, 0))

        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.cancel"),
            command=self._on_cancel,
            fg_color=T.GC_PURPLE_SURFACE,
            hover_color=T.GC_PURPLE_LIGHT,
            text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=100,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

    # ── Controller picker ─────────────────────────────────────────

    def _show_picker(self, devices: list[dict],
                     all_devices: Optional[list[dict]] = None):
        self._clear_content()

        is_filtered = all_devices is not None and len(all_devices) > len(devices)
        n_ctrl = sum(1 for d in devices if _is_likely_controller(d))

        if is_filtered:
            title = t("scan.controllers_found")
            subtitle = t("scan.controllers_found_detail",
                         n_ctrl=n_ctrl, n_total=len(all_devices))
        else:
            title = t("scan.all_devices")
            subtitle = t("scan.all_devices_detail", n=len(devices))

        customtkinter.CTkLabel(
            self._content, text=title,
            text_color=T.TEXT_PRIMARY,
            font=(T.FONT_FAMILY, 16, "bold"),
        ).pack(anchor=tk.W, pady=(0, 8))

        customtkinter.CTkLabel(
            self._content,
            text=subtitle,
            text_color=T.TEXT_SECONDARY,
            font=(T.FONT_FAMILY, 13),
        ).pack(anchor=tk.W, pady=(0, 8))

        self._build_device_tree(devices)

        # Buttons
        self._connect_btn = customtkinter.CTkButton(
            self._btn_bar, text=t("btn.connect"),
            command=self._on_picker_connect,
            fg_color=T.BTN_FG,
            hover_color=T.BTN_HOVER,
            text_color=T.BTN_TEXT,
            corner_radius=12, height=36, width=120,
            font=(T.FONT_FAMILY, 14),
        )
        self._connect_btn.pack(side=tk.RIGHT, padx=(8, 0))

        if is_filtered:
            customtkinter.CTkButton(
                self._btn_bar, text=t("btn.show_all"),
                command=lambda: self._show_picker(all_devices),
                fg_color=T.GC_PURPLE_SURFACE,
                hover_color=T.GC_PURPLE_LIGHT,
                text_color=T.TEXT_PRIMARY,
                corner_radius=12, height=36, width=100,
                font=(T.FONT_FAMILY, 14),
            ).pack(side=tk.RIGHT, padx=(8, 0))

        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.retry"),
            command=self._show_scan_prompt,
            fg_color=T.GC_PURPLE_SURFACE,
            hover_color=T.GC_PURPLE_LIGHT,
            text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=100,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT, padx=(8, 0))

        customtkinter.CTkButton(
            self._btn_bar, text=t("btn.cancel"),
            command=self._on_cancel,
            fg_color=T.GC_PURPLE_SURFACE,
            hover_color=T.GC_PURPLE_LIGHT,
            text_color=T.TEXT_PRIMARY,
            corner_radius=12, height=36, width=100,
            font=(T.FONT_FAMILY, 14),
        ).pack(side=tk.RIGHT)

    def _build_device_tree(self, devices: list[dict]):
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
            self._content, columns=cols, show="headings",
            height=min(max(len(devices), 1), 8),
            style='WizBLE.Treeview')
        self._tree.heading("type", text=t("scan.col_type"))
        self._tree.heading("address", text=t("scan.col_address"))
        self._tree.heading("signal", text=t("scan.col_signal"))
        self._tree.column("type", width=180)
        self._tree.column("address", width=160)
        self._tree.column("signal", width=60, anchor=tk.CENTER)

        sorted_devices = sorted(devices, key=lambda d: (
            not _is_likely_controller(d),
            d.get('rssi', -999) * -1,
        ))
        first_iid = None
        for dev in sorted_devices:
            rssi = dev.get('rssi', -999)
            signal = f"{rssi} dBm" if rssi > -999 else "?"
            if _is_likely_controller(dev):
                label = dev.get('name') or t("scan.nintendo_controller")
            else:
                label = dev.get('name') or t("scan.unknown_device")
            iid = self._tree.insert("", tk.END, values=(
                label, dev['address'], signal))
            if first_iid is None:
                first_iid = iid

        if first_iid:
            self._tree.selection_set(first_iid)

        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<Double-1>", lambda _: self._on_picker_connect())

    def _on_picker_connect(self):
        sel = self._tree.selection()
        if sel:
            values = self._tree.item(sel[0], "values")
            self._result = values[1]  # address column
            logger.info("User selected controller: %s", self._result)
            self._dlg.destroy()

    # ── Common ────────────────────────────────────────────────────

    def _on_cancel(self):
        self._result = None
        self._dlg.destroy()

    def show(self) -> Optional[str]:
        """Show the dialog and block until closed. Returns address or None."""
        self._dlg.wait_window()
        return self._result
