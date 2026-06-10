#pragma once
#include <array>
#include <random>
#include <cmath>

// Wheel-like humanization layer.
//
// Three mechanisms combine to make output look like a human on a direct-drive wheel:
//
// 1. Spring-damper steering column
//    The "desired" steer computed by Stanley goes through a virtual
//    spring-damper column. Controlled by `smoothness`:
//      low  smoothness → stiff, quick wheel (like a Formula car)
//      high smoothness → heavy, slow wheel (like a GT car on stock settings)
//
// 2. Brownian motion noise
//    Real wheel drivers produce correlated low-frequency micro-corrections.
//    A mean-reverting random walk (Ornstein–Uhlenbeck process) is added to
//    steer. `humanization` scales the noise amplitude.
//
// 3. Reaction delay
//    The decided trajectory is buffered for a short random delay (100–180 ms)
//    before being applied, mimicking human processing + mechanical lag.
//    Modulated by `humanization`.

struct WheelOutput {
    float steer;    // [-1, 1]
    float throttle; // [0, 1]
    float brake;    // [0, 1]
};

class WheelModel {
public:
    void setParams(float smoothness,      // 0–1
                   float humanization);   // 0–1

    // Feed desired controls, get humanized output back.
    // dt in seconds.
    WheelOutput process(float desired_steer,
                        float raw_throttle,
                        float raw_brake,
                        float dt);

    void reset();

private:
    // Spring-damper state
    float col_pos_ = 0.f;    // current wheel column position
    float col_vel_ = 0.f;    // current wheel column velocity
    float spring_k_ = 30.f;
    float damping_  = 8.f;

    // Ornstein–Uhlenbeck noise state
    float ou_steer_ = 0.f;
    float ou_theta_ = 2.0f;    // mean-reversion rate
    float ou_sigma_ = 0.012f;  // noise amplitude (scales with humanization)

    // Throttle / brake S-curve state
    float prev_throttle_ = 0.f;
    float prev_brake_    = 0.f;

    // Reaction delay circular buffer (max 200 ms @ 333 Hz ≈ 67 entries)
    static constexpr int kDelayBuf = 80;
    struct DelayEntry { float steer, throttle, brake; };
    std::array<DelayEntry, kDelayBuf> delay_buf_{};
    int delay_head_ = 0;
    int delay_samples_ = 15; // default ~45 ms at 333 Hz

    // RNG
    std::mt19937 rng_{ std::random_device{}() };
    std::normal_distribution<float> gauss_{ 0.f, 1.f };
    std::uniform_real_distribution<float> uniform_{ 0.f, 1.f };

    float applyPedalFilter(float target, float current, float dt, float max_rate);
};
