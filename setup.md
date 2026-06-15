# No Hesi Bot — Setup

## Prerequisites

- Windows 10/11 64-bit
- Assetto Corsa (Steam)
- Custom Shaders Patch (CSP) v0.2.x+
- Visual Studio 2022 (Community is fine) with C++ workload
- CMake 3.20+
- **ViGEm Bus** (virtual gamepad driver — free, one-time install)

---

## 1. Install ViGEm Bus (virtual controller driver)

The bot controls your car via a virtual Xbox 360 controller.
No CSP config needed — this works in online multiplayer.

1. Download **ViGEmBus_Setup_x64.exe** from:
   `https://github.com/nefarius/ViGEmBus/releases` (latest release)
2. Run the installer, reboot if prompted.

---

## 2. Configure AC to use the virtual controller

> Do this ONCE after installing ViGEm and first running the bot.

1. Start the bot (`run.bat`) — it will create the virtual gamepad
2. Open AC → **Options → Controls**
3. At the top, select **"Xbox 360 Controller (ViGEm)"** (or similar name) as your device
4. Assign axes:
   - **Steer**: Left Stick X (move stick right to confirm; if inverted, tick "Invert")
   - **Gas**: Right Trigger
   - **Brake**: Left Trigger
5. Set **Steering lock** to match your car (360° is a good default)
6. **Save** and exit Controls
7. You only need to do this once — AC remembers the profile.

---

## 3. Get the track spline

Copy fast_lane.ai from the SRP track folder:
```
assettocorsa/content/tracks/shuto_revival_project_beta/ai/fast_lane.ai
```
→ place at `<repo>/data/fast_lane.ai`

Or download the optimised SRP AI splines from OverTake.gg (ID 61359).

---

## 4. Build

```bat
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
```

Binary at `build/Release/nohesi_bot.exe`.

---

## 5. Install Lua overlay

Copy `lua/nohesi_bot/` to:
```
assettocorsa/apps/lua/nohesi_bot/
```

Enable it in AC's app list (right side of screen in-game).

---

## 6. Run

Start AC, join a No Hesi server, wait until on track, then:

```bat
build\Release\nohesi_bot.exe --fast-lane data\fast_lane.ai
```

The bot starts **disabled**. Press **F5** to enable.

```
Options:
  --target-kph 160     Cruise speed
  --humanization 0.7   Wheel-like feel (0=robotic, 1=very human)
  --smoothness 0.6     Wheel weight (0=direct, 1=heavy GT)
  --safety 1.2         Hard collision buffer (m)
  --dry-run            Compute only, no output
```

---

## 7. In-game overlay

The CSP Lua app shows:

```
┌─ NO HESI BOT  [ON] ──────────────┐
│ Speed   :  158.3 kph              │
│ Target  :  160.0 kph              │
│ Offset d:  +0.42 m                │
│ 3x passes: 14   1x: 8             │
├───────────────────────────────────┤
│  [  Disable Bot [F5]  ]           │
│  Human      ████░░ 70%            │
│  Smooth     ██████ 60%            │
│  Speed kph  ───────■── 160        │
├───────────────────────────────────┤
│  3x gap m   ──■───── 4.0          │
│  1x gap m   ────■─── 7.0          │
│  Pass dist  ───■──── 3.0          │
└───────────────────────────────────┘
```

**Hotkeys** (work both inside AC and in the bot console window):
| Key | Action |
|-----|--------|
| F5  | Toggle bot on/off |
| F6  | Humanization +10% |
| F7  | Humanization -10% |
| F8  | Target speed +10 kph |
| F9  | Target speed -10 kph |

---

## 8. Architecture

```
333 Hz — Control thread (main)
  Read Car0.v0 mmap (CSP) → position, heading, speed
  Stanley lateral controller → desired steer
  PID longitudinal → throttle/brake
  WheelModel: spring-damper + OU noise + reaction delay → humanized output
  Write CarControls0.v0 (CSP) → game drives the car

144 Hz — Planning thread
  Read CarPublic<N>.v0 mmaps → traffic positions
  Project all to Frenet (s, d) coordinates
  Werling quintic trajectories × 200-500 candidates
  Hard filter: reject if < safety_margin from any traffic
  Soft score: progress + speed + 3x/1x close-pass reward - jerk
  Atomic write: target_d, target_v → control thread

UDP — Lua overlay
  Bot sends status every ~200ms to 127.0.0.1:27015
  Overlay sends slider changes back immediately
```

---

## 9. Tuning guide

After each run, check `logs/session_*.jsonl`. Key fields:
- `cte`: cross-track error — if consistently > 0.3m, increase `stanley_ke`
- `dt`: loop time in ms — should stay < 3ms (333 Hz budget)
- Proximity events tracked in planning thread close-pass counters

| Want | Adjust |
|------|--------|
| More 3x passes | Reduce `--safety` (1.0 minimum), reduce **3x gap** slider |
| More 1x passes | Increase **1x gap** slider |
| Closer weave | Reduce **Pass dist** slider (default 3.0 m clearance) |
| Less oscillation | Increase `--smoothness` |
| Slower reaction | Increase `--humanization` |
| Higher top speed | Increase `--target-kph` |
| Tighter line | Reduce `stanley_ke` in controller.cpp |
