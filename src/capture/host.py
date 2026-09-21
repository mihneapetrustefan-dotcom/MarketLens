"""
src/capture/host.py
---------------------------
Keep the host awake while a session is being captured (Phase 25.9H).

WHY. On 2026-09-19 the capture host went to sleep at 16:22 local and did
not wake until Monday; a sleep during market hours loses every minute it
lasts (recorded as HOST_SUSPEND_GAP, never filled). Windows lets a
process ask that the system not IDLE-sleep while it works -- the request
media players make. It is:

  - a runtime request of THIS process, not a power-setting change;
  - made only from the pre-open to the post-close of a trading day, and
    withdrawn outside it and when the process exits (Windows drops it
    with the thread);
  - unable to stop a lid close, a Sleep chosen by a person, or a
    battery-critical sleep. Those still happen and are still recorded.

No-op on other platforms.

POWER SOURCE (Phase 25.9H Stage B). On 2026-09-21 the laptop was taken
off AC at 16:26 UTC with 94% charge and ran flat at 19:24 UTC, 36 minutes
before the close: an unclean power loss, three Windows Update reboots and
a system clock 674 s slow on boot. Keep-awake cannot help with that, so
the runner reads the power source and says so, while there is still
battery left to plug in.
"""

from __future__ import annotations

import sys
from typing import Dict, Optional

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def power_status() -> Dict[str, Optional[int]]:
    """{'on_ac': True/False/None, 'battery_percent': 0-100/None}. Read-only."""
    unknown: Dict[str, Optional[int]] = {"on_ac": None, "battery_percent": None}
    if sys.platform != "win32":
        return unknown
    try:
        import ctypes

        class SystemPowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                        ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]

        status = SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return unknown
        return {"on_ac": {0: False, 1: True}.get(status.ACLineStatus),
                "battery_percent": (status.BatteryLifePercent
                                    if status.BatteryLifePercent <= 100 else None)}
    except Exception:                                      # noqa: BLE001
        return unknown


def set_keep_awake(on: bool) -> bool:
    """Ask Windows to keep the system awake (or stop asking). True if honoured."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:                                      # noqa: BLE001
        return False
