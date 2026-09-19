#!/usr/bin/env python3
"""
scripts/capture_supervisor.py
-----------------------------------------------------------
Phase 25.9G — keep exactly one capture process alive, or stop and say why.

    python scripts/capture_supervisor.py                 run (Task Scheduler)
    python scripts/capture_supervisor.py --clear-attention

WHAT IT DOES
----------------
Starts `run_capture.py`, watches it, and reacts to how it ended:

    exit 0   clean stop     -> honour a STOP file; otherwise respawn
    exit 1   crash          -> respawn after 5s, 10s, 20s ... (cap 300s)
    exit 2   configuration  -> MANUAL_ATTENTION: restarting cannot fix it
    exit 3   lease busy     -> an orphaned child may still hold it: retry
                               every 30s for up to 10 minutes, then
                               MANUAL_ATTENTION
    exit 4   safety         -> MANUAL_ATTENTION, immediately

CRASH-LOOP PROTECTION (§54). Five crashes inside ten minutes is not bad
luck; it is a defect, and restarting it forever only hides it. The
supervisor writes MANUAL_ATTENTION with the last exit, stops, and will
not start again until a human runs `--clear-attention`. Task Scheduler
relaunching it does not clear that state.

ONE SUPERVISOR (§56). An OS file lock (released by the kernel when the
process dies, however it dies) admits one supervisor per store. Its
lease identity is unique per supervisor process; each child it starts
uses that identity, so a restarted child re-acquires its own lease at
once, while a child left behind by a dead supervisor exits by itself
once the state file stops being refreshed.

STOP. Create `data/capture/STOP` (the deploy script's `stop` does). The
child sees it within a second and shuts down cleanly -- the minute in
progress is written as incomplete and never archived -- and the
supervisor exits 0. While the file exists, a scheduled relaunch exits
at once: an operator's stop outlives the next trigger.

NO CREDENTIALS. Nothing here or in the child reads, stores or passes a
password, token or cookie; the command line is the script path and
file locations only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.run_capture import (  # noqa: E402
    CAPTURE_DIR, DEFAULT_DB, DEFAULT_LOG_DIR, DEFAULT_STOP, configure_logging,
)

STATE_FILE = os.path.join(CAPTURE_DIR, "supervisor.json")
LOCK_FILE = os.path.join(CAPTURE_DIR, "supervisor.lock")

BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
CRASH_WINDOW_SECONDS = 600.0
CRASH_LIMIT = 5
LEASE_RETRY_SECONDS = 30.0
LEASE_RETRY_LIMIT = 20
STOP_GRACE_SECONDS = 60.0
TICK_SECONDS = 5.0

LOG = logging.getLogger("marketlens.capture.supervisor")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SingleInstance:
    """An exclusive OS lock on a file, released when the process ends."""

    def __init__(self, path: str):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True


def read_state(path: str = STATE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_state(state: dict, path: str = STATE_FILE) -> None:
    state["updated_at"] = now_iso()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


class Supervisor:

    def __init__(self, args, python: str = sys.executable,
                 child_script: Optional[str] = None):
        self.args = args
        self.python = python
        self.child_script = child_script or os.path.join(ROOT, "scripts",
                                                         "run_capture.py")
        self.owner = f"capture-supervisor-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.child: Optional[subprocess.Popen] = None
        self.crashes: List[float] = []
        self.lease_retries = 0
        self.state = {"state": "RUNNING", "pid": os.getpid(), "owner": self.owner,
                      "started_at": now_iso(), "restarts": 0, "child_pid": None,
                      "last_exit": None, "reason": ""}

    def command(self) -> List[str]:
        return [self.python, self.child_script,
                "--db", self.args.db, "--owner", self.owner,
                "--stop-file", self.args.stop_file,
                "--supervisor-state", self.args.state_file,
                "--log-dir", self.args.log_dir]

    def spawn(self) -> None:
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stderr = open(os.path.join(self.args.log_dir, "child-stderr.log"), "w",
                      encoding="utf-8")
        self.child = subprocess.Popen(self.command(), cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=stderr,
                                      creationflags=flags)
        stderr.close()
        self.state["child_pid"] = self.child.pid
        LOG.info("started capture child pid %d", self.child.pid)
        self.save()

    def save(self) -> None:
        write_state(self.state, self.args.state_file)

    def attention(self, reason: str, code: int) -> int:
        self.state.update(state="MANUAL_ATTENTION", reason=reason, child_pid=None,
                          attention_code=code)
        self.save()
        LOG.critical("MANUAL_ATTENTION: %s", reason)
        return code

    def wait(self, seconds: float) -> bool:
        """Sleep, refreshing the state file. True if a STOP arrived."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if os.path.exists(self.args.stop_file):
                return True
            self.save()
            time.sleep(min(TICK_SECONDS, max(0.0, deadline - time.monotonic())))
        return os.path.exists(self.args.stop_file)

    def stop_child(self) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        try:
            self.child.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            LOG.warning("child did not stop within %.0fs; terminating",
                        STOP_GRACE_SECONDS)
            self.child.terminate()
            self.child.wait(timeout=30)

    def run(self) -> int:
        self.spawn()
        while True:
            if os.path.exists(self.args.stop_file):
                LOG.info("STOP file present; stopping capture")
                self.stop_child()
                self.state.update(state="STOPPED", child_pid=None, reason="STOP file")
                self.save()
                return 0
            code = self.child.poll()
            if code is None:
                self.save()
                time.sleep(TICK_SECONDS)
                continue

            self.state["last_exit"] = {"code": code, "at": now_iso()}
            self.state["child_pid"] = None
            LOG.info("capture child exited with %d", code)
            if code == 2:
                return self.attention("configuration error (exit 2); see capture.log", 2)
            if code == 4:
                return self.attention("SAFETY: a venue write was attempted (exit 4)", 4)
            if code == 3:
                self.lease_retries += 1
                if self.lease_retries > LEASE_RETRY_LIMIT:
                    return self.attention("capture lease held by another runner "
                                          "for over 10 minutes (exit 3)", 3)
                if self.wait(LEASE_RETRY_SECONDS):
                    continue
                self.spawn()
                continue
            self.lease_retries = 0
            if code == 0:
                if os.path.exists(self.args.stop_file):
                    continue
                self.state["restarts"] += 1
                if self.wait(BACKOFF_START):
                    continue
                self.spawn()
                continue

            now = time.monotonic()
            self.crashes = [t for t in self.crashes if now - t < CRASH_WINDOW_SECONDS]
            self.crashes.append(now)
            if len(self.crashes) >= CRASH_LIMIT:
                return self.attention(
                    f"crash loop: {len(self.crashes)} crashes in "
                    f"{CRASH_WINDOW_SECONDS / 60:.0f} minutes (last exit {code})", 1)
            delay = min(BACKOFF_START * 2 ** (len(self.crashes) - 1), BACKOFF_CAP)
            self.state["restarts"] += 1
            LOG.warning("restarting in %.0fs (crash %d of %d allowed)",
                        delay, len(self.crashes), CRASH_LIMIT - 1)
            if self.wait(delay):
                continue
            self.spawn()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--stop-file", default=DEFAULT_STOP)
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--lock-file", default=LOCK_FILE)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--clear-attention", action="store_true",
                        help="acknowledge MANUAL_ATTENTION so the next start runs")
    args = parser.parse_args(argv)
    configure_logging(args.log_dir, "supervisor.log")

    previous = read_state(args.state_file)
    if args.clear_attention:
        if previous.get("state") == "MANUAL_ATTENTION":
            previous.update(state="CLEARED", cleared_at=now_iso())
            write_state(previous, args.state_file)
            print("MANUAL_ATTENTION cleared; the next start will run capture.")
        else:
            print("Nothing to clear (state: %s)." % previous.get("state", "none"))
        return 0

    if os.path.exists(args.stop_file):
        LOG.info("STOP file present at launch; not starting (remove it to run)")
        return 0
    if previous.get("state") == "MANUAL_ATTENTION":
        LOG.error("MANUAL_ATTENTION is set (%s); refusing to start until a human "
                  "runs --clear-attention", previous.get("reason"))
        return int(previous.get("attention_code") or 1)

    lock = SingleInstance(args.lock_file)
    if not lock.acquire():
        LOG.error("another capture supervisor is running; exiting")
        return 3
    return Supervisor(args).run()


if __name__ == "__main__":
    sys.exit(main())
