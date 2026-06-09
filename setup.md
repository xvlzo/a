# No Hesi Bot — Setup Guide

## Prerequisites

- Windows 10/11 (64-bit)
- Assetto Corsa (Steam)
- Custom Shaders Patch (CSP) v0.2.x or later
- Python 3.10+ (64-bit) — **must be 64-bit**

---

## 1. Enable CSP Custom AI

**`assettocorsa/extension/config/new_behaviour.ini`** — create if it doesn't exist:
```ini
[CUSTOM_AI]
ENABLED=1
```

**Track surfaces.ini** (for Shutoko Revival Project — or whichever No Hesi track):
Find the file at `assettocorsa/content/tracks/shuto_revival_project_beta/data/surfaces.ini`
Add this section:
```ini
[_EXTRA_PERMISSIONS]
ALLOW_CUSTOM_AI_MANIPULATION=1
```

> Without this the bot cannot write control commands.

---

## 2. Get the track spline

Copy `fast_lane.ai` from the track's AI folder:
```
assettocorsa/content/tracks/shuto_revival_project_beta/ai/fast_lane.ai
```
Place it at:
```
<repo>/data/fast_lane.ai
```

The SRP AI splines can also be downloaded from OverTake.gg (ID 61359) or
the SRP Discord `#ai-spline-releases` channel — use the fastest spline available.

---

## 3. Install Python dependencies

```bash
pip install numpy scipy
```

Optional (only needed if CSP Custom AI is unavailable — fallback gamepad emulation):
```bash
pip install vgamepad
```
Also install [ViGEmBus driver](https://github.com/ViGEm/ViGEmBus/releases) if using vgamepad.

---

## 4. Run the bot

Start Assetto Corsa, load a No Hesi server, wait until you're on track, then:

```bash
python main.py
```

Options:
```
--dry-run        Compute trajectories but don't write any controls
--hz 333         Control loop rate (default: 333)
--target-kph 160 Target cruise speed (default: 160)
--fast-lane path/to/fast_lane.ai
```

---

## 5. How it works

```
┌─ 60 Hz: Planning thread ─────────────────────────────────┐
│  Read all traffic car positions (CarPublic mmaps)         │
│  Project to Frenet (s, d) coordinates                     │
│  Find driveable gaps in traffic                           │
│  Generate ~200-500 candidate trajectories (quintic poly)  │
│  Filter: reject any within 1.5m of a traffic car          │
│  Score: reward close passes (≤4m = 3x, ≤7m = 1x)         │
│  Output: target_d (lateral offset), target_v (speed)      │
└─────────────────────────────────────────────────────────┘
          ↓ (shared state, thread-safe)
┌─ 333 Hz: Control thread ─────────────────────────────────┐
│  Read own car state (Car0 mmap / AC shared memory)        │
│  Stanley controller: compute steer from target_d + CTE    │
│  PID: compute throttle/brake from target_v                │
│  Write to CarControls0 mmap → CSP drives the car         │
└─────────────────────────────────────────────────────────┘
          ↓ (async queue)
┌─ Async: Logger thread ───────────────────────────────────┐
│  JSON-lines log → logs/session_<timestamp>.jsonl          │
│  Records every frame: steer, throttle, proximity events   │
│  Post-session: score_session() reports pass efficiency    │
└─────────────────────────────────────────────────────────┘
```

---

## 6. Tuning

All tunable parameters are in `config.py`. Key ones to adjust:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `speed.target_kph` | 160 | Cruise speed |
| `scoring.safety_margin_m` | 1.2 | Hard collision buffer (reduce with caution) |
| `scoring.target_pass_dist_m` | 3.0 | Aim for this edge-to-edge clearance on passes |
| `control.stanley_ke` | 2.5 | Lateral tracking aggressiveness |
| `control.speed_kp` | 0.06 | Speed PID proportional gain |
| `planner.w_close_pass` | 8.0 | How much to reward close passes in trajectory scoring |
| `planner.min_gap_size_m` | 3.5 | Minimum gap width the planner will attempt |

After each run, check `logs/session_*.jsonl` or call:
```python
from src.logger.session_logger import SessionLogger
# scores printed at shutdown automatically
```

---

## 7. Score inference

The bot tracks its own proximity events (no screen-reading needed).
A "3x pass" is logged whenever the bot's edge-to-edge clearance to a traffic car
it is passing drops below `scoring.close_3x_m` (default 4m).

For ground-truth validation, compare inferred pass counts with the on-screen HUD.
If they diverge, adjust `close_3x_m`/`close_1x_m` to match the server's thresholds.
