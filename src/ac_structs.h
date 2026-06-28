#pragma once
#include <cstdint>
#include <wchar.h>

// Standard Assetto Corsa shared memory structs.
// Maps:  Local\acpmf_physics   (333 Hz)
//        Local\acpmf_graphics  (per frame)
//        Local\acpmf_static    (once)

#pragma pack(push, 4)

struct SPageFilePhysics {
    int32_t packetId;
    float   gas;
    float   brake;
    float   fuel;
    int32_t gear;
    int32_t rpms;
    float   steerAngle;
    float   speedKmh;
    float   velocity[3];
    float   accG[3];
    float   wheelSlip[4];
    float   wheelLoad[4];
    float   wheelsPressure[4];
    float   wheelAngularSpeed[4];
    float   tyreWear[4];
    float   tyreDirtyLevel[4];
    float   tyreCoreTemperature[4];
    float   camberRAD[4];
    float   suspensionTravel[4];
    float   drs;
    float   tc;
    float   heading;
    float   pitch;
    float   roll;
    float   cgHeight;
    float   carDamage[5];
    int32_t numberOfTyresOut;
    int32_t pitLimiterOn;
    float   abs;
    float   kersCharge;
    float   kersInput;
    int32_t autoShifterOn;
    float   rideHeight[2];
    float   turboBoost;
    float   ballast;
    float   airDensity;
    float   airTemp;
    float   roadTemp;
    float   localVelocity[3];
};

struct SPageFileGraphic {
    int32_t  packetId;
    int32_t  status;           // AC_STATUS: 0=OFF 1=REPLAY 2=LIVE 3=PAUSE
    int32_t  session;
    wchar_t  currentTime[15];
    wchar_t  lastTime[15];
    wchar_t  bestTime[15];
    wchar_t  split[15];
    int32_t  completedLaps;
    int32_t  position;
    int32_t  iCurrentTime;
    int32_t  iLastTime;
    int32_t  iBestTime;
    float    sessionTimeLeft;
    float    distanceTraveled;
    int32_t  isInPit;
    int32_t  currentSectorIndex;
    int32_t  lastSectorTime;
    int32_t  numberOfLaps;
    wchar_t  tyreCompound[33];
    float    replayTimeMultiplier;
    float    normalizedCarPosition;
    int32_t  activeCars;
    float    carCoordinates[60 * 3]; // XYZ for up to 60 cars
    int32_t  carID[60];
    int32_t  playerCarID;
    float    penaltyTime;
    int32_t  flag;
    int32_t  penalty;
    int32_t  idealLineOn;
    int32_t  isInPitLane;
    float    surfaceGrip;
    int32_t  mandatoryPitDone;
    float    windSpeed;
    float    windDirection;
    int32_t  isSetupMenuVisible;
    int32_t  mainDisplayIndex;
    int32_t  secondaryDisplayIndex;
    int32_t  TC;
    int32_t  TCCUT;
    int32_t  EngineMap;
    int32_t  ABS;
    float    fuelXLap;
    int32_t  rainLights;
    int32_t  flashingLights;
    int32_t  lightsStage;
    float    exhaustTemperature;
    int32_t  wiperLV;
    int32_t  driverStintTotalTimeLeft;
    int32_t  driverStintTimeLeft;
    int32_t  rainTyres;
};

#pragma pack(pop)

#define AC_STATUS_LIVE 2
