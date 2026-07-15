"""
Emulator Controller-Config Helper

Generates and installs controller configuration files so the NSO GameCube
virtual Xbox 360 pad works out-of-the-box across emulators, without the user
hand-binding anything. This is what makes a setup portable: run it on a fresh
PC (or for another user) and the emulators pick up the GC controllers.

Design goals:
- Pure stdlib, NO Tkinter — testable and frozen-build safe (no new deps, no
  PyInstaller hidden imports, all config text is generated from string
  constants so nothing needs bundling).
- Non-destructive: never blind-overwrite. Every existing file that is modified
  is backed up to ``<name>.gcbak-<timestamp>`` first, and we only edit the
  specific keys we own (read-modify-write), leaving unrelated settings intact.
- Honest scope: RetroArch and Dolphin are where the real value is. PCSX2 and
  DuckStation auto-detect a standard Xbox 360 pad via SDL, so for those we only
  detect + report "no action needed" rather than touching their config.

The virtual pad the app creates is byte-identical to a Microsoft Xbox 360
controller (VID 0x045E / PID 0x028E), so every binding here uses the canonical
XInput numbering (validated against RetroArch's own bundled Xbox 360 profile).
"""

import os
import sys
import shutil
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform not in ("win32", "darwin")

# Virtual pad identity (ViGEmBus Xbox 360 emulation).
XBOX360_VENDOR_ID = "1118"   # 0x045E
XBOX360_PRODUCT_ID = "654"   # 0x028E

# ─────────────────────────────────────────────────────────────────────────
# Canonical GC->Xbox360 bindings, in each emulator's on-disk dialect.
# All derived from the single runtime mapping in controller_constants:
#   A/B/X/Y->A/B/X/Y, Z->RB, ZL->LB, L/R(analog)->LT/RT, Start->Start,
#   Home->L3, Capture->Back, Chat->R3, D-pad->D-pad, stick->left, C-stick->right
# ─────────────────────────────────────────────────────────────────────────

# RetroArch, xinput driver (Windows). Keys are the RetroPad bind suffixes; the
# writer prefixes them with input_playerN_ (retroarch.cfg) or leaves them bare
# (autoconfig .cfg). Numbers/hats/axes are the RetroArch xinput convention.
RA_XINPUT_BINDS: Dict[str, str] = {
    "up_btn": "h0up",
    "down_btn": "h0down",
    "left_btn": "h0left",
    "right_btn": "h0right",
    "a_btn": "1",          # RetroPad A (east)  = Xbox B
    "b_btn": "0",          # RetroPad B (south) = Xbox A
    "x_btn": "3",          # RetroPad X (north) = Xbox Y
    "y_btn": "2",          # RetroPad Y (west)  = Xbox X
    "select_btn": "7",     # Back  (GC Capture)
    "start_btn": "6",      # Start (GC Start)
    "l_btn": "4",          # LB    (GC ZL)
    "r_btn": "5",          # RB    (GC Z)
    "l2_axis": "+4",       # LT    (GC L analog)
    "r2_axis": "+5",       # RT    (GC R analog)
    "l3_btn": "8",         # L3    (GC Home)
    "r3_btn": "9",         # R3    (GC Chat)
    "l_x_plus_axis": "+0",
    "l_x_minus_axis": "-0",
    "l_y_plus_axis": "-1",
    "l_y_minus_axis": "+1",
    "r_x_plus_axis": "+2",
    "r_x_minus_axis": "-2",
    "r_y_plus_axis": "-3",
    "r_y_minus_axis": "+3",
}

# GC-flavoured on-screen labels (behaviour-neutral) for the autoconfig file.
RA_XINPUT_LABELS: Dict[str, str] = {
    "b_btn_label": "GC A", "a_btn_label": "GC B",
    "y_btn_label": "GC X", "x_btn_label": "GC Y",
    "l_btn_label": "GC ZL", "r_btn_label": "GC Z",
    "l2_axis_label": "GC L", "r2_axis_label": "GC R",
    "start_btn_label": "GC Start", "select_btn_label": "GC Capture",
    "l3_btn_label": "GC Home", "r3_btn_label": "GC Chat",
    "up_btn_label": "GC D-Pad Up", "down_btn_label": "GC D-Pad Down",
    "left_btn_label": "GC D-Pad Left", "right_btn_label": "GC D-Pad Right",
}

# Global RetroArch keys we set so the pad is actually usable.
RA_GLOBAL_KEYS: Dict[str, str] = {
    "input_autodetect_enable": "true",
    "input_menu_toggle_btn": "8",    # GC Home button opens the RetroArch menu
    "input_enable_hotkey_btn": "nul",
}

# Dolphin GCPad bindings (backend-independent emulated-control keys). RHS is an
# XInput control expression wrapped in backticks. Face-button expressions use
# the cross-backend-safe `Button A/B/X/Y` spelling.
DOLPHIN_GCPAD_BINDS = [
    ("Buttons/A", "Button A"),
    ("Buttons/B", "Button B"),
    ("Buttons/X", "Button X"),
    ("Buttons/Y", "Button Y"),
    ("Buttons/Z", "Shoulder R"),
    ("Buttons/Start", "Start"),
    ("Main Stick/Up", "Left Y+"),
    ("Main Stick/Down", "Left Y-"),
    ("Main Stick/Left", "Left X-"),
    ("Main Stick/Right", "Left X+"),
    ("C-Stick/Up", "Right Y+"),
    ("C-Stick/Down", "Right Y-"),
    ("C-Stick/Left", "Right X-"),
    ("C-Stick/Right", "Right X+"),
    ("Triggers/L", "Trigger L"),
    ("Triggers/R", "Trigger R"),
    ("Triggers/L-Analog", "Trigger L"),
    ("Triggers/R-Analog", "Trigger R"),
    ("D-Pad/Up", "Pad N"),
    ("D-Pad/Down", "Pad S"),
    ("D-Pad/Left", "Pad W"),
    ("D-Pad/Right", "Pad E"),
    ("Rumble/Motor", "Motor L` | `Motor R"),  # backticks added around each side below
]

MAX_PORTS = 4


# ─────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class GeneratedFile:
    """One file the helper would write. Nothing is written until apply()."""
    path: str
    contents: str
    action: str                     # 'create' or 'modify'
    summary: str = ""


@dataclass
class EmulatorTarget:
    """A detected emulator we can configure."""
    id: str                         # 'retroarch' | 'dolphin' | 'pcsx2' | 'duckstation'
    name: str
    config_dir: str
    detected_from: str              # 'launchbox' | 'system' | 'portable'
    actionable: bool = True         # False = detected but auto-detects on its own
    note: str = ""
    files: List[GeneratedFile] = field(default_factory=list)


@dataclass
class ApplyResult:
    written: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────

def _real_home() -> str:
    return os.path.expanduser("~")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _first_existing_dir(*candidates: str) -> Optional[str]:
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def find_launchbox(hint: Optional[str] = None) -> Optional[str]:
    """Locate the LaunchBox root (portable app; no registry entry)."""
    if hint and os.path.isdir(hint):
        return hint
    home = _real_home()
    return _first_existing_dir(
        os.path.join(home, "LaunchBox"),
        os.path.join(home, "Documents", "LaunchBox"),
        "C:\\LaunchBox",
        "D:\\LaunchBox",
        os.path.join("D:\\", "Games", "LaunchBox"),
    )


# ── RetroArch ──
def retroarch_config_dir(launchbox_root: Optional[str] = None) -> Optional[str]:
    """RetroArch config dir. Portable/LaunchBox instance wins over %APPDATA%."""
    candidates: List[str] = []
    if launchbox_root:
        candidates.append(os.path.join(launchbox_root, "Emulators", "RetroArch"))
    if IS_WIN:
        candidates.append(os.path.join(_env("APPDATA"), "RetroArch"))
    elif IS_MAC:
        candidates.append(os.path.join(
            _real_home(), "Library", "Application Support", "RetroArch"))
    else:
        base = _env("XDG_CONFIG_HOME", os.path.join(_real_home(), ".config"))
        candidates.append(os.path.join(base, "retroarch"))
    # Prefer a dir that actually contains a cfg or autoconfig folder.
    for c in candidates:
        if os.path.exists(os.path.join(c, "retroarch.cfg")) or \
           os.path.isdir(os.path.join(c, "autoconfig")):
            return c
    return _first_existing_dir(*candidates)


# ── Dolphin ──
def dolphin_config_dir() -> Optional[str]:
    """Dolphin CONFIG root (holds Profiles/, GCPadNew.ini, Dolphin.ini)."""
    if IS_WIN:
        docs = os.path.join(_env("USERPROFILE", _real_home()),
                            "Documents", "Dolphin Emulator", "Config")
        roaming = os.path.join(_env("APPDATA"), "Dolphin Emulator", "Config")
        return _first_existing_dir(docs, roaming) or roaming
    if IS_MAC:
        return os.path.join(_real_home(), "Library", "Application Support",
                            "Dolphin", "Config")
    base = _env("XDG_CONFIG_HOME", os.path.join(_real_home(), ".config"))
    legacy = os.path.join(_real_home(), ".dolphin-emu", "Config")
    return _first_existing_dir(os.path.join(base, "dolphin-emu"), legacy) \
        or os.path.join(base, "dolphin-emu")


# ── PCSX2 / DuckStation (detection only) ──
def pcsx2_data_root() -> Optional[str]:
    if IS_WIN:
        return _first_existing_dir(
            os.path.join(_env("USERPROFILE", _real_home()), "Documents", "PCSX2"))
    if IS_MAC:
        return _first_existing_dir(
            os.path.join(_real_home(), "Library", "Application Support", "PCSX2"))
    base = _env("XDG_CONFIG_HOME", os.path.join(_real_home(), ".config"))
    return _first_existing_dir(os.path.join(base, "PCSX2"))


def duckstation_user_dir() -> Optional[str]:
    if IS_WIN:
        return _first_existing_dir(
            os.path.join(_env("LOCALAPPDATA"), "DuckStation"),
            os.path.join(_env("USERPROFILE", _real_home()), "Documents", "DuckStation"))
    if IS_MAC:
        return _first_existing_dir(
            os.path.join(_real_home(), "Library", "Application Support", "DuckStation"))
    base = _env("XDG_DATA_HOME", os.path.join(_real_home(), ".local", "share"))
    return _first_existing_dir(os.path.join(base, "duckstation"))


# ─────────────────────────────────────────────────────────────────────────
# Config-text generation
# ─────────────────────────────────────────────────────────────────────────

def _cfg_upsert(text: str, keyvals: Dict[str, str]) -> str:
    """Replace-or-append ``key = "value"`` lines in a RetroArch-style cfg.

    Preserves every other line and existing newline style; only the keys in
    ``keyvals`` are touched. Missing keys are appended in a labelled block.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split("\n")
    # Normalise away trailing \r so matching is clean; re-join with `newline`.
    lines = [ln.rstrip("\r") for ln in lines]
    remaining = dict(keyvals)
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f'{key} = "{remaining.pop(key)}"'
    if remaining:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("# --- NSO GameCube pad (added by NSO GC app) ---")
        for key, val in remaining.items():
            lines.append(f'{key} = "{val}"')
    return newline.join(lines)


def _retroarch_cfg_keyvals() -> Dict[str, str]:
    """All retroarch.cfg keys we set: driver, globals, and P1-4 binds."""
    kv: Dict[str, str] = {}
    if IS_WIN:
        kv["input_joypad_driver"] = "xinput"
    kv.update(RA_GLOBAL_KEYS)
    for port in range(1, MAX_PORTS + 1):
        kv[f"input_player{port}_joypad_index"] = str(port - 1)
        for suffix, val in RA_XINPUT_BINDS.items():
            kv[f"input_player{port}_{suffix}"] = val
    return kv


def _retroarch_autoconfig_text() -> str:
    """A GC-labelled autoconfig profile for the xinput driver."""
    lines = [
        'input_driver = "xinput"',
        'input_device = "XInput Controller"',
        'input_device_display_name = "NSO GameCube Controller"',
        f'input_vendor_id = "{XBOX360_VENDOR_ID}"',
        f'input_product_id = "{XBOX360_PRODUCT_ID}"',
        "",
    ]
    for suffix, val in RA_XINPUT_BINDS.items():
        lines.append(f'input_{suffix} = "{val}"')
    lines.append("")
    for suffix, label in RA_XINPUT_LABELS.items():
        lines.append(f'input_{suffix} = "{label}"')
    return "\n".join(lines) + "\n"


def _dolphin_profile_text() -> str:
    """A Dolphin GCPad profile (appears in the Profile dropdown)."""
    lines = ["[Profile]", "Device = XInput/0/Gamepad"]
    for key, expr in DOLPHIN_GCPAD_BINDS:
        if key == "Rumble/Motor":
            lines.append("Rumble/Motor = `Motor L` | `Motor R`")
        else:
            lines.append(f"{key} = `{expr}`")
    return "\n".join(lines) + "\n"


def _dolphin_gcpadnew_text(existing: str) -> str:
    """Write/replace [GCPad1..4] sections in GCPadNew.ini, preserving the rest.

    Each port N is bound to XInput/(N-1)/Gamepad so all four work immediately
    without the user opening Dolphin's controller dialog.
    """
    import configparser
    # interpolation=None so literal '%' in existing values never breaks
    # read/write; optionxform=str preserves Dolphin's case-sensitive keys.
    def _new_parser():
        p = configparser.ConfigParser(interpolation=None)
        p.optionxform = str
        return p

    cp = _new_parser()
    if existing.strip():
        try:
            cp.read_string(existing)
        except configparser.Error:
            cp = _new_parser()
    for port in range(1, MAX_PORTS + 1):
        section = f"GCPad{port}"
        cp[section] = {}
        cp[section]["Device"] = f"XInput/{port - 1}/Gamepad"
        for key, expr in DOLPHIN_GCPAD_BINDS:
            if key == "Rumble/Motor":
                cp[section][key] = "`Motor L` | `Motor R`"
            else:
                cp[section][key] = f"`{expr}`"
    from io import StringIO
    buf = StringIO()
    cp.write(buf, space_around_delimiters=True)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────
# Per-emulator generation
# ─────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def generate_retroarch(config_dir: str) -> List[GeneratedFile]:
    files: List[GeneratedFile] = []
    cfg = os.path.join(config_dir, "retroarch.cfg")
    if os.path.isfile(cfg):
        patched = _cfg_upsert(_read(cfg), _retroarch_cfg_keyvals())
        files.append(GeneratedFile(
            path=cfg, contents=patched, action="modify",
            summary="Bind players 1-4 to the GC pad (xinput) + Home=menu"))
    # Autoconfig profile (GC labels; also helps auto-binding on fresh installs).
    ac = os.path.join(config_dir, "autoconfig", "xinput",
                      "NSO GameCube Controller.cfg")
    files.append(GeneratedFile(
        path=ac, contents=_retroarch_autoconfig_text(),
        action="modify" if os.path.isfile(ac) else "create",
        summary="Auto-config profile with GameCube button labels"))
    return files


def generate_dolphin(config_dir: str) -> List[GeneratedFile]:
    files: List[GeneratedFile] = []
    profile = os.path.join(config_dir, "Profiles", "GCPad", "NSO GameCube.ini")
    files.append(GeneratedFile(
        path=profile, contents=_dolphin_profile_text(),
        action="modify" if os.path.isfile(profile) else "create",
        summary="GCPad profile (analog triggers + C-stick, selectable in Dolphin)"))
    gcpadnew = os.path.join(config_dir, "GCPadNew.ini")
    files.append(GeneratedFile(
        path=gcpadnew, contents=_dolphin_gcpadnew_text(_read(gcpadnew)),
        action="modify" if os.path.isfile(gcpadnew) else "create",
        summary="Bind GC pad to all 4 ports immediately (no manual step)"))
    return files


# ─────────────────────────────────────────────────────────────────────────
# Detection + apply
# ─────────────────────────────────────────────────────────────────────────

def detect_emulators(launchbox_path: Optional[str] = None) -> List[EmulatorTarget]:
    """Find installed emulators we can configure, on this machine/OS."""
    lb = find_launchbox(launchbox_path)
    targets: List[EmulatorTarget] = []

    ra_dir = retroarch_config_dir(lb)
    if ra_dir:
        src = "launchbox" if (lb and lb in ra_dir) else "system"
        t = EmulatorTarget(id="retroarch", name="RetroArch", config_dir=ra_dir,
                           detected_from=src)
        t.files = generate_retroarch(ra_dir)
        targets.append(t)

    if not IS_MAC:  # no XInput/SDL Dolphin profile applies on macOS (pipe only)
        dol_dir = dolphin_config_dir()
        if dol_dir and os.path.isdir(dol_dir):
            t = EmulatorTarget(id="dolphin", name="Dolphin", config_dir=dol_dir,
                               detected_from="system")
            t.files = generate_dolphin(dol_dir)
            targets.append(t)

    px = pcsx2_data_root()
    if px:
        targets.append(EmulatorTarget(
            id="pcsx2", name="PCSX2", config_dir=px, detected_from="system",
            actionable=False,
            note="Auto-détecte le pad Xbox 360 via SDL — aucune config à écrire."))

    ds = duckstation_user_dir()
    if ds:
        targets.append(EmulatorTarget(
            id="duckstation", name="DuckStation", config_dir=ds,
            detected_from="system", actionable=False,
            note="Auto-détecte le pad Xbox 360 via SDL — aucune config à écrire."))

    return targets


_PROC_NAMES = {
    "retroarch": ["retroarch"],
    "dolphin": ["dolphin"],
    "pcsx2": ["pcsx2"],
    "duckstation": ["duckstation"],
}


def running_emulators(ids: List[str]) -> List[str]:
    """Best-effort: which of the given emulator ids are currently running."""
    if not IS_WIN:
        return []
    try:
        import subprocess
        out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
    except Exception:
        return []
    hit = []
    for eid in ids:
        if any(name in out for name in _PROC_NAMES.get(eid, [])):
            hit.append(eid)
    return hit


def apply_files(files: List[GeneratedFile], backup: bool = True) -> ApplyResult:
    """Write the generated files, backing up any existing file first."""
    result = ApplyResult()
    ts = time.strftime("%Y%m%d-%H%M%S")
    for gf in files:
        try:
            os.makedirs(os.path.dirname(gf.path), exist_ok=True)
            if backup and os.path.isfile(gf.path):
                bak = f"{gf.path}.gcbak-{ts}"
                shutil.copy2(gf.path, bak)
                result.backups.append(bak)
            with open(gf.path, "w", encoding="utf-8", newline="") as f:
                f.write(gf.contents)
            result.written.append(gf.path)
        except OSError as e:
            result.errors.append(f"{gf.path}: {e}")
    return result
