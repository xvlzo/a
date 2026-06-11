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

// ── StanleyController ─────────────────────────────────────────────────────────
StanleyController::StanleyController(const Spline& spline,
                                     float ke, float ks,
                                     float max_steer_deg)
    : spline_(spline), ke_(ke), ks_(ks),
      max_steer_rad_(max_steer_deg * 3.14159f / 180.f),
      speed_pid_(0.06f, 0.003f, 0.008f, 20.f)
{}

static float wrapAngle(float a) {
    while (a >  3.14159f) a -= 6.28318f;
    while (a < -3.14159f) a += 6.28318f;
    return a;
}

ControlDemand StanleyController::update(float wx, float wz,
                                         float heading, float speed_ms,
                                         float target_d, float target_v_ms,
                                         float dt, int hint_idx) {
    FrenetState fs = spline_.project(wx, wz, hint_idx, 80);
    last_hint_ = fs.idx;

    // Cross-track error: signed distance from desired lateral offset
    // d > 0 = left, so if we're left of target, cte > 0 → steer right
    float cte = fs.d - target_d;

    // Heading error relative to road
    float herr = wrapAngle(heading - fs.road_heading);

    // Derivative of heading error — damps yaw oscillation
    float dherr = first_herr_ ? 0.f : wrapAngle(herr - prev_herr_) / dt;
    prev_herr_  = herr;
    first_herr_ = false;

    // Stanley: δ = heading_err - arctan(ke * cte / (v + ks))
    // Negative sign: in AC steer<0=right, d>0=left, so CTE correction must be negated
    float stanley = -std::atan2(ke_ * cte, speed_ms + ks_);
    float raw_rad = herr - 0.15f * dherr + stanley;
    raw_rad = std::clamp(raw_rad, -max_steer_rad_, max_steer_rad_);
    float steer_raw = raw_rad / max_steer_rad_; // normalise to [-1, 1]
    // Low-pass filter: damps oscillation without killing responsiveness
    float steer = 0.25f * steer_raw + 0.75f * prev_steer_;
    prev_steer_ = steer;

    // Longitudinal PID
    float accel = speed_pid_.update(target_v_ms - speed_ms, dt);
    float throttle = std::clamp(accel, 0.f, 1.f);
    float brake    = std::clamp(-accel / 3.f, 0.f, 1.f); // 3 m/s² = full brake

    return { steer, throttle, brake, cte, herr };
}

void StanleyController::reset() {
    speed_pid_.reset();
    prev_steer_ = 0.f;
    prev_herr_  = 0.f;
    first_herr_ = true;
}
