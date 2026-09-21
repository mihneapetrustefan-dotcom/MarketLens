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
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


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
