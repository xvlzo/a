#pragma once
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdint>

// Virtual Xbox 360 controller via ViGEm Bus (dynamic load — no import lib needed).
// Falls back gracefully (prints warning) if ViGEmClient.dll is not installed.
//
// Install ViGEm Bus: https://github.com/nefarius/ViGEmBus/releases
//   → download ViGEmBus_Setup_x64.exe, run once, reboot if prompted

#pragma pack(push, 1)
struct XusbReport {
    uint16_t wButtons;
    uint8_t  bLeftTrigger;
    uint8_t  bRightTrigger;
    int16_t  sThumbLX;
    int16_t  sThumbLY;
    int16_t  sThumbRX;
    int16_t  sThumbRY;
};
static_assert(sizeof(XusbReport) == 12, "XUSB_REPORT size mismatch");
#pragma pack(pop)

class VirtualController {
public:
    ~VirtualController();
    bool init();
    void update(float steer, float throttle, float brake);
    void shutdown();
    bool available() const { return available_; }

private:
    HMODULE hLib_      = nullptr;
    void*   client_    = nullptr;
    void*   target_    = nullptr;
    bool    available_ = false;

    // ViGEm function pointers — loaded at runtime
    void* (*fn_alloc_)()                                    = nullptr;
    int   (*fn_connect_)(void*)                             = nullptr;
    void* (*fn_target_alloc_)()                             = nullptr;
    int   (*fn_target_add_)(void*, void*)                   = nullptr;
    int   (*fn_update_)(void*, void*, XusbReport)           = nullptr;
    void  (*fn_target_remove_)(void*, void*)                = nullptr;
    void  (*fn_target_free_)(void*)                         = nullptr;
    void  (*fn_free_)(void*)                                = nullptr;
    void  (*fn_disconnect_)(void*)                          = nullptr;
};
