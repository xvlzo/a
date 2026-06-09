"""
Frenet-frame trajectory planner.

Based on Werling et al. (2010) "Optimal Trajectory Generation for Dynamic
Street Scenarios in a Frenet Frame."

Generates a grid of candidate trajectories (quintic lateral × quartic
longitudinal), filters by hard collision constraints, then scores the
remainder for progress, speed, and close-pass reward.
"""
import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from config import cfg
from src.track.spline_parser import TrackSpline, project_to_spline_fast
from src.utils.geometry import frenet_to_world


# ---------------------------------------------------------------------------
# Polynomial helpers
# ---------------------------------------------------------------------------

class QuinticPolynomial:
    """
    5th-degree polynomial d(t) with exact boundary conditions.
    Minimises jerk integral for lateral trajectories.
    """

    def __init__(self, d0: float, dd0: float, ddd0: float,
                 dT: float, ddT: float, dddT: float, T: float):
        self.a0 = d0
        self.a1 = dd0
        self.a2 = ddd0 / 2.0

        T2, T3, T4, T5 = T**2, T**3, T**4, T**5
        A = np.array([
            [T3,     T4,      T5],
            [3*T2,   4*T3,    5*T4],
            [6*T,    12*T2,   20*T3],
        ])
        c = np.array([
            dT  - self.a0 - self.a1*T - self.a2*T2,
            ddT - self.a1 - 2*self.a2*T,
            dddT - 2*self.a2,
        ])
        try:
            coeffs = np.linalg.solve(A, c)
        except np.linalg.LinAlgError:
            coeffs = np.zeros(3)
        self.a3, self.a4, self.a5 = coeffs

    def d(self, t: float) -> float:
        return (self.a0 + self.a1*t + self.a2*t**2
                + self.a3*t**3 + self.a4*t**4 + self.a5*t**5)

    def dd(self, t: float) -> float:
        return (self.a1 + 2*self.a2*t + 3*self.a3*t**2
                + 4*self.a4*t**3 + 5*self.a5*t**4)

    def ddd(self, t: float) -> float:
        return (2*self.a2 + 6*self.a3*t + 12*self.a4*t**2 + 20*self.a5*t**3)

    def dddd(self, t: float) -> float:
        return 6*self.a3 + 24*self.a4*t + 60*self.a5*t**2

    def jerk_integral(self, T: float, n: int = 20) -> float:
        """Numerical jerk integral ∫₀ᵀ (d⃛)² dt."""
        dt = T / n
        total = 0.0
        for i in range(n):
            t = (i + 0.5) * dt
            j = self.ddd(t)
            total += j * j * dt
        return total


class QuarticPolynomial:
    """
    4th-degree polynomial s(t) for longitudinal trajectory.
    Fixes initial state and terminal velocity, leaves terminal position free.
    """

    def __init__(self, s0: float, ds0: float, dds0: float,
                 dsT: float, ddsT: float, T: float):
        self.b0 = s0
        self.b1 = ds0
        self.b2 = dds0 / 2.0

        T2, T3 = T**2, T**3
        A = np.array([
            [3*T2, 4*T3],
            [6*T,  12*T2],
        ])
        c = np.array([
            dsT  - self.b1 - 2*self.b2*T,
            ddsT - 2*self.b2,
        ])
        try:
            coeffs = np.linalg.solve(A, c)
        except np.linalg.LinAlgError:
            coeffs = np.zeros(2)
        self.b3, self.b4 = coeffs

    def s(self, t: float) -> float:
        return self.b0 + self.b1*t + self.b2*t**2 + self.b3*t**3 + self.b4*t**4

    def ds(self, t: float) -> float:
        return self.b1 + 2*self.b2*t + 3*self.b3*t**2 + 4*self.b4*t**3

    def dds(self, t: float) -> float:
        return 2*self.b2 + 6*self.b3*t + 12*self.b4*t**2

    def jerk_integral(self, T: float, n: int = 20) -> float:
        dt = T / n
        total = 0.0
        for i in range(n):
            t = (i + 0.5) * dt
            j = 6*self.b3 + 24*self.b4*t
            total += j * j * dt
        return total


# ---------------------------------------------------------------------------
# Traffic prediction
# ---------------------------------------------------------------------------

@dataclass
class TrafficPrediction:
    """Pre-computed Frenet trajectory for one traffic car over a time horizon."""
    s0: float
    d0: float
    v_s: float       # longitudinal speed (m/s)
    v_d: float       # lateral speed (m/s, small for lane-keeping)
    car_width_m: float = 1.8
    car_length_m: float = 4.5

    # Precomputed time steps
    times: np.ndarray = field(default_factory=lambda: np.empty(0))
    s_pred: np.ndarray = field(default_factory=lambda: np.empty(0))
    d_pred: np.ndarray = field(default_factory=lambda: np.empty(0))

    def build(self, dt: float, n_steps: int):
        t_arr = np.arange(1, n_steps + 1) * dt
        self.times = t_arr
        self.s_pred = self.s0 + self.v_s * t_arr
        # Lane-keep: d relaxes toward lane centre slowly
        # τ = 2s time constant — conservative model
        tau = 2.0
        self.d_pred = self.d0 * np.exp(-t_arr / tau)


# ---------------------------------------------------------------------------
# Trajectory candidate
# ---------------------------------------------------------------------------

@dataclass
class Trajectory:
    # Polynomial definitions
    lat_poly: Optional[QuinticPolynomial] = None
    lon_poly: Optional[QuarticPolynomial] = None

    # Sampled arrays (length = n_steps)
    times: np.ndarray = field(default_factory=lambda: np.empty(0))
    s: np.ndarray = field(default_factory=lambda: np.empty(0))
    d: np.ndarray = field(default_factory=lambda: np.empty(0))
    ds: np.ndarray = field(default_factory=lambda: np.empty(0))
    dd: np.ndarray = field(default_factory=lambda: np.empty(0))

    # World coords
    wx: np.ndarray = field(default_factory=lambda: np.empty(0))
    wz: np.ndarray = field(default_factory=lambda: np.empty(0))

    T_horizon: float = 0.0
    score: float = -1e9
    feasible: bool = False

    # Close-pass counting
    close_3x_count: int = 0
    close_1x_count: int = 0


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class FrenetPlanner:
    """
    Main planner. Call plan() at ~60 Hz.

    Returns the highest-scoring feasible Trajectory, which defines the
    target lateral offset and speed profile for the next control steps.
    """

    # Ego car half-dimensions
    EGO_HALF_W = 0.95   # metres
    EGO_HALF_L = 2.3    # metres

    def __init__(self, spline: TrackSpline):
        self.spline = spline
        self.kdtree = None
        self._build_kdtree()

        self._pcfg = cfg.planner
        self._scfg = cfg.scoring
        self._spcfg = cfg.speed

        # State carried between frames
        self._hint_idx: int = 0
        self._ego_s: float = 0.0
        self._ego_d: float = 0.0
        self._ego_ds: float = 40.0   # m/s
        self._ego_dd: float = 0.0
        self._ego_dds: float = 0.0
        self._ego_ddd: float = 0.0

        self._last_trajectory: Optional[Trajectory] = None

    def _build_kdtree(self):
        try:
            from scipy.spatial import cKDTree
            self.kdtree = cKDTree(self.spline.xz)
        except ImportError:
            self.kdtree = None

    def update_ego(self, pos_x: float, pos_z: float,
                   speed_ms: float, heading: float):
        """Update ego Frenet state from world position. Call before plan()."""
        s, d, road_heading, idx = project_to_spline_fast(
            pos_x, pos_z, self.spline, self.kdtree, self._hint_idx
        )
        self._hint_idx = idx
        self._ego_s = s
        self._ego_d = d

        heading_err = _wrap_angle(heading - road_heading)
        self._ego_ds = speed_ms * math.cos(heading_err)
        self._ego_dd = speed_ms * math.sin(heading_err)

    def plan(
        self,
        traffic: List[TrafficPrediction],
        target_speed_ms: Optional[float] = None,
    ) -> Optional[Trajectory]:
        """
        Generate and score candidate trajectories. Returns best feasible one.
        """
        if target_speed_ms is None:
            target_speed_ms = self._spcfg.target_kph / 3.6

        t_start = time.monotonic()

        N_steps = 30
        DT = 0.1    # 3 second horizon at 30 steps

        # Longitudinal speed candidates (m/s)
        v_targets = np.arange(
            max(self._spcfg.min_kph / 3.6, target_speed_ms - 20.0),
            target_speed_ms + 10.0,
            5.0
        )

        # Lateral offset candidates — prefer gaps between traffic, plus centre-line
        d_targets = self._compute_d_candidates(traffic)

        # Time-horizon variants
        T_variants = [2.5, 3.0, 3.5, 4.0]

        best: Optional[Trajectory] = None
        best_score = -1e9

        for T in T_variants:
            n_steps = int(T / DT)
            t_arr = np.linspace(DT, T, n_steps)

            # Pre-compute traffic positions for this horizon
            for tp in traffic:
                tp.build(DT, n_steps)

            for dT in d_targets:
                # Clamp to track boundaries at ego position
                sl, sr = self.spline.get_track_width(self._hint_idx)
                dT = max(-sl + self.EGO_HALF_W, min(sr - self.EGO_HALF_W, dT))

                lat = QuinticPolynomial(
                    self._ego_d, self._ego_dd, self._ego_ddd,
                    dT, 0.0, 0.0, T
                )

                for v_t in v_targets:
                    lon = QuarticPolynomial(
                        self._ego_s, self._ego_ds, self._ego_dds,
                        v_t, 0.0, T
                    )

                    traj = self._sample_trajectory(lat, lon, t_arr, T)

                    # Hard feasibility check
                    if not self._is_feasible(traj, traffic):
                        continue

                    traj.feasible = True
                    traj.score = self._score(traj, traffic, target_speed_ms)

                    if traj.score > best_score:
                        best_score = traj.score
                        best = traj

        elapsed = (time.monotonic() - t_start) * 1000
        if elapsed > 12.0:
            print(f"[planner] WARNING: plan() took {elapsed:.1f}ms")

        if best is None:
            # Fallback: emergency deceleration, hold current d
            best = self._emergency_trajectory(DT, N_steps)

        self._last_trajectory = best
        return best

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_d_candidates(self, traffic: List[TrafficPrediction]) -> List[float]:
        """
        Build a list of lateral offsets to try, biased toward gaps between
        traffic and scored to hit the 3× zone where possible.
        """
        pcfg = self._pcfg

        # Base candidates: uniform grid
        d_vals = list(np.arange(
            -pcfg.max_lateral_offset,
            pcfg.max_lateral_offset + 1e-9,
            pcfg.candidate_d_step,
        ))

        # Add offset specifically targeting the 3× scoring band next to each traffic car
        for tp in traffic:
            # Only look ahead
            if tp.s0 > self._ego_s and tp.s0 < self._ego_s + pcfg.gap_scan_ahead_m:
                target_gap = self._scfg.target_pass_dist_m + tp.car_width_m / 2 + self.EGO_HALF_W
                d_vals.append(tp.d0 + target_gap)
                d_vals.append(tp.d0 - target_gap)

        # Deduplicate and sort
        d_vals = sorted(set(round(d, 2) for d in d_vals))
        return d_vals

    def _sample_trajectory(
        self,
        lat: QuinticPolynomial,
        lon: QuarticPolynomial,
        t_arr: np.ndarray,
        T: float,
    ) -> Trajectory:
        traj = Trajectory()
        traj.lat_poly = lat
        traj.lon_poly = lon
        traj.T_horizon = T

        n = len(t_arr)
        traj.times = t_arr
        traj.s = np.array([lon.s(t) for t in t_arr])
        traj.d = np.array([lat.d(t) for t in t_arr])
        traj.ds = np.array([lon.ds(t) for t in t_arr])
        traj.dd = np.array([lat.dd(t) for t in t_arr])

        # Convert to world coords for logging (optional)
        wx = np.zeros(n)
        wz = np.zeros(n)
        for i, (si, di) in enumerate(zip(traj.s, traj.d)):
            pt = frenet_to_world(si, di, self.spline.xz, self.spline.s, self.spline.headings)
            wx[i], wz[i] = pt[0], pt[1]
        traj.wx = wx
        traj.wz = wz

        return traj

    def _is_feasible(self, traj: Trajectory, traffic: List[TrafficPrediction]) -> bool:
        """Hard constraint: reject if any point is within HARD_COLLISION_MARGIN of any traffic car."""
        hard_margin_m = self._scfg.safety_margin_m

        for tp in traffic:
            if len(tp.s_pred) == 0:
                continue
            n = min(len(traj.times), len(tp.s_pred))
            for i in range(n):
                ds = traj.s[i] - tp.s_pred[i]
                if abs(ds) > tp.car_length_m * 2:
                    continue   # longitudinally clear, skip expensive lateral check
                dd = abs(traj.d[i] - tp.d_pred[i])
                if dd < (tp.car_width_m / 2 + self.EGO_HALF_W + hard_margin_m):
                    return False
        return True

    def _score(
        self,
        traj: Trajectory,
        traffic: List[TrafficPrediction],
        target_v: float,
    ) -> float:
        scfg = self._scfg
        pcfg = self._pcfg

        # --- Progress reward ---
        progress = traj.s[-1] - self._ego_s
        score = 10.0 * progress

        # --- Speed reward ---
        mean_speed = float(np.mean(traj.ds))
        min_speed = self._spcfg.min_kph / 3.6
        if mean_speed < min_speed:
            score -= 500.0   # heavy penalty for dropping below scoring threshold
        else:
            score += 5.0 * (mean_speed / target_v)

        # --- Close-pass reward ---
        close_3x = 0
        close_1x = 0
        for tp in traffic:
            if len(tp.s_pred) == 0:
                continue
            n = min(len(traj.times), len(tp.s_pred))
            passed = False
            for i in range(n):
                # Check if we've passed this car at this time step
                if traj.s[i] > tp.s_pred[i] + tp.car_length_m / 2:
                    passed = True
                if passed:
                    dd = abs(traj.d[i] - tp.d_pred[i])
                    lateral_clearance = dd - tp.car_width_m / 2 - self.EGO_HALF_W
                    if 0 < lateral_clearance <= scfg.close_3x_m:
                        close_3x += 1
                        break
                    elif scfg.close_3x_m < lateral_clearance <= scfg.close_1x_m:
                        close_1x += 1
                        break

        score += 8.0 * close_3x + 2.0 * close_1x
        traj.close_3x_count = close_3x
        traj.close_1x_count = close_1x

        # --- Jerk penalty ---
        lat_jerk = traj.lat_poly.jerk_integral(traj.T_horizon)
        score -= 0.05 * lat_jerk

        # --- Lateral deviation penalty ---
        score -= 0.3 * float(np.mean(np.abs(traj.d)))

        return score

    def _emergency_trajectory(self, dt: float, n_steps: int) -> Trajectory:
        """Fallback: slow down, hold current lane."""
        traj = Trajectory()
        traj.T_horizon = dt * n_steps
        t_arr = np.linspace(dt, traj.T_horizon, n_steps)
        traj.times = t_arr
        decel_v = max(self._spcfg.min_kph / 3.6, self._ego_ds - 5.0)
        lon = QuarticPolynomial(self._ego_s, self._ego_ds, 0.0, decel_v, 0.0, traj.T_horizon)
        lat = QuinticPolynomial(self._ego_d, self._ego_dd, 0.0, 0.0, 0.0, 0.0, traj.T_horizon)
        traj.s = np.array([lon.s(t) for t in t_arr])
        traj.d = np.array([lat.d(t) for t in t_arr])
        traj.ds = np.array([lon.ds(t) for t in t_arr])
        traj.dd = np.array([lat.dd(t) for t in t_arr])
        traj.wx = np.zeros(n_steps)
        traj.wz = np.zeros(n_steps)
        traj.feasible = True
        traj.score = -1000.0
        return traj

    @property
    def ego_frenet(self) -> Tuple[float, float, float, float]:
        """Return (s, d, ds, dd) of ego in Frenet frame."""
        return self._ego_s, self._ego_d, self._ego_ds, self._ego_dd


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a
