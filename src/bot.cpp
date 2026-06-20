// winsock2.h is already included transitively via bot.h (before windows.h)
#include "bot.h"
#include "timer.h"
#include <cstdio>
#include <cstring>
#include <ctime>
#include <algorithm>
#include <utility>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "winmm.lib")

// ─── MmapHandle ──────────────────────────────────────────────────────────────
bool MmapHandle::openRead(const char* name, size_t size) {
    hMap = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
    if (!hMap || hMap == INVALID_HANDLE_VALUE) return false;
    pView = MapViewOfFile(hMap, FILE_MAP_READ, 0, 0, size);
    valid = pView != nullptr;
    return valid;
}

bool MmapHandle::openReadWrite(const char* name, size_t size) {
    hMap = CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                              0, static_cast<DWORD>(size), name);
    if (!hMap || hMap == INVALID_HANDLE_VALUE) return false;
    pView = MapViewOfFile(hMap, FILE_MAP_WRITE, 0, 0, size);
    valid = pView != nullptr;
    if (valid) memset(pView, 0, size); // zero-init
    return valid;
}

void MmapHandle::close() {
    if (pView) { UnmapViewOfFile(pView); pView = nullptr; }
    if (hMap && hMap != INVALID_HANDLE_VALUE) { CloseHandle(hMap); hMap = INVALID_HANDLE_VALUE; }
    valid = false;
}

// ─── Bot constructor ──────────────────────────────────────────────────────────
Bot::Bot(const Config& cfg) : cfg_(cfg) {
    planner_    = std::make_unique<FrenetPlanner>(spline_);
    controller_ = std::make_unique<StanleyController>(spline_);
    wheel_      = std::make_unique<WheelModel>();
    std::fill(std::begin(car_was_behind_), std::end(car_was_behind_), true);
}

Bot::~Bot() { stop(); }

// ─── Init ─────────────────────────────────────────────────────────────────────
bool Bot::init() {
    // Load spline
    if (!spline_.load(cfg_.fast_lane_path)) {
        printf("[Bot] ERROR: Could not load fast_lane.ai from '%s'\n",
               cfg_.fast_lane_path.c_str());
        return false;
    }
    printf("[Bot] Spline loaded: %d points, %.0f m\n",
           spline_.size(), spline_.total_length);
    if (spline_.size() > 0)
        printf("[Bot] Spline origin: pt[0]=(%.1f, %.1f)  pt[last]=(%.1f, %.1f)\n",
               spline_.pts[0].x, spline_.pts[0].z,
               spline_.pts.back().x, spline_.pts.back().z);

    PlannerConfig pc;
    pc.target_kph      = cfg_.target_kph;
    pc.min_kph         = cfg_.min_kph;
    pc.max_kph         = cfg_.max_kph;
    pc.safety_margin_m = cfg_.safety_margin;
    planner_->setConfig(pc);

    wheel_->setParams(cfg_.smoothness, cfg_.humanization);

    // Default settings
    settings_.enabled               = false;
    settings_.humanization          = cfg_.humanization;
    settings_.smoothness            = cfg_.smoothness;
    settings_.target_kph            = cfg_.target_kph;
    settings_.safety_margin_m       = cfg_.safety_margin;
    settings_.close_pass_aggressive = true;

    live_target_kph_.store(cfg_.target_kph);
    live_safety_margin_.store(cfg_.safety_margin);

    if (!openMmaps()) return false;
    vctrl_.init();  // virtual gamepad for online multiplayer; gracefully no-ops if ViGEm not installed
    openLogFile();
    initUDP();

    printf("[Bot] Init OK. Press F5 to enable. F6/F7 = humanization. F8/F9 = speed.\n");
    return true;
}

bool Bot::openMmaps() {
    char name[256];

    // CSP Custom AI mmaps — only valid offline/singleplayer AI slots.
    // In online multiplayer these never open; vJoy + Lua telemetry handle it.
    snprintf(name, sizeof(name),
             "AcTools.CSP.NewBehaviour.CustomAI.CarControls%d.v0", cfg_.own_car_index);
    own_ctrl_mm_.openReadWrite(name, sizeof(CarControls));

    snprintf(name, sizeof(name),
             "AcTools.CSP.NewBehaviour.CustomAI.Car%d.v0", cfg_.own_car_index);
    for (int i = 0; i < 30; ++i) {
        if (own_car_mm_.openRead(name, sizeof(CarData))) break;
        Sleep(100);
    }

    // AC shared memory (physics: speed/heading fallback; graphics: pit lane status)
    physics_mm_.openRead("Local\\acpmf_physics", sizeof(SPageFilePhysics));
    graphics_mm_.openRead("Local\\acpmf_graphics", sizeof(SPageFileGraphic));

    // Settings/status mmaps for optional offline Lua overlay
    settings_mm_.openReadWrite("NohesiBot.Settings.v0", sizeof(BotSettings));
    if (settings_mm_.valid) {
        auto* s = static_cast<BotSettings*>(settings_mm_.pView);
        s->humanization       = cfg_.humanization;
        s->smoothness         = cfg_.smoothness;
        s->target_kph         = cfg_.target_kph;
        s->safety_margin_m    = cfg_.safety_margin;
        const auto& pc        = planner_->config();
        s->close_3x_m         = pc.close_3x_m;
        s->close_1x_m         = pc.close_1x_m;
        s->target_pass_dist_m = pc.target_pass_dist;
    }
    status_mm_.openReadWrite("NohesiBot.Status.v0", sizeof(BotStatus));

    return true;
}



// ─── Run ──────────────────────────────────────────────────────────────────────
void Bot::run() {
    running_ = true;
    plan_thread_   = std::thread(&Bot::planningLoop, this);
    hotkey_thread_ = std::thread(&Bot::hotkeyLoop,   this);
    controlLoop(); // main thread
    if (plan_thread_.joinable())   plan_thread_.join();
    if (hotkey_thread_.joinable()) hotkey_thread_.join();
}

void Bot::stop() {
    running_ = false;
    if (plan_thread_.joinable())   plan_thread_.join();
    if (hotkey_thread_.joinable()) hotkey_thread_.join();
    if (!cfg_.dry_run && own_ctrl_mm_.valid)
        writeControls({ 0.f, 0.f, 0.f }); // release
    if (log_file_) {
        std::lock_guard<std::mutex> lk(log_mutex_);
        char buf[128];
        snprintf(buf, sizeof(buf),
                 "{\"session_end\":true,\"passes_3x\":%d,\"passes_1x\":%d}\n",
                 passes_3x_total_.load(), passes_1x_total_.load());
        log_file_ << buf;
        log_file_.flush();
    }
}

// ─── Control loop (333 Hz, main thread) ──────────────────────────────────────
void Bot::controlLoop() {
    setRealtimePriority();
    LoopTimer timer(cfg_.control_hz);
    const float dt = 1.f / cfg_.control_hz;

    float px = 0, pz = 0, heading = 0, speed_ms = 0, spline_pos = 0;

    while (running_) {
        timer.beginFrame();

        readSettings();
        readOwnCar(px, pz, heading, speed_ms, spline_pos);

        // Read pit state
        bool in_pit_lane = false, in_pit = false;
        if (graphics_mm_.valid) {
            auto* g = static_cast<const SPageFileGraphic*>(graphics_mm_.pView);
            in_pit_lane = g->isInPitLane != 0;
            in_pit      = g->isInPit     != 0;
        }

        // ── F5 enable: pick starting state ──────────────────────────────────
        if (enabled_.load() && state_ == BotState::DISABLED) {
            if (in_pit_lane || in_pit) {
                state_ = BotState::PIT_EXIT;
                pit_exit_timer_ = 0.f;
                wheel_->reset();
                printf("[Bot] Starting from pits — driving to track\n");
            } else {
                state_ = BotState::ACTIVE;
                is_active_ = true;
                wheel_->reset();
                controller_->reset();
                // Heading-filtered scan to seed plan.hint_idx before the controller's
                // first call, so the very first frame projects onto the correct lane
                // (not the opposing carriageway) — same logic the planning loop uses.
                {
                    bool ww = false;
                    int  idx = findStartIndex(px, pz, heading, ww);
                    std::lock_guard<std::mutex> lk(plan_mutex_);
                    latest_plan_.hint_idx = idx;
                }
                if (own_car_mm_.valid)
                    last_collision_counter_ =
                        static_cast<const CarData*>(own_car_mm_.pView)->collision_counter;
                printf("[Bot] ENABLED\n");
            }
        }

        // ── F5 disable: abort everything ────────────────────────────────────
        if (!enabled_.load() && state_ != BotState::DISABLED) {
            state_ = BotState::DISABLED;
            is_active_ = false;
            wheel_->reset();
            controller_->reset();
            if (!cfg_.dry_run) {
                writeControls({0.f, 0.f, 0.f});
                if (vctrl_.available()) vctrl_.update(0.f, 0.f, 0.f);
            }
            printf("[Bot] DISABLED\n");
        }

        PlanOutput plan;
        { std::lock_guard<std::mutex> lk(plan_mutex_); plan = latest_plan_; }

        WheelOutput wheel_out{};

        switch (state_) {

        // ── ACTIVE: full planner + Stanley + wheel model ─────────────────────
        case BotState::ACTIVE: {
            if (own_car_mm_.valid) {
                auto* d = static_cast<const CarData*>(own_car_mm_.pView);
                // Accumulate collision depth on new events; decay when clear.
                // A brief tap (<0.15m) won't reach the 0.6m threshold.
                // A real crash (single 0.6m+ hit, or sustained heavy contact) will.
                if (d->collision_counter != last_collision_counter_) {
                    if (d->collision_depth > 0.15f)
                        crash_depth_acc_ += d->collision_depth;
                    last_collision_counter_ = d->collision_counter;
                }
                crash_depth_acc_ *= 0.995f; // half-life ~420ms at 333Hz

                if (crash_depth_acc_ > 0.6f) {
                    printf("[Bot] Crash! (acc=%.2f) Returning to pits — score reset\n",
                           crash_depth_acc_);
                    crash_depth_acc_ = 0.f;
                    passes_3x_total_.store(0);
                    passes_1x_total_.store(0);
                    std::fill(std::begin(car_was_behind_), std::end(car_was_behind_), true);
                    is_active_ = false;
                    state_ = BotState::CRASHED;
                    crashed_timer_ = 0.f;
                    teleport_sent_ = false;
                    break;
                }
            }

            float yaw_rate = 0.f;
            if (own_car_mm_.valid) {
                auto* cd = static_cast<const CarData*>(own_car_mm_.pView);
                yaw_rate = cd->local_angular_vel.y; // rad/s, +ve = left
            }
            ControlDemand raw = controller_->update(
                px, pz, heading, speed_ms,
                plan.target_d, plan.target_v, dt,
                plan.hint_idx, yaw_rate);

            wheel_out = wheel_->process(raw.steer, raw.throttle, raw.brake, dt);

            // Diagnostic (~1 Hz): where the bot thinks the line is vs where we are.
            // Lets us tell a bad/offset spline (line off-road) from a control bias.
            {
                static int diag = 0;
                if (++diag % 333 == 0) {
                    FrenetState fs = spline_.project(px, pz, plan.hint_idx, 80, heading);
                    float lx, lz;
                    spline_.frenetToWorld(fs.s, 0.f, lx, lz);
                    float herr_deg = raw.heading_err * 57.2958f;
                    printf("[Diag] car=(%.1f,%.1f) cte=%.1fm herr=%.0fdeg "
                           "steer=%.2f thr=%.2f brk=%.2f tgt_d=%.1f spd=%.0f\n",
                           px, pz, raw.cte, herr_deg,
                           wheel_out.steer, wheel_out.throttle, wheel_out.brake,
                           plan.target_d, speed_ms * 3.6f);
                }
            }

            float frame_ms = static_cast<float>(timer.frameElapsed() * 1000.0);
            logFrame(wheel_out, raw, frame_ms);

            status_.active          = true;
            status_.speed_kph       = speed_ms * 3.6f;
            status_.target_kph      = plan.target_v * 3.6f;
            status_.target_d        = plan.target_d;
            status_.ego_d           = plan.ego_d;
            status_.cross_track_err = raw.cte;
            status_.passes_3x       = passes_3x_total_.load();
            status_.passes_1x       = passes_1x_total_.load();
            status_.plan_dt_ms      = plan.plan_dt_ms;
            break;
        }

        // ── CRASHED: hard brake → teleport → wait for pit box ────────────────
        case BotState::CRASHED:
            crashed_timer_ += dt;
            if (crashed_timer_ < 0.2f) {
                wheel_out = {0.f, 0.f, 1.f};          // hard brake
            } else if (!teleport_sent_) {
                triggerTeleportToPits();               // sets teleport_to=1 for exactly one frame
                teleport_sent_ = true;
                wheel_out = {0.f, 0.f, 0.f};
            }
            // else hold idle while game places us in pit box (~2-3s total)
            if (crashed_timer_ > 3.0f) {
                state_ = BotState::PIT_EXIT;
                pit_exit_timer_ = 0.f;
                wheel_->reset();
                printf("[Bot] In pit box — driving out\n");
            }
            status_.active    = false;
            status_.speed_kph = speed_ms * 3.6f;
            break;

        // ── PIT_EXIT: gentle throttle until clear of pit lane ────────────────
        case BotState::PIT_EXIT:
            pit_exit_timer_ += dt;
            wheel_out = drivePitExit(speed_ms, dt);

            if (pit_exit_timer_ > 30.f
                || (!in_pit_lane && !in_pit && speed_ms > 8.f && pit_exit_timer_ > 4.f)) {
                state_ = BotState::ACTIVE;
                is_active_ = true;
                crash_depth_acc_ = 0.f;
                wheel_->reset();
                controller_->reset();
                if (own_car_mm_.valid)
                    last_collision_counter_ =
                        static_cast<const CarData*>(own_car_mm_.pView)->collision_counter;
                printf("[Bot] Clear of pit lane — ACTIVE\n");
            }
            status_.active    = false;
            status_.speed_kph = speed_ms * 3.6f;
            break;

        // ── DISABLED: idle ───────────────────────────────────────────────────
        case BotState::DISABLED:
        default:
            status_.active     = false;
            status_.speed_kph  = speed_ms * 3.6f;
            status_.target_kph = live_target_kph_.load();
            break;
        }

        // Calibration mode: sweep one axis only so AC controls can detect it
        int calib = calib_axis_.load();
        if (calib != 0 && vctrl_.available()) {
            float t = static_cast<float>(GetTickCount64()) / 1000.f;
            float sweep = std::sin(t * 1.5f); // ~0.25 Hz
            if      (calib == 1) vctrl_.update(sweep, 0.f, 0.f);  // steer only
            else if (calib == 2) vctrl_.update(0.f, (sweep+1.f)*0.5f, 0.f); // throttle only
            else if (calib == 3) vctrl_.update(0.f, 0.f, (sweep+1.f)*0.5f); // brake only
        } else if (!cfg_.dry_run && state_ != BotState::DISABLED) {
            writeControls(wheel_out);
        }

        status_.loop_dt_ms = static_cast<float>(timer.frameElapsed() * 1000.0);
        writeStatus();
        tickUDP();

        timer.endFrame();
    }
}

// ─── Planning loop (144 Hz, background thread) ────────────────────────────────
void Bot::planningLoop() {
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
    LoopTimer timer(cfg_.planning_hz);

    float px = 0, pz = 0, heading = 0, speed_ms = 0, spline_pos = 0;
    bool spline_dir_checked = false;

    while (running_) {
        timer.beginFrame();

        // Only run full planning when on-track and ACTIVE
        // (avoids garbage Frenet projections while car is in pit lane)
        if (!is_active_) { spline_dir_checked = false; timer.endFrame(); continue; }

        readOwnCar(px, pz, heading, speed_ms, spline_pos);

        // On first active frame: lock onto the nearest spline point whose heading
        // matches the car's (within 90°). The SRP spline is one long ribbon that
        // runs out one carriageway and back the other; the globally-nearest point
        // is often the OPPOSING lane (~12m away, 180° off). Filtering by heading
        // picks the lane going our way. We never reverse the whole spline — that
        // would break every other part of the loop.
        if (!spline_dir_checked) {
            bool wrong_way = false;
            int  idx = findStartIndex(px, pz, heading, wrong_way);

            FrenetState fs = spline_.project(px, pz, idx, 80);
            float herr_deg = (heading - fs.road_heading) * 57.2958f;
            while (herr_deg >  180.f) herr_deg -= 360.f;
            while (herr_deg < -180.f) herr_deg += 360.f;
            printf("[Bot] World pos x=%.1f z=%.1f car_hdg=%.0fdeg | "
                   "locked pt[%d]=(%.1f,%.1f) road_hdg=%.0fdeg d=%.1fm herr=%.0fdeg\n",
                   px, pz, heading * 57.2958f, fs.idx,
                   spline_.pts[fs.idx].x, spline_.pts[fs.idx].z,
                   fs.road_heading * 57.2958f, fs.d, herr_deg);
            if (wrong_way)
                printf("[Bot] WARNING: no racing line within 60m pointing your way.\n"
                       "[Bot]   You're likely facing AGAINST traffic — turn around,\n"
                       "[Bot]   face the normal flow of the highway, then press F5.\n");
            else if (std::abs(fs.d) > 20.f)
                printf("[Bot] NOTE: %.0fm from the line — it'll merge over before tracking.\n",
                       std::abs(fs.d));

            // Warm up both planner and control-loop hints
            planner_->updateEgo(px, pz, speed_ms, heading, fs.idx);
            {
                std::lock_guard<std::mutex> lk(plan_mutex_);
                latest_plan_.hint_idx = fs.idx;
            }
            spline_dir_checked = true;
        }

        // Update ego Frenet state
        planner_->updateEgo(px, pz, speed_ms, heading, planner_->hintIdx());

        // Read + project traffic
        auto traffic = readTraffic();

        // Adapt speed for tight gaps
        const float cur_target_kph  = live_target_kph_.load();
        const float cur_safety_m    = live_safety_margin_.load();
        float target_v = cur_target_kph / 3.6f;
        {
            const float ego_hw   = planner_->config().ego_half_w;
            const float ego_s    = planner_->egoS();
            const float track_len = spline_.total_length;
            for (auto& tc : traffic) {
                float rel_s = tc.s0 - ego_s;
                if (rel_s < 0.f) rel_s += track_len;  // wrap
                if (rel_s > 0.f && rel_s < 30.f) {
                    float lat = std::abs(planner_->egoD() - tc.d0) - ego_hw - tc.half_w;
                    if (lat < cur_safety_m + 1.f)
                        target_v = std::min(target_v, cfg_.min_kph / 3.6f + 10.f);
                }
            }
        }

        // Run planner
        LARGE_INTEGER t0; QueryPerformanceCounter(&t0);
        Trajectory traj = planner_->plan(traffic, target_v);
        LARGE_INTEGER t1; QueryPerformanceCounter(&t1);
        float plan_ms = static_cast<float>(
            (t1.QuadPart - t0.QuadPart) * 1000.0 / timer.freqQpc());

        if (plan_ms > 6.f) {
            static DWORD last_warn_ms = 0;
            DWORD now_ms = GetTickCount();
            if (now_ms - last_warn_ms > 1000) {
                printf("[Bot] Planning spike: %.1f ms\n", plan_ms);
                last_warn_ms = now_ms;
            }
        }

        // Detect actual passes: count only when ego transitions from behind to ahead of a car.
        // This avoids the ~100-200x overcount from scoring the same future pass every frame.
        {
            const auto& pc  = planner_->config();
            const float es  = planner_->egoS();
            const float ed  = planner_->egoD();
            const float track_len = spline_.total_length;
            for (auto& tc : traffic) {
                if (tc.car_idx < 0 || tc.car_idx >= 64) continue;
                // Circular comparison: ds > 0 means traffic rear is still ahead of ego
                float ds = (tc.s0 + tc.half_l) - es;
                if (ds >  track_len * 0.5f) ds -= track_len;
                if (ds < -track_len * 0.5f) ds += track_len;
                bool still_behind = (ds > 0.f);
                if (car_was_behind_[tc.car_idx] && !still_behind) {
                    float lat = std::abs(ed - tc.d0) - tc.half_w - pc.ego_half_w;
                    if      (lat > 0.f && lat <= pc.close_3x_m) ++passes_3x_total_;
                    else if (lat > 0.f && lat <= pc.close_1x_m) ++passes_1x_total_;
                }
                car_was_behind_[tc.car_idx] = still_behind;
            }
        }

        PlanOutput out;
        out.target_d   = traj.target_d;
        out.target_v   = std::max(cfg_.min_kph / 3.6f,
                                  std::min(cfg_.max_kph / 3.6f, traj.target_ds));
        out.ego_d      = planner_->egoD();
        out.plan_dt_ms = plan_ms;
        out.hint_idx   = planner_->hintIdx();

        { std::lock_guard<std::mutex> lk(plan_mutex_); latest_plan_ = out; }
        // plan_dt_ms is picked up by control thread via PlanOutput — no direct status_ write here

        timer.endFrame();
    }
}

// ─── Hotkey loop ──────────────────────────────────────────────────────────────
void Bot::hotkeyLoop() {
    // F5=toggle, F6=hum+, F7=hum-, F8=speed+, F9=speed-
    // Num7/8/9 = calib steer/throttle/brake (F10-F12 intercepted by system/debugger)
    RegisterHotKey(nullptr, 1, 0, VK_F5);
    RegisterHotKey(nullptr, 2, 0, VK_F6);
    RegisterHotKey(nullptr, 3, 0, VK_F7);
    RegisterHotKey(nullptr, 4, 0, VK_F8);
    RegisterHotKey(nullptr, 5, 0, VK_F9);
    RegisterHotKey(nullptr, 6, 0, VK_NUMPAD7);
    RegisterHotKey(nullptr, 7, 0, VK_NUMPAD8);
    RegisterHotKey(nullptr, 8, 0, VK_NUMPAD9);

    MSG msg;
    while (running_) {
        if (PeekMessage(&msg, nullptr, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)) {
            switch (msg.wParam) {
            case 1:
                enabled_ = !enabled_.load();
                printf("[Bot] hotkey %s\n", enabled_.load() ? "ENABLED" : "DISABLED");
                break;
            case 2:
                if (settings_mm_.valid) {
                    auto* s = static_cast<BotSettings*>(settings_mm_.pView);
                    s->humanization = std::min(1.f, s->humanization + 0.1f);
                    printf("[Bot] Humanization: %.1f\n", s->humanization);
                }
                break;
            case 3:
                if (settings_mm_.valid) {
                    auto* s = static_cast<BotSettings*>(settings_mm_.pView);
                    s->humanization = std::max(0.f, s->humanization - 0.1f);
                    printf("[Bot] Humanization: %.1f\n", s->humanization);
                }
                break;
            case 4:
                settings_.target_kph = std::min(220.f, settings_.target_kph + 10.f);
                cfg_.target_kph      = settings_.target_kph;
                live_target_kph_.store(settings_.target_kph);
                if (settings_mm_.valid)
                    static_cast<BotSettings*>(settings_mm_.pView)->target_kph = settings_.target_kph;
                printf("[Bot] Target: %.0f kph\n", settings_.target_kph);
                break;
            case 5:
                settings_.target_kph = std::max(80.f, settings_.target_kph - 10.f);
                cfg_.target_kph      = settings_.target_kph;
                live_target_kph_.store(settings_.target_kph);
                if (settings_mm_.valid)
                    static_cast<BotSettings*>(settings_mm_.pView)->target_kph = settings_.target_kph;
                printf("[Bot] Target: %.0f kph\n", settings_.target_kph);
                break;
            case 6: { // Num7 — calibrate steer
                int next = (calib_axis_.load() == 1) ? 0 : 1;
                calib_axis_.store(next);
                printf("[Bot] Calib %s — STEER sweeping. Click 'Steering' in AC controls now.\n",
                       next ? "ON" : "OFF");
                break;
            }
            case 7: { // Num8 — calibrate throttle
                int next = (calib_axis_.load() == 2) ? 0 : 2;
                calib_axis_.store(next);
                printf("[Bot] Calib %s — THROTTLE sweeping. Click 'Throttle' in AC controls now.\n",
                       next ? "ON" : "OFF");
                break;
            }
            case 8: { // Num9 — calibrate brake
                int next = (calib_axis_.load() == 3) ? 0 : 3;
                calib_axis_.store(next);
                printf("[Bot] Calib %s — BRAKE sweeping. Click 'Brakes' in AC controls now.\n",
                       next ? "ON" : "OFF");
                break;
            }
            }
        }
        Sleep(10);
    }

    UnregisterHotKey(nullptr, 1);
    UnregisterHotKey(nullptr, 2);
    UnregisterHotKey(nullptr, 3);
    UnregisterHotKey(nullptr, 4);
    UnregisterHotKey(nullptr, 5);
    UnregisterHotKey(nullptr, 6);
    UnregisterHotKey(nullptr, 7);
    UnregisterHotKey(nullptr, 8);
}

// ─── Spline start-index match (heading-filtered) ──────────────────────────────
int Bot::findStartIndex(float px, float pz, float heading, bool& wrong_way) const {
    int   N = spline_.size();
    float best_aligned_d2 = 1e30f; int best_aligned_i = -1;
    float best_any_d2     = 1e30f; int best_any_i     = 0;
    for (int i = 0; i < N; ++i) {
        float dx = spline_.pts[i].x - px;
        float dz = spline_.pts[i].z - pz;
        float d2 = dx*dx + dz*dz;
        if (d2 < best_any_d2) { best_any_d2 = d2; best_any_i = i; }
        float hd = heading - spline_.headings[i];
        while (hd >  3.14159f) hd -= 6.28318f;
        while (hd < -3.14159f) hd += 6.28318f;
        if (std::abs(hd) < 1.5708f && d2 < best_aligned_d2) {
            best_aligned_d2 = d2; best_aligned_i = i;
        }
    }
    if (best_aligned_i >= 0 && best_aligned_d2 < 60.f * 60.f) {
        wrong_way = false;
        return best_aligned_i;
    }
    wrong_way = true;     // nothing aligned within 60 m — car faces against traffic
    return best_any_i;
}

// ─── Own car read ──────────────────────────────────────────────────────────────
bool Bot::readOwnCar(float& px, float& pz, float& heading,
                      float& speed_ms, float& spline_pos) {
    // Preferred: live telemetry streamed from CSP Lua (works online).
    {
        std::lock_guard<std::mutex> lk(telem_mutex_);
        if (lua_ego_valid_) {
            float age = std::chrono::duration<float>(
                std::chrono::steady_clock::now() - lua_ego_time_).count();
            if (age < 0.5f) {
                px         = lua_ego_.x;
                pz         = lua_ego_.z;
                heading    = lua_ego_.heading;
                speed_ms   = lua_ego_.speed_kmh / 3.6f;
                spline_pos = 0.f;  // not provided by Lua feed; not required
                return true;
            }
        }
    }
    if (own_car_mm_.valid) {
        auto* d = static_cast<const CarData*>(own_car_mm_.pView);
        px = d->position.x; pz = d->position.z;
        heading  = std::atan2(d->look.x, d->look.z);
        speed_ms = d->speed_kmh / 3.6f;
        spline_pos = d->spline_position;
        return true;
    }
    if (physics_mm_.valid && graphics_mm_.valid) {
        auto* p = static_cast<const SPageFilePhysics*>(physics_mm_.pView);
        auto* g = static_cast<const SPageFileGraphic*>(graphics_mm_.pView);
        heading  = p->heading;
        speed_ms = p->speedKmh / 3.6f;
        spline_pos = g->normalizedCarPosition;
        // Find player's slot in carCoordinates by matching playerCarID
        int player_slot = -1;
        for (int i = 0; i < 60; ++i) {
            if (g->carID[i] == g->playerCarID) { player_slot = i; break; }
        }
        if (player_slot >= 0) {
            px = g->carCoordinates[player_slot * 3 + 0];
            pz = g->carCoordinates[player_slot * 3 + 2];
        } else {
            float est_s = spline_pos * spline_.total_length;
            spline_.frenetToWorld(est_s, 0.f, px, pz);
        }
        return true;
    }
    return false;
}

// ─── Traffic read (acpmf_graphics fallback) ────────────────────────────────────
std::vector<TrafficCar> Bot::readTrafficFromGraphics() {
    std::vector<TrafficCar> result;
    if (!graphics_mm_.valid) return result;
    auto* g = static_cast<const SPageFileGraphic*>(graphics_mm_.pView);
    int active = std::min(g->activeCars, 60);
    auto now = std::chrono::steady_clock::now();
    result.reserve(active);

    for (int i = 0; i < active; ++i) {
        int cid = g->carID[i];
        if (cid == g->playerCarID) continue;

        float tx = g->carCoordinates[i * 3 + 0];
        float tz = g->carCoordinates[i * 3 + 2];

        // Estimate velocity from previous frame position
        auto& trk = traffic_tracker_[cid];
        float vx = 0.f, vz = 0.f;
        if (trk.valid) {
            float dt = std::chrono::duration<float>(now - trk.last_t).count();
            if (dt > 0.001f && dt < 0.5f) {
                vx = (tx - trk.x) / dt;
                vz = (tz - trk.z) / dt;
            }
        }
        trk.x = tx; trk.z = tz;
        trk.vx = vx; trk.vz = vz;
        trk.valid = true;
        trk.last_t = now;

        FrenetState fs = spline_.project(tx, tz, planner_->hintIdx(), 100);
        float speed = std::sqrt(vx*vx + vz*vz);
        float car_h = (speed > 0.5f) ? std::atan2(vx, vz) : fs.road_heading;
        float herr  = car_h - fs.road_heading;
        while (herr >  3.14159f) herr -= 6.28318f;
        while (herr < -3.14159f) herr += 6.28318f;

        TrafficCar tc;
        tc.s0     = fs.s;
        tc.d0     = fs.d;
        tc.v_s    = speed * std::cos(herr);
        tc.v_d    = speed * std::sin(herr);
        tc.half_w = planner_->config().traffic_half_w;
        tc.half_l = planner_->config().traffic_half_l;
        tc.car_idx = cid % 64; // car_was_behind_ array is 64 wide
        result.push_back(tc);
    }
    return result;
}

// ─── Telemetry from CSP Lua ────────────────────────────────────────────────────
// Packet: "t|ex,ez,eheading,espeed|idx,x,z,heading,speed;idx,...;"
void Bot::parseTelemetry(const char* buf) {
    // buf points just past the leading "t|"
    LuaCar ego{};
    if (std::sscanf(buf, "%f,%f,%f,%f",
                    &ego.x, &ego.z, &ego.heading, &ego.speed_kmh) != 4)
        return;

    std::vector<LuaCar> traffic;
    const char* p = std::strchr(buf, '|');   // end of ego segment
    if (p) {
        ++p;  // first traffic record
        while (*p) {
            LuaCar c{};
            if (std::sscanf(p, "%d,%f,%f,%f,%f",
                            &c.idx, &c.x, &c.z, &c.heading, &c.speed_kmh) == 5)
                traffic.push_back(c);
            const char* semi = std::strchr(p, ';');
            if (!semi) break;
            p = semi + 1;
        }
    }

    std::lock_guard<std::mutex> lk(telem_mutex_);
    lua_ego_       = ego;
    lua_ego_valid_ = true;
    lua_ego_time_  = std::chrono::steady_clock::now();
    lua_traffic_   = std::move(traffic);
}

std::vector<TrafficCar> Bot::readTrafficFromLua() {
    std::vector<LuaCar> cars;
    {
        std::lock_guard<std::mutex> lk(telem_mutex_);
        cars = lua_traffic_;
    }

    std::vector<TrafficCar> result;
    result.reserve(cars.size());
    for (const auto& c : cars) {
        if (c.speed_kmh < 1.f) continue;
        FrenetState fs = spline_.project(c.x, c.z, planner_->hintIdx(), 100, c.heading);
        float herr = c.heading - fs.road_heading;
        while (herr >  3.14159f) herr -= 6.28318f;
        while (herr < -3.14159f) herr += 6.28318f;

        float speed_ms = c.speed_kmh / 3.6f;
        TrafficCar tc;
        tc.s0      = fs.s;
        tc.d0      = fs.d;
        tc.v_s     = speed_ms * std::cos(herr);
        tc.v_d     = speed_ms * std::sin(herr);
        tc.half_w  = planner_->config().traffic_half_w;
        tc.half_l  = planner_->config().traffic_half_l;
        tc.car_idx = c.idx % 64;  // car_was_behind_ array is 64 wide
        result.push_back(tc);
    }
    return result;
}

// ─── Traffic read ──────────────────────────────────────────────────────────────
std::vector<TrafficCar> Bot::readTraffic() {
    // Preferred: live telemetry from CSP Lua (online-capable, exact positions)
    bool lua_fresh = false;
    {
        std::lock_guard<std::mutex> lk(telem_mutex_);
        if (lua_ego_valid_) {
            float age = std::chrono::duration<float>(
                std::chrono::steady_clock::now() - lua_ego_time_).count();
            lua_fresh = (age < 0.5f);
        }
    }
    if (lua_fresh)
        return readTrafficFromLua();

    // Fallback: acpmf_graphics (when Lua overlay isn't connected)
    return readTrafficFromGraphics();
}

// ─── Pit helpers ──────────────────────────────────────────────────────────────
WheelOutput Bot::drivePitExit(float speed_ms, float dt) {
    // Target 40 kph through the pit lane — well under the 60 kph limit
    const float target_ms = 40.f / 3.6f;
    float err      = target_ms - speed_ms;
    float throttle = std::clamp(err * 0.6f, 0.f, 0.5f);
    float brake    = (speed_ms > target_ms + 2.f) ? 0.3f : 0.f;
    // Neutral steer (car is correctly oriented after teleport / normal pit spawn)
    return wheel_->process(0.f, throttle, brake, dt);
}

void Bot::triggerTeleportToPits() {
    teleport_pending_ = true;
}

// ─── Write controls ────────────────────────────────────────────────────────────
void Bot::writeControls(const WheelOutput& out) {
    // Virtual gamepad (online multiplayer — AC reads it as a real Xbox controller)
    if (vctrl_.available()) {
        vctrl_.update(out.steer, out.throttle, out.brake);
    }

    // CSP Custom AI mmap (offline / singleplayer AI car slots — fallback/legacy)
    if (own_ctrl_mm_.valid) {
        auto* ctrl = static_cast<CarControls*>(own_ctrl_mm_.pView);
        ctrl->gas              = std::clamp(out.throttle, 0.f, 1.f);
        ctrl->brake            = std::clamp(out.brake,    0.f, 1.f);
        ctrl->steer            = std::clamp(out.steer,   -1.f, 1.f);
        ctrl->clutch           = 0.f;
        ctrl->handbrake        = 0.f;
        ctrl->autoshift_active = true;
        ctrl->teleport_to      = teleport_pending_ ? static_cast<uint8_t>(1) : static_cast<uint8_t>(0);
        teleport_pending_      = false;
    } else {
        teleport_pending_ = false;
    }
}

// ─── Settings / Status ────────────────────────────────────────────────────────
void Bot::readSettings() {
    if (!settings_mm_.valid) return;
    auto* s = static_cast<const BotSettings*>(settings_mm_.pView);

    bool wheel_dirty = false;

    if (s->humanization >= 0.f && s->humanization <= 1.f &&
        std::abs(s->humanization - settings_.humanization) > 0.005f) {
        settings_.humanization = s->humanization;
        wheel_dirty = true;
    }
    if (s->smoothness >= 0.f && s->smoothness <= 1.f &&
        std::abs(s->smoothness - settings_.smoothness) > 0.005f) {
        settings_.smoothness = s->smoothness;
        wheel_dirty = true;
    }
    if (wheel_dirty)
        wheel_->setParams(settings_.smoothness, settings_.humanization);

    if (s->target_kph >= 80.f && s->target_kph <= 220.f &&
        std::abs(s->target_kph - settings_.target_kph) > 0.5f) {
        settings_.target_kph = s->target_kph;
        cfg_.target_kph      = s->target_kph;
        live_target_kph_.store(s->target_kph);
    }
    if (s->safety_margin_m >= 0.5f && s->safety_margin_m <= 5.f &&
        std::abs(s->safety_margin_m - settings_.safety_margin_m) > 0.01f) {
        settings_.safety_margin_m = s->safety_margin_m;
        cfg_.safety_margin        = s->safety_margin_m;
        live_safety_margin_.store(s->safety_margin_m);
        PlannerConfig pc = planner_->config();
        pc.safety_margin_m = s->safety_margin_m;
        planner_->setConfig(pc);
    }

    {
        PlannerConfig pc = planner_->config();
        bool pc_dirty = false;
        if (s->close_3x_m >= 1.f && s->close_3x_m <= 10.f &&
            std::abs(s->close_3x_m - pc.close_3x_m) > 0.05f) {
            pc.close_3x_m = s->close_3x_m; pc_dirty = true;
        }
        if (s->close_1x_m >= 1.f && s->close_1x_m <= 15.f &&
            std::abs(s->close_1x_m - pc.close_1x_m) > 0.05f) {
            pc.close_1x_m = s->close_1x_m; pc_dirty = true;
        }
        if (s->target_pass_dist_m >= 0.5f && s->target_pass_dist_m <= 8.f &&
            std::abs(s->target_pass_dist_m - pc.target_pass_dist) > 0.05f) {
            pc.target_pass_dist = s->target_pass_dist_m; pc_dirty = true;
        }
        if (pc_dirty)
            planner_->setConfig(pc);
    }
}

void Bot::writeStatus() {
    if (!status_mm_.valid) return;
    memcpy(status_mm_.pView, &status_, sizeof(BotStatus));
}

// ─── UDP for Lua overlay ──────────────────────────────────────────────────────
void Bot::initUDP() {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2,2), &wsa);
    udp_sock_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    u_long mode = 1;
    ioctlsocket(udp_sock_, FIONBIO, &mode); // non-blocking

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(static_cast<u_short>(cfg_.udp_port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    bind(udp_sock_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
}

void Bot::tickUDP() {
    if (udp_sock_ == INVALID_SOCKET) return;

    // Receive settings from Lua (non-blocking)
    // lua_from persists between calls so we always know where to reply
    static sockaddr_in lua_from{};
    static bool lua_connected = false;
    static int  lua_from_len  = sizeof(lua_from);

    // Drain all pending datagrams each tick (telemetry arrives every frame)
    char buf[8192];
    sockaddr_in recv_from{};
    int rflen = sizeof(recv_from);
    int n;
    while ((n = recvfrom(udp_sock_, buf, sizeof(buf)-1, 0,
                         reinterpret_cast<sockaddr*>(&recv_from), &rflen)) > 0) {
        lua_from      = recv_from;
        lua_from_len  = std::min(rflen, (int)sizeof(lua_from));
        lua_connected = true;
        buf[n] = '\0';

        // Telemetry feed: "t|ego|traffic..." — authoritative position source
        if (buf[0] == 't' && buf[1] == '|') {
            parseTelemetry(buf + 2);
            continue;
        }

        // Simple key=value protocol: "enabled=1", "hum=0.8", "speed=170"
        if (strncmp(buf, "enabled=", 8) == 0) {
            enabled_ = (buf[8] == '1');
        } else if (strncmp(buf, "hum=", 4) == 0) {
            float v = static_cast<float>(std::clamp(atof(buf+4), 0.0, 1.0));
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->humanization = v;
            // readSettings() picks this up next frame and calls wheel_->setParams()
        } else if (strncmp(buf, "smooth=", 7) == 0) {
            float v = static_cast<float>(std::clamp(atof(buf+7), 0.0, 1.0));
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->smoothness = v;
        } else if (strncmp(buf, "speed=", 6) == 0) {
            settings_.target_kph = static_cast<float>(std::clamp(atof(buf+6), 80.0, 220.0));
            cfg_.target_kph      = settings_.target_kph;
            live_target_kph_.store(settings_.target_kph);
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->target_kph = settings_.target_kph;
        } else if (strncmp(buf, "close3x=", 8) == 0) {
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->close_3x_m =
                    static_cast<float>(std::clamp(atof(buf+8), 1.0, 10.0));
        } else if (strncmp(buf, "close1x=", 8) == 0) {
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->close_1x_m =
                    static_cast<float>(std::clamp(atof(buf+8), 1.0, 15.0));
        } else if (strncmp(buf, "passdist=", 9) == 0) {
            if (settings_mm_.valid)
                static_cast<BotSettings*>(settings_mm_.pView)->target_pass_dist_m =
                    static_cast<float>(std::clamp(atof(buf+9), 0.5, 8.0));
        }
    }

    // Send status to Lua every ~200ms (every ~67 control frames at 333 Hz)
    static int tick = 0;
    if (++tick % 67 == 0 && lua_connected) {
        char msg[256];
        snprintf(msg, sizeof(msg),
                 "speed=%.1f|target=%.1f|d=%.2f|3x=%d|1x=%d|on=%d",
                 status_.speed_kph, status_.target_kph,
                 status_.target_d,  status_.passes_3x,
                 status_.passes_1x, status_.active ? 1 : 0);
        sendto(udp_sock_, msg, static_cast<int>(strlen(msg)), 0,
               reinterpret_cast<const sockaddr*>(&lua_from), lua_from_len);
    }
}

// ─── Logger ───────────────────────────────────────────────────────────────────
void Bot::openLogFile() {
    CreateDirectoryA(cfg_.log_dir.c_str(), nullptr);
    time_t now = time(nullptr);
    char path[256];
    snprintf(path, sizeof(path), "%s/session_%lld.jsonl",
             cfg_.log_dir.c_str(), static_cast<long long>(now));
    log_file_.open(path, std::ios::app);
    if (log_file_)
        printf("[Bot] Logging to %s\n", path);
}

void Bot::logFrame(const WheelOutput& out, const ControlDemand& raw, float dt_ms) {
    if (!log_file_) return;
    static int frame = 0;
    if (++frame % 10 != 0) return; // ~33 Hz log rate

    std::lock_guard<std::mutex> lk(log_mutex_);
    char buf[512];
    snprintf(buf, sizeof(buf),
             "{\"t\":%.3f,\"spd\":%.1f,\"d\":%.3f,\"steer\":%.4f,"
             "\"thr\":%.3f,\"brk\":%.3f,\"cte\":%.3f,\"dt\":%.2f}\n",
             static_cast<float>(GetTickCount64()) / 1000.f,
             status_.speed_kph,
             status_.ego_d,   // read from status (set by control thread, no race)
             out.steer, out.throttle, out.brake,
             raw.cte, dt_ms);
    log_file_ << buf;
}

