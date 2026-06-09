#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmsystem.h>
#include <cstdint>

// High-resolution spin-yield loop timer for Windows.
// Usage:
//   LoopTimer t(333.0);   // 333 Hz
//   while (running) {
//       t.beginFrame();
//       doWork();
//       t.endFrame();     // sleeps + spins until next tick
//   }

class LoopTimer {
public:
    explicit LoopTimer(double hz) {
        timeBeginPeriod(1);
        period_qpc_ = static_cast<LONGLONG>(frequency() / hz);
        QueryPerformanceCounter(&next_);
    }

    ~LoopTimer() {
        timeEndPeriod(1);
    }

    void beginFrame() {
        QueryPerformanceCounter(&now_);
    }

    // Busy-sleep until the next scheduled tick.
    void endFrame() {
        next_.QuadPart += period_qpc_;
        LARGE_INTEGER cur;
        QueryPerformanceCounter(&cur);
        LONGLONG remaining = next_.QuadPart - cur.QuadPart;
        if (remaining <= 0) {
            // Already overdue — don't drift further
            next_.QuadPart = cur.QuadPart + period_qpc_;
            return;
        }
        // OS sleep for most of the interval (minus 0.5 ms guard)
        LONGLONG guard = frequency() / 2000;
        if (remaining > guard) {
            DWORD ms = static_cast<DWORD>((remaining - guard) * 1000 / frequency());
            if (ms > 0) Sleep(ms);
        }
        // Spin the last 0.5 ms
        do { QueryPerformanceCounter(&cur); } while (cur.QuadPart < next_.QuadPart);
    }

    // Elapsed time of current frame in seconds
    double frameElapsed() const {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        return static_cast<double>(now.QuadPart - now_.QuadPart) / frequency();
    }

    // Wall-clock seconds since construction
    double elapsed() const {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        return static_cast<double>(now.QuadPart - start_.QuadPart) / frequency();
    }

    static LONGLONG frequency() {
        static LARGE_INTEGER f = [] { LARGE_INTEGER v; QueryPerformanceFrequency(&v); return v; }();
        return f.QuadPart;
    }

private:
    LARGE_INTEGER next_{}, now_{}, start_{};
    LONGLONG period_qpc_;
    LARGE_INTEGER _init = [this]() -> LARGE_INTEGER {
        QueryPerformanceCounter(&start_);
        next_ = start_;
        return start_;
    }();
};


// Set thread and process priority for real-time control
inline void setRealtimePriority() {
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
}
