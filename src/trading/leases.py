"""
src/trading/leases.py
----------------------------
One operator per broker account (Phase 25.9E).

THE GAP
-----------
Phase 25.8 defined `DEFAULT_LEASE_SECONDS` and never used it. Two
session runners started against the same account each built their own
session, polled, and advanced the loop. The loop's atomic cycle claim
stopped them running the SAME anchor twice; it did nothing about two
runners on different session ids, or on anchors a minute apart, each
reading the account and each free to decide.

THE LEASE
-------------
A row in `session_runner_leases`, keyed by `ibkr:<account>`. Acquired
atomically -- insert if absent, take over only if expired or already
ours -- and renewed every tick under the same owner condition. A runner
that fails to renew has lost the account and must stop acting on it.

A crashed runner cannot hold the account forever: its lease expires and
the next runner takes over, which is the restart path. The takeover is
recorded, never silent.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.data_access.trading_loop_schema import initialize_trading_loop_schema

DEFAULT_LEASE_SECONDS = 300.0


class LeaseRefused(RuntimeError):
    """Another live runner operates this account."""


def lease_scope(broker_id: str, account_id: str) -> str:
    return f"{broker_id or 'ibkr'}:{account_id or 'default'}"


def new_owner(worker: str) -> str:
    """Unique per process, readable per worker."""
    return f"{worker or 'runner'}-{uuid.uuid4().hex[:12]}"


@dataclass
class Lease:
    scope: str
    owner: str
    session_id: str
    acquired_at: datetime
    expires_at: datetime
    took_over_from: str = ""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def acquire(conn: sqlite3.Connection, scope: str, owner: str, now: datetime,
            session_id: str = "",
            ttl_seconds: float = DEFAULT_LEASE_SECONDS) -> Lease:
    """
    Take the account, or raise `LeaseRefused` naming who holds it.

    One conditional statement decides, so two runners racing here get
    exactly one winner from SQLite rather than from timing.
    """
    initialize_trading_loop_schema(conn)
    expires = now + timedelta(seconds=ttl_seconds)
    previous = conn.execute(
        "SELECT owner, expires_at, released_at FROM session_runner_leases "
        "WHERE scope = ?", (scope,)).fetchone()
    cursor = conn.execute("""
        INSERT INTO session_runner_leases
        (scope, owner, session_id, acquired_at, heartbeat_at, expires_at,
         released_at)
        VALUES (?,?,?,?,?,?,NULL)
        ON CONFLICT(scope) DO UPDATE SET
            owner = excluded.owner, session_id = excluded.session_id,
            acquired_at = excluded.acquired_at,
            heartbeat_at = excluded.heartbeat_at,
            expires_at = excluded.expires_at, released_at = NULL
        WHERE session_runner_leases.owner = excluded.owner
           OR session_runner_leases.released_at IS NOT NULL
           OR session_runner_leases.expires_at <= excluded.acquired_at
    """, (scope, owner, session_id, _iso(now), _iso(now), _iso(expires)))
    conn.commit()
    if cursor.rowcount != 1:
        holder = conn.execute(
            "SELECT owner, expires_at FROM session_runner_leases WHERE scope = ?",
            (scope,)).fetchone()
        raise LeaseRefused(
            f"account {scope} is operated by {holder[0]} until {holder[1]}; "
            f"a second runner may not act on it")
    took_over = ""
    if previous and previous[0] != owner and previous[2] is None:
        took_over = previous[0]
    return Lease(scope=scope, owner=owner, session_id=session_id,
                 acquired_at=now, expires_at=expires, took_over_from=took_over)


def renew(conn: sqlite3.Connection, lease: Lease, now: datetime,
          ttl_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
    """Extend our lease. False means we no longer own the account."""
    expires = now + timedelta(seconds=ttl_seconds)
    cursor = conn.execute("""
        UPDATE session_runner_leases
           SET heartbeat_at = ?, expires_at = ?
         WHERE scope = ? AND owner = ? AND released_at IS NULL
           AND expires_at > ?
    """, (_iso(now), _iso(expires), lease.scope, lease.owner, _iso(now)))
    conn.commit()
    if cursor.rowcount == 1:
        lease.expires_at = expires
        return True
    return False


def release(conn: sqlite3.Connection, lease: Lease, now: datetime) -> None:
    conn.execute("""
        UPDATE session_runner_leases SET released_at = ?
         WHERE scope = ? AND owner = ? AND released_at IS NULL
    """, (_iso(now), lease.scope, lease.owner))
    conn.commit()


def holder(conn: sqlite3.Connection, scope: str,
           now: datetime) -> Optional[str]:
    """The owner of a live lease on `scope`, or None."""
    try:
        row = conn.execute("""
            SELECT owner FROM session_runner_leases
             WHERE scope = ? AND released_at IS NULL AND expires_at > ?
        """, (scope, _iso(now))).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return None
        raise
    return row[0] if row else None
