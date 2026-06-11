#include "planner.h"
#include <algorithm>

// ── Solve 3×3 via Cramer's rule ─────────────────────────────────────────────
static bool solve3x3(const float A[3][3], const float b[3], float x[3]) {
    float det = A[0][0]*(A[1][1]*A[2][2]-A[1][2]*A[2][1])
              - A[0][1]*(A[1][0]*A[2][2]-A[1][2]*A[2][0])
              + A[0][2]*(A[1][0]*A[2][1]-A[1][1]*A[2][0]);
    if (std::abs(det) < 1e-9f) return false;
    float inv = 1.f / det;
    for (int c = 0; c < 3; ++c) {
        float M[3][3];
        for (int r = 0; r < 3; ++r)
            for (int k = 0; k < 3; ++k)
                M[r][k] = (k == c) ? b[r] : A[r][k];
        float d = M[0][0]*(M[1][1]*M[2][2]-M[1][2]*M[2][1])
                - M[0][1]*(M[1][0]*M[2][2]-M[1][2]*M[2][0])
                + M[0][2]*(M[1][0]*M[2][1]-M[1][1]*M[2][0]);
        x[c] = d * inv;
    }
    return true;
}

// ── Solve 2×2 directly ──────────────────────────────────────────────────────
static bool solve2x2(float a00, float a01, float a10, float a11,
                     float b0, float b1, float& x0, float& x1) {
    float det = a00*a11 - a01*a10;
    if (std::abs(det) < 1e-9f) return false;
    float inv = 1.f / det;
    x0 = (b0*a11 - b1*a01) * inv;
    x1 = (b1*a00 - b0*a10) * inv;
    return true;
}

// ── QuinticPoly ──────────────────────────────────────────────────────────────
QuinticPoly::QuinticPoly(float d0, float dd0, float ddd0,
                         float dT, float ddT, float dddT, float T) {
    a[0] = d0;
    a[1] = dd0;
    a[2] = ddd0 / 2.f;

    float T2=T*T, T3=T*T2, T4=T*T3, T5=T*T4;
    float A[3][3] = {
        { T3,     T4,      T5    },
        { 3.f*T2, 4.f*T3,  5.f*T4 },
        { 6.f*T,  12.f*T2, 20.f*T3 }
    };
    float rhs[3] = {
        dT   - a[0] - a[1]*T  - a[2]*T2,
        ddT  - a[1] - 2.f*a[2]*T,
        dddT - 2.f*a[2]
    };
    float coef[3] = {};
    solve3x3(A, rhs, coef);
    a[3] = coef[0]; a[4] = coef[1]; a[5] = coef[2];
}

float QuinticPoly::d(float t) const {
    return a[0]+t*(a[1]+t*(a[2]+t*(a[3]+t*(a[4]+t*a[5]))));
}
float QuinticPoly::dd(float t) const {
    return a[1]+t*(2.f*a[2]+t*(3.f*a[3]+t*(4.f*a[4]+t*5.f*a[5])));
}
float QuinticPoly::ddd(float t) const {
    return 2.f*a[2]+t*(6.f*a[3]+t*(12.f*a[4]+t*20.f*a[5]));
}
// ── QuarticPoly ──────────────────────────────────────────────────────────────
QuarticPoly::QuarticPoly(float s0, float ds0, float dds0,
                         float dsT, float ddsT, float T) {
    b[0] = s0;
    b[1] = ds0;
    b[2] = dds0 / 2.f;

    float T2=T*T, T3=T*T2;
    float x0, x1;
    bool ok = solve2x2(3.f*T2, 4.f*T3, 6.f*T, 12.f*T2,
                       dsT  - b[1] - 2.f*b[2]*T,
                       ddsT - 2.f*b[2],
                       x0, x1);
    b[3] = ok ? x0 : 0.f;
    b[4] = ok ? x1 : 0.f;
}

float QuarticPoly::s(float t) const {
    return b[0]+t*(b[1]+t*(b[2]+t*(b[3]+t*b[4])));
}
float QuarticPoly::ds(float t) const {
    return b[1]+t*(2.f*b[2]+t*(3.f*b[3]+t*4.f*b[4]));
}
float QuarticPoly::dds(float t) const {
    return 2.f*b[2]+t*(6.f*b[3]+t*12.f*b[4]);
}
// ── FrenetPlanner ─────────────────────────────────────────────────────────────
FrenetPlanner::FrenetPlanner(const Spline& spline) : spline_(spline) {}

void FrenetPlanner::setConfig(const PlannerConfig& c) {
    std::lock_guard<std::mutex> lk(cfg_mutex_);
    cfg_pending_ = c;
}

PlannerConfig FrenetPlanner::config() const {
    std::lock_guard<std::mutex> lk(cfg_mutex_);
    return cfg_pending_;
}

float FrenetPlanner::wrapS(float delta) const {
    float L = spline_.total_length;
    if (delta >  L * 0.5f) delta -= L;
    if (delta < -L * 0.5f) delta += L;
    return delta;
}

static float wrapAngle(float a) {
    while (a >  3.14159f) a -= 6.28318f;
    while (a < -3.14159f) a += 6.28318f;
    return a;
}

void FrenetPlanner::updateEgo(float wx, float wz, float speed_ms,
                               float heading, int hint_idx) {
    auto now = std::chrono::steady_clock::now();

    FrenetState fs = spline_.project(wx, wz, hint_idx, 80);
    hint_idx_ = fs.idx;
    ego_s_ = fs.s;
    ego_d_ = fs.d;

    float herr = wrapAngle(heading - fs.road_heading);
    float new_ds = speed_ms * std::cos(herr);
    float new_dd = speed_ms * std::sin(herr);

    if (!first_update_) {
        float dt = std::chrono::duration<float>(now - last_update_).count();
        if (dt > 0.001f && dt < 0.5f) {
            ego_dds_ = (new_ds - prev_ego_ds_) / dt;
            ego_ddd_ = (new_dd - prev_ego_dd_) / dt;
        }
    } else {
        first_update_ = false;
    }

    ego_ds_      = new_ds;
    ego_dd_      = new_dd;
    prev_ego_ds_ = new_ds;
    prev_ego_dd_ = new_dd;
    last_update_ = now;
}

std::vector<float> FrenetPlanner::buildDCandidates(
    const std::vector<TrafficCar>& traffic) const
{
    std::vector<float> ds;
    float step = cfg_.d_step;
    for (float d = -cfg_.max_d; d <= cfg_.max_d + 1e-4f; d += step)
        ds.push_back(d);

    // Add scoring-band offsets next to nearby traffic cars
    float target_clearance = cfg_.target_pass_dist + cfg_.ego_half_w;
    for (auto& tc : traffic) {
        float rel_s = tc.s0 - ego_s_;
        if (rel_s < 0.f) rel_s += spline_.total_length;  // wrap at S/F line
        if (rel_s > 5.f && rel_s < 80.f) {
            ds.push_back(tc.d0 + tc.half_w + target_clearance);
            ds.push_back(tc.d0 - tc.half_w - target_clearance);
        }
    }

    // Deduplicate (round to 2 dp)
    std::sort(ds.begin(), ds.end());
    ds.erase(std::unique(ds.begin(), ds.end(),
        [](float a, float b){ return std::abs(a-b) < 0.05f; }), ds.end());
    return ds;
}

bool FrenetPlanner::isFeasible(const Trajectory& traj,
                                const std::vector<TrafficCar>& traffic) const {
    float margin = cfg_.safety_margin_m;
    float dt = traj.T / traj.n_steps;
    for (auto& tc : traffic) {
        for (int i = 0; i < traj.n_steps; ++i) {
            float t = (i + 1) * dt;
            float ts, td;
            tc.predict(t, ts, td);
            if (std::abs(wrapS(traj.s[i] - ts)) > (tc.half_l + cfg_.ego_half_l) * 3.f)
                continue; // longitudinally clear
            float dd = std::abs(traj.d[i] - td);
            float min_gap = tc.half_w + cfg_.ego_half_w + margin;
            if (dd < min_gap) return false;
        }
    }
    return true;
}

float FrenetPlanner::scoreTrajectory(Trajectory& traj,
                                      const std::vector<TrafficCar>& traffic,
                                      float target_v) const {
    float score = 0.f;

    // Progress (wrap so crossing S/F line isn't penalised)
    score += 10.f * wrapS(traj.s[traj.n_steps-1] - ego_s_);

    // Speed
    float mean_ds = 0.f;
    for (int i = 0; i < traj.n_steps; ++i) mean_ds += traj.ds[i];
    mean_ds /= traj.n_steps;
    float min_v = cfg_.min_kph / 3.6f;
    if (mean_ds < min_v)
        score -= 500.f;
    else
        score += 5.f * mean_ds / target_v;

    // Close-pass reward
    float dt = traj.T / traj.n_steps;
    int c3x = 0, c1x = 0;
    for (auto& tc : traffic) {
        bool counted = false;
        for (int i = 0; i < traj.n_steps && !counted; ++i) {
            float t = (i+1)*dt;
            float ts, td;
            tc.predict(t, ts, td);
            if (wrapS(traj.s[i] - ts) > tc.half_l) { // ego has passed traffic car's rear
                float lateral = std::abs(traj.d[i] - td) - tc.half_w - cfg_.ego_half_w;
                if (lateral > 0.f && lateral <= cfg_.close_3x_m) {
                    ++c3x; counted = true;
                } else if (lateral > 0.f && lateral <= cfg_.close_1x_m) {
                    ++c1x; counted = true;
                }
            }
        }
    }
    score += 8.f * c3x + 2.f * c1x;
    traj.close_3x = c3x;
    traj.close_1x = c1x;

    // Lateral jerk penalty
    // (approximate — recompute from d array)
    float jerk_sum = 0.f;
    for (int i = 2; i < traj.n_steps; ++i) {
        float jerk = traj.dd[i] - 2.f*traj.dd[i-1] + traj.dd[i-2];
        jerk_sum += jerk * jerk;
    }
    score -= 0.05f * jerk_sum;

    // Deviation from centre-line
    float dev = 0.f;
    for (int i = 0; i < traj.n_steps; ++i) dev += std::abs(traj.d[i]);
    score -= 0.3f * dev / traj.n_steps;

    return score;
}

Trajectory FrenetPlanner::plan(const std::vector<TrafficCar>& traffic,
                                float target_speed_ms) {
    { std::lock_guard<std::mutex> lk(cfg_mutex_); cfg_ = cfg_pending_; }

    float T_variants[] = { 2.5f, 3.0f, 3.5f, 4.0f };
    float v_min = std::max(cfg_.min_kph / 3.6f, target_speed_ms - 20.f);
    float v_max = target_speed_ms + 5.f;

    auto d_cands = buildDCandidates(traffic);

    float best_score = -1e9f;
    Trajectory best{};
    bool found = false;

    for (float T : T_variants) {
        int n = std::min(Trajectory::kMaxSteps, static_cast<int>(T / 0.1f));
        float dt = T / n;

        for (float dT : d_cands) {
            // Clamp to track boundaries; skip if track too narrow to fit
            float sl, sr;
            spline_.getWidths(hint_idx_, sl, sr);
            float d_min = -(sr - cfg_.ego_half_w);
            float d_max =   sl - cfg_.ego_half_w;
            if (d_min > d_max) continue;
            dT = std::clamp(dT, d_min, d_max);

            QuinticPoly lat(ego_d_, ego_dd_, ego_ddd_, dT, 0.f, 0.f, T);

            for (float vT = v_min; vT <= v_max + 1e-4f; vT += 5.f) {
                QuarticPoly lon(ego_s_, ego_ds_, ego_dds_, vT, 0.f, T);

                Trajectory traj;
                traj.T = T;
                traj.n_steps = n;
                for (int i = 0; i < n; ++i) {
                    float t = (i+1) * dt;
                    traj.s[i]  = lon.s(t);
                    if (traj.s[i] >= spline_.total_length)
                        traj.s[i] -= spline_.total_length; // normalise across S/F line
                    traj.d[i]  = lat.d(t);
                    traj.ds[i] = lon.ds(t);
                    traj.dd[i] = lat.dd(t);
                }
                traj.target_d  = dT;          // aim at final lane, not first tiny step
                traj.target_ds = traj.ds[0];

                if (!isFeasible(traj, traffic)) continue;
                traj.feasible = true;

                float sc = scoreTrajectory(traj, traffic, target_speed_ms);
                traj.score = sc;
                if (sc > best_score) { best_score = sc; best = traj; found = true; }
            }
        }
    }

    return found ? best : emergencyTrajectory();
}

Trajectory FrenetPlanner::emergencyTrajectory() const {
    float T = 3.0f;
    int n = 30;
    float dt = T / n;
    float decel_v = std::max(cfg_.min_kph / 3.6f, ego_ds_ - 8.f);

    QuarticPoly lon(ego_s_, ego_ds_, 0.f, decel_v, 0.f, T);
    QuinticPoly lat(ego_d_, ego_dd_, 0.f, 0.f,     0.f, 0.f, T);

    Trajectory traj;
    traj.T = T;
    traj.n_steps = n;
    for (int i = 0; i < n; ++i) {
        float t = (i+1) * dt;
        traj.s[i]  = lon.s(t);
        traj.d[i]  = lat.d(t);
        traj.ds[i] = lon.ds(t);
        traj.dd[i] = lat.dd(t);
    }
    traj.target_d  = 0.f;
    traj.target_ds = decel_v;
    traj.feasible  = true;
    traj.score     = -1000.f;
    return traj;
}
