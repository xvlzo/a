#include "virtual_controller.h"
#include <cstdio>
#include <cmath>
#include <algorithm>

VirtualController::~VirtualController() { shutdown(); }

bool VirtualController::init() {
    // vJoy installs vJoyInterface.dll to System32 — no path needed
    hLib_ = LoadLibraryA("vJoyInterface.dll");
    if (!hLib_) {
        printf("[VCtrl] vJoyInterface.dll not found.\n");
        printf("[VCtrl]   Install vJoy: https://github.com/jshafer817/vJoy/releases\n");
        printf("[VCtrl]   Then run vJoyConf: enable Device 1, axes X + Y + Z\n");
        return false;
    }

    fn_acquire_    = reinterpret_cast<bool(*)(UINT)>         (GetProcAddress(hLib_, "AcquireVJD"));
    fn_relinquish_ = reinterpret_cast<void(*)(UINT)>         (GetProcAddress(hLib_, "RelinquishVJD"));
    fn_set_axis_   = reinterpret_cast<bool(*)(LONG,UINT,UINT)>(GetProcAddress(hLib_, "SetAxis"));
    fn_status_     = reinterpret_cast<int(*)(UINT)>          (GetProcAddress(hLib_, "GetVJDStatus"));
    fn_driver_match_ = reinterpret_cast<bool(*)(WORD*,WORD*,WORD*)>(GetProcAddress(hLib_, "DriverMatch"));

    if (!fn_acquire_ || !fn_relinquish_ || !fn_set_axis_ || !fn_status_) {
        printf("[VCtrl] vJoyInterface.dll missing expected exports — reinstall vJoy\n");
        FreeLibrary(hLib_); hLib_ = nullptr;
        return false;
    }

    // Check device status
    int st = fn_status_(kDeviceId);
    // VJD_STAT_FREE=3, VJD_STAT_OWN=2
    if (st != 3 && st != 2) {
        printf("[VCtrl] vJoy Device %d not available (status=%d).\n", kDeviceId, st);
        printf("[VCtrl]   Open vJoyConf and enable Device 1 with axes X, Y, Z\n");
        FreeLibrary(hLib_); hLib_ = nullptr;
        return false;
    }

    if (!fn_acquire_(kDeviceId)) {
        printf("[VCtrl] AcquireVJD(%d) failed — is another app using it?\n", kDeviceId);
        FreeLibrary(hLib_); hLib_ = nullptr;
        return false;
    }

    // Centre all axes on startup
    fn_set_axis_(kCenter, kDeviceId, kAxisX);
    fn_set_axis_(1,       kDeviceId, kAxisY);
    fn_set_axis_(1,       kDeviceId, kAxisZ);

    available_ = true;
    printf("[VCtrl] vJoy Device %d acquired — virtual joystick ready\n", kDeviceId);
    printf("[VCtrl]   AC → Options → Controls → vJoy Device\n");
    printf("[VCtrl]   Steer=Axis X  |  Gas=Axis Y  |  Brake=Axis Z\n");
    return true;
}

void VirtualController::update(float steer, float throttle, float brake) {
    if (!available_) return;

    // Steer: bot +1=left, -1=right  →  vJoy X: 1=left, kMax=right, kCenter=straight
    // So: steer=+1 → X=1 (full left), steer=-1 → X=kMax (full right)
    float s  = std::clamp(-steer, -1.f, 1.f);        // negate: +1→right
    LONG x   = static_cast<LONG>((s + 1.f) * 0.5f * (kMax - 1) + 1);
    LONG y   = static_cast<LONG>(std::clamp(throttle, 0.f, 1.f) * (kMax - 1) + 1);
    LONG z   = static_cast<LONG>(std::clamp(brake,    0.f, 1.f) * (kMax - 1) + 1);

    fn_set_axis_(x, kDeviceId, kAxisX);
    fn_set_axis_(y, kDeviceId, kAxisY);
    fn_set_axis_(z, kDeviceId, kAxisZ);
}

void VirtualController::shutdown() {
    if (!hLib_) return;
    available_ = false;
    if (fn_set_axis_ && fn_relinquish_) {
        fn_set_axis_(kCenter, kDeviceId, kAxisX);
        fn_set_axis_(1,       kDeviceId, kAxisY);
        fn_set_axis_(1,       kDeviceId, kAxisZ);
        fn_relinquish_(kDeviceId);
    }
    FreeLibrary(hLib_);
    hLib_ = nullptr;
}
