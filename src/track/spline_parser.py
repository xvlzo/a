"""
Assetto Corsa fast_lane.ai binary parser.

Format version 7 layout:
  Header:  version(i32), point_count(i32), lap_time(i32), sample_count(i32)
  Points:  for each of point_count: x(f32), y(f32), z(f32), length(f32), id(i32)
  Extras:  extra_count(i32), then for each:
             speed(f32), gas(f32), brake(f32), obs_latg(f32), radius(f32),
             side_left(f32), side_right(f32), camber(f32), direction_angle(f32),
             normal(f32*3), length2(f32), forward(f32*3), tag(f32), grade(f32)
"""
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AiPoint:
    pos: Tuple[float, float, float]     # world XYZ
    length: float                        # arc-length from start (m)
    pid: int


@dataclass
class AiPointExtra:
    speed: float         # optimal speed (m/s)
    gas: float
    brake: float
    radius: float        # curvature radius (m)
    side_left: float     # track width left of centre (m)
    side_right: float    # track width right of centre (m)
    normal: Tuple[float, float, float]
    forward: Tuple[float, float, float]
    grade: float         # road grade


@dataclass
class TrackSpline:
    """Parsed fast_lane.ai spline with derived numpy arrays for fast lookup."""
    points: List[AiPoint] = field(default_factory=list)
    extras: List[AiPointExtra] = field(default_factory=list)
    total_length: float = 0.0

    # Derived arrays (populated by build_arrays())
    xz: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))   # [N, 2] world XZ
    s: np.ndarray = field(default_factory=lambda: np.empty(0))          # [N] arc-length
    headings: np.ndarray = field(default_factory=lambda: np.empty(0))   # [N] heading rad
    side_left: np.ndarray = field(default_factory=lambda: np.empty(0))  # [N] m
    side_right: np.ndarray = field(default_factory=lambda: np.empty(0)) # [N] m
    speeds: np.ndarray = field(default_factory=lambda: np.empty(0))     # [N] m/s
    radii: np.ndarray = field(default_factory=lambda: np.empty(0))      # [N] m

    def build_arrays(self):
        N = len(self.points)
        if N == 0:
            return

        xz = np.array([(p.pos[0], p.pos[2]) for p in self.points], dtype=np.float64)
        s = np.array([p.length for p in self.points], dtype=np.float64)

        # Compute headings from forward vectors when available, else from positions
        headings = np.zeros(N, dtype=np.float64)
        if self.extras:
            for i, ex in enumerate(self.extras):
                fx, _, fz = ex.forward
                headings[i] = math.atan2(fx, fz)
        else:
            for i in range(N - 1):
                dx = xz[i + 1, 0] - xz[i, 0]
                dz = xz[i + 1, 1] - xz[i, 1]
                headings[i] = math.atan2(dx, dz)
            headings[-1] = headings[-2]

        self.xz = xz
        self.s = s
        self.headings = headings
        self.total_length = float(s[-1]) if len(s) > 0 else 0.0

        if self.extras:
            self.side_left = np.array([e.side_left for e in self.extras], dtype=np.float64)
            self.side_right = np.array([e.side_right for e in self.extras], dtype=np.float64)
            self.speeds = np.array([e.speed for e in self.extras], dtype=np.float64)
            self.radii = np.array([e.radius for e in self.extras], dtype=np.float64)

    def get_track_width(self, spline_idx: int) -> Tuple[float, float]:
        """Return (side_left, side_right) in metres at a spline index."""
        if len(self.side_left) == 0 or spline_idx >= len(self.side_left):
            return 5.0, 5.0
        return float(self.side_left[spline_idx]), float(self.side_right[spline_idx])

    def recommended_speed_ms(self, spline_idx: int) -> float:
        """Return AI recommended speed at a spline index."""
        if len(self.speeds) == 0 or spline_idx >= len(self.speeds):
            return 44.0  # ~160 kph fallback
        return float(self.speeds[spline_idx])


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_fast_lane(path: str) -> TrackSpline:
    """
    Parse a fast_lane.ai file and return a TrackSpline.
    Raises ValueError on version mismatch or file truncation.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"fast_lane.ai not found: {path}")

    with p.open("rb") as f:
        # Header
        raw = f.read(16)
        if len(raw) < 16:
            raise ValueError("File too short for header")
        version, point_count, lap_time, sample_count = struct.unpack_from("<iiii", raw)

        if version not in (7,):
            # Version 5/6 exist but have slightly different layouts; warn and attempt
            print(f"[spline_parser] Warning: fast_lane version {version} (expected 7) — proceeding")

        # Points
        points: List[AiPoint] = []
        for _ in range(point_count):
            raw = f.read(20)
            if len(raw) < 20:
                raise ValueError("Truncated point data")
            x, y, z, length, pid = struct.unpack_from("<ffffi", raw)
            points.append(AiPoint(pos=(x, y, z), length=length, pid=pid))

        # Extras
        extras: List[AiPointExtra] = []
        header_raw = f.read(4)
        if len(header_raw) >= 4:
            (extra_count,) = struct.unpack_from("<i", header_raw)
            for _ in range(extra_count):
                raw = f.read(72)
                if len(raw) < 72:
                    break
                (speed, gas, brake, obs_latg, radius,
                 side_l, side_r, camber, direction_angle) = struct.unpack_from("<9f", raw, 0)
                nx, ny, nz = struct.unpack_from("<3f", raw, 36)
                (length2,) = struct.unpack_from("<f", raw, 48)
                fx, fy, fz = struct.unpack_from("<3f", raw, 52)
                tag, grade = struct.unpack_from("<2f", raw, 64)
                extras.append(AiPointExtra(
                    speed=speed,
                    gas=gas,
                    brake=brake,
                    radius=radius,
                    side_left=side_l,
                    side_right=side_r,
                    normal=(nx, ny, nz),
                    forward=(fx, fy, fz),
                    grade=grade,
                ))

    spline = TrackSpline(points=points, extras=extras)
    spline.build_arrays()
    print(f"[spline_parser] Loaded {len(points)} points, "
          f"extras={len(extras)}, length={spline.total_length:.0f}m")
    return spline


# ---------------------------------------------------------------------------
# Spline utilities
# ---------------------------------------------------------------------------

def build_kdtree(spline: TrackSpline):
    """Build a scipy KD-tree for fast nearest-point lookup."""
    from scipy.spatial import cKDTree
    return cKDTree(spline.xz)


def project_to_spline_fast(
    pos_x: float, pos_z: float,
    spline: TrackSpline,
    kdtree=None,
    hint_idx: int = 0,
    window: int = 80,
) -> Tuple[float, float, float, int]:
    """
    Project world (x, z) onto spline. Returns (s, d, heading, nearest_idx).
    Uses KD-tree for coarse search if provided, else window search.
    """
    from src.utils.geometry import project_point_to_segment, signed_lateral_offset

    N = len(spline.xz)
    if N < 2:
        return 0.0, 0.0, 0.0, 0

    if kdtree is not None:
        _, nearest_idx = kdtree.query([pos_x, pos_z])
    else:
        lo = max(0, hint_idx - window)
        hi = min(N - 1, hint_idx + window)
        sub = spline.xz[lo:hi] - np.array([pos_x, pos_z])
        d2 = (sub * sub).sum(axis=1)
        nearest_idx = int(lo + np.argmin(d2))

    # Segment refinement
    if nearest_idx < N - 1:
        i0, i1 = nearest_idx, nearest_idx + 1
    else:
        i0, i1 = nearest_idx - 1, nearest_idx

    ax, az = float(spline.xz[i0, 0]), float(spline.xz[i0, 1])
    bx, bz = float(spline.xz[i1, 0]), float(spline.xz[i1, 1])

    t, cx, cz = project_point_to_segment(pos_x, pos_z, ax, az, bx, bz)
    s = float(spline.s[i0]) + t * (float(spline.s[i1]) - float(spline.s[i0]))
    d = signed_lateral_offset(pos_x, pos_z, ax, az, bx, bz)
    heading = float(spline.headings[i0])

    return s, d, heading, i0
