"""
CSP Custom AI memory-mapped file interface.

Exact struct layouts from gro-ove's official C# gist:
  https://gist.github.com/gro-ove/11489f32b3eb3c9e3df1e7819bb3008e

Enabling CSP Custom AI:
  1. assettocorsa/extension/config/new_behaviour.ini:
       [CUSTOM_AI]
       ENABLED=1
  2. Track's surfaces.ini:
       [_EXTRA_PERMISSIONS]
       ALLOW_CUSTOM_AI_MANIPULATION=1

File sizes (confirmed):
  CarControls<N>.v0  — 72 bytes  (YOU create & write)
  Car<N>.v0          — 672 bytes (CSP creates & writes at 333 Hz)
  CarPublic<N>.v0    — ~108 bytes (CSP creates & writes at 60 Hz)
"""
import ctypes
import math
import mmap
import platform
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Vec3 helper
# ---------------------------------------------------------------------------

class Vec3(ctypes.Structure):
    _pack_ = 4
    _fields_ = [('x', ctypes.c_float), ('y', ctypes.c_float), ('z', ctypes.c_float)]

    def as_tuple(self) -> Tuple[float, float, float]:
        return (float(self.x), float(self.y), float(self.z))


# ---------------------------------------------------------------------------
# WheelData (120 bytes × 4 wheels = 480 bytes inside Car<N>)
# ---------------------------------------------------------------------------

class WheelData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('position',         Vec3),
        ('contact_point',    Vec3),
        ('contact_normal',   Vec3),
        ('look',             Vec3),
        ('side',             Vec3),
        ('velocity',         Vec3),
        # Physics scalars
        ('slip_ratio',       ctypes.c_float),
        ('load',             ctypes.c_float),
        ('pressure',         ctypes.c_float),
        ('angular_velocity', ctypes.c_float),
        ('wear',             ctypes.c_float),
        ('dirty_level',      ctypes.c_float),
        ('core_temperature', ctypes.c_float),
        ('camber_rad',       ctypes.c_float),
        ('disc_temperature', ctypes.c_float),
        ('slip',             ctypes.c_float),
        ('slip_angle_deg',   ctypes.c_float),
        ('nd_slip',          ctypes.c_float),
    ]


# ---------------------------------------------------------------------------
# CarControls (72 bytes) — YOU write this to drive a car
# ---------------------------------------------------------------------------

class CSPCarControls(ctypes.Structure):
    """
    Write to AcTools.CSP.NewBehaviour.CustomAI.CarControls<N>.v0
    steer: normalised -1.0 (full left) → +1.0 (full right)
    """
    _pack_ = 4
    _fields_ = [
        ('gas',                  ctypes.c_float),    # 0
        ('brake',                ctypes.c_float),    # 4
        ('clutch',               ctypes.c_float),    # 8
        ('steer',                ctypes.c_float),    # 12  normalised [-1, 1]
        ('handbrake',            ctypes.c_float),    # 16
        ('gear_up',              ctypes.c_bool),     # 20
        ('gear_dn',              ctypes.c_bool),     # 21
        ('drs',                  ctypes.c_bool),     # 22
        ('kers',                 ctypes.c_bool),     # 23
        ('brake_balance_up',     ctypes.c_bool),     # 24
        ('brake_balance_dn',     ctypes.c_bool),     # 25
        ('abs_up',               ctypes.c_bool),     # 26
        ('abs_dn',               ctypes.c_bool),     # 27
        ('tc_up',                ctypes.c_bool),     # 28
        ('tc_dn',                ctypes.c_bool),     # 29
        ('turbo_up',             ctypes.c_bool),     # 30
        ('turbo_dn',             ctypes.c_bool),     # 31
        ('engine_brake_up',      ctypes.c_bool),     # 32
        ('engine_brake_dn',      ctypes.c_bool),     # 33
        ('mguk_delivery_up',     ctypes.c_bool),     # 34
        ('mguk_delivery_dn',     ctypes.c_bool),     # 35
        ('mguk_recovery_up',     ctypes.c_bool),     # 36
        ('mguk_recovery_dn',     ctypes.c_bool),     # 37
        ('mguh_mode',            ctypes.c_uint8),    # 38
        ('headlights',           ctypes.c_bool),     # 39
        ('teleport_to',          ctypes.c_uint8),    # 40
        ('autoclutch_on_start',  ctypes.c_bool),     # 41
        ('autoclutch_on_change', ctypes.c_bool),     # 42
        ('autoblip_active',      ctypes.c_bool),     # 43
        ('_pad',                 ctypes.c_uint8),    # 44 (align to 4)
        ('teleport_pos',         Vec3),              # 44..56
        ('teleport_dir',         Vec3),              # 56..68
        ('autoshift_active',     ctypes.c_bool),     # 68
        ('_pad2',                ctypes.c_uint8 * 3),# 69..72
    ]


# ---------------------------------------------------------------------------
# CarData (672 bytes) — CSP writes this; you read it
# ---------------------------------------------------------------------------

class CSPCarData(ctypes.Structure):
    """
    Read from AcTools.CSP.NewBehaviour.CustomAI.Car<N>.v0 at 333 Hz.
    Byte offsets confirmed from gro-ove gist (ReadSingle/ReadInt32 calls).
    Note: steer here is in DEGREES (not normalised).
    """
    _pack_ = 4
    _fields_ = [
        ('packet_id',              ctypes.c_int32),   # 0
        ('gas',                    ctypes.c_float),   # 4
        ('brake',                  ctypes.c_float),   # 8
        ('clutch',                 ctypes.c_float),   # 12
        ('steer',                  ctypes.c_float),   # 16  degrees
        ('handbrake',              ctypes.c_float),   # 20
        ('fuel',                   ctypes.c_float),   # 24
        ('gear',                   ctypes.c_int32),   # 28
        ('rpm',                    ctypes.c_float),   # 32
        ('speed_kmh',              ctypes.c_float),   # 36
        ('velocity',               Vec3),             # 40
        ('acc_g',                  Vec3),             # 52
        ('look',                   Vec3),             # 64
        ('up',                     Vec3),             # 76
        ('position',               Vec3),             # 88
        ('local_velocity',         Vec3),             # 100
        ('local_angular_velocity', Vec3),             # 112
        ('cg_height',              ctypes.c_float),   # 124
        ('car_damage',             ctypes.c_float * 5),  # 128
        ('wheels',                 WheelData * 4),    # 148
        ('turbo_boost',            ctypes.c_float),   # 628
        ('final_ff',               ctypes.c_float),   # 632
        ('final_pure_ff',          ctypes.c_float),   # 636
        ('pit_limiter',            ctypes.c_bool),    # 640
        ('abs_in_action',          ctypes.c_bool),    # 641
        ('traction_control',       ctypes.c_bool),    # 642
        ('_pad',                   ctypes.c_uint8),   # 643
        ('lap_time_ms',            ctypes.c_uint32),  # 644
        ('best_lap_time_ms',       ctypes.c_uint32),  # 648
        ('drivetrain_torque',      ctypes.c_float),   # 652
        ('spline_position',        ctypes.c_float),   # 656
        ('collision_depth',        ctypes.c_float),   # 660
        ('collision_counter',      ctypes.c_uint32),  # 664
        ('wheels_valid_surface',   ctypes.c_uint32),  # 668
    ]


# ---------------------------------------------------------------------------
# CarPublicData (~108 bytes) — CSP writes, you read at 60 Hz
# ---------------------------------------------------------------------------

class CSPCarPublicData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packet_id',    ctypes.c_int32),    # 0
        ('steer',        ctypes.c_float),    # 4
        ('rpm',          ctypes.c_float),    # 8
        ('spline_pos',   ctypes.c_float),    # 12  normalised 0-1
        ('speed_kmh',    ctypes.c_float),    # 16
        ('velocity',     Vec3),              # 20
        ('acc_g',        Vec3),              # 32
        ('look',         Vec3),              # 44
        ('up',           Vec3),              # 56
        ('position',     Vec3),              # 68
        ('car_damage',   ctypes.c_float * 5),  # 80
        ('is_braking',   ctypes.c_bool),     # 100
        ('_pad',         ctypes.c_uint8 * 3),  # 101..104
    ]


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class CarState:
    index: int = 0
    pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    vel: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    look: Tuple[float, float, float] = (0.0, 0.0, 1.0)
    speed_ms: float = 0.0
    heading: float = 0.0                    # radians, derived from look vector
    spline_pos: float = 0.0                 # normalised 0-1
    packet_id: int = 0
    timestamp: float = field(default_factory=time.monotonic)
    is_active: bool = True

    @property
    def speed_kph(self) -> float:
        return self.speed_ms * 3.6

    @property
    def pos_xz(self) -> Tuple[float, float]:
        return (self.pos[0], self.pos[2])


# ---------------------------------------------------------------------------
# mmap helpers
# ---------------------------------------------------------------------------

_CONTROLS_TMPL = "AcTools.CSP.NewBehaviour.CustomAI.CarControls{n}.v0"
_CAR_TMPL      = "AcTools.CSP.NewBehaviour.CustomAI.Car{n}.v0"
_PUBLIC_TMPL   = "AcTools.CSP.NewBehaviour.CustomAI.CarPublic{n}.v0"


def _try_open_mmap(name: str, size: int, write: bool = False) -> Optional[mmap.mmap]:
    if platform.system() != "Windows":
        return None
    access = mmap.ACCESS_WRITE if write else mmap.ACCESS_READ
    try:
        return mmap.mmap(-1, size, tagname=name, access=access)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class CSPInterface:
    """
    Manages CSP Custom AI shared memory connections.

    For player car control (index 0):
      - read_own()           → CSPCarData from Car0.v0 (333 Hz)
      - write_controls(...)  → writes to CarControls0.v0
    For traffic (indices 1..N):
      - read_traffic()       → list[CarState] from CarPublic{n}.v0 (60 Hz)
    """

    def __init__(self, own_index: int = 0, max_cars: int = 64):
        self.own_index = own_index
        self.max_cars = max_cars

        self._car_mm: Optional[mmap.mmap] = None
        self._controls_mm: Optional[mmap.mmap] = None
        self._public_mms: Dict[int, mmap.mmap] = {}

        self._controls_size = ctypes.sizeof(CSPCarControls)
        self._car_size = ctypes.sizeof(CSPCarData)
        self._pub_size = ctypes.sizeof(CSPCarPublicData)

        self.available = False
        self.controls_available = False

    def open(self) -> bool:
        if platform.system() != "Windows":
            print("[CSP] Non-Windows — mmap disabled")
            return False

        # Own car read
        self._car_mm = _try_open_mmap(
            _CAR_TMPL.format(n=self.own_index), self._car_size, write=False
        )

        # Controls write — create if it doesn't exist (zero-init 72 bytes)
        try:
            self._controls_mm = mmap.mmap(
                -1, self._controls_size,
                tagname=_CONTROLS_TMPL.format(n=self.own_index),
                access=mmap.ACCESS_WRITE,
            )
            # Zero-init so CSP picks it up
            self._controls_mm.seek(0)
            self._controls_mm.write(b'\x00' * self._controls_size)
            self._controls_mm.seek(0)
            self.controls_available = True
        except Exception as e:
            print(f"[CSP] CarControls open failed: {e}")

        # Traffic
        opened = 0
        for i in range(self.max_cars):
            if i == self.own_index:
                continue
            mm = _try_open_mmap(_PUBLIC_TMPL.format(n=i), self._pub_size, write=False)
            if mm is not None:
                self._public_mms[i] = mm
                opened += 1

        self.available = (self._car_mm is not None) or (opened > 0)
        print(f"[CSP] car={'OK' if self._car_mm else 'N/A'} "
              f"controls={'OK' if self.controls_available else 'N/A'} "
              f"traffic_slots={opened}")
        return self.available

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def read_own(self) -> Optional[CarState]:
        if self._car_mm is None:
            return None
        try:
            self._car_mm.seek(0)
            raw = self._car_mm.read(self._car_size)
            s = CSPCarData.from_buffer_copy(raw)
            heading = math.atan2(float(s.look.x), float(s.look.z))
            return CarState(
                index=self.own_index,
                pos=s.position.as_tuple(),
                vel=s.velocity.as_tuple(),
                look=s.look.as_tuple(),
                speed_ms=s.speed_kmh / 3.6,
                heading=heading,
                spline_pos=s.spline_position,
                packet_id=s.packet_id,
                timestamp=time.monotonic(),
            )
        except Exception:
            return None

    def read_traffic(self) -> List[CarState]:
        now = time.monotonic()
        result: List[CarState] = []
        for idx, mm in self._public_mms.items():
            try:
                mm.seek(0)
                raw = mm.read(self._pub_size)
                s = CSPCarPublicData.from_buffer_copy(raw)
                heading = math.atan2(float(s.look.x), float(s.look.z))
                result.append(CarState(
                    index=idx,
                    pos=s.position.as_tuple(),
                    vel=s.velocity.as_tuple(),
                    look=s.look.as_tuple(),
                    speed_ms=s.speed_kmh / 3.6,
                    heading=heading,
                    spline_pos=s.spline_pos,
                    packet_id=s.packet_id,
                    timestamp=now,
                ))
            except Exception:
                continue
        return result

    # ------------------------------------------------------------------
    # Writer
    # ------------------------------------------------------------------

    def write_controls(
        self,
        gas: float = 0.0,
        brake: float = 0.0,
        steer: float = 0.0,         # normalised [-1, 1]
        clutch: float = 0.0,
        handbrake: float = 0.0,
        gear_up: bool = False,
        gear_dn: bool = False,
        autoshift: bool = True,
    ) -> bool:
        if not self.controls_available or self._controls_mm is None:
            return False
        try:
            c = CSPCarControls()
            c.gas         = max(0.0, min(1.0, gas))
            c.brake       = max(0.0, min(1.0, brake))
            c.steer       = max(-1.0, min(1.0, steer))
            c.clutch      = max(0.0, min(1.0, clutch))
            c.handbrake   = max(0.0, min(1.0, handbrake))
            c.gear_up     = gear_up
            c.gear_dn     = gear_dn
            c.autoshift_active = autoshift

            self._controls_mm.seek(0)
            self._controls_mm.write(bytes(c))
            return True
        except Exception:
            return False

    def close(self):
        for mm in [self._car_mm, self._controls_mm, *self._public_mms.values()]:
            if mm:
                try:
                    mm.close()
                except Exception:
                    pass
        self._public_mms.clear()
        self.available = False

    def __del__(self):
        self.close()
