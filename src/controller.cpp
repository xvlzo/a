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
    FrenetState fs = spline_.project(wx, wz, hint_idx, 80);
    last_hint_ = fs.idx;

    // Cross-track error: signed distance from desired lateral offset
    // d > 0 = left, so if we're left of target, cte > 0 → steer right
    float cte = fs.d - target_d;

    // Heading error relative to road
    float herr = wrapAngle(heading - fs.road_heading);

    // ── Seek/merge mode ───────────────────────────────────────────────────────
    // Stanley saturates to full lock (→ donuts) when the car is far off the line,
    // e.g. spawned in a pit/staging area. When the offset is large, switch to a
    // pure-pursuit controller that aims at a lookahead point ON the racing line
    // and drives toward it, merging smoothly. Hysteresis: enter at 8 m, exit at 4 m.
    if (!seeking_ && std::abs(cte) > 8.f)  seeking_ = true;
    if ( seeking_ && std::abs(cte) < 4.f)  seeking_ = false;

    if (seeking_) {
        // Lookahead grows with speed and with how far off we are → shallow merge
        float L = std::clamp(speed_ms * 1.5f + 0.3f * std::abs(cte), 15.f, 50.f);
        float s_target = fs.s + L;
        if (spline_.total_length > 1.f)
            s_target = std::fmod(s_target, spline_.total_length);

        float tx, tz;
        spline_.frenetToWorld(s_target, target_d, tx, tz);

        // Steer toward the target point. Same convention as Stanley's herr:
        // reference heading = direction car→target; error = heading - reference.
        float ang        = std::atan2(tx - wx, tz - wz);
        float seek_err   = wrapAngle(heading - ang);
        float raw        = std::clamp(seek_err, -max_steer_rad_, max_steer_rad_);
        float steer_raw  = raw / max_steer_rad_;
        float steer      = 0.25f * steer_raw + 0.75f * prev_steer_;
        prev_steer_      = steer;

        // Gentle, capped throttle while merging (don't rocket off the line)
        float merge_v    = std::min(target_v_ms, 22.f); // ~80 kph cap
        float accel      = speed_pid_.update(merge_v - speed_ms, dt);
        float throttle   = std::clamp(accel, 0.f, 0.5f);
        float brake      = std::clamp(-accel / 3.f, 0.f, 1.f);
        return { steer, throttle, brake, cte, herr };
    }

    // Stanley: δ = heading_err - arctan(ke * cte / (v + ks))
    // Negative sign: in AC steer<0=right, d>0=left, so CTE correction must be negated
    float stanley = -std::atan2(ke_ * cte, speed_ms + ks_);

    // Yaw-rate damping: counteracts heading oscillation using smooth measured yaw rate.
    // yaw_rate_rad_s > 0 = turning left; in AC steer>0 = left, so subtract to oppose.
    float raw_rad = herr + stanley - 0.3f * yaw_rate_rad_s;
    raw_rad = std::clamp(raw_rad, -max_steer_rad_, max_steer_rad_);
    float steer_raw = raw_rad / max_steer_rad_; // normalise to [-1, 1]
    // Light IIR to filter frame-to-frame noise without adding meaningful lag
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
    seeking_    = false;
}
