"""
Emulation Manager

Handles virtual controller creation, teardown, and the hot-path
update that maps GC input to the virtual gamepad.

Supports Xbox 360 mode and Dolphin named pipe mode.
"""

import errno
import logging
import threading
from typing import Optional, Dict

from .virtual_gamepad import VirtualGamepad, create_gamepad
from .controller_constants import BUTTON_MAPPING
from .calibration import CalibrationManager

logger = logging.getLogger(__name__)


class EmulationManager:
    """Manages controller emulation lifecycle and input forwarding."""

    def __init__(self, cal_mgr: CalibrationManager):
        self._cal_mgr = cal_mgr
        self.gamepad: Optional[VirtualGamepad] = None
        self.is_emulating = False
        self.mode: str = 'xbox360'
        # None = needs a full button sync on the next update()
        self._prev_buttons: Optional[Dict[str, bool]] = None
        self._last_axes: Optional[tuple] = None

    def start(self, mode: str = 'xbox360', slot_index: int = 0,
              cancel_event: threading.Event | None = None,
              rumble_callback=None) -> None:
        """Create the virtual gamepad and begin emulation. Raises on failure."""
        self.mode = mode
        self._prev_buttons = None
        self._last_axes = None
        logger.info("Starting emulation: mode=%s slot=%d", mode, slot_index)
        self.gamepad = create_gamepad(mode, slot_index=slot_index,
                                     cancel_event=cancel_event)
        if rumble_callback and mode in ('xbox360', 'dsu'):
            self.gamepad.set_rumble_callback(rumble_callback)
        self.is_emulating = True

    def stop(self) -> None:
        """Stop emulation and destroy the virtual gamepad."""
        logger.info("Stopping emulation (mode=%s)", self.mode)
        self.is_emulating = False
        self._prev_buttons = None
        self._last_axes = None
        if self.gamepad:
            try:
                self.gamepad.stop_rumble_listener()
            except Exception:
                pass
            try:
                self.gamepad.close()
            except Exception:
                pass
            self.gamepad = None

    def update(self, left_x, left_y, right_x, right_y,
               left_trigger, right_trigger, button_states: Dict[str, bool],
               buttons_changed: bool = True):
        """Update virtual Xbox 360 controller state (hot path).

        buttons_changed is a hint from the input processor: when False, the
        button bytes are identical to the previous report and the per-button
        delta loop can be skipped entirely.
        """
        if not self.gamepad:
            return

        try:
            left_x_scaled = int(max(-32767, min(32767, left_x * 32767)))
            left_y_scaled = int(max(-32767, min(32767, left_y * 32767)))
            right_x_scaled = int(max(-32767, min(32767, right_x * 32767)))
            right_y_scaled = int(max(-32767, min(32767, right_y * 32767)))

            # Only emit press/release on state changes (delta updates).
            # _prev_buttons is None right after start() — run a full sync
            # then so buttons held at connect time are registered.
            buttons_dirty = False
            if buttons_changed or self._prev_buttons is None:
                prev = self._prev_buttons if self._prev_buttons is not None else {}
                for button_name, xbox_button in BUTTON_MAPPING.items():
                    pressed = button_states.get(button_name, False)
                    if pressed != prev.get(button_name, False):
                        if pressed:
                            self.gamepad.press_button(xbox_button)
                        else:
                            self.gamepad.release_button(xbox_button)
                        prev[button_name] = pressed
                        buttons_dirty = True
                self._prev_buttons = prev

            # Shoulder buttons force full trigger pull
            if button_states.get('L', False):
                left_trigger_out = 255
            else:
                left_trigger_out = self._cal_mgr.calibrate_trigger_fast(left_trigger, 'left')
            if button_states.get('R', False):
                right_trigger_out = 255
            else:
                right_trigger_out = self._cal_mgr.calibrate_trigger_fast(right_trigger, 'right')

            axes = (left_x_scaled, left_y_scaled, right_x_scaled, right_y_scaled,
                    left_trigger_out, right_trigger_out)

            # Xbox 360 mode (ViGEmBus/uhid/evdev) is stateful: skipping an
            # update with a byte-identical state is free.  DSU and Dolphin
            # pipe clients expect a continuous stream — never skip those.
            if (self.mode == 'xbox360' and not buttons_dirty
                    and axes == self._last_axes):
                return
            self._last_axes = axes

            self.gamepad.left_joystick(x_value=left_x_scaled, y_value=left_y_scaled)
            self.gamepad.right_joystick(x_value=right_x_scaled, y_value=right_y_scaled)
            self.gamepad.left_trigger(left_trigger_out)
            self.gamepad.right_trigger(right_trigger_out)

            self.gamepad.update()

        except Exception as e:
            print(f"Virtual controller update error: {e}")
