#include "virtual_controller.h"
#include <cstdio>
#include <algorithm>
#include <cmath>

#define LOAD(sym, type, name) \
    sym = reinterpret_cast<type>(GetProcAddress(hLib_, name)); \
    if (!sym) { printf("[VCtrl] Missing export: %s\n", name); goto fail; }

VirtualController::~VirtualController() { shutdown(); }

bool VirtualController::init() {
    hLib_ = LoadLibraryA("ViGEmClient.dll");
    if (!hLib_) {
        printf("[VCtrl] ViGEmClient.dll not found.\n");
        printf("[VCtrl]   Download + run: https://github.com/nefarius/ViGEmBus/releases\n");
        printf("[VCtrl]   (ViGEmBus_Setup_x64.exe, then rebuild + rerun this bot)\n");
        return false;
    }

    LOAD(fn_alloc_,         void*(*)(),              "vigem_alloc");
    LOAD(fn_connect_,       int(*)(void*),           "vigem_connect");
    LOAD(fn_target_alloc_,  void*(*)(),              "vigem_target_x360_alloc");
    LOAD(fn_target_add_,    int(*)(void*,void*),     "vigem_target_add");
    LOAD(fn_update_,        int(*)(void*,void*,XusbReport), "vigem_target_x360_update");
    fn_target_remove_ = reinterpret_cast<void(*)(void*,void*)>(GetProcAddress(hLib_, "vigem_target_remove"));
    fn_target_free_   = reinterpret_cast<void(*)(void*)>(GetProcAddress(hLib_, "vigem_target_free"));
    fn_free_          = reinterpret_cast<void(*)(void*)>(GetProcAddress(hLib_, "vigem_free"));
    fn_disconnect_    = reinterpret_cast<void(*)(void*)>(GetProcAddress(hLib_, "vigem_disconnect"));

    client_ = fn_alloc_();
    if (!client_) { printf("[VCtrl] vigem_alloc returned null\n"); goto fail; }

    if (int e = fn_connect_(client_)) {
        printf("[VCtrl] vigem_connect failed (err=%d) — ViGEm Bus driver not installed?\n", e);
        if (fn_free_) fn_free_(client_); client_ = nullptr;
        goto fail;
    }

    target_ = fn_target_alloc_();
    if (!target_) { printf("[VCtrl] vigem_target_x360_alloc returned null\n"); goto fail; }

    if (int e = fn_target_add_(client_, target_)) {
        printf("[VCtrl] vigem_target_add failed (err=%d)\n", e);
        if (fn_target_free_) fn_target_free_(target_); target_ = nullptr;
        goto fail;
    }

    available_ = true;
    printf("[VCtrl] Virtual Xbox 360 controller ready\n");
    printf("[VCtrl]   In AC → Options → Controls → select the new gamepad → assign axes\n");
    printf("[VCtrl]   Steer = Left Stick X  |  Gas = Right Trigger  |  Brake = Left Trigger\n");
    return true;

fail:
    if (hLib_) { FreeLibrary(hLib_); hLib_ = nullptr; }
    return false;
}

void VirtualController::update(float steer, float throttle, float brake) {
    if (!available_) return;

    XusbReport r{};
    // AC: steer > 0 = left, steer < 0 = right
    // Xbox LX: positive = stick pushed right = steer right → negate
    float lx = std::clamp(-steer, -1.f, 1.f);
    r.sThumbLX       = static_cast<int16_t>(lx       * 32767.f);
    r.bRightTrigger  = static_cast<uint8_t>(std::clamp(throttle, 0.f, 1.f) * 255.f);
    r.bLeftTrigger   = static_cast<uint8_t>(std::clamp(brake,    0.f, 1.f) * 255.f);
    fn_update_(client_, target_, r);
}

void VirtualController::shutdown() {
    if (!hLib_) return;
    available_ = false;
    if (fn_update_ && client_ && target_) {
        XusbReport zero{};
        fn_update_(client_, target_, zero);
    }
    if (fn_target_remove_ && client_ && target_) fn_target_remove_(client_, target_);
    if (fn_target_free_   && target_)            fn_target_free_(target_);
    if (fn_disconnect_    && client_)            fn_disconnect_(client_);
    if (fn_free_          && client_)            fn_free_(client_);
    target_ = client_ = nullptr;
    FreeLibrary(hLib_);
    hLib_ = nullptr;
}
