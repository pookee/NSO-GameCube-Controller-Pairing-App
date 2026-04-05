# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NSO GameCube Controller Pairing App — a cross-platform Python/Tkinter application that makes Nintendo Switch Online GameCube controllers work as Xbox 360 controllers or Dolphin emulator input via USB or Bluetooth. Supports up to 4 simultaneous controllers with independent calibration.

## Build & Run Commands

```bash
# Install in development mode
pip install -e .

# Run the application
python -m gc_controller

# Run minimized to system tray (used by autostart)
python -m gc_controller --minimized

# Run with latency profiling (prints per-slot stats to stderr)
python -m gc_controller --latency

# Run headless (no GUI)
python -m gc_controller --headless [--mode dolphin_pipe|dsu]

# Standalone latency benchmark (USB or BLE)
python latency_benchmark.py [--ble] [--duration 15] [--csv report.csv]

# Build native USB enabler (Linux/macOS, requires libusb-1.0-dev)
cd native && make && sudo make install

# Build platform executables (PyInstaller)
python build_all.py
# Or platform-specific:
platform/linux/build.sh
platform/macos/build.sh
platform/windows/build.bat
```

There is no test suite in this project.

## Architecture

The app follows a **per-slot architecture** — up to 4 independent controller slots, each with its own managers:

```
GUI (customtkinter) → App Orchestrator (app.py)
  → ControllerSlot (controller_slot.py) — per-slot state container
    ├── ConnectionManager  — USB HID init via hidapi/pyusb
    ├── InputProcessor     — dedicated HID read thread per slot
    ├── EmulationManager   — virtual gamepad lifecycle
    └── CalibrationManager — octagon stick + trigger calibration
```

### Key modules in `src/gc_controller/`

- **app.py** — Main orchestrator, multi-slot management, settings persistence, BLE subprocess coordination
- **controller_slot.py** — Encapsulates all managers for one controller slot
- **connection_manager.py** — USB enumeration/init (pyusb), HID open/close (hidapi), path-based device claiming
- **input_processor.py** — Per-slot HID read thread, button/stick remapping, handles USB and BLE input formats, optional latency profiling via `set_latency_profiling()`
- **emulation_manager.py** — Creates platform-specific virtual gamepads, hot-path input forwarding
- **virtual_gamepad.py** — Abstract base + platform implementations (Windows: vgamepad/ViGEmBus, Linux: uhid preferred / evdev+uinput fallback, Dolphin: named FIFO pipes)
- **autostart.py** — Cross-platform auto-start at login (Windows: Task Scheduler, Linux: XDG autostart, macOS: LaunchAgent)
- **dsu_server.py** — DSU/Cemuhook UDP server + `DSUGamepad` implementation for emulator compatibility (Dolphin, Cemu, Yuzu, Ryujinx)
- **calibration.py** — 8-sector octagon stick calibration, 3-point trigger calibration, thread-safe with locks
- **settings_manager.py** — JSON persistence with v1→v2→v3 migration, global-only settings storage
- **controller_constants.py** — Shared button/stick constants and mappings

### BLE subsystem (`src/gc_controller/ble/`)

- **sw2_protocol.py** — Switch 2 BLE protocol (pairing, initialization)
- **bumble_backend.py** — Linux: direct HCI transport via Bumble (requires elevated privileges via pkexec)
- **bleak_backend.py** — macOS/Windows: userspace BLE via Bleak
- **ble_subprocess.py / bleak_subprocess.py** — Privileged subprocess runners
- **ble_event_loop.py** — Singleton asyncio daemon thread shared across BLE operations
- BLE requires MTU ≥185 bytes; input reports are 63 bytes on GATT characteristic 0x000E

### UI modules

- **controller_ui.py** — Per-slot controller cards with calibration/connection UI
- **ui_controller_canvas.py** — Stick/trigger visualization canvas
- **ui_ble_dialog.py** — BLE device picker dialog
- **ui_ble_scan_wizard.py** — Two-step differential BLE scan wizard (baseline scan then pairing scan to identify new controllers)
- **ui_settings_dialog.py** — Settings dialog
- **ui_theme.py** — CustomTkinter theme configuration

### Native USB enabler (`native/`)

- **gc-enabler.c** — Minimal C program that sends the two USB bulk transfers needed to activate HID input reports on the NSO GC adapter, then exits. After init, the controller is a standard HID gamepad recognized by SDL 3.4+ without any resident process.
- **Makefile / CMakeLists.txt** — Build system for gc-enabler (requires libusb-1.0).
- **gamecontrollerdb_nso_gc.txt** — SDL GameControllerDB mappings for the NSO GC (VID 057e PID 2073) on all platforms.
- **hid-nso-gc/** — Linux kernel HID driver (DKMS) that handles init + 4-port multiplexing natively. Includes `dkms.conf` and `install-dkms.sh`.

### Platform integration (`platform/`)

- **linux/99-gc-controller.rules** — udev rule: sets permissions and auto-runs `gc-enabler` on USB plug-in.
- **linux/gc-controller.service** — systemd user service for headless mode.
- **linux/install.sh** — User-local installer (binary, icon, desktop entry, systemd service).
- **macos/com.nso.gc-controller.plist** — LaunchAgent template for auto-start at login.

## Platform-Specific Notes

| Platform | Xbox 360 Emulation | Dolphin Pipe | DSU (Cemuhook) | BLE Backend | Notes |
|----------|-------------------|--------------|----------------|-------------|-------|
| Windows  | vgamepad (ViGEmBus) | N/A | UDP server | Bleak | USB rumble needs WinUSB driver (Zadig) |
| Linux    | uhid (preferred) or evdev/uinput | Named FIFO | UDP server | Bumble (HCI) | BLE needs elevated privileges; BlueZ stopped while Bumble active |
| macOS    | Not supported | Named FIFO | UDP server | Bleak | Use Dolphin pipe or DSU mode |

## Important Patterns

- **Latency optimizations**: Blocking HID reads (no sleep), BLE queue draining, delta-only button updates, lock-free calibration hot path, pre-allocated buffers (BLE protocol + DSU packets), binary IPC for BLE subprocess data, platform-specific BLE connection interval tuning
- **Thread safety**: Calibration modifications use locks; UI updates go through `root.after()` to stay on the Tkinter main thread
- **Device claiming**: Path-based to prevent two slots from connecting to the same physical controller
- **Report formats**: Standard GC USB binary format vs Windows NSO (report ID 0x05, different button encoding handled via `_translate_report_0x05()`) vs BLE (63-byte native Switch format)
- **Platform detection**: Uses `sys.platform` throughout (`win32`, `linux`, `darwin`)
- **BLE state**: Lazy initialization on first pair; subprocess messaging via events/queues
- **PyInstaller builds**: vgamepad DLL paths need special handling in frozen builds via `sys._MEIPASS`
- **Entry points**: `--ble-subprocess` and `--bleak-subprocess` flags in `__main__.py` dispatch to BLE subprocess runners instead of the main app; `--latency` enables per-slot profiling output to stderr; `--minimized` starts in system tray (used by autostart)
- **System tray**: Uses `pystray` with platform-specific backends (AppIndicator on Linux, native on macOS/Windows). Optional — gracefully disabled if unavailable.
- **Autostart**: `autostart.py` manages run-at-login registration per platform (Windows Task Scheduler, Linux XDG desktop entry, macOS LaunchAgent plist). Controlled via the "Run at startup" checkbox in Settings.
- **Native USB init**: For SDL 3.4+ games, the controller only needs two USB bulk transfers to become a standard HID gamepad. The `gc-enabler` binary (or udev `RUN+=`) handles this without any Python process. The full app is only needed for BLE, calibration, or non-SDL games.

## Frozen Build Checklist

**When adding new features or dependencies, always verify they work in PyInstaller frozen builds on all three platforms.** Specifically:

1. **New imports**: If a library uses dynamic/conditional imports (like `pystray`'s backend selection), PyInstaller can't trace them automatically. Add explicit hidden imports to the platform-specific sections in `gc_controller_enabler.spec`.
2. **New data/asset files**: Any runtime-loaded files (images, fonts, configs) must be added to the `datas` list in the spec file AND loaded via `getattr(sys, '_MEIPASS', os.path.dirname(__file__))` in code.
3. **New pip dependencies**: Add to `pyproject.toml` with platform markers where appropriate (e.g., `; sys_platform == 'win32'`).
4. **System-only packages** (not pip-installable, e.g., `gi`/PyGObject on Linux): Use `collect_all()` wrapped in try/except in the spec file, and ensure the feature degrades gracefully at runtime if the package is missing.
5. **New C extensions or DLLs**: Add as `binaries` (not `datas`) in the spec file so PyInstaller resolves transitive dependencies.
6. **Optional features**: Always wrap imports in try/except with a `_FEATURE_AVAILABLE` flag so the app runs even if bundling is incomplete.

## Dependencies

Core: `hidapi`, `pyusb`, `customtkinter`, `Pillow`
Tray: `pystray` (all platforms), `pyobjc-framework-Cocoa` (macOS), `python3-gi` + `gir1.2-appindicator3-0.1` (Linux, system packages)
Platform: `vgamepad` (Windows), `evdev` + `bumble` (Linux), `bleak` (macOS/Windows)
Build: `pyinstaller`

## License

GPLv3
