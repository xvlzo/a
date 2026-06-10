#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmsystem.h>

// High-resolution spin-yield loop timer.
//
// Usage:
//   LoopTimer t(333.0);
//   while (running) {
//       t.beginFrame();
//       doWork();
//       t.endFrame();   // sleeps + busy-spins to next deadline
//   }

class LoopTimer {
public:
    explicit LoopTimer(double hz) {
        timeBeginPeriod(1);
        QueryPerformanceFrequency(&freq_);
        period_qpc_ = static_cast<LONGLONG>(freq_.QuadPart / hz);
        QueryPerformanceCounter(&start_);
        next_ = start_;
        now_  = start_;
    }

    ~LoopTimer() { timeEndPeriod(1); }

    void beginFrame() {
        QueryPerformanceCounter(&now_);
    }

    void endFrame() {
        next_.QuadPart += period_qpc_;
        LARGE_INTEGER cur;
        QueryPerformanceCounter(&cur);
        LONGLONG remaining = next_.QuadPart - cur.QuadPart;

        if (remaining <= 0) {
            // Overran — re-sync deadline to now to avoid spiral
            next_.QuadPart = cur.QuadPart + period_qpc_;
            return;
        }

        // OS sleep for most of the interval (leave 0.5 ms for spin)
        LONGLONG guard = freq_.QuadPart / 2000;
        if (remaining > guard) {
            DWORD ms = static_cast<DWORD>((remaining - guard) * 1000 /
                                          freq_.QuadPart);
            if (ms > 0) Sleep(ms);
        }

        // Busy-spin the final fraction
        do { QueryPerformanceCounter(&cur); } while (cur.QuadPart < next_.QuadPart);
    }

    // Elapsed seconds during current frame (call after beginFrame)
    double frameElapsed() const {
        LARGE_INTEGER cur;
        QueryPerformanceCounter(&cur);
        return static_cast<double>(cur.QuadPart - now_.QuadPart) /
               static_cast<double>(freq_.QuadPart);
    }

    // Wall-clock seconds since construction
    double elapsed() const {
        LARGE_INTEGER cur;
        QueryPerformanceCounter(&cur);
        return static_cast<double>(cur.QuadPart - start_.QuadPart) /
               static_cast<double>(freq_.QuadPart);
    }

    LONGLONG freqQpc() const { return freq_.QuadPart; }

private:
    LARGE_INTEGER freq_{};
    LARGE_INTEGER start_{};
    LARGE_INTEGER next_{};
    LARGE_INTEGER now_{};
    LONGLONG period_qpc_ = 0;
};

inline void setRealtimePriority() {
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
}
