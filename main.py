"""
No Hesi Autonomous Driving Bot — main entry point.

Architecture (all on Windows with CSP installed):
  ┌─────────────────────────────────────────────────────────────┐
  │  333 Hz  — Hot control loop (this thread)                   │
  │            Reads Car<0>.v0 → Stanley controller → writes    │
  │            CarControls<0>.v0                                │
  │                                                             │
  │   60 Hz  — Planning thread (separate)                       │
  │            Reads CarPublic<1..N>.v0 → Frenet planner →      │
  │            updates target_d / target_v for control loop     │
  │                                                             │
  │  Async   — Logger thread                                    │
  │            JSON-lines, no impact on control timing          │
  └─────────────────────────────────────────────────────────────┘

Usage:
    python main.py [--dry-run] [--hz 100] [--track shuto_revival_project_beta]

Prerequisites:
  1. Assetto Corsa + CSP installed
  2. extension/config/new_behaviour.ini: [CUSTOM_AI] ENABLED=1
  3. Track surfaces.ini: [_EXTRA_PERMISSIONS] ALLOW_CUSTOM_AI_MANIPULATION=1
  4. fast_lane.ai placed at data/fast_lane.ai (copy from track folder)
  5. pip install numpy scipy (vgamepad optional fallback)
"""
import argparse
import ctypes
import math
import platform
import signal
import sys
import threading
import time
from typing import Optional

from config import cfg
from src.control.input_writer import InputWriter
from src.control.stanley_controller import ControlOutput, StanleyController
from src.logger.session_logger import SessionLogger, make_meta_from_config
from src.planning.frenet_planner import FrenetPlanner, TrafficPrediction
from src.planning.gap_finder import GapFinder
from src.telemetry.csp_custom_ai import CSPInterface, CarState
from src.telemetry.sim_info import SimInfo
from src.track.spline_parser import parse_fast_lane


# ---------------------------------------------------------------------------
# Windows timer resolution — call once at startup
# ---------------------------------------------------------------------------

def set_windows_timer_resolution():
    if platform.system() != "Windows":
        return
    try:
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000080)  # HIGH
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)           # HIGHEST
        print("[main] Windows timer resolution set to 1ms, HIGH priority")
    except Exception as e:
        print(f"[main] Timer setup warning: {e}")


def precise_sleep_until(target: float):
    """Spin-yield hybrid: OS sleep for bulk, busy-spin last ~0.5ms."""
    remaining = target - time.perf_counter()
    if remaining > 0.001:
        time.sleep(remaining - 0.0005)
    while time.perf_counter() < target:
        pass


# ---------------------------------------------------------------------------
# Shared state between planning thread and control thread
# ---------------------------------------------------------------------------

class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.target_d: float = 0.0
        self.target_v_ms: float = cfg.speed.target_kph / 3.6
        self.plan_score: float = 0.0
        self.plan_close_3x: int = 0
        self.plan_close_1x: int = 0
        self.latest_traffic = []    # list[CarState]
        self.running: bool = True

    def set_plan(self, target_d: float, target_v_ms: float,
                 score: float, c3x: int, c1x: int):
        with self._lock:
            self.target_d = target_d
            self.target_v_ms = target_v_ms
            self.plan_score = score
            self.plan_close_3x = c3x
            self.plan_close_1x = c1x

    def get_target(self):
        with self._lock:
            return self.target_d, self.target_v_ms

    def set_traffic(self, cars):
        with self._lock:
            self.latest_traffic = cars

    def get_traffic(self):
        with self._lock:
            return list(self.latest_traffic)


# ---------------------------------------------------------------------------
# Planning thread (60 Hz)
# ---------------------------------------------------------------------------

def planning_thread(
    shared: SharedState,
    csp: CSPInterface,
    planner: FrenetPlanner,
    gap_finder: GapFinder,
    sim_info: SimInfo,
    logger: SessionLogger,
):
    """
    Runs at ~60 Hz. Reads traffic positions, runs Frenet planner,
    updates SharedState with target_d and target_v.
    """
    period = 1.0 / 60.0
    print("[planning] thread started")

    while shared.running:
        t0 = time.perf_counter()

        # Read traffic
        traffic_states = csp.read_traffic()
        shared.set_traffic(traffic_states)

        # Read own state for planner
        own = csp.read_own()
        if own is None:
            # Fallback: use AC shared memory
            ps = sim_info.get_player_state()
            if ps.is_live:
                # Reconstruct CarState from shared memory
                from src.telemetry.csp_custom_ai import CarState as CS
                vx, vy, vz = ps.world_vel
                speed_ms = ps.speed_ms
                heading = ps.heading
                own = CS(
                    index=0,
                    pos=(0.0, 0.0, 0.0),   # position not available from shared mem
                    vel=(vx, vy, vz),
                    look=(math.sin(heading), 0.0, math.cos(heading)),
                    speed_ms=speed_ms,
                    heading=heading,
                    spline_pos=ps.norm_spline_pos,
                )

        if own is None:
            time.sleep(period)
            continue

        # Update planner ego state
        planner.update_ego(own.pos[0], own.pos[2], own.speed_ms, own.heading)
        ego_s, ego_d, ego_ds, ego_dd = planner.ego_frenet

        # Project traffic to Frenet
        slots = gap_finder.project_traffic(traffic_states, ego_s)

        # Build traffic predictions for planner
        predictions = gap_finder.compute_traffic_predictions(slots, ego_s)

        # Adapt target speed: slow if gap is tight
        scfg = cfg.speed
        target_v = scfg.target_kph / 3.6
        for slot in slots:
            if 0 < (slot.s - ego_s) < 30:
                lateral_clear = abs(ego_d - slot.d) - 0.95 - slot.half_width
                if lateral_clear < cfg.scoring.safety_margin_m + 1.0:
                    target_v = min(target_v, scfg.tight_gap_speed_kph / 3.6)

        # Run planner
        traj = planner.plan(predictions, target_v)

        if traj is not None and traj.feasible:
            # First point of trajectory = immediate target
            t_d = float(traj.d[0]) if len(traj.d) > 0 else 0.0
            t_v = float(traj.ds[0]) if len(traj.ds) > 0 else target_v
            t_v = max(scfg.min_kph / 3.6, min(scfg.max_kph / 3.6, t_v))
            shared.set_plan(t_d, t_v, traj.score, traj.close_3x_count, traj.close_1x_count)

        # Log proximity
        logger.check_and_log_proximity(ego_s, ego_d, own.speed_kph, slots)

        elapsed = time.perf_counter() - t0
        sleep_t = period - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)
        elif elapsed > period * 2:
            from src.logger.session_logger import ErrorEvent
            logger.log_error(ErrorEvent(t=time.monotonic(), event_type="latency_spike",
                                        message=f"planning took {elapsed*1000:.1f}ms"))


# ---------------------------------------------------------------------------
# Main control loop (333 Hz)
# ---------------------------------------------------------------------------

def control_loop(
    shared: SharedState,
    csp: CSPInterface,
    sim_info: SimInfo,
    controller: StanleyController,
    planner: FrenetPlanner,
    writer: InputWriter,
    logger: SessionLogger,
    hz: float,
):
    period = 1.0 / hz
    print(f"[control] loop starting at {hz:.0f} Hz (period={period*1000:.2f}ms)")
    t = time.perf_counter()
    prev_t = t

    while shared.running:
        t += period
        now = time.perf_counter()
        dt = now - prev_t
        prev_t = now

        # Read own state
        own = csp.read_own()
        if own is None:
            ps = sim_info.get_player_state()
            heading = ps.heading
            speed_ms = ps.speed_ms
            pos_x, pos_z = 0.0, 0.0   # not available from standard shm
        else:
            heading = own.heading
            speed_ms = own.speed_ms
            pos_x, pos_z = own.pos[0], own.pos[2]

        # Get latest plan
        target_d, target_v = shared.get_target()
        controller.set_target(target_d, target_v)

        # Compute control
        out = controller.update(pos_x, pos_z, heading, speed_ms, dt)

        # Write to game
        writer.write(out)

        # Log frame
        from src.logger.session_logger import FrameEvent
        logger.log_frame(FrameEvent(
            t=now,
            speed_kph=speed_ms * 3.6,
            ego_s=planner._ego_s,
            ego_d=target_d,
            steer=out.steer,
            throttle=out.throttle,
            brake=out.brake,
            target_d=target_d,
            target_v_kph=target_v * 3.6,
            cte=out.cross_track_err,
            heading_err_deg=math.degrees(out.heading_err),
            plan_score=shared.plan_score,
            plan_close_3x=shared.plan_close_3x,
            plan_close_1x=shared.plan_close_1x,
            loop_dt_ms=dt * 1000,
        ))

        precise_sleep_until(t)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="No Hesi autonomous bot")
    p.add_argument("--dry-run", action="store_true", help="Compute but don't write controls")
    p.add_argument("--hz", type=float, default=333.0, help="Control loop rate (Hz)")
    p.add_argument("--track", type=str, default=cfg.track.name)
    p.add_argument("--fast-lane", type=str, default=cfg.track.fast_lane_path)
    p.add_argument("--max-cars", type=int, default=cfg.csp.max_cars)
    p.add_argument("--target-kph", type=float, default=cfg.speed.target_kph)
    return p.parse_args()


def main():
    args = parse_args()
    cfg.dry_run = args.dry_run
    cfg.speed.target_kph = args.target_kph
    cfg.track.name = args.track
    cfg.track.fast_lane_path = args.fast_lane

    set_windows_timer_resolution()

    print("=" * 60)
    print("  No Hesi Bot")
    print(f"  Track : {cfg.track.name}")
    print(f"  Target: {cfg.speed.target_kph:.0f} kph")
    print(f"  Dry   : {cfg.dry_run}")
    print("=" * 60)

    # --- Load track spline ---
    try:
        spline = parse_fast_lane(cfg.track.fast_lane_path)
    except FileNotFoundError:
        print(f"[main] ERROR: fast_lane.ai not found at '{cfg.track.fast_lane_path}'")
        print("       Copy the track's ai/fast_lane.ai into the data/ folder.")
        sys.exit(1)

    # --- Open telemetry ---
    csp = CSPInterface(
        own_index=cfg.csp.own_car_index,
        max_cars=args.max_cars,
    )
    sim_info = SimInfo()

    if not cfg.dry_run:
        csp.open()

    # --- Build components ---
    planner    = FrenetPlanner(spline)
    gap_finder = GapFinder(spline)
    controller = StanleyController(spline)
    writer     = InputWriter(csp)
    logger     = SessionLogger(cfg.logger.log_dir, cfg.logger.max_file_size_mb)

    if not cfg.dry_run:
        writer.open()

    # --- Start logger ---
    logger.start(make_meta_from_config())

    # --- Shared state ---
    shared = SharedState()

    # --- Graceful shutdown ---
    def on_signal(sig, frame):
        print("\n[main] Shutting down...")
        shared.running = False

    signal.signal(signal.SIGINT,  on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # --- Start planning thread ---
    plan_t = threading.Thread(
        target=planning_thread,
        args=(shared, csp, planner, gap_finder, sim_info, logger),
        daemon=True,
        name="planning",
    )
    plan_t.start()

    # --- Run control loop on main thread ---
    try:
        control_loop(shared, csp, sim_info, controller, planner, writer, logger, args.hz)
    except KeyboardInterrupt:
        shared.running = False
    finally:
        shared.running = False
        plan_t.join(timeout=2.0)
        writer.close()
        csp.close()
        sim_info.close()
        logger.stop()
        print("[main] Exited cleanly")


if __name__ == "__main__":
    main()
