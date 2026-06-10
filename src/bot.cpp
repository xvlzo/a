// winsock2.h is already included transitively via bot.h (before windows.h)
#include "bot.h"
#include "timer.h"
#include <cstdio>
#include <cstring>
#include <ctime>
#include <algorithm>
#include <sstream>
#include <iomanip>
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

    PlannerConfig pc;
    pc.target_kph      = cfg_.target_kph;
    pc.min_kph         = cfg_.min_kph;
    pc.max_kph         = cfg_.max_kph;
    pc.safety_margin_m = cfg_.safety_margin;
    planner_->setConfig(pc);

    wheel_->setParams(cfg_.smoothness, cfg_.humanization);

    // Default settings
    settings_.enabled            = false;
    settings_.humanization       = cfg_.humanization;
    settings_.smoothness         = cfg_.smoothness;
    settings_.target_kph         = cfg_.target_kph;
    settings_.safety_margin_m    = cfg_.safety_margin;
    settings_.close_pass_aggressive = true;

    if (!openMmaps()) return false;
    openLogFile();
    initUDP();

    printf("[Bot] Init OK. Press F5 to enable. F6/F7 = humanization. F8/F9 = speed.\n");
    return true;
}

bool Bot::openMmaps() {
    char name[256];

    // Own car state (CSP)
    snprintf(name, sizeof(name),
             "AcTools.CSP.NewBehaviour.CustomAI.Car%d.v0", cfg_.own_car_index);
    if (own_car_mm_.openRead(name, sizeof(CarData)))
        printf("[Bot] Car%d.v0 opened\n", cfg_.own_car_index);
    else
        printf("[Bot] Car%d.v0 not found, using acpmf_physics fallback\n", cfg_.own_car_index);

    // Own car controls (CSP) — we create this
    snprintf(name, sizeof(name),
             "AcTools.CSP.NewBehaviour.CustomAI.CarControls%d.v0", cfg_.own_car_index);
    if (own_ctrl_mm_.openReadWrite(name, sizeof(CarControls)))
        printf("[Bot] CarControls%d.v0 created\n", cfg_.own_car_index);
    else
        printf("[Bot] WARNING: CarControls%d.v0 open failed — CSP Custom AI not enabled?\n",
               cfg_.own_car_index);

    // AC shared memory fallback
    physics_mm_.openRead("Local\\acpmf_physics", sizeof(SPageFilePhysics));
    graphics_mm_.openRead("Local\\acpmf_graphics", sizeof(SPageFileGraphic));

    // Settings mmap (Lua writes, we read)
    settings_mm_.openReadWrite("NohesiBot.Settings.v0", sizeof(BotSettings));

    // Status mmap (we write, Lua reads)
    status_mm_.openReadWrite("NohesiBot.Status.v0", sizeof(BotStatus));

    // Open as many traffic slots as possible
    openTrafficMmaps();

    return true;
}

bool Bot::openTrafficMmaps() {
    int opened = 0;
    char name[256];
    for (int i = 0; i < cfg_.max_traffic; ++i) {
        if (i == cfg_.own_car_index) continue;
        snprintf(name, sizeof(name),
                 "AcTools.CSP.NewBehaviour.CustomAI.CarPublic%d.v0", i);
        auto& slot = traffic_slots_.emplace_back();
        slot.idx = i;
        if (slot.mmap.openRead(name, sizeof(CarPublicData))) {
            slot.active = true;
            ++opened;
        }
    }
    printf("[Bot] Traffic slots: %d opened\n", opened);
    return opened > 0;
}

// ─── Run ──────────────────────────────────────────────────────────────────────
void Bot::run() {
    running_ = true;
    plan_thread_   = std::thread(&Bot::planningLoop, this);
    hotkey_thread_ = std::thread(&Bot::hotkeyLoop,   this);
    controlLoop(); // main thread
    plan_thread_.join();
    hotkey_thread_.join();
}

void Bot::stop() {
    running_ = false;
    if (plan_thread_.joinable())   plan_thread_.join();
    if (hotkey_thread_.joinable()) hotkey_thread_.join();
    if (!cfg_.dry_run && own_ctrl_mm_.valid)
        writeControls({ 0.f, 0.f, 0.f }); // release
}

// ─── Control loop (333 Hz, main thread) ──────────────────────────────────────
void Bot::controlLoop() {
    setRealtimePriority();
    LoopTimer timer(cfg_.control_hz);

    float px = 0, pz = 0, heading = 0, speed_ms = 0, spline_pos = 0;

    while (running_) {
        timer.beginFrame();

        // Read settings from mmap (Lua may have updated them)
        readSettings();

        // Read own state
        readOwnCar(px, pz, heading, speed_ms, spline_pos);

        // Get latest plan
        PlanOutput plan;
        { std::lock_guard<std::mutex> lk(plan_mutex_); plan = latest_plan_; }

        WheelOutput wheel_out{};

        if (settings_.enabled) {
            float target_v = plan.target_v;
            float target_d = plan.target_d;

            // Compute raw control demand (Stanley + PID)
            ControlDemand raw = controller_->update(
                px, pz, heading, speed_ms,
                target_d, target_v, 1.f / cfg_.control_hz,
                controller_->lastHintIdx());

            // Humanize through wheel model
            wheel_out = wheel_->process(raw.steer, raw.throttle, raw.brake,
                                        1.f / cfg_.control_hz);

            if (!cfg_.dry_run)
                writeControls(wheel_out);

            // Log at ~33 Hz
            float frame_ms = static_cast<float>(timer.frameElapsed() * 1000.0);
            logFrame(wheel_out, raw, frame_ms);

            // Update status (all writes from control thread — no data race)
            status_.active          = true;
            status_.speed_kph       = speed_ms * 3.6f;
            status_.target_kph      = target_v * 3.6f;
            status_.target_d        = target_d;
            status_.ego_d           = plan.ego_d;   // from plan handoff, not planner directly
            status_.cross_track_err = raw.cte;
            status_.passes_3x       = passes_3x_total_.load();
            status_.passes_1x       = passes_1x_total_.load();
            status_.plan_dt_ms      = plan.plan_dt_ms;
        } else {
            status_.active = false;
            status_.speed_kph = speed_ms * 3.6f;
            wheel_->reset();
            controller_->reset();
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

    while (running_) {
        timer.beginFrame();

        if (!settings_.enabled) { timer.endFrame(); continue; }

        float px = 0, pz = 0, heading = 0, speed_ms = 0, spline_pos = 0;
        readOwnCar(px, pz, heading, speed_ms, spline_pos);

        // Update ego Frenet state
        planner_->updateEgo(px, pz, speed_ms, heading, planner_->hintIdx());

        // Read + project traffic
        auto traffic = readTraffic();

        // Collision detection — if hit, reset accumulated score
        if (collisionDetected(traffic, planner_->egoD())) {
            passes_3x_total_.store(0);
            passes_1x_total_.store(0);
            printf("[Bot] Collision detected — score reset!\n");
        }

        // Adapt speed for tight gaps
        float target_v = cfg_.target_kph / 3.6f;
        for (auto& tc : traffic) {
            float rel_s = tc.s0 - planner_->egoS();
            if (rel_s > 0.f && rel_s < 30.f) {
                float lat = std::abs(planner_->egoD() - tc.d0) - 0.95f - tc.half_w;
                if (lat < cfg_.safety_margin + 1.f)
                    target_v = std::min(target_v, cfg_.min_kph / 3.6f + 10.f);
            }
        }

        // Run planner
        LARGE_INTEGER t0; QueryPerformanceCounter(&t0);
        Trajectory traj = planner_->plan(traffic, target_v);
        LARGE_INTEGER t1; QueryPerformanceCounter(&t1);
        float plan_ms = static_cast<float>(
            (t1.QuadPart - t0.QuadPart) * 1000.0 / timer.freqQpc());

        // Commit pass counts (atomic — safe across threads)
        passes_3x_total_ += traj.close_3x;
        passes_1x_total_ += traj.close_1x;

        PlanOutput out;
        out.target_d   = traj.target_d;
        out.target_v   = std::max(cfg_.min_kph / 3.6f,
                                  std::min(cfg_.max_kph / 3.6f, traj.target_ds));
        out.ego_s      = planner_->egoS();
        out.ego_d      = planner_->egoD();
        out.close_3x   = traj.close_3x;
        out.close_1x   = traj.close_1x;
        out.plan_dt_ms = plan_ms;

        { std::lock_guard<std::mutex> lk(plan_mutex_); latest_plan_ = out; }
        // plan_dt_ms is picked up by control thread via PlanOutput — no direct status_ write here

        timer.endFrame();
    }
}

// ─── Hotkey loop ──────────────────────────────────────────────────────────────
void Bot::hotkeyLoop() {
    // F5=toggle, F6=hum+, F7=hum-, F8=speed+, F9=speed-
    RegisterHotKey(nullptr, 1, 0, VK_F5);
    RegisterHotKey(nullptr, 2, 0, VK_F6);
    RegisterHotKey(nullptr, 3, 0, VK_F7);
    RegisterHotKey(nullptr, 4, 0, VK_F8);
    RegisterHotKey(nullptr, 5, 0, VK_F9);

    MSG msg;
    while (running_) {
        if (PeekMessage(&msg, nullptr, WM_HOTKEY, WM_HOTKEY, PM_REMOVE)) {
            switch (msg.wParam) {
            case 1:
                settings_.enabled = !settings_.enabled;
                if (settings_.enabled) {
                    wheel_->reset();
                    controller_->reset();
                }
                printf("[Bot] %s\n", settings_.enabled ? "ENABLED" : "DISABLED");
                break;
            case 2:
                settings_.humanization = std::min(1.f, settings_.humanization + 0.1f);
                wheel_->setParams(settings_.smoothness, settings_.humanization);
                printf("[Bot] Humanization: %.1f\n", settings_.humanization);
                break;
            case 3:
                settings_.humanization = std::max(0.f, settings_.humanization - 0.1f);
                wheel_->setParams(settings_.smoothness, settings_.humanization);
                printf("[Bot] Humanization: %.1f\n", settings_.humanization);
                break;
            case 4:
                settings_.target_kph = std::min(220.f, settings_.target_kph + 10.f);
                printf("[Bot] Target: %.0f kph\n", settings_.target_kph);
                break;
            case 5:
                settings_.target_kph = std::max(80.f, settings_.target_kph - 10.f);
                printf("[Bot] Target: %.0f kph\n", settings_.target_kph);
                break;
            }
        }
        Sleep(10);
    }

    UnregisterHotKey(nullptr, 1);
    UnregisterHotKey(nullptr, 2);
    UnregisterHotKey(nullptr, 3);
    UnregisterHotKey(nullptr, 4);
    UnregisterHotKey(nullptr, 5);
}

// ─── Own car read ──────────────────────────────────────────────────────────────
bool Bot::readOwnCar(float& px, float& pz, float& heading,
                      float& speed_ms, float& spline_pos) {
    if (own_car_mm_.valid) {
        auto* d = static_cast<const CarData*>(own_car_mm_.pView);
        px = d->position.x; pz = d->position.z;
        heading  = std::atan2(d->look.x, d->look.z);
        speed_ms = d->speed_kmh / 3.6f;
        spline_pos = d->spline_position;
        return true;
    }
    if (physics_mm_.valid) {
        auto* p = static_cast<const SPageFilePhysics*>(physics_mm_.pView);
        heading  = p->heading;
        speed_ms = p->speedKmh / 3.6f;
        // Estimate world position from normalizedCarPosition + spline
        if (graphics_mm_.valid) {
            auto* g = static_cast<const SPageFileGraphic*>(graphics_mm_.pView);
            float norm = g->normalizedCarPosition;
            float est_s = norm * spline_.total_length;
            spline_.frenetToWorld(est_s, 0.f, px, pz);
            spline_pos = norm;
        }
        return true;
    }
    return false;
}

// ─── Traffic read ──────────────────────────────────────────────────────────────
std::vector<TrafficCar> Bot::readTraffic() {
    std::vector<TrafficCar> result;
    result.reserve(traffic_slots_.size());

    float ego_s = planner_->egoS();

    for (auto& slot : traffic_slots_) {
        if (!slot.mmap.valid) continue;
        auto* pd = static_cast<const CarPublicData*>(slot.mmap.pView);
        if (pd->speed_kmh < 1.f) continue; // inactive / parked

        float tx = pd->position.x, tz = pd->position.z;
        FrenetState fs = spline_.project(tx, tz, planner_->hintIdx(), 100);

        float heading = std::atan2(pd->look.x, pd->look.z);
        float road_h  = fs.road_heading;
        float herr    = heading - road_h;
        while (herr >  3.14159f) herr -= 6.28318f;
        while (herr < -3.14159f) herr += 6.28318f;

        float speed_ms = pd->speed_kmh / 3.6f;
        TrafficCar tc;
        tc.s0 = fs.s;
        tc.d0 = fs.d;
        tc.v_s = speed_ms * std::cos(herr);
        tc.v_d = speed_ms * std::sin(herr);
        tc.half_w  = 0.9f;   // ~1.8m wide sedan
        tc.half_l  = 2.3f;   // ~4.6m long sedan
        tc.car_idx = slot.idx;

        result.push_back(tc);
    }
    return result;
}

// ─── Write controls ────────────────────────────────────────────────────────────
void Bot::writeControls(const WheelOutput& out) {
    if (!own_ctrl_mm_.valid) return;
    auto* ctrl = static_cast<CarControls*>(own_ctrl_mm_.pView);
    ctrl->gas          = std::clamp(out.throttle, 0.f, 1.f);
    ctrl->brake        = std::clamp(out.brake,    0.f, 1.f);
    ctrl->steer        = std::clamp(out.steer,   -1.f, 1.f);
    ctrl->clutch       = 0.f;
    ctrl->handbrake    = 0.f;
    ctrl->autoshift_active = true;
}

// ─── Settings / Status ────────────────────────────────────────────────────────
void Bot::readSettings() {
    if (!settings_mm_.valid) return;
    auto* s = static_cast<const BotSettings*>(settings_mm_.pView);
    // Only pull from Lua if it actually changed something meaningful
    if (s->humanization >= 0.f && s->humanization <= 1.f)
        settings_.humanization = s->humanization;
    if (s->smoothness >= 0.f && s->smoothness <= 1.f)
        settings_.smoothness   = s->smoothness;
    if (s->target_kph >= 80.f && s->target_kph <= 220.f)
        settings_.target_kph   = s->target_kph;
    if (s->safety_margin_m >= 0.5f && s->safety_margin_m <= 5.f)
        settings_.safety_margin_m = s->safety_margin_m;

    wheel_->setParams(settings_.smoothness, settings_.humanization);
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

    char buf[256];
    sockaddr_in recv_from{};
    int rflen = sizeof(recv_from);
    int n = recvfrom(udp_sock_, buf, sizeof(buf)-1, 0,
                     reinterpret_cast<sockaddr*>(&recv_from), &rflen);
    if (n > 0) {
        lua_from      = recv_from;
        lua_from_len  = rflen;
        lua_connected = true;
        buf[n] = '\0';
        // Simple key=value protocol: "enabled=1", "hum=0.8", "speed=170"
        if (strncmp(buf, "enabled=", 8) == 0)
            settings_.enabled = buf[8] == '1';
        else if (strncmp(buf, "hum=", 4) == 0)
            settings_.humanization = static_cast<float>(std::clamp(atof(buf+4), 0.0, 1.0));
        else if (strncmp(buf, "smooth=", 7) == 0)
            settings_.smoothness = static_cast<float>(std::clamp(atof(buf+7), 0.0, 1.0));
        else if (strncmp(buf, "speed=", 6) == 0)
            settings_.target_kph = static_cast<float>(std::clamp(atof(buf+6), 80.0, 220.0));
        wheel_->setParams(settings_.smoothness, settings_.humanization);
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

bool Bot::collisionDetected(const std::vector<TrafficCar>& traffic, float ego_d) {
    float ego_s = planner_->egoS();
    for (auto& tc : traffic) {
        float ds = std::abs(tc.s0 - ego_s);
        if (ds < (tc.half_l + 2.3f) * 1.5f) {
            float dd = std::abs(tc.d0 - ego_d);
            if (dd < tc.half_w + 0.95f + 0.1f) return true;
        }
    }
    return false;
}
