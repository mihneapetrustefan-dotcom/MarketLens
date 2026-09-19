#!/usr/bin/env python3
"""
scripts/run_capture.py
-----------------------------------------------------------
Phase 25.9G — the capture process. Normally started by the supervisor.

    python scripts/run_capture.py [--db data/capture/intraday_capture.db]
                                  [--universe config/capture_universe_v1.json]
                                  [--owner <lease owner>]

CAPTURE ONLY. The gateway it builds refuses every venue write at the
gateway and at the transport; no model, signal, risk approval or trading
mode is read. It asks the IBKR Client Portal Gateway whether a human has
logged in and waits if not -- it never logs in itself and holds no
credential.

EXIT CODES (read by the supervisor and by capture_status)
    0  clean stop (STOP file, signal, or the supervisor went away)
    1  crash -- the supervisor restarts it with backoff
    2  configuration -- restarting cannot help
    3  another capture runner holds the lease
    4  a venue write was attempted and refused -- stop for a human
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.capture.runner import (  # noqa: E402
    EXIT_CLEAN, EXIT_CONFIG, EXIT_CRASH, EXIT_DUPLICATE, EXIT_SAFETY,
    CaptureConfigError, CaptureRunner, CaptureSafetyViolation,
)
from src.capture.stack import build_capture_gateway  # noqa: E402
from src.capture.universe import (  # noqa: E402
    DEFAULT_UNIVERSE_PATH, UniverseVersionConflict, load_definition,
)
from src.trading import leases  # noqa: E402
from src.trading.clock import WallClock  # noqa: E402

CAPTURE_DIR = os.path.join(ROOT, "data", "capture")
DEFAULT_DB = os.path.join(CAPTURE_DIR, "intraday_capture.db")
DEFAULT_STOP = os.path.join(CAPTURE_DIR, "STOP")
DEFAULT_LOG_DIR = os.path.join(CAPTURE_DIR, "logs")

#: A child whose supervisor stopped refreshing its state file for this
#: long assumes it was orphaned and exits cleanly, releasing the lease.
SUPERVISOR_STALE_SECONDS = 90.0

LOG = logging.getLogger("marketlens.capture")


def configure_logging(log_dir: str, name: str = "capture.log") -> None:
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, name),
                                  maxBytes=5 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [handler, console]


#: Research/production stores capture must never write into. The capture
#: store is its own file; see src/capture/schema.py for why.
FORBIDDEN_STORES = ("marketlens.db",)


class UnsafeStore(RuntimeError):
    """The store path is invalid, unintended or unreadable. Fail closed."""


def open_store(path: str) -> sqlite3.Connection:
    """
    Open the capture store, or refuse (section 100).

    Only the DEFAULT location's directory is ever created. Any other path
    must already have its directory, so a typo cannot quietly start a
    fresh empty store somewhere and report success. The production
    research database is refused by name, and an existing file must pass
    SQLite's own integrity check before a single row is written.
    """
    absolute = os.path.abspath(path)
    if os.path.basename(absolute).lower() in FORBIDDEN_STORES:
        raise UnsafeStore(f"{absolute} is the research database; capture writes "
                          f"only to its own store")
    parent = os.path.dirname(absolute)
    if absolute == os.path.abspath(DEFAULT_DB):
        os.makedirs(parent, exist_ok=True)
    elif not os.path.isdir(parent):
        raise UnsafeStore(f"directory {parent} does not exist; refusing to create "
                          f"a store in an unintended location")
    try:
        conn = sqlite3.connect(absolute, timeout=30)
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise UnsafeStore(f"{absolute} is not a usable SQLite database: {error}")
    if check != "ok":
        conn.close()
        raise UnsafeStore(f"{absolute} failed its integrity check: {check}")
    # WAL, so capture_status and research reads proceed while it writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def supervisor_gone(state_path: str) -> bool:
    if not state_path:
        return False
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        stamp = datetime.fromisoformat(state["updated_at"])
    except (OSError, ValueError, KeyError):
        return True
    return (datetime.now(timezone.utc) - stamp).total_seconds() > SUPERVISOR_STALE_SECONDS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--universe", default=os.path.join(ROOT, DEFAULT_UNIVERSE_PATH))
    parser.add_argument("--owner", default=None,
                        help="lease owner; the supervisor passes its identity")
    parser.add_argument("--stop-file", default=DEFAULT_STOP)
    parser.add_argument("--supervisor-state", default="",
                        help="exit cleanly if this file stops being refreshed")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    args = parser.parse_args(argv)

    configure_logging(args.log_dir)
    stop = {"signal": False}

    def on_signal(signum, frame):                          # noqa: ARG001
        stop["signal"] = True
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    last_check = {"at": 0.0, "gone": False}

    def stop_requested() -> bool:
        if stop["signal"] or os.path.exists(args.stop_file):
            return True
        now = time.monotonic()
        if now - last_check["at"] >= 10:
            last_check["at"] = now
            last_check["gone"] = supervisor_gone(args.supervisor_state)
        return last_check["gone"]

    try:
        definition = load_definition(args.universe)
    except (OSError, ValueError) as error:
        LOG.error("universe cannot be loaded: %s", error)
        return EXIT_CONFIG

    try:
        conn = open_store(args.db)
    except UnsafeStore as error:
        LOG.error("store refused: %s", error)
        return EXIT_CONFIG
    owner = args.owner or leases.new_owner("capture-manual")
    try:
        gateway = build_capture_gateway(conn)
        runner = CaptureRunner(conn, gateway, definition, clock=WallClock(),
                               lease_owner=owner, stop_requested=stop_requested)
        LOG.info("capture starting: universe %s (%d members), store %s, owner %s",
                 definition.version, len(definition.members), args.db, owner)
        reason = runner.run()
        LOG.info("capture stopped: %s", reason)
        return EXIT_CLEAN
    except (CaptureConfigError, UniverseVersionConflict) as error:
        LOG.error("configuration: %s", error)
        return EXIT_CONFIG
    except leases.LeaseRefused as error:
        LOG.error("duplicate runner: %s", error)
        return EXIT_DUPLICATE
    except CaptureSafetyViolation as error:
        LOG.critical("SAFETY: %s", error)
        return EXIT_SAFETY
    except Exception:                                      # noqa: BLE001
        LOG.critical("capture crashed:\n%s", traceback.format_exc())
        return EXIT_CRASH
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
