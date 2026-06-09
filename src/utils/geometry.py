"""
Fast 3-D and 2-D geometry helpers used throughout the bot.
All positions are in Assetto Corsa world space: X = right, Y = up, Z = forward.
"""
import math
from typing import Tuple

import numpy as np


Vec3 = Tuple[float, float, float]


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def norm2(x: float, y: float) -> float:
    return math.sqrt(x * x + y * y)


def dist2d(ax: float, az: float, bx: float, bz: float) -> float:
    dx, dz = ax - bx, az - bz
    return math.sqrt(dx * dx + dz * dz)


def dist3d(a: Vec3, b: Vec3) -> float:
    dx, dy, dz = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def heading_from_dir(dx: float, dz: float) -> float:
    """Return heading angle in radians from a direction vector (XZ plane)."""
    return math.atan2(dx, dz)


def wrap_angle(a: float) -> float:
    """Wrap angle to [-π, π]."""
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


# ---------------------------------------------------------------------------
# Spline projection
# ---------------------------------------------------------------------------

def project_point_to_segment(
    px: float, pz: float,
    ax: float, az: float,
    bx: float, bz: float,
) -> Tuple[float, float, float]:
    """
    Project point P onto segment AB.
    Returns (t, closest_x, closest_z) where t ∈ [0,1] is the parameter along AB.
    """
    abx, abz = bx - ax, bz - az
    apx, apz = px - ax, pz - az
    ab2 = abx * abx + abz * abz
    if ab2 < 1e-12:
        return 0.0, ax, az
    t = (apx * abx + apz * abz) / ab2
    t = max(0.0, min(1.0, t))
    return t, ax + t * abx, az + t * abz


def signed_lateral_offset(
    px: float, pz: float,
    ax: float, az: float,
    bx: float, bz: float,
) -> float:
    """
    Return signed lateral offset of P from segment AB.
    Positive = left of forward direction (AB).
    """
    abx, abz = bx - ax, bz - az
    apx, apz = px - ax, pz - az
    # cross product (AB × AP) in XZ plane gives sign
    cross = abx * apz - abz * apx
    seg_len = math.sqrt(abx * abx + abz * abz)
    if seg_len < 1e-9:
        return 0.0
    return cross / seg_len


# ---------------------------------------------------------------------------
# Numpy-based batch operations (used by Frenet planner)
# ---------------------------------------------------------------------------

def closest_spline_index(pos_xz: np.ndarray, spline_xz: np.ndarray) -> int:
    """
    Return index of the nearest spline point to pos_xz (shape [2]).
    spline_xz has shape [N, 2].
    """
    diff = spline_xz - pos_xz
    d2 = (diff * diff).sum(axis=1)
    return int(np.argmin(d2))


def world_to_frenet(
    pos_xz: np.ndarray,         # [2] world XZ
    spline_xz: np.ndarray,      # [N, 2] spline points
    spline_s: np.ndarray,       # [N] cumulative arc-lengths
    hint_idx: int = 0,
    search_window: int = 60,
) -> Tuple[float, float, float]:
    """
    Convert world XZ position to Frenet (s, d, heading_error).
    Returns (s, d, dpsi) where:
      s   = along-track distance (m)
      d   = lateral offset (m, positive = left)
      dpsi = heading error relative to spline tangent (rad)
    hint_idx limits search to a window for real-time efficiency.
    """
    N = len(spline_xz)
    lo = max(0, hint_idx - search_window)
    hi = min(N - 1, hint_idx + search_window)

    seg_xz = spline_xz[lo:hi]
    diff = seg_xz - pos_xz
    d2 = (diff * diff).sum(axis=1)
    local_idx = int(np.argmin(d2))
    best_idx = lo + local_idx

    # Refine with segment projection
    if best_idx < N - 1:
        i0, i1 = best_idx, best_idx + 1
    else:
        i0, i1 = best_idx - 1, best_idx

    ax, az = spline_xz[i0]
    bx, bz = spline_xz[i1]
    t, cx, cz = project_point_to_segment(pos_xz[0], pos_xz[1], ax, az, bx, bz)

    s = spline_s[i0] + t * (spline_s[i1] - spline_s[i0])
    d = signed_lateral_offset(pos_xz[0], pos_xz[1], ax, az, bx, bz)

    # Spline tangent heading
    fwd_x, fwd_z = bx - ax, bz - az
    spline_heading = math.atan2(fwd_x, fwd_z)

    return s, d, spline_heading, i0


def frenet_to_world(
    s: float, d: float,
    spline_xz: np.ndarray,
    spline_s: np.ndarray,
    spline_headings: np.ndarray,
) -> np.ndarray:
    """
    Convert Frenet (s, d) back to world XZ.
    Returns [2] array.
    """
    N = len(spline_s)
    # Find segment containing s
    idx = int(np.searchsorted(spline_s, s, side='right')) - 1
    idx = max(0, min(idx, N - 2))

    seg_len = spline_s[idx + 1] - spline_s[idx]
    if seg_len > 1e-9:
        t = (s - spline_s[idx]) / seg_len
    else:
        t = 0.0
    t = max(0.0, min(1.0, t))

    # Interpolate position
    pt = spline_xz[idx] * (1 - t) + spline_xz[idx + 1] * t

    # Interpolate heading
    h = spline_headings[idx] * (1 - t) + spline_headings[idx + 1] * t

    # Offset laterally (positive d = left = perpendicular CCW from forward)
    # AC XZ: forward = +Z, right = +X  → left normal = (-sin(h)... wait)
    # heading = atan2(dx, dz), so forward dir = (sin(h), cos(h)) in XZ
    # left normal (CCW 90° in XZ) = (-cos(h), sin(h))
    left_x = -math.cos(h)
    left_z = math.sin(h)

    return np.array([pt[0] + d * left_x, pt[1] + d * left_z])
