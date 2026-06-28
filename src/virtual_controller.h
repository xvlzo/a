#pragma once
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdint>

// Virtual joystick via vJoy (dynamic load from vJoyInterface.dll, installed system-wide).
// Install: https://github.com/jshafer817/vJoy/releases  → vJoySetup.exe
// Then run vJoyConf and enable Device 1 with axes: X, Y, Z
//
// AC controller setup (one-time):
//   Options → Controls → select "vJoy Device" → assign:
//   Steer = Axis X  |  Gas = Axis Y  |  Brake = Axis Z

class VirtualController {
public:
    ~VirtualController();
    bool init();
    void update(float steer, float throttle, float brake);
    void shutdown();
    bool available() const { return available_; }

private:
    static constexpr UINT kDeviceId = 1;

    HMODULE hLib_      = nullptr;
    bool    available_ = false;

    // vJoy function pointers
    bool (*fn_acquire_)(UINT)                          = nullptr;
    void (*fn_relinquish_)(UINT)                       = nullptr;
    bool (*fn_set_axis_)(LONG, UINT, UINT)             = nullptr;
    int  (*fn_status_)(UINT)                           = nullptr;
    bool (*fn_driver_match_)(WORD*, WORD*, WORD*)      = nullptr;

    // HID axis usage constants (matching vJoy header values)
    static constexpr UINT kAxisX  = 0x30; // steer
    static constexpr UINT kAxisY  = 0x31; // throttle
    static constexpr UINT kAxisZ  = 0x32; // brake
    static constexpr LONG kCenter = 0x4000; // 16384 = center of 1–32768 range
    static constexpr LONG kMax    = 0x8000; // 32768 = full deflection
};
