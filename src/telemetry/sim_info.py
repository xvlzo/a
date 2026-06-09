"""
Assetto Corsa standard shared memory reader.
Works on any AC installation (no CSP required).
Provides player car physics, graphics state, and static session info.

Memory layout from AC SDK:
  Local\acpmf_physics  — 333 Hz physics (velocity, accel, wheel data, etc.)
  Local\acpmf_graphics — per-frame graphics state (status, tyres, etc.)
  Local\acpmf_static   — session constants (car model, track name, etc.)
"""
import ctypes
import mmap
import platform
import struct
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# ctypes struct definitions (match AC SDK exactly)
# ---------------------------------------------------------------------------

class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId",         ctypes.c_int),
        ("gas",              ctypes.c_float),
        ("brake",            ctypes.c_float),
        ("fuel",             ctypes.c_float),
        ("gear",             ctypes.c_int),
        ("rpms",             ctypes.c_int),
        ("steerAngle",       ctypes.c_float),
        ("speedKmh",         ctypes.c_float),
        ("velocity",         ctypes.c_float * 3),   # world-space m/s
        ("accG",             ctypes.c_float * 3),   # g-forces
        ("wheelSlip",        ctypes.c_float * 4),
        ("wheelLoad",        ctypes.c_float * 4),
        ("wheelsPressure",   ctypes.c_float * 4),
        ("wheelAngularSpeed",ctypes.c_float * 4),
        ("tyreWear",         ctypes.c_float * 4),
        ("tyreDirtyLevel",   ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4),
        ("camberRAD",        ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4),
        ("drs",              ctypes.c_float),
        ("tc",               ctypes.c_float),
        ("heading",          ctypes.c_float),       # radians, world Y-up
        ("pitch",            ctypes.c_float),
        ("roll",             ctypes.c_float),
        ("cgHeight",         ctypes.c_float),
        ("carDamage",        ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int),
        ("pitLimiterOn",     ctypes.c_int),
        ("abs",              ctypes.c_float),
        ("kersCharge",       ctypes.c_float),
        ("kersInput",        ctypes.c_float),
        ("autoShifterOn",    ctypes.c_int),
        ("rideHeight",       ctypes.c_float * 2),
        ("turboBoost",       ctypes.c_float),
        ("ballast",          ctypes.c_float),
        ("airDensity",       ctypes.c_float),
        ("airTemp",          ctypes.c_float),
        ("roadTemp",         ctypes.c_float),
        ("localVelocity",    ctypes.c_float * 3),   # car-local m/s (x=right,y=up,z=fwd)
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId",             ctypes.c_int),
        ("status",               ctypes.c_int),    # AC_STATUS enum
        ("session",              ctypes.c_int),    # AC_SESSION_TYPE
        ("currentTime",          ctypes.c_wchar * 15),
        ("lastTime",             ctypes.c_wchar * 15),
        ("bestTime",             ctypes.c_wchar * 15),
        ("split",                ctypes.c_wchar * 15),
        ("completedLaps",        ctypes.c_int),
        ("position",             ctypes.c_int),
        ("iCurrentTime",         ctypes.c_int),
        ("iLastTime",            ctypes.c_int),
        ("iBestTime",            ctypes.c_int),
        ("sessionTimeLeft",      ctypes.c_float),
        ("distanceTraveled",     ctypes.c_float),
        ("isInPit",              ctypes.c_int),
        ("currentSectorIndex",   ctypes.c_int),
        ("lastSectorTime",       ctypes.c_int),
        ("numberOfLaps",         ctypes.c_int),
        ("tyreCompound",         ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition",ctypes.c_float),  # 0-1 along track spline
        ("activeCars",           ctypes.c_int),
        ("carCoordinates",       ctypes.c_float * (60 * 3)),  # up to 60 cars XYZ
        ("carID",                ctypes.c_int * 60),
        ("playerCarID",          ctypes.c_int),
        ("penaltyTime",          ctypes.c_float),
        ("flag",                 ctypes.c_int),
        ("penalty",              ctypes.c_int),
        ("idealLineOn",          ctypes.c_int),
        ("isInPitLane",          ctypes.c_int),
        ("surfaceGrip",          ctypes.c_float),
        ("mandatoryPitDone",     ctypes.c_int),
        ("windSpeed",            ctypes.c_float),
        ("windDirection",        ctypes.c_float),
        ("isSetupMenuVisible",   ctypes.c_int),
        ("mainDisplayIndex",     ctypes.c_int),
        ("secondaryDisplayIndex",ctypes.c_int),
        ("TC",                   ctypes.c_int),
        ("TCCUT",                ctypes.c_int),
        ("EngineMap",            ctypes.c_int),
        ("ABS",                  ctypes.c_int),
        ("fuelXLap",             ctypes.c_float),
        ("rainLights",           ctypes.c_int),
        ("flashingLights",       ctypes.c_int),
        ("lightsStage",          ctypes.c_int),
        ("exhaustTemperature",   ctypes.c_float),
        ("wiperLV",              ctypes.c_int),
        ("driverStintTotalTimeLeft", ctypes.c_int),
        ("driverStintTimeLeft",  ctypes.c_int),
        ("rainTyres",            ctypes.c_int),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion",     ctypes.c_wchar * 15),
        ("acVersion",     ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int),
        ("numCars",       ctypes.c_int),
        ("carModel",      ctypes.c_wchar * 33),
        ("track",         ctypes.c_wchar * 33),
        ("playerName",    ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick",    ctypes.c_wchar * 33),
        ("sectorCount",   ctypes.c_int),
        ("maxTorque",     ctypes.c_float),
        ("maxPower",      ctypes.c_float),
        ("maxRpm",        ctypes.c_int),
        ("maxFuel",       ctypes.c_float),
        ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius",    ctypes.c_float * 4),
        ("maxTurboBoost", ctypes.c_float),
        ("deprecated1",   ctypes.c_float),
        ("deprecated2",   ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int),
        ("aidFuelRate",   ctypes.c_float),
        ("aidTireRate",   ctypes.c_float),
        ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int),
        ("aidStability",  ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int),
        ("aidAutoBlip",   ctypes.c_int),
        ("hasDRS",        ctypes.c_int),
        ("hasERS",        ctypes.c_int),
        ("hasKERS",       ctypes.c_int),
        ("kersMaxJ",      ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int),
        ("ersPowerControllerCount", ctypes.c_int),
        ("trackSPlineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33),
        ("ersMaxJ",       ctypes.c_float),
        ("isTimedRace",   ctypes.c_int),
        ("hasExtraLap",   ctypes.c_int),
        ("carSkin",       ctypes.c_wchar * 33),
        ("reversedGridPositions", ctypes.c_int),
        ("PitWindowStart",ctypes.c_int),
        ("PitWindowEnd",  ctypes.c_int),
        ("isOnline",      ctypes.c_int),
    ]


# ---------------------------------------------------------------------------
# Shared memory reader
# ---------------------------------------------------------------------------

AC_STATUS_LIVE = 2   # AC_STATUS.AC_LIVE


@dataclass
class PlayerState:
    """Snapshot of player car state from shared memory."""
    speed_kph: float = 0.0
    speed_ms: float = 0.0
    world_vel: tuple = (0.0, 0.0, 0.0)     # m/s world-space
    local_vel: tuple = (0.0, 0.0, 0.0)     # m/s car-local
    heading: float = 0.0                    # radians
    pitch: float = 0.0
    roll: float = 0.0
    norm_spline_pos: float = 0.0            # 0-1 along track
    is_live: bool = False
    gear: int = 0
    rpms: int = 0
    gas: float = 0.0
    brake: float = 0.0
    steer: float = 0.0
    packet_id: int = -1


class SimInfo:
    """
    Opens AC shared memory maps and provides read access.
    Compatible with AC 1.x on Windows. Gracefully degrades on other platforms.
    """

    def __init__(self):
        self._physics_mmap: Optional[mmap.mmap] = None
        self._graphics_mmap: Optional[mmap.mmap] = None
        self._static_mmap: Optional[mmap.mmap] = None
        self._available = False
        self._open()

    def _open(self):
        if platform.system() != "Windows":
            print("[SimInfo] Not on Windows — shared memory unavailable (dry-run mode)")
            return
        try:
            import mmap as _mmap
            self._physics_mmap = _mmap.mmap(-1, ctypes.sizeof(SPageFilePhysics),
                                             "Local\\acpmf_physics",
                                             access=_mmap.ACCESS_READ)
            self._graphics_mmap = _mmap.mmap(-1, ctypes.sizeof(SPageFileGraphic),
                                              "Local\\acpmf_graphics",
                                              access=_mmap.ACCESS_READ)
            self._static_mmap = _mmap.mmap(-1, ctypes.sizeof(SPageFileStatic),
                                            "Local\\acpmf_static",
                                            access=_mmap.ACCESS_READ)
            self._available = True
            print("[SimInfo] AC shared memory opened OK")
        except Exception as e:
            print(f"[SimInfo] Failed to open AC shared memory: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def read_physics(self) -> Optional[SPageFilePhysics]:
        if not self._available or self._physics_mmap is None:
            return None
        self._physics_mmap.seek(0)
        raw = self._physics_mmap.read(ctypes.sizeof(SPageFilePhysics))
        return SPageFilePhysics.from_buffer_copy(raw)

    def read_graphics(self) -> Optional[SPageFileGraphic]:
        if not self._available or self._graphics_mmap is None:
            return None
        self._graphics_mmap.seek(0)
        raw = self._graphics_mmap.read(ctypes.sizeof(SPageFileGraphic))
        return SPageFileGraphic.from_buffer_copy(raw)

    def read_static(self) -> Optional[SPageFileStatic]:
        if not self._available or self._static_mmap is None:
            return None
        self._static_mmap.seek(0)
        raw = self._static_mmap.read(ctypes.sizeof(SPageFileStatic))
        return SPageFileStatic.from_buffer_copy(raw)

    def get_player_state(self) -> PlayerState:
        """Return a clean PlayerState snapshot. Safe to call at 333 Hz."""
        state = PlayerState()
        phys = self.read_physics()
        graph = self.read_graphics()

        if phys is None:
            return state

        state.packet_id = phys.packetId
        state.speed_kph = phys.speedKmh
        state.speed_ms = phys.speedKmh / 3.6
        state.world_vel = tuple(phys.velocity)
        state.local_vel = tuple(phys.localVelocity)
        state.heading = phys.heading
        state.pitch = phys.pitch
        state.roll = phys.roll
        state.gear = phys.gear
        state.rpms = phys.rpms
        state.gas = phys.gas
        state.brake = phys.brake
        state.steer = phys.steerAngle

        if graph is not None:
            state.norm_spline_pos = graph.normalizedCarPosition
            state.is_live = (graph.status == AC_STATUS_LIVE)

        return state

    def get_all_car_positions(self):
        """
        Return list of (x, y, z) for all active cars from graphics memory.
        Index 0 = player. Only valid in singleplayer; in multiplayer use CSP or AssettoServer.
        NOTE: This is player-only in standard AC; use CSP CarPublic for true opponent positions.
        """
        graph = self.read_graphics()
        if graph is None:
            return []
        n = graph.activeCars
        coords = graph.carCoordinates
        return [(coords[i * 3], coords[i * 3 + 1], coords[i * 3 + 2]) for i in range(n)]

    def close(self):
        for mm in (self._physics_mmap, self._graphics_mmap, self._static_mmap):
            if mm:
                try:
                    mm.close()
                except Exception:
                    pass
        self._available = False

    def __del__(self):
        self.close()
