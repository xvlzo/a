#include "wheel_model.h"
#include <algorithm>

void WheelModel::setParams(float smoothness, float humanization) {
    // Smoothness: high = heavy/slow wheel, low = direct
    // spring_k range: 80 (twitchy) → 15 (heavy)
    spring_k_ = 80.f - smoothness * 65.f;
    // damping: critical damping = 2*sqrt(spring_k), then scale for overdamping
    float crit = 2.f * std::sqrt(spring_k_);
    damping_   = crit * (1.0f + smoothness * 0.8f);

    // OU noise: humanization scales amplitude and reaction delay
    ou_sigma_ = humanization * 0.018f;
    ou_theta_ = 2.0f + (1.f - humanization) * 3.0f; // faster reversion when less human

    // Reaction delay: 0 humanization = 8 samples (~24 ms), 1.0 = 23 samples (~69 ms)
    delay_samples_ = static_cast<int>(8.f + humanization * 15.f);
    delay_samples_ = std::min(delay_samples_, kDelayBuf - 1);
}

WheelOutput WheelModel::process(float desired_steer,
                                float raw_throttle,
                                float raw_brake,
                                float dt) {
    // ── 1. Spring-damper column ──────────────────────────────────────────
    float error = desired_steer - col_pos_;
    float force = spring_k_ * error - damping_ * col_vel_;
    col_vel_ += force * dt;
    col_pos_ += col_vel_ * dt;
    col_pos_ = std::clamp(col_pos_, -1.f, 1.f);

    // ── 2. Ornstein–Uhlenbeck noise (Euler–Maruyama) ─────────────────────
    // dX = -θ·X·dt + σ·dW,  dW ~ N(0, sqrt(dt))
    float dw = gauss_(rng_) * std::sqrt(dt);
    ou_steer_ += -ou_theta_ * ou_steer_ * dt + ou_sigma_ * dw;
    ou_steer_ = std::clamp(ou_steer_, -0.06f, 0.06f);

    float steer_humanized = std::clamp(col_pos_ + ou_steer_, -1.f, 1.f);

    // ── 3. Throttle S-curve (organic pedal application) ──────────────────
    // Apply a smoothstep to the *rate of change*, not the absolute value.
    // This creates the soft-start / soft-release feel of a real pedal.
    float throttle_smoothed = applyPedalFilter(raw_throttle, prev_throttle_, dt, 4.0f);
    float brake_smoothed    = applyPedalFilter(raw_brake,    prev_brake_,    dt, 5.0f);
    prev_throttle_ = throttle_smoothed;
    prev_brake_    = brake_smoothed;

    // ── 4. Write into delay buffer, read delayed output ───────────────────
    int write_idx = delay_head_;
    delay_buf_[write_idx] = { steer_humanized, throttle_smoothed, brake_smoothed };
    delay_head_ = (delay_head_ + 1) % kDelayBuf;

    int read_idx = (delay_head_ + kDelayBuf - delay_samples_) % kDelayBuf;
    auto& out = delay_buf_[read_idx];

    return { out.steer, out.throttle, out.brake };
}

void WheelModel::reset() {
    col_pos_ = col_vel_ = 0.f;
    ou_steer_ = 0.f;
    prev_throttle_ = prev_brake_ = 0.f;
    for (auto& e : delay_buf_) e = {};
    delay_head_ = 0;
}

// First-order IIR filter on pedal value with rate limiting
// max_rate: max change per second
float WheelModel::applyPedalFilter(float target, float current, float dt, float max_rate) {
    float delta = target - current;
    float max_step = max_rate * dt;
    delta = std::clamp(delta, -max_step, max_step);
    return current + delta;
}
