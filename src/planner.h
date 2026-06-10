#pragma once
#include "spline.h"
#include <vector>
#include <array>
#include <cmath>
#include <chrono>

// Frenet-frame trajectory planner (Werling 2010).
//
// Generates a grid of candidate trajectories:
//   lateral  d(t) : quintic polynomial (6 BCs)
//   longitudinal s(t) : quartic polynomial (5 BCs)
//
// Filters by hard collision constraint (edge-to-edge < safety_margin),
// then scores for: progress, speed, close-pass reward (3× ≤4m, 1× ≤7m),
// jerk penalty, deviation penalty.

// ── Traffic prediction snapshot ─────────────────────────────────────────────
struct TrafficCar {
    float s0, d0;           // Frenet position
    float v_s, v_d;         // Frenet velocity components
    float half_w;           // half-width (m)
    float half_l;           // half-length (m)
    int   car_idx;

    // Predict position at time t (constant-speed lane-keep)
    void predict(float t, float& s_out, float& d_out) const {
        s_out = s0 + v_s * t;
        // Lane-keep model: d decays toward 0 with τ = 2.5s
        d_out = d0 * std::exp(-t / 2.5f);
    }
};

// ── Quintic polynomial d(t) for lateral ─────────────────────────────────────
struct QuinticPoly {
    float a[6]; // a0..a5

    QuinticPoly(float d0, float dd0, float ddd0,
                float dT, float ddT, float dddT,
                float T);

    float d(float t)   const;
    float dd(float t)  const;
    float ddd(float t) const;
    float jerkIntegral(float T, int n = 20) const;
};

// ── Quartic polynomial s(t) for longitudinal ─────────────────────────────────
struct QuarticPoly {
    float b[5]; // b0..b4

    QuarticPoly(float s0, float ds0, float dds0,
                float dsT, float ddsT,
                float T);

    float s(float t)   const;
    float ds(float t)  const;
    float dds(float t) const;
    float jerkIntegral(float T, int n = 20) const;
};

// ── Single candidate trajectory ───────────────────────────────────────────────
struct Trajectory {
    static constexpr int kMaxSteps = 40;

    float s[kMaxSteps];
    float d[kMaxSteps];
    float ds[kMaxSteps];
    float dd[kMaxSteps];
    int   n_steps = 0;
    float T = 0.f;

    float score = -1e9f;
    bool  feasible = false;
    int   close_3x = 0;
    int   close_1x = 0;

    // Target output (first step of trajectory)
    float target_d  = 0.f;
    float target_ds = 0.f;
};

// ── Planner config ────────────────────────────────────────────────────────────
struct PlannerConfig {
    float target_kph        = 160.f;
    float min_kph           = 80.f;
    float max_kph           = 220.f;
    float safety_margin_m   = 1.2f;
    float close_3x_m        = 4.0f;
    float close_1x_m        = 7.0f;
    float target_pass_dist  = 3.0f;   // aim for this edge-to-edge clearance
    float max_d             = 6.0f;   // max |d| candidate
    float d_step            = 0.4f;   // lateral sampling resolution
    float ego_half_w        = 0.95f;
    float ego_half_l        = 2.3f;
};

// ── Planner ──────────────────────────────────────────────────────────────────
class FrenetPlanner {
public:
    explicit FrenetPlanner(const Spline& spline);
    void setConfig(const PlannerConfig& cfg) { cfg_ = cfg; }
    const PlannerConfig& config() const { return cfg_; }

    // Update ego Frenet state (call before plan())
    void updateEgo(float wx, float wz, float speed_ms, float heading, int hint_idx);

    // Run planner. Returns best trajectory (or emergency if none found).
    Trajectory plan(const std::vector<TrafficCar>& traffic,
                    float target_speed_ms);

    // Current ego Frenet state
    float egoS()  const { return ego_s_; }
    float egoD()  const { return ego_d_; }
    float egoDS() const { return ego_ds_; }
    int   hintIdx() const { return hint_idx_; }

private:
    const Spline& spline_;
    PlannerConfig cfg_;

    float ego_s_  = 0.f;
    float ego_d_  = 0.f;
    float ego_ds_ = 40.f;
    float ego_dd_ = 0.f;
    float ego_dds_ = 0.f;
    float ego_ddd_ = 0.f;
    int   hint_idx_ = 0;

    float prev_ego_ds_ = 0.f;
    float prev_ego_dd_ = 0.f;
    std::chrono::steady_clock::time_point last_update_{};
    bool  first_update_ = true;

    // Build d-target candidates (centre-line + scoring band offsets)
    std::vector<float> buildDCandidates(const std::vector<TrafficCar>& traffic) const;

    // Check trajectory against all traffic (hard constraint)
    bool isFeasible(const Trajectory& traj,
                    const std::vector<TrafficCar>& traffic) const;

    // Score a feasible trajectory
    float scoreTrajectory(Trajectory& traj,
                          const std::vector<TrafficCar>& traffic,
                          float target_v) const;

    // Emergency: decelerate + hold lane
    Trajectory emergencyTrajectory() const;
};
