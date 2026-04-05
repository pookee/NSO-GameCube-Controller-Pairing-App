"""
Calibration Manager

Owns all stick calibration tracking state, trigger calibration wizard state,
and the cached calibration used on the emulation hot path.
"""

import logging
import math
import threading

from .controller_constants import normalize
from .i18n import t

logger = logging.getLogger(__name__)

_TRIGGER_CAL_KEYS = (
    'trigger_left_base', 'trigger_left_bump', 'trigger_left_max',
    'trigger_right_base', 'trigger_right_bump', 'trigger_right_max',
)

_MIN_TRIGGER_DELTA = 15


class CalibrationManager:
    """Manages stick and trigger calibration state."""

    def __init__(self, calibration: dict):
        self._calibration = calibration
        self._cal_lock = threading.Lock()
        self._cached_calibration = calibration.copy()

        # Stick calibration state
        self.stick_calibrating = False
        self._stick_cal_min = {}
        self._stick_cal_max = {}
        self._stick_cal_octagon_points = {'left': [(0, 0)] * 8, 'right': [(0, 0)] * 8}
        self._stick_cal_octagon_dists = {'left': [0.0] * 8, 'right': [0.0] * 8}

        # Trigger calibration wizard state
        self.trigger_cal_step = 0
        self._trigger_cal_last_left = 0
        self._trigger_cal_last_right = 0
        self._trigger_cal_peak_left = 0
        self._trigger_cal_peak_right = 0
        self._trigger_cal_snapshot: dict | None = None
        self._trigger_cal_force_next = False

    def refresh_cache(self):
        """Update the cached calibration dict after external mutations."""
        self._cached_calibration = self._calibration.copy()

    # ── Stick calibration ────────────────────────────────────────────

    def track_stick_data(self, left_stick_x, left_stick_y, right_stick_x, right_stick_y):
        """Track min/max and octagon sectors during stick calibration.
        Called from the read thread while stick_calibrating is True."""
        with self._cal_lock:
            axes = {
                'left_x': left_stick_x, 'left_y': left_stick_y,
                'right_x': right_stick_x, 'right_y': right_stick_y,
            }
            for axis, val in axes.items():
                if self._stick_cal_min.get(axis) is None or val < self._stick_cal_min[axis]:
                    self._stick_cal_min[axis] = val
                if self._stick_cal_max.get(axis) is None or val > self._stick_cal_max[axis]:
                    self._stick_cal_max[axis] = val

            # Track octagon sectors per stick
            cal = self._calibration
            for side, raw_x, raw_y in [('left', left_stick_x, left_stick_y),
                                        ('right', right_stick_x, right_stick_y)]:
                cx = cal[f'stick_{side}_center_x']
                cy = cal[f'stick_{side}_center_y']
                dx = raw_x - cx
                dy = raw_y - cy
                dist = math.hypot(dx, dy)
                if dist > 0:
                    angle_deg = math.degrees(math.atan2(dy, dx)) % 360
                    sector = round(angle_deg / 45) % 8
                    if dist > self._stick_cal_octagon_dists[side][sector]:
                        self._stick_cal_octagon_dists[side][sector] = dist
                        self._stick_cal_octagon_points[side][sector] = (raw_x, raw_y)

    def start_stick_calibration(self):
        """Begin stick calibration — reset tracking and start recording."""
        with self._cal_lock:
            self._stick_cal_min = {'left_x': None, 'left_y': None, 'right_x': None, 'right_y': None}
            self._stick_cal_max = {'left_x': None, 'left_y': None, 'right_x': None, 'right_y': None}
            self._stick_cal_octagon_points = {'left': [(0, 0)] * 8, 'right': [(0, 0)] * 8}
            self._stick_cal_octagon_dists = {'left': [0.0] * 8, 'right': [0.0] * 8}
        self.stick_calibrating = True

    def finish_stick_calibration(self):
        """Finish stick calibration — compute center, range, and octagon data.
        Returns the updated calibration dict for the UI to redraw."""
        self.stick_calibrating = False

        axis_map = {
            'left_x': ('stick_left_center_x', 'stick_left_range_x'),
            'left_y': ('stick_left_center_y', 'stick_left_range_y'),
            'right_x': ('stick_right_center_x', 'stick_right_range_x'),
            'right_y': ('stick_right_center_y', 'stick_right_range_y'),
        }

        with self._cal_lock:
            cal_min = dict(self._stick_cal_min)
            cal_max = dict(self._stick_cal_max)
            octagon_points = {s: list(pts) for s, pts in self._stick_cal_octagon_points.items()}
            octagon_dists = {s: list(dists) for s, dists in self._stick_cal_octagon_dists.items()}

        for axis, (center_key, range_key) in axis_map.items():
            mn = cal_min.get(axis)
            mx = cal_max.get(axis)
            if mn is not None and mx is not None and mx > mn:
                self._calibration[center_key] = (mn + mx) / 2.0
                self._calibration[range_key] = (mx - mn) / 2.0

        # Compute normalized octagon points for each stick
        cal = self._calibration
        for side in ('left', 'right'):
            cx = cal[f'stick_{side}_center_x']
            rx = max(cal[f'stick_{side}_range_x'], 1)
            cy = cal[f'stick_{side}_center_y']
            ry = max(cal[f'stick_{side}_range_y'], 1)

            octagon = []
            for i in range(8):
                raw_x, raw_y = octagon_points[side][i]
                dist = octagon_dists[side][i]
                if dist > 0:
                    x_norm = normalize(raw_x, cx, rx)
                    y_norm = normalize(raw_y, cy, ry)
                else:
                    angle = math.radians(i * 45)
                    x_norm = math.cos(angle)
                    y_norm = math.sin(angle)
                octagon.append([x_norm, y_norm])

            cal[f'stick_{side}_octagon'] = octagon

        self._cached_calibration = self._calibration.copy()

    def get_live_octagon_data(self, side):
        """Return (octagon_dists, octagon_points, cx, rx, cy, ry) for live preview.
        Uses in-progress min/max to compute temporary center/range."""
        mn_x = self._stick_cal_min.get(f'{side}_x')
        mx_x = self._stick_cal_max.get(f'{side}_x')
        mn_y = self._stick_cal_min.get(f'{side}_y')
        mx_y = self._stick_cal_max.get(f'{side}_y')

        if mn_x is not None and mx_x is not None and mx_x > mn_x:
            cx = (mn_x + mx_x) / 2.0
            rx = (mx_x - mn_x) / 2.0
        else:
            cx = self._calibration[f'stick_{side}_center_x']
            rx = max(self._calibration[f'stick_{side}_range_x'], 1)

        if mn_y is not None and mx_y is not None and mx_y > mn_y:
            cy = (mn_y + mx_y) / 2.0
            ry = (mx_y - mn_y) / 2.0
        else:
            cy = self._calibration[f'stick_{side}_center_y']
            ry = max(self._calibration[f'stick_{side}_range_y'], 1)

        return self._stick_cal_octagon_dists[side], self._stick_cal_octagon_points[side], cx, rx, cy, ry

    # ── Trigger calibration ──────────────────────────────────────────

    @property
    def trigger_cal_last_left(self):
        with self._cal_lock:
            return self._trigger_cal_last_left

    @property
    def trigger_cal_last_right(self):
        with self._cal_lock:
            return self._trigger_cal_last_right

    @property
    def trigger_cal_peak_left(self):
        with self._cal_lock:
            return self._trigger_cal_peak_left

    @property
    def trigger_cal_peak_right(self):
        with self._cal_lock:
            return self._trigger_cal_peak_right

    def _reset_trigger_peaks(self):
        """Reset peak tracking to current instantaneous values."""
        self._trigger_cal_peak_left = self._trigger_cal_last_left
        self._trigger_cal_peak_right = self._trigger_cal_last_right

    def update_trigger_raw(self, left_trigger, right_trigger):
        """Store latest raw trigger values and update peaks for calibration."""
        with self._cal_lock:
            self._trigger_cal_last_left = left_trigger
            self._trigger_cal_last_right = right_trigger
            if self.trigger_cal_step > 0:
                if left_trigger > self._trigger_cal_peak_left:
                    self._trigger_cal_peak_left = left_trigger
                if right_trigger > self._trigger_cal_peak_right:
                    self._trigger_cal_peak_right = right_trigger

    def trigger_cal_cancel(self):
        """Cancel an in-progress trigger calibration, restoring prior values."""
        if self.trigger_cal_step == 0:
            return
        if self._trigger_cal_snapshot is not None:
            for key in _TRIGGER_CAL_KEYS:
                self._calibration[key] = self._trigger_cal_snapshot[key]
            self._cached_calibration = self._calibration.copy()
            logger.debug("Trigger calibration cancelled — restored snapshot")
        self._trigger_cal_snapshot = None
        self._trigger_cal_force_next = False
        self._trigger_cal_peak_left = 0
        self._trigger_cal_peak_right = 0
        self.trigger_cal_step = 0

    def _trigger_val_ok(self, val: float, ref: float, label: str) -> bool:
        """Check that a captured trigger value is far enough from the reference.
        Returns True if valid (or if the user forced past the warning)."""
        if self._trigger_cal_force_next:
            self._trigger_cal_force_next = False
            logger.warning("Trigger cal: forcing %s=%d despite being close to ref=%d", label, val, ref)
            return True
        if abs(val - ref) < _MIN_TRIGGER_DELTA:
            logger.warning("Trigger cal: %s=%d too close to ref=%d (delta=%d < %d)",
                           label, val, ref, abs(val - ref), _MIN_TRIGGER_DELTA)
            self._trigger_cal_force_next = True
            return False
        return True

    def trigger_cal_next_step(self):
        """Advance the trigger calibration wizard one step.

        Uses PEAK values (max seen since the step started) rather than
        instantaneous values so the user can press-and-release before
        clicking Continue.

        Returns (step, btn_text, status_text) for the UI to update.
        If a captured value seems wrong, the step is NOT advanced and
        the user gets a warning with the option to click again to force.
        """
        step = self.trigger_cal_step

        if step == 0:
            self._trigger_cal_snapshot = {k: self._calibration[k] for k in _TRIGGER_CAL_KEYS}
            self._trigger_cal_force_next = False
            self._reset_trigger_peaks()
            self.trigger_cal_step = 1
            return (1, t("btn.continue"), t("cal.trigger_release"))

        elif step == 1:
            left_val = self.trigger_cal_last_left
            right_val = self.trigger_cal_last_right
            logger.debug("Trigger cal step 1 (base): L=%d  R=%d", left_val, right_val)
            self._calibration['trigger_left_base'] = float(left_val)
            self._calibration['trigger_right_base'] = float(right_val)
            self._cached_calibration = self._calibration.copy()
            self._reset_trigger_peaks()
            self.trigger_cal_step = 2
            return (2, t("btn.continue"), t("cal.trigger_left_bump"))

        elif step == 2:
            val = self._trigger_cal_peak_left
            base = self._calibration['trigger_left_base']
            logger.debug("Trigger cal step 2 (L bump): peak=%d  base=%.0f", val, base)
            if not self._trigger_val_ok(val, base, "left_bump"):
                return (2, t("cal.btn_retry_or_force"),
                        t("cal.trigger_retry_left",
                          val=val, base=f"{base:.0f}"))
            self._calibration['trigger_left_bump'] = float(val)
            self._cached_calibration = self._calibration.copy()
            self._reset_trigger_peaks()
            self.trigger_cal_step = 3
            return (3, t("btn.continue"), t("cal.trigger_left_max"))

        elif step == 3:
            val = self._trigger_cal_peak_left
            base = self._calibration['trigger_left_base']
            logger.debug("Trigger cal step 3 (L max): peak=%d  base=%.0f", val, base)
            if not self._trigger_val_ok(val, base, "left_max"):
                return (3, t("cal.btn_retry_or_force"),
                        t("cal.trigger_retry_left",
                          val=val, base=f"{base:.0f}"))
            self._calibration['trigger_left_max'] = float(val)
            self._cached_calibration = self._calibration.copy()
            self._reset_trigger_peaks()
            self.trigger_cal_step = 4
            return (4, t("btn.continue"), t("cal.trigger_right_bump"))

        elif step == 4:
            val = self._trigger_cal_peak_right
            base = self._calibration['trigger_right_base']
            logger.debug("Trigger cal step 4 (R bump): peak=%d  base=%.0f", val, base)
            if not self._trigger_val_ok(val, base, "right_bump"):
                return (4, t("cal.btn_retry_or_force"),
                        t("cal.trigger_retry_right",
                          val=val, base=f"{base:.0f}"))
            self._calibration['trigger_right_bump'] = float(val)
            self._cached_calibration = self._calibration.copy()
            self._reset_trigger_peaks()
            self.trigger_cal_step = 5
            return (5, t("btn.continue"), t("cal.trigger_right_max"))

        elif step == 5:
            val = self._trigger_cal_peak_right
            base = self._calibration['trigger_right_base']
            logger.debug("Trigger cal step 5 (R max): peak=%d  base=%.0f", val, base)
            if not self._trigger_val_ok(val, base, "right_max"):
                return (5, t("cal.btn_retry_or_force"),
                        t("cal.trigger_retry_right",
                          val=val, base=f"{base:.0f}"))
            self._calibration['trigger_right_max'] = float(val)
            self._cached_calibration = self._calibration.copy()
            self._trigger_cal_snapshot = None
            self._trigger_cal_force_next = False
            self.trigger_cal_step = 0
            logger.info("Trigger calibration completed: base_l=%.0f bump_l=%.0f max_l=%.0f "
                        "base_r=%.0f bump_r=%.0f max_r=%.0f",
                        self._calibration['trigger_left_base'],
                        self._calibration['trigger_left_bump'],
                        self._calibration['trigger_left_max'],
                        self._calibration['trigger_right_base'],
                        self._calibration['trigger_right_bump'],
                        self._calibration['trigger_right_max'])
            return (0, t("ui.cal_triggers"), t("cal.trigger_completed"))

    # ── Hot-path trigger calibration ─────────────────────────────────

    def calibrate_trigger_fast(self, raw_value: int, side: str) -> int:
        """Fast trigger calibration using cached values (emulation hot path)."""
        base = self._cached_calibration[f'trigger_{side}_base']
        bump = self._cached_calibration[f'trigger_{side}_bump']
        max_val = self._cached_calibration[f'trigger_{side}_max']

        calibrated = raw_value - base
        if calibrated < 0:
            calibrated = 0

        if self._cached_calibration['trigger_bump_100_percent']:
            range_val = bump - base
        else:
            range_val = max_val - base

        if range_val <= 0:
            return 0

        result = int((calibrated / range_val) * 255)
        return max(0, min(255, result))
