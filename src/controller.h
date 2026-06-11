#pragma once
#include "spline.h"
#include <cmath>

// Stanley lateral controller + PID longitudinal controller.

struct ControlDemand {
    float steer;    // desired steer, normalised [-1, 1] — BEFORE wheel model
    float throttle; // [0, 1]
    float brake;    // [0, 1]
    float cte;      // cross-track error (m) for logging
    float heading_err; // radians
};

class PID {
public:
    PID(float kp, float ki, float kd, float clamp = 30.f)
        : kp_(kp), ki_(ki), kd_(kd), clamp_(clamp) {}

    float update(float error, float dt);
    void  reset();

private:
    float kp_, ki_, kd_, clamp_;
    float integral_ = 0.f;
    float prev_err_ = 0.f;
    bool  first_    = true;
};

class StanleyController {
public:
    StanleyController(const Spline& spline,
                      float ke = 1.0f,
                      float ks = 1.0f,
                      float max_steer_deg = 30.f);

    void setGains(float ke, float ks) { ke_ = ke; ks_ = ks; }

    // Compute desired control demand.
    // target_d: lateral offset from centre-line (Frenet d, metres)
    // target_v: target speed (m/s)
    // yaw_rate_rad_s: car's measured yaw rate (+ve = left) for oscillation damping
    // Returns demand BEFORE wheel model humanisation.
    ControlDemand update(float wx, float wz, float heading,
                         float speed_ms,
                         float target_d,
                         float target_v_ms,
                         float dt,
                         int hint_idx = 0,
                         float yaw_rate_rad_s = 0.f);

    void reset();
    int lastHintIdx() const { return last_hint_; }

private:
    const Spline& spline_;
    float ke_, ks_;
    float max_steer_rad_;
    PID   speed_pid_;
    int   last_hint_ = 0;
    float prev_steer_ = 0.f;
};
