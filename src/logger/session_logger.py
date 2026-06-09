"""
Structured JSON session logger.

Records every control cycle as a JSON-lines log (one JSON object per line).
Designed for offline analysis and auto-tuning:
  - Proximity events (near-misses, scored passes)
  - Control commands + state for each frame
  - Close-pass counts and timing for score reconstruction
  - Error events (feasibility failures, latency spikes)

Log rotation prevents runaway disk use.
Auto-tuning hooks: score_session() computes pass efficiency metrics post-run.
"""
import json
import math
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Log event types
# ---------------------------------------------------------------------------

@dataclass
class FrameEvent:
    t: float                # monotonic timestamp (s)
    speed_kph: float
    ego_s: float
    ego_d: float
    steer: float
    throttle: float
    brake: float
    target_d: float
    target_v_kph: float
    cte: float              # cross-track error (m)
    heading_err_deg: float
    plan_score: float
    plan_close_3x: int
    plan_close_1x: int
    loop_dt_ms: float       # actual loop period in ms


@dataclass
class ProximityEvent:
    t: float
    car_index: int
    ego_s: float
    traffic_s: float
    lateral_clearance_m: float  # edge-to-edge
    longitudinal_offset_m: float
    ego_speed_kph: float
    traffic_speed_kph: float
    zone: str                   # "3x" | "1x" | "collision_risk"


@dataclass
class ErrorEvent:
    t: float
    event_type: str
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMeta:
    start_iso: str
    track_name: str
    fast_lane_path: str
    config_snapshot: Dict[str, Any]


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class SessionLogger:
    """
    Async JSON-lines logger. All writes go to a background thread so the
    control loop never blocks on I/O.
    """

    def __init__(self, log_dir: str = "logs", max_size_mb: float = 50.0):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = int(max_size_mb * 1024 * 1024)

        self._q: queue.Queue = queue.Queue(maxsize=10000)
        self._file = None
        self._file_bytes = 0
        self._session_id = int(time.time())
        self._path: Optional[Path] = None

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._running = False

        # In-memory proximity event list for scoring
        self._prox_events: List[ProximityEvent] = []
        self._frame_count = 0
        self._start_t = time.monotonic()

    def start(self, meta: SessionMeta):
        self._running = True
        self._thread.start()
        self._path = self._dir / f"session_{self._session_id}.jsonl"
        self._enqueue({"type": "meta", **asdict(meta)})
        print(f"[Logger] Logging to {self._path}")

    def log_frame(self, ev: FrameEvent):
        self._frame_count += 1
        if self._frame_count % 3 != 0:    # log every 3rd frame ≈ 33 Hz
            return
        self._enqueue({"type": "frame", **asdict(ev)})

    def log_proximity(self, ev: ProximityEvent):
        self._prox_events.append(ev)
        self._enqueue({"type": "proximity", **asdict(ev)})

    def log_error(self, ev: ErrorEvent):
        self._enqueue({"type": "error", **asdict(ev)})

    def score_session(self) -> Dict[str, Any]:
        """
        Compute session performance metrics from logged proximity events.
        Call after session ends for tuning feedback.
        """
        if not self._prox_events:
            return {"close_3x": 0, "close_1x": 0, "collisions": 0}

        close_3x = sum(1 for e in self._prox_events if e.zone == "3x")
        close_1x = sum(1 for e in self._prox_events if e.zone == "1x")
        risks    = sum(1 for e in self._prox_events if e.zone == "collision_risk")

        duration = time.monotonic() - self._start_t
        return {
            "session_id": self._session_id,
            "duration_s": round(duration, 1),
            "frames": self._frame_count,
            "close_3x": close_3x,
            "close_1x": close_1x,
            "collision_risks": risks,
            "passes_per_minute": round((close_3x + close_1x) / (duration / 60), 2) if duration > 0 else 0,
        }

    def stop(self):
        if not self._running:
            return
        self._running = False
        summary = self.score_session()
        self._enqueue({"type": "summary", **summary})
        self._q.put(None)   # sentinel
        self._thread.join(timeout=5.0)
        print(f"[Logger] Session summary: {summary}")

    # ------------------------------------------------------------------
    # Proximity helpers (called by main loop)
    # ------------------------------------------------------------------

    def check_and_log_proximity(
        self,
        ego_s: float,
        ego_d: float,
        ego_speed_kph: float,
        traffic_slots,         # list of TrafficSlot
        ego_half_w: float = 0.95,
    ):
        """Automatically detect and log proximity events each planning cycle."""
        t = time.monotonic()
        for slot in traffic_slots:
            lon_offset = slot.s - ego_s
            if abs(lon_offset) > slot.half_length * 4:
                continue
            lat_clearance = abs(ego_d - slot.d) - ego_half_w - slot.half_width
            if lat_clearance <= 7.0:
                if lat_clearance < 0:
                    zone = "collision_risk"
                elif lat_clearance <= 4.0:
                    zone = "3x"
                else:
                    zone = "1x"
                ev = ProximityEvent(
                    t=t,
                    car_index=slot.car_index,
                    ego_s=ego_s,
                    traffic_s=slot.s,
                    lateral_clearance_m=round(lat_clearance, 3),
                    longitudinal_offset_m=round(lon_offset, 2),
                    ego_speed_kph=round(ego_speed_kph, 1),
                    traffic_speed_kph=round(slot.speed_ms * 3.6, 1),
                    zone=zone,
                )
                self.log_proximity(ev)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enqueue(self, obj: Dict):
        try:
            self._q.put_nowait(obj)
        except queue.Full:
            pass  # drop if queue overflows — control loop must not block

    def _worker(self):
        with open(self._path, 'a', encoding='utf-8') as f:
            while True:
                try:
                    item = self._q.get(timeout=1.0)
                except queue.Empty:
                    f.flush()
                    continue
                if item is None:
                    f.flush()
                    break
                line = json.dumps(item) + '\n'
                f.write(line)
                self._file_bytes += len(line)
                if self._file_bytes >= self._max_bytes:
                    # Rotate
                    f.flush()
                    self._session_id += 1
                    new_path = self._dir / f"session_{self._session_id}.jsonl"
                    f = open(new_path, 'a', encoding='utf-8')
                    self._file_bytes = 0
                    self._path = new_path


def make_meta_from_config() -> SessionMeta:
    from config import cfg
    import datetime
    return SessionMeta(
        start_iso=datetime.datetime.utcnow().isoformat(),
        track_name=cfg.track.name,
        fast_lane_path=cfg.track.fast_lane_path,
        config_snapshot={
            "target_kph": cfg.speed.target_kph,
            "close_3x_m": cfg.scoring.close_3x_m,
            "close_1x_m": cfg.scoring.close_1x_m,
            "safety_margin_m": cfg.scoring.safety_margin_m,
            "stanley_ke": cfg.control.stanley_ke,
            "speed_kp": cfg.control.speed_kp,
        }
    )
