#include <cstdio>
#include <csignal>
#include <cstring>
#include <string>
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "bot.h"

static Bot* g_bot = nullptr;

static void onSignal(int) {
    printf("\n[main] Shutting down...\n");
    if (g_bot) g_bot->stop();
}

static void printUsage(const char* exe) {
    printf("Usage: %s [options]\n"
           "  --fast-lane <path>   Path to fast_lane.ai  (default: data/fast_lane.ai)\n"
           "  --target-kph <n>     Target cruise speed    (default: 160)\n"
           "  --car <n>            Own car index          (default: 0)\n"
           "  --max-traffic <n>    Max traffic cars       (default: 64)\n"
           "  --humanization <f>   0.0–1.0                (default: 0.7)\n"
           "  --smoothness <f>     0.0–1.0                (default: 0.6)\n"
           "  --safety <f>         Collision margin (m)   (default: 1.2)\n"
           "  --dry-run            Compute only, no output\n"
           "  --hz <n>             Control loop rate      (default: 333)\n"
           "  --log-dir <path>     Log directory          (default: logs)\n"
           "\n"
           "Hotkeys (active at all times):\n"
           "  F5      Toggle bot on/off\n"
           "  F6/F7   Humanization up/down\n"
           "  F8/F9   Target speed +/- 10 kph\n",
           exe);
}

int main(int argc, char* argv[]) {
    Bot::Config cfg;

    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            printUsage(argv[0]); return 0;
        }
        else if (!strcmp(argv[i], "--fast-lane") && i+1 < argc)
            cfg.fast_lane_path = argv[++i];
        else if (!strcmp(argv[i], "--target-kph") && i+1 < argc)
            cfg.target_kph = static_cast<float>(atof(argv[++i]));
        else if (!strcmp(argv[i], "--car") && i+1 < argc)
            cfg.own_car_index = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--max-traffic") && i+1 < argc)
            cfg.max_traffic = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--humanization") && i+1 < argc)
            cfg.humanization = static_cast<float>(atof(argv[++i]));
        else if (!strcmp(argv[i], "--smoothness") && i+1 < argc)
            cfg.smoothness = static_cast<float>(atof(argv[++i]));
        else if (!strcmp(argv[i], "--safety") && i+1 < argc)
            cfg.safety_margin = static_cast<float>(atof(argv[++i]));
        else if (!strcmp(argv[i], "--dry-run"))
            cfg.dry_run = true;
        else if (!strcmp(argv[i], "--hz") && i+1 < argc)
            cfg.control_hz = static_cast<float>(atof(argv[++i]));
        else if (!strcmp(argv[i], "--log-dir") && i+1 < argc)
            cfg.log_dir = argv[++i];
    }

    printf("=============================================================\n");
    printf("  No Hesi Bot\n");
    printf("  Track spline : %s\n", cfg.fast_lane_path.c_str());
    printf("  Target speed : %.0f kph\n", cfg.target_kph);
    printf("  Car index    : %d\n", cfg.own_car_index);
    printf("  Control Hz   : %.0f\n", cfg.control_hz);
    printf("  Humanization : %.1f\n", cfg.humanization);
    printf("  Smoothness   : %.1f\n", cfg.smoothness);
    printf("  Dry run      : %s\n", cfg.dry_run ? "YES" : "no");
    printf("=============================================================\n");

    Bot bot(cfg);
    g_bot = &bot;

    signal(SIGINT,  onSignal);
    signal(SIGTERM, onSignal);

    if (!bot.init()) {
        fprintf(stderr, "[main] Init failed.\n");
        return 1;
    }

    bot.run();  // blocks until F5 off + Ctrl-C

    printf("[main] Done. 3x passes: %d  1x passes: %d\n",
           0, 0); // TODO: expose from bot
    return 0;
}
