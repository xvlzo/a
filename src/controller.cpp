#include "controller.h"
#include <algorithm>
#include <cmath>

// ── PID ──────────────────────────────────────────────────────────────────────
float PID::update(float error, float dt) {
    dt = std::max(0.001f, std::min(dt, 0.2f));
    integral_ += error * dt;
    integral_  = std::clamp(integral_, -clamp_, clamp_);
    float deriv = first_ ? 0.f : (error - prev_err_) / dt;
    prev_err_ = error;
    first_ = false;
    return kp_ * error + ki_ * integral_ + kd_ * deriv;
}

void PID::reset() {
    integral_ = 0.f;
    prev_err_ = 0.f;
    first_    = true;
}

// ── StanleyController (pure-pursuit) ──────────────────────────────────────────
StanleyController::StanleyController(const Spline& spline, float max_steer_deg)
    : spline_(spline),
      max_steer_rad_(max_steer_deg * 3.14159f / 180.f),
      speed_pid_(0.08f, 0.012f, 0.008f, 50.f)
{}

static float wrapAngle(float a) {
    while (a >  3.14159f) a -= 6.28318f;
    while (a < -3.14159f) a += 6.28318f;
    return a;
}

ControlDemand StanleyController::update(float wx, float wz,
                                         float heading, float speed_ms,
                                         float target_d, float target_v_ms,
                                         float dt, int hint_idx,
                                         float yaw_rate_rad_s) {
    FrenetState fs = spline_.project(wx, wz, hint_idx, 80, heading);
    last_hint_ = fs.idx;

    // Cross-track error: signed distance from desired lateral offset
    // d > 0 = left, so if we're left of target, cte > 0 → steer right
    float cte = fs.d - target_d;

    // Heading error relative to road
    float herr = wrapAngle(heading - fs.road_heading);

    // ── Wrong-way guard ─────────────────────────────────────────────────────────
    // If the heading error is huge (>100°) the car is pointed against the racing
    // line — either matched to the opposing carriageway or genuinely facing the
    // wrong way. Steering hard into this just spins the car (donuts). Instead,
    // ease off and coast straight so the situation can resolve without a spin.
    if (std::abs(herr) > 1.745f) { // 100°
        float steer = 0.6f * prev_steer_; // bleed off any existing lock toward centre
        prev_steer_ = steer;
        return { steer, 0.f, 0.2f, cte, herr };
    }

    // ── Pure-pursuit lateral control ────────────────────────────────────────────
    // Aim at a point further along the racing line and steer toward it. Unlike
    // Stanley, pure pursuit does NOT blow up at low speed — the steering angle is
    // pure geometry (bearing to the lookahead point), bounded and smooth.
    float L = std::clamp(0.9f * speed_ms + 14.f, 14.f, 45.f); // lookahead (m)
    float s_target = fs.s + L;
    if (spline_.total_length > 1.f)
        s_target = std::fmod(s_target, spline_.total_length);

    float tx, tz;
    spline_.frenetToWorld(s_target, target_d, tx, tz);

    // Bearing to the lookahead point, same convention as herr:
    // reference = direction car→target; error = heading - reference.
    float ang       = std::atan2(tx - wx, tz - wz);
    float err       = wrapAngle(heading - ang);
    float steer_cmd = std::clamp(err / max_steer_rad_, -1.f, 1.f);

    // Yaw-rate damping to settle heading oscillation (steer>0 = left in AC).
    steer_cmd -= 0.15f * yaw_rate_rad_s;
    steer_cmd  = std::clamp(steer_cmd, -1.f, 1.f);

    // Speed-scaled steering authority: at a standstill, big steering just pivots
    // the car in place (→ spin into the wall). Allow only gentle steer until the
    // car is rolling, then ramp to full authority by ~15 m/s. This makes the car
    // drive forward to build speed first, then merge onto the line.
    float auth = std::clamp(0.18f + speed_ms * (0.82f / 15.f), 0.18f, 1.f);
    steer_cmd  = std::clamp(steer_cmd, -auth, auth);

    // Steering rate limit: no instant lurch from the previous value.
    float max_delta = 3.0f * dt; // full lock-to-lock in ~0.66 s
    float steer = std::clamp(steer_cmd, prev_steer_ - max_delta, prev_steer_ + max_delta);
    prev_steer_ = steer;

    // ── Longitudinal: cap speed until merged so we never rocket off the line ────
    bool  merged   = std::abs(cte) < 3.f && std::abs(herr) < 0.35f; // <20°
    float target_v = merged ? target_v_ms : std::min(target_v_ms, 50.f / 3.6f);
    float accel    = speed_pid_.update(target_v - speed_ms, dt);
    float throttle = std::clamp(accel, 0.f, 1.f);
    float brake    = std::clamp(-accel / 3.f, 0.f, 1.f);

    return { steer, throttle, brake, cte, herr };
}

void StanleyController::reset() {
    speed_pid_.reset();
    prev_steer_ = 0.f;
}
