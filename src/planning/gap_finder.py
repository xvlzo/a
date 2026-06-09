"""
Gap finder — identifies driveable lateral corridors in traffic.

For each cross-section of the road ahead, builds a 1-D occupancy profile
in Frenet d-coordinates and finds clear corridors wide enough to drive through.
Corridors are ranked: prefer those that allow close-pass scoring (3m–4m from
a traffic car side) over those that are wide open.
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from config import cfg
from src.track.spline_parser import TrackSpline, project_to_spline_fast


# ---------------------------------------------------------------------------
# Traffic snapshot in Frenet
# ---------------------------------------------------------------------------

@dataclass
class TrafficSlot:
    """Traffic car projected into Frenet frame."""
    car_index: int
    s: float                # along-track position (m)
    d: float                # lateral offset from centre (m)
    speed_ms: float
    heading_err: float      # relative to road (rad)

    # Dimensions (default = average sedan)
    half_length: float = 2.3
    half_width: float = 0.9


# ---------------------------------------------------------------------------
# Gap descriptor
# ---------------------------------------------------------------------------

@dataclass
class Gap:
    d_center: float             # target lateral offset to drive to (m)
    d_left: float               # left boundary of clear zone (m)
    d_right: float              # right boundary of clear zone (m)
    width: float                # d_left - d_right (m)
    adjacent_car_d: float       # d of nearest flanking car
    adjacent_car_dist: float    # edge-to-edge clearance to that car (m)
    score_3x: bool              # passing through here gives 3× bonus
    score_1x: bool              # gives 1× bonus

    @property
    def is_valid(self) -> bool:
        return self.width >= cfg.planner.min_gap_size_m


# ---------------------------------------------------------------------------
# GapFinder
# ---------------------------------------------------------------------------

class GapFinder:
    """
    Converts traffic car positions (Frenet) into ranked gap list.
    Call update() once per planning cycle to refresh internal state.
    """

    # Ego half-width (metres) — add safety buffer
    EGO_HALF_W = 0.95

    def __init__(self, spline: TrackSpline):
        self.spline = spline
        self.kdtree = None
        self._build_kdtree()
        self._scfg = cfg.scoring
        self._pcfg = cfg.planner

    def _build_kdtree(self):
        try:
            from scipy.spatial import cKDTree
            self.kdtree = cKDTree(self.spline.xz)
        except ImportError:
            pass

    def project_traffic(
        self,
        traffic_states,           # list of CarState
        ego_s: float,
        ego_hint_idx: int = 0,
    ) -> List[TrafficSlot]:
        """Project all traffic cars into Frenet frame."""
        slots: List[TrafficSlot] = []
        for cs in traffic_states:
            s, d, road_h, idx = project_to_spline_fast(
                cs.pos[0], cs.pos[2],
                self.spline, self.kdtree, ego_hint_idx
            )
            heading_err = _wrap_angle(cs.heading - road_h)
            slots.append(TrafficSlot(
                car_index=cs.index,
                s=s,
                d=d,
                speed_ms=cs.speed_ms,
                heading_err=heading_err,
            ))
        return slots

    def find_gaps(
        self,
        slots: List[TrafficSlot],
        ego_s: float,
        ego_d: float,
        ego_hint_idx: int = 0,
    ) -> List[Gap]:
        """
        Find driveable lateral gaps ahead of ego.

        Only considers cars within (gap_scan_behind_m, gap_scan_ahead_m).
        Builds a 1-D occupancy profile in d, identifies clear corridors,
        ranks by close-pass scoring potential.
        """
        ahead_m = self._pcfg.gap_scan_ahead_m
        behind_m = self._pcfg.gap_scan_behind_m

        # Filter: only cars roughly ahead and within scan window
        relevant = [
            s for s in slots
            if -behind_m <= (s.s - ego_s) <= ahead_m
        ]

        if not relevant:
            return [self._centre_gap(ego_hint_idx)]

        # Track boundaries at ego position
        sl, sr = self.spline.get_track_width(ego_hint_idx)
        d_min = -sl   # left boundary (most negative d)
        d_max = sr    # right boundary

        # Build occupancy intervals from traffic car extents (with safety margin)
        margin = self._scfg.safety_margin_m
        occupied: List[Tuple[float, float]] = []  # (d_lo, d_hi) blocked bands
        for slot in relevant:
            d_lo = slot.d - slot.half_width - self.EGO_HALF_W - margin
            d_hi = slot.d + slot.half_width + self.EGO_HALF_W + margin
            occupied.append((d_lo, d_hi))

        # Merge overlapping occupied bands
        occupied.sort(key=lambda x: x[0])
        merged: List[Tuple[float, float]] = []
        for lo, hi in occupied:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append([lo, hi])

        # Extract free intervals between merged occupied bands
        free_intervals: List[Tuple[float, float]] = []
        cursor = d_min
        for lo, hi in merged:
            if lo > cursor:
                free_intervals.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < d_max:
            free_intervals.append((cursor, d_max))

        min_gap = self._pcfg.min_gap_size_m
        gaps: List[Gap] = []

        for d_lo, d_hi in free_intervals:
            width = d_hi - d_lo
            if width < min_gap:
                continue

            d_center = (d_lo + d_hi) / 2.0

            # Find the nearest flanking car for this corridor
            nearest_dist = 1e9
            nearest_d = 0.0
            for slot in relevant:
                edge_dist = min(
                    abs(slot.d - slot.half_width - (d_lo + self.EGO_HALF_W)),
                    abs(slot.d + slot.half_width - (d_hi - self.EGO_HALF_W)),
                )
                if edge_dist < nearest_dist:
                    nearest_dist = edge_dist
                    nearest_d = slot.d

            # Check close-pass scoring: is there a car we could pass at 3–4m?
            score_3x = nearest_dist <= self._scfg.close_3x_m
            score_1x = not score_3x and nearest_dist <= self._scfg.close_1x_m

            # Adjust d_center toward scoring band if possible
            if score_3x:
                # Bias toward 3x zone
                target_side_clearance = (self._scfg.close_3x_m + self._scfg.safety_margin_m) / 2
                if nearest_d > d_center:
                    d_center = nearest_d - nearest_d * 0.5  # crude bias left
                else:
                    d_center = nearest_d + nearest_d * 0.5
                d_center = max(d_lo + self.EGO_HALF_W, min(d_hi - self.EGO_HALF_W, d_center))

            gaps.append(Gap(
                d_center=d_center,
                d_left=d_hi,
                d_right=d_lo,
                width=width,
                adjacent_car_d=nearest_d,
                adjacent_car_dist=nearest_dist,
                score_3x=score_3x,
                score_1x=score_1x,
            ))

        # Sort: prefer 3x > 1x > widest
        gaps.sort(key=lambda g: (
            -int(g.score_3x) * 10,
            -int(g.score_1x) * 5,
            -g.width,
        ))

        return gaps if gaps else [self._centre_gap(ego_hint_idx)]

    def _centre_gap(self, idx: int) -> Gap:
        """Fallback gap: stay at road centre."""
        sl, sr = self.spline.get_track_width(idx)
        return Gap(
            d_center=0.0,
            d_left=sl,
            d_right=-sr,
            width=sl + sr,
            adjacent_car_d=0.0,
            adjacent_car_dist=20.0,
            score_3x=False,
            score_1x=False,
        )

    def compute_traffic_predictions(
        self,
        slots: List[TrafficSlot],
        ego_s: float,
    ):
        """
        Build TrafficPrediction objects for the Frenet planner.
        Only includes cars ahead of ego (within scan window).
        """
        from src.planning.frenet_planner import TrafficPrediction
        ahead_m = self._pcfg.gap_scan_ahead_m
        preds = []
        for slot in slots:
            if slot.s < ego_s - 5.0:
                continue
            if slot.s > ego_s + ahead_m:
                continue
            v_s = slot.speed_ms * math.cos(slot.heading_err)
            v_d = slot.speed_ms * math.sin(slot.heading_err)
            tp = TrafficPrediction(
                s0=slot.s,
                d0=slot.d,
                v_s=v_s,
                v_d=v_d,
                car_width_m=slot.half_width * 2,
                car_length_m=slot.half_length * 2,
            )
            preds.append(tp)
        return preds


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a
