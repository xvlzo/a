#pragma once
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <winsock2.h>  // must precede windows.h to avoid redefinition errors
#include <windows.h>
#include <atomic>
#include <thread>
#include <mutex>
#include <vector>
#include <string>
#include <fstream>
#include <chrono>
#include <unordered_map>

#include "csp_structs.h"
#include "ac_structs.h"
#include "spline.h"
#include "planner.h"
#include "controller.h"
#include "wheel_model.h"
#include "virtual_controller.h"

// ── Mmap handle wrapper ───────────────────────────────────────────────────────
struct MmapHandle {
    HANDLE hMap  = INVALID_HANDLE_VALUE;
    void*  pView = nullptr;
    bool   valid = false;

    bool openRead(const char* name, size_t size);
    bool openReadWrite(const char* name, size_t size);
    void close();
    ~MmapHandle() { close(); }
};

// ── Per-traffic-car runtime state ─────────────────────────────────────────────
struct TrafficSlot {
    MmapHandle mmap;
    bool       active = false;
    int        idx    = 0;
};

// ── Planner output (atomic handoff between planning and control threads) ─────
struct PlanOutput {
    float target_d   = 0.f;
    float target_v   = 44.f; // m/s
    float ego_d      = 0.f;  // ego Frenet d (for status/logging)
    float plan_dt_ms = 0.f;
};

// ── Bot state machine ────────────────────────────────────────────────────────
enum class BotState {
    DISABLED,   // F5 off — no output
    ACTIVE,     // full planner + controller
    CRASHED,    // braking, then teleporting to pits
    PIT_EXIT    // driving out of pit lane onto track
};

// ── Main bot class ─────────────────────────────────────────────────────────────
class Bot {
public:
    struct Config {
        std::string fast_lane_path = "data/fast_lane.ai";
        int    own_car_index  = 0;
        int    max_traffic    = 64;
        float  control_hz     = 333.f;
        float  planning_hz    = 144.f;
        float  target_kph     = 160.f;
        float  min_kph        = 80.f;
        float  max_kph        = 220.f;
        float  safety_margin  = 1.2f;
        float  humanization   = 0.7f;
        float  smoothness     = 0.6f;
        bool   dry_run        = false;
        std::string log_dir   = "logs";
        int    udp_port       = 27015; // Lua overlay comms
    };

    explicit Bot(const Config& cfg);
    ~Bot();

    bool init();
    void run();    // blocks until stop() called
    void stop();

    int totalPasses3x() const { return passes_3x_total_.load(); }
    int totalPasses1x() const { return passes_1x_total_.load(); }

private:
    Config cfg_;
    Spline spline_;

    // Mmaps
    MmapHandle own_car_mm_;     // Car<N>.v0
    MmapHandle own_ctrl_mm_;    // CarControls<N>.v0
    MmapHandle physics_mm_;     // acpmf_physics (fallback)
    MmapHandle graphics_mm_;    // acpmf_graphics (traffic fallback)
    MmapHandle settings_mm_;    // BotSettings.v0 (from Lua overlay)
    MmapHandle status_mm_;      // BotStatus.v0   (to Lua overlay)

    std::vector<TrafficSlot> traffic_slots_;

    // Settings (live, updated from mmap)
    BotSettings settings_{};
    BotStatus   status_{};

    // Planner + controller
    std::unique_ptr<FrenetPlanner>    planner_;
    std::unique_ptr<StanleyController> controller_;
    std::unique_ptr<WheelModel>        wheel_;
    VirtualController                  vctrl_;   // virtual Xbox gamepad for online multiplayer

    // Atomic plan handoff (planning → control)
    std::mutex         plan_mutex_;
    PlanOutput         latest_plan_{};

    // State
    std::atomic<bool>  running_{ false };
    std::atomic<bool>  is_active_{ false };
    std::atomic<bool>  enabled_{ false };
    std::atomic<int>   calib_axis_{ 0 };  // 0=off 1=steer 2=throttle 3=brake (F10/F11/F12)
    std::atomic<float> live_target_kph_{ 160.f }; // written by any thread, read by planningLoop
    std::atomic<float> live_safety_margin_{ 1.2f };
    BotState state_              = BotState::DISABLED;
    float    crashed_timer_      = 0.f;
    float    pit_exit_timer_     = 0.f;
    bool     teleport_pending_   = false;
    bool     teleport_sent_      = false; // one-shot guard: only send teleport once per CRASHED entry
    uint32_t last_collision_counter_ = 0;
    float    crash_depth_acc_    = 0.f;  // accumulated collision depth — decays when clear
    std::thread        plan_thread_;
    std::thread        hotkey_thread_;

    // Logger
    std::ofstream      log_file_;
    std::mutex         log_mutex_;
    std::atomic<int>   passes_3x_total_{ 0 };
    std::atomic<int>   passes_1x_total_{ 0 };
    bool car_was_behind_[64]{};  // per-car_idx: true = ego hasn't yet passed this car

    // Velocity tracker for acpmf_graphics traffic fallback
    struct TrafficTrackEntry {
        float x = 0, z = 0, vx = 0, vz = 0;
        bool  valid = false;
        std::chrono::steady_clock::time_point last_t;
    };
    std::unordered_map<int, TrafficTrackEntry> traffic_tracker_;

    // ── Live telemetry from CSP Lua (authoritative position feed, online-capable) ─
    // CSP Lua streams the player + traffic world positions over UDP because the
    // C++ side cannot read them from shared memory in online multiplayer.
    struct LuaCar { int idx = 0; float x = 0, z = 0, heading = 0, speed_kmh = 0; };
    std::mutex                          telem_mutex_;
    LuaCar                              lua_ego_{};
    bool                               lua_ego_valid_ = false;
    std::chrono::steady_clock::time_point lua_ego_time_;
    std::vector<LuaCar>                 lua_traffic_;
    void parseTelemetry(const char* buf);
    std::vector<TrafficCar> readTrafficFromLua();

    // UDP socket for Lua overlay
    SOCKET udp_sock_ = INVALID_SOCKET;

    // ── Thread entry points ──────────────────────────────────────────────────
    void controlLoop();   // 333 Hz, own thread
    void planningLoop();  // 144 Hz, plan_thread_
    void hotkeyLoop();    // polls hotkeys, hotkey_thread_

    // ── Helpers ──────────────────────────────────────────────────────────────
    bool openMmaps();
    bool openTrafficMmaps();
    void retryTrafficSlots();   // re-scan for any mmaps that weren't ready at init
    void verifyMmaps(); // polls packet_id to confirm CSP is writing, sanity-checks fields
    void readSettings();
    void writeStatus();
    void logFrame(const WheelOutput& out, const ControlDemand& raw, float dt_ms);
    void initUDP();
    void tickUDP();
    void openLogFile();

    // Read own car state (prefer CSP Car.v0, fallback acpmf_physics)
    bool readOwnCar(float& px, float& pz, float& heading,
                    float& speed_ms, float& spline_pos);

    // Read + project traffic into Frenet
    std::vector<TrafficCar> readTraffic();
    std::vector<TrafficCar> readTrafficFromGraphics(); // fallback when CSP mmaps unavailable

    // Write control output (CarControls mmap); clears teleport_pending_ after one frame
    void writeControls(const WheelOutput& out);

    // Drive pit lane exit — gentle throttle, neutral steer until clear
    WheelOutput drivePitExit(float speed_ms, float dt);

    // Set teleport_to=1 for one frame via writeControls
    void triggerTeleportToPits();
};
