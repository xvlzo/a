"""
No Hesi Bot — Central configuration.
All tunable parameters live here. Adjust before each session; logger will record which values were active.
"""
from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class TrackConfig:
    name: str = "shuto_revival_project_beta"
    fast_lane_path: str = "data/fast_lane.ai"
    # Lateral track half-width used when spline side data is unavailable
    default_lane_half_width: float = 5.0   # metres


@dataclass
class SpeedConfig:
    target_kph: float = 160.0   # cruise target
    min_kph: float = 80.0       # below this = score pause
    max_kph: float = 220.0      # hard cap (engine/safety)
    # Approach speed reduction when a gap is tighter than this
    tight_gap_threshold_m: float = 5.0
    tight_gap_speed_kph: float = 140.0


@dataclass
class ScoringConfig:
    # Lateral distance to the nearest traffic car that triggers a multiplier
    close_3x_m: float = 4.0     # ≤ 4 m → 3× bonus
    close_1x_m: float = 7.0     # ≤ 7 m → 1× bonus
    # Aim to pass exactly this far from the nearest car when targeting a 3× bonus
    target_pass_dist_m: float = 3.0
    # Minimum acceptable lateral clearance (hard constraint)
    safety_margin_m: float = 1.2


@dataclass
class PlannerConfig:
    # Frenet trajectory generation
    lookahead_m: float = 120.0          # horizon along track
    candidate_d_step: float = 0.4       # lateral offset resolution (m)
    max_lateral_offset: float = 6.0     # max |d| from centre-line
    # Trajectory sample count along s-axis
    s_samples: int = 40
    # Weights for trajectory scoring
    w_safety: float = 10.0              # penalty per collision risk
    w_close_pass: float = 5.0           # reward for tight passes
    w_lateral_accel: float = 1.0        # penalty for lateral jerk
    w_deviation: float = 0.3            # penalty for deviation from centre
    # Gap finder
    gap_scan_ahead_m: float = 80.0      # how far ahead to look for gaps
    gap_scan_behind_m: float = 20.0     # how far behind to check closing cars
    min_gap_size_m: float = 3.5         # minimum exploitable gap width


@dataclass
class ControlConfig:
    loop_hz: float = 100.0              # main control loop rate
    # Stanley lateral controller
    stanley_ke: float = 2.5             # cross-track error gain
    stanley_ks: float = 0.5             # speed denominator softening (m/s)
    stanley_kh: float = 1.0             # heading error multiplier
    max_steer_deg: float = 30.0         # physical steer limit for normalisation
    # Speed PID
    speed_kp: float = 0.06
    speed_ki: float = 0.003
    speed_kd: float = 0.008
    speed_integral_clamp: float = 20.0  # prevent wind-up
    # Output smoothing (IIR, α = fraction kept from previous frame)
    steer_smooth_alpha: float = 0.35
    throttle_smooth_alpha: float = 0.25
    brake_smooth_alpha: float = 0.3
    # Brake bias: apply brake when decel demand exceeds this (m/s²)
    brake_threshold_mss: float = 0.5


@dataclass
class VGamepadConfig:
    # Axis mapping for vgamepad Xbox360
    # AC usually: left-stick-X = steer, RT = throttle, LT = brake
    steer_axis: str = "left_x"          # "left_x" | "right_x"
    throttle_trigger: str = "right"     # "right" | "left"
    brake_trigger: str = "left"         # "right" | "left"
    update_hz: float = 100.0


@dataclass
class CSPConfig:
    # Car index: 0 = player car in singleplayer
    own_car_index: int = 0
    max_cars: int = 64
    # mmap name templates (format with car index)
    controls_mmap: str = "AcTools.CSP.NewBehaviour.CustomAI.CarControls{n}.v0"
    car_mmap: str = "AcTools.CSP.NewBehaviour.CustomAI.Car{n}.v0"
    car_public_mmap: str = "AcTools.CSP.NewBehaviour.CustomAI.CarPublic{n}.v0"
    # Standard AC shared memory
    physics_mmap: str = "Local\\acpmf_physics"
    graphics_mmap: str = "Local\\acpmf_graphics"
    static_mmap: str = "Local\\acpmf_static"


@dataclass
class LoggerConfig:
    log_dir: str = "logs"
    log_level: str = "INFO"             # DEBUG | INFO | WARNING
    flush_interval_s: float = 1.0       # how often to flush JSON to disk
    max_file_size_mb: float = 50.0
    # What to log at DEBUG level (verbose, only enable during tuning)
    log_trajectory_candidates: bool = False
    log_raw_controls: bool = True
    log_traffic_state: bool = True


@dataclass
class Config:
    track: TrackConfig = field(default_factory=TrackConfig)
    speed: SpeedConfig = field(default_factory=SpeedConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    vgamepad: VGamepadConfig = field(default_factory=VGamepadConfig)
    csp: CSPConfig = field(default_factory=CSPConfig)
    logger: LoggerConfig = field(default_factory=LoggerConfig)

    # Runtime toggles
    use_csp_car_public: bool = True     # False = skip opponent position reading
    use_vgamepad: bool = True           # False = dry-run (no actual input)
    dry_run: bool = False               # True = compute but don't write controls


# Singleton
cfg = Config()
