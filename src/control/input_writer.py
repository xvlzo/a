"""
Control output writer.

Primary: CSP Custom AI (CarControls mmap) — works at 333 Hz, undetectable.
Fallback: vgamepad (Xbox360 virtual controller) — requires ViGEm driver.

The CSP path is preferred because:
  - No driver dependency beyond CSP itself
  - Full 333 Hz update rate
  - Zero input lag (direct mmap write)
  - Cannot be distinguished from normal game AI

vgamepad fallback is for environments where CSP Custom AI isn't configured
but the user still wants bot control of their player car via gamepad emulation.
"""
import platform
import time
from dataclasses import dataclass
from typing import Optional

from config import cfg
from src.control.stanley_controller import ControlOutput
from src.telemetry.csp_custom_ai import CSPInterface


# ---------------------------------------------------------------------------
# Unified writer
# ---------------------------------------------------------------------------

class InputWriter:
    """
    Accepts a ControlOutput and routes it to the active output backend.
    Instantiate, call open(), then write() in the hot loop.
    """

    def __init__(self, csp: CSPInterface):
        self.csp = csp
        self._vgp = None
        self._vgp_available = False
        self._dry_run = cfg.dry_run
        self._frame = 0

    def open(self):
        if not self.csp.controls_available and cfg.use_vgamepad:
            self._try_open_vgamepad()

    def _try_open_vgamepad(self):
        if platform.system() != "Windows":
            return
        try:
            import vgamepad as vg
            self._vgp = vg.VX360Gamepad()
            self._vgp_available = True
            print("[InputWriter] vgamepad (Xbox360) opened OK")
        except ImportError:
            print("[InputWriter] vgamepad not installed — pip install vgamepad")
        except Exception as e:
            print(f"[InputWriter] vgamepad failed: {e}")

    def write(self, out: ControlOutput) -> bool:
        if self._dry_run:
            return True

        self._frame += 1

        if self.csp.controls_available:
            return self.csp.write_controls(
                gas=out.throttle,
                brake=out.brake,
                steer=out.steer,
                autoshift=True,
            )

        if self._vgp_available and self._vgp is not None:
            return self._write_vgamepad(out)

        return False

    def _write_vgamepad(self, out: ControlOutput) -> bool:
        try:
            vgcfg = cfg.vgamepad

            # Steering → left stick X axis
            if vgcfg.steer_axis == "left_x":
                self._vgp.left_joystick_float(
                    x_value_float=float(out.steer),
                    y_value_float=0.0,
                )
            else:
                self._vgp.right_joystick_float(
                    x_value_float=float(out.steer),
                    y_value_float=0.0,
                )

            # Throttle
            if vgcfg.throttle_trigger == "right":
                self._vgp.right_trigger_float(value_float=float(out.throttle))
            else:
                self._vgp.left_trigger_float(value_float=float(out.throttle))

            # Brake
            if vgcfg.brake_trigger == "left":
                self._vgp.left_trigger_float(value_float=float(out.brake))
            else:
                self._vgp.right_trigger_float(value_float=float(out.brake))

            self._vgp.update()
            return True
        except Exception as e:
            print(f"[InputWriter] vgamepad write error: {e}")
            return False

    def release(self):
        """Zero all inputs (call on shutdown or pause)."""
        if not self._dry_run:
            if self.csp.controls_available:
                self.csp.write_controls(0.0, 0.0, 0.0)
            if self._vgp_available and self._vgp:
                try:
                    self._vgp.left_joystick_float(0.0, 0.0)
                    self._vgp.right_trigger_float(0.0)
                    self._vgp.left_trigger_float(0.0)
                    self._vgp.update()
                except Exception:
                    pass

    def close(self):
        self.release()
        if self._vgp:
            try:
                self._vgp.reset()
            except Exception:
                pass
