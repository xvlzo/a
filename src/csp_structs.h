#pragma once
#include <cstdint>

// Exact byte layouts confirmed from gro-ove's official C# gist:
// https://gist.github.com/gro-ove/11489f32b3eb3c9e3df1e7819bb3008e

#pragma pack(push, 1)

struct Vec3 {
    float x, y, z;
};

// ── CarControls<N>.v0  (72 bytes) — YOU create & write ────────────────────
// steer: normalised [-1, 1]  (NOT degrees)
struct CarControls {
    float  gas;                   // 0
    float  brake;                 // 4
    float  clutch;                // 8
    float  steer;                 // 12
    float  handbrake;             // 16
    bool   gear_up;               // 20
    bool   gear_dn;               // 21
    bool   drs;                   // 22
    bool   kers;                  // 23
    bool   brake_balance_up;      // 24
    bool   brake_balance_dn;      // 25
    bool   abs_up;                // 26
    bool   abs_dn;                // 27
    bool   tc_up;                 // 28
    bool   tc_dn;                 // 29
    bool   turbo_up;              // 30
    bool   turbo_dn;              // 31
    bool   engine_brake_up;       // 32
    bool   engine_brake_dn;       // 33
    bool   mguk_delivery_up;      // 34
    bool   mguk_delivery_dn;      // 35
    bool   mguk_recovery_up;      // 36
    bool   mguk_recovery_dn;      // 37
    uint8_t mguh_mode;            // 38
    bool   headlights;            // 39
    uint8_t teleport_to;          // 40
    bool   autoclutch_on_start;   // 41
    bool   autoclutch_on_change;  // 42
    bool   autoblip_active;       // 43
    uint8_t _pad;                 // 43 → align to 44
    Vec3   teleport_pos;          // 44
    Vec3   teleport_dir;          // 56
    bool   autoshift_active;      // 68
    uint8_t _pad2[3];             // 69-71
};
static_assert(sizeof(CarControls) == 72, "CarControls must be 72 bytes");

// ── WheelData (120 bytes × 4 = 480 bytes inside CarData) ─────────────────
struct WheelData {
    Vec3   position;              // 0
    Vec3   contact_point;         // 12
    Vec3   contact_normal;        // 24
    Vec3   look;                  // 36
    Vec3   side;                  // 48
    Vec3   velocity;              // 60
    float  slip_ratio;            // 72
    float  load;                  // 76
    float  pressure;              // 80
    float  angular_velocity;      // 84
    float  wear;                  // 88
    float  dirty_level;           // 92
    float  core_temperature;      // 96
    float  camber_rad;            // 100
    float  disc_temperature;      // 104
    float  slip;                  // 108
    float  slip_angle_deg;        // 112
    float  nd_slip;               // 116
};
static_assert(sizeof(WheelData) == 120, "WheelData must be 120 bytes");

// ── CarData<N>.v0  (672 bytes) — CSP creates, you read at 333 Hz ──────────
// steer here is in DEGREES (not normalised like CarControls.steer)
struct CarData {
    int32_t packet_id;            // 0
    float   gas;                  // 4
    float   brake;                // 8
    float   clutch;               // 12
    float   steer;                // 16  (degrees)
    float   handbrake;            // 20
    float   fuel;                 // 24
    int32_t gear;                 // 28
    float   rpm;                  // 32
    float   speed_kmh;            // 36
    Vec3    velocity;             // 40
    Vec3    acc_g;                // 52
    Vec3    look;                 // 64
    Vec3    up;                   // 76
    Vec3    position;             // 88
    Vec3    local_velocity;       // 100
    Vec3    local_angular_vel;    // 112
    float   cg_height;            // 124
    float   car_damage[5];        // 128
    WheelData wheels[4];          // 148   (480 bytes)
    float   turbo_boost;          // 628
    float   final_ff;             // 632
    float   final_pure_ff;        // 636
    bool    pit_limiter;          // 640
    bool    abs_in_action;        // 641
    bool    traction_control;     // 642
    uint8_t _pad;                 // 643
    uint32_t lap_time_ms;         // 644
    uint32_t best_lap_time_ms;    // 648
    float   drivetrain_torque;    // 652
    float   spline_position;      // 656
    float   collision_depth;      // 660
    uint32_t collision_counter;   // 664
    uint32_t wheels_valid_surface;// 668
};
static_assert(sizeof(CarData) == 672, "CarData must be 672 bytes");

// ── CarPublicData<N>.v0  (~104 bytes) — CSP creates, read at 60 Hz ────────
struct CarPublicData {
    int32_t packet_id;            // 0
    float   steer;                // 4
    float   rpm;                  // 8
    float   spline_pos;           // 12
    float   speed_kmh;            // 16
    Vec3    velocity;             // 20
    Vec3    acc_g;                // 32
    Vec3    look;                 // 44
    Vec3    up;                   // 56
    Vec3    position;             // 68
    float   car_damage[5];        // 80
    bool    is_braking;           // 100
    uint8_t _pad[3];              // 101
};
static_assert(sizeof(CarPublicData) == 104, "CarPublicData must be 104 bytes");

// ── BotSettings  — custom mmap shared with Lua overlay ────────────────────
struct BotSettings {
    bool    enabled;              // 0
    uint8_t _pad[3];
    float   humanization;         // 4   0.0–1.0
    float   smoothness;           // 8   0.0–1.0
    float   target_kph;           // 12
    float   safety_margin_m;      // 16
    bool    close_pass_aggressive;// 20
    uint8_t _pad2[3];
};

// ── BotStatus  — written by bot, read by Lua overlay ──────────────────────
struct BotStatus {
    bool    active;
    uint8_t _pad[3];
    float   speed_kph;
    float   target_kph;
    float   ego_d;
    float   target_d;
    float   cross_track_err;
    int32_t passes_3x;
    int32_t passes_1x;
    float   loop_dt_ms;
    float   plan_dt_ms;
};

#pragma pack(pop)
