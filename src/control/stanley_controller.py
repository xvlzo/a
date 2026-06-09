"""
Stanley lateral controller + PID longitudinal controller.

Stanley controller reference:
  Thrun et al. (2006), "Stanley: The Robot That Won the DARPA Grand Challenge"

The controller converts a target (s, d) from the Frenet planner into steering,
throttle, and brake commands at every control step.
"""
import math
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

from config import cfg
from src.track.spline_parser import TrackSpline, project_to_spline_fast
from src.utils.ring_buffer import RingBuffer


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclass
class ControlOutput:
    steer: float = 0.0      # normalised [-1, 1]
    throttle: float = 0.0   # [0, 1]
    brake: float = 0.0      # [0, 1]
    gear_up: bool = False
    gear_dn: bool = False
    target_speed_ms: float = 0.0
    cross_track_err: float = 0.0
    heading_err: float = 0.0


# ---------------------------------------------------------------------------
# PID
# ---------------------------------------------------------------------------

class PID:
    def __init__(self, kp: float, ki: float, kd: float, clamp: float = 30.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.clamp = clamp
        self._integral: float = 0.0
        self._prev_error: Optional[float] = None
        self._prev_t: Optional[float] = None

    def update(self, error: float, t: Optional[float] = None) -> float:
        now = t or time.monotonic()
        dt = (now - self._prev_t) if self._prev_t else 0.01
        dt = max(0.001, min(dt, 0.2))

        self._integral += error * dt
        self._integral = max(-self.clamp, min(self.clamp, self._integral))

        derivative = 0.0
        if self._prev_error is not None:
            derivative = (error - self._prev_error) / dt

        self._prev_error = error
        self._prev_t = now

        return self.kp * error + self.ki * self._integral + self.kd * derivative

    def reset(self):
        self._integral = 0.0
        self._prev_error = None
        self._prev_t = None


# ---------------------------------------------------------------------------
# StanleyController
# ---------------------------------------------------------------------------

class StanleyController:
    """
    Full lateral + longitudinal controller.

    Inputs (call set_target() once per planner update):
      - target_d:        lateral offset to track (Frenet d, metres)
      - target_speed_ms: longitudinal target speed (m/s)

    Then call update() at 100+ Hz with current ego state.
    """

    def __init__(self, spline: TrackSpline):
        self.spline = spline
        self.kdtree = None
        self._build_kdtree()

        ccfg = cfg.control
        scfg = cfg.speed

        self._ke  = ccfg.stanley_ke
        self._ks  = ccfg.stanley_ks
        self._kh  = ccfg.stanley_kh
        self._max_steer_rad = math.radians(ccfg.max_steer_deg)

        self._speed_pid = PID(
            ccfg.speed_kp, ccfg.speed_ki, ccfg.speed_kd,
            ccfg.speed_integral_clamp
        )
        self._brake_thr = ccfg.brake_threshold_mss

        # Smoothing state
        self._steer_smooth   = 0.0
        self._throttle_smooth = 0.0
        self._brake_smooth   = 0.0
        self._steer_a   = ccfg.steer_smooth_alpha
        self._throttle_a = ccfg.throttle_smooth_alpha
        self._brake_a   = ccfg.brake_smooth_alpha

        self._target_d: float = 0.0
        self._target_v: float = scfg.target_kph / 3.6
        self._hint_idx: int = 0

        # Human-noise RNG for anti-detection (Gaussian, very small)
        import random
        self._rng = random.Random()

    def _build_kdtree(self):
        try:
            from scipy.spatial import cKDTree
            self.kdtree = cKDTree(self.spline.xz)
        except ImportError:
            pass

    def set_target(self, target_d: float, target_speed_ms: float):
        self._target_d = target_d
        self._target_v = target_speed_ms

    def update(
        self,
        pos_x: float,
        pos_z: float,
        heading: float,
        speed_ms: float,
        dt: float = 0.01,
    ) -> ControlOutput:
        """
        Compute control output for current ego state.
        Returns ControlOutput with steer/throttle/brake.
        """
        # Project ego onto spline at target_d offset
        s, cte_raw, road_heading, idx = project_to_spline_fast(
            pos_x, pos_z, self.spline, self.kdtree, self._hint_idx
        )
        self._hint_idx = idx

        # Cross-track error: signed distance from desired lateral offset
        # Positive cte_raw = left of centre
        cte = cte_raw - self._target_d

        # Heading error
        heading_err = _wrap_angle(heading - road_heading)

        # Stanley formula:
        # δ = ψ_e + arctan(k * e / (v + k_s))
        # ψ_e = heading_error, e = cross-track error
        stanley_angle = math.atan2(self._ke * cte, speed_ms + self._ks)
        raw_steer_rad = self._kh * heading_err + stanley_angle
        raw_steer_rad = max(-self._max_steer_rad, min(self._max_steer_rad, raw_steer_rad))

        # Normalise to [-1, 1] and add tiny human-like noise
        raw_steer_norm = raw_steer_rad / self._max_steer_rad
        noise = self._rng.gauss(0.0, 0.015)   # σ=1.5% → looks human
        raw_steer_norm = max(-1.0, min(1.0, raw_steer_norm + noise))

        # Longitudinal: PID on speed error
        speed_err = self._target_v - speed_ms
        accel_demand = self._speed_pid.update(speed_err)

        if accel_demand >= 0:
            throttle = min(1.0, accel_demand)
            brake = 0.0
        else:
            throttle = 0.0
            brake = min(1.0, -accel_demand / 3.0)   # scale: full demand = 3 m/s²

        # IIR smoothing
        a_s = self._steer_a
        a_t = self._throttle_a
        a_b = self._brake_a
        self._steer_smooth    = a_s * self._steer_smooth    + (1 - a_s) * raw_steer_norm
        self._throttle_smooth = a_t * self._throttle_smooth + (1 - a_t) * throttle
        self._brake_smooth    = a_b * self._brake_smooth    + (1 - a_b) * brake

        out = ControlOutput()
        out.steer = self._steer_smooth
        out.throttle = self._throttle_smooth
        out.brake = self._brake_smooth
        out.target_speed_ms = self._target_v
        out.cross_track_err = cte
        out.heading_err = heading_err
        return out

    def reset(self):
        self._speed_pid.reset()
        self._steer_smooth = 0.0
        self._throttle_smooth = 0.0
        self._brake_smooth = 0.0


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a
