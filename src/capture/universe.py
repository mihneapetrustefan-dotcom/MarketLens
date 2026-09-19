"""
src/capture/universe.py
-------------------------------
The capture universe: a versioned file, its contracts, and the budget.

VERSIONED AND IMMUTABLE (§16-§18)
-------------------------------------
A universe is a committed JSON file (`config/capture_universe_<v>.json`,
written by `scripts/build_capture_universe.py`). Registering it stores
the file's sha256; registering the same version with different content
is refused, because a membership that changed under an unchanged name
would make every session recorded under it ambiguous. A new membership
is a new version.

Which instruments a SESSION expected is copied into
`capture_session_members` when that session opens, with the contract
each resolved to at that moment. A later universe version never
rewrites it -- a coverage figure is always judged against what was
expected on the day.

CONTRACT MAPPING (§19-§21)
------------------------------
Every member is classified, never silently dropped:

    RESOLVED     exactly one IBKR contract; persisted as the Phase 14
                 mapping, so MarketDataService polls it like any other
    AMBIGUOUS    several listings the discriminators cannot separate.
                 NOT retried automatically: a retry returns the same
                 candidates. A human narrows it (run_ibkr.py --resolve
                 --exchange ...), and the next preflight reads that.
    UNSUPPORTED  the venue has no contract for the query. Retried once a
                 day, because listings do change.
    FAILED       a transient error (gateway, rate limit, timeout).
                 Retried with exponential backoff.

An instrument that already carries a resolved mapping costs no request.

THE REQUEST BUDGET (§22)
---------------------------
`IBKR_MAX_REQUESTS_PER_MINUTE` (50 by default) is shared by everything
this process sends. The quote snapshot is ONE request per minute for the
whole universe and the keepalive one more; mapping retries get only what
is left after those are reserved, so a retry storm can never starve the
data it exists to enable. The limit itself is never raised.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Sequence

DEFAULT_UNIVERSE_PATH = "config/capture_universe_v1.json"

#: Exponential retry for transient mapping failures, capped.
FAILED_RETRY_BASE_SECONDS = 300.0
FAILED_RETRY_CAP_SECONDS = 6 * 3600.0
#: A venue that has no contract today is asked again tomorrow.
UNSUPPORTED_RETRY_SECONDS = 24 * 3600.0

#: Requests per minute kept back for the snapshot and the keepalive.
RESERVED_PER_MINUTE = 2


class UniverseVersionConflict(RuntimeError):
    """The same universe version was registered with different content."""


class MappingStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


# ======================================================================
# Definition
# ======================================================================

@dataclass(frozen=True)
class UniverseMember:
    instrument_id: str
    ticker: str
    sector_id: str = ""
    asset_class: str = "stock"
    sec_type: str = "STK"
    currency: str = "USD"
    role: str = "member"


@dataclass
class UniverseDefinition:
    version: str
    sha256: str
    members: List[UniverseMember]
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def instrument_ids(self) -> List[str]:
        return [m.instrument_id for m in self.members]

    def member(self, instrument_id: str) -> Optional[UniverseMember]:
        for m in self.members:
            if m.instrument_id == instrument_id:
                return m
        return None


def load_definition(path: str) -> UniverseDefinition:
    """Read and validate a universe file. Raises ValueError when malformed."""
    with open(path, "rb") as handle:
        blob = handle.read()
    raw = json.loads(blob.decode("utf-8"))
    version = str(raw.get("version") or "").strip()
    if not version:
        raise ValueError(f"{path}: universe has no version")
    members: List[UniverseMember] = []
    seen = set()
    for item in raw.get("instruments") or []:
        instrument_id = str(item.get("instrument_id") or "").strip()
        ticker = str(item.get("ticker") or "").strip()
        if not instrument_id or not ticker:
            raise ValueError(f"{path}: member without instrument_id/ticker: {item}")
        if instrument_id in seen:
            raise ValueError(f"{path}: {instrument_id} listed twice")
        seen.add(instrument_id)
        members.append(UniverseMember(
            instrument_id=instrument_id, ticker=ticker,
            sector_id=str(item.get("sector_id") or ""),
            asset_class=str(item.get("asset_class") or "stock"),
            sec_type=str(item.get("sec_type") or "STK"),
            currency=str(item.get("currency") or "USD"),
            role=str(item.get("role") or "member")))
    if not members:
        raise ValueError(f"{path}: universe has no instruments")
    return UniverseDefinition(version=version,
                              sha256=hashlib.sha256(blob).hexdigest(),
                              members=members, raw=raw)


def register_version(conn: sqlite3.Connection, definition: UniverseDefinition,
                     now: datetime) -> bool:
    """Record the version. True when new; raises on a content conflict."""
    row = conn.execute(
        "SELECT sha256 FROM capture_universe_versions WHERE version = ?",
        (definition.version,)).fetchone()
    if row is not None:
        if row[0] != definition.sha256:
            raise UniverseVersionConflict(
                f"universe {definition.version} was registered with sha256 "
                f"{row[0][:12]} and is now {definition.sha256[:12]}; a version "
                f"is immutable -- write a new version file instead")
        return False
    conn.execute(
        "INSERT INTO capture_universe_versions (version, sha256, "
        "definition_json, registered_at) VALUES (?,?,?,?)",
        (definition.version, definition.sha256,
         json.dumps(definition.raw, sort_keys=True), now.isoformat()))
    conn.commit()
    return True


def ensure_reference_rows(conn: sqlite3.Connection,
                          definition: UniverseDefinition) -> None:
    """
    Minimal instrument rows, so the 25.9F session rules apply unchanged.

    `session_governed` reads `instruments.asset_class`; without a row a
    stock would be treated as ungoverned in this separate store. The
    benchmark is left out on purpose -- that function already governs
    `benchmark-*` ids by name. `INSERT OR IGNORE` throughout: a row that
    exists is never rewritten.
    """
    conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id, name, country, "
                 "timezone) VALUES ('US_AND_INTL', 'US & International "
                 "(unspecified)', 'US', 'America/New_York')")
    for member in definition.members:
        if member.instrument_id.startswith("benchmark-"):
            continue
        company_id = f"capture-{member.instrument_id}"
        conn.execute("INSERT OR IGNORE INTO companies (company_id, "
                     "canonical_name, sector_id) VALUES (?,?,NULL)",
                     (company_id, member.ticker))
        conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, "
                     "instrument_type, currency) VALUES (?,?,?,?)",
                     (company_id, company_id, "equity", member.currency))
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_id, "
                     "security_id, exchange_id, ticker, asset_class) "
                     "VALUES (?,?,?,?,?)",
                     (member.instrument_id, company_id, "US_AND_INTL",
                      member.ticker, member.asset_class))
    conn.commit()


# ======================================================================
# Request budget
# ======================================================================

class RequestBudget:
    """A sliding one-minute window over every request this process sends."""

    def __init__(self, per_minute: int, reserved: int = RESERVED_PER_MINUTE):
        self.per_minute = max(0, int(per_minute))
        self.reserved = max(0, int(reserved))
        self._sent: Deque[datetime] = deque()
        self.refused = 0

    def _trim(self, now: datetime) -> None:
        horizon = now - timedelta(seconds=60)
        while self._sent and self._sent[0] <= horizon:
            self._sent.popleft()

    def used(self, now: datetime) -> int:
        self._trim(now)
        return len(self._sent)

    def spend(self, now: datetime, count: int = 1) -> None:
        for _ in range(max(0, int(count))):
            self._sent.append(now)

    def allow_discretionary(self, now: datetime, count: int = 1) -> bool:
        """For requests that may wait (mapping); reserve kept for data."""
        if self.used(now) + count <= self.per_minute - self.reserved:
            return True
        self.refused += 1
        return False


# ======================================================================
# Contract mapping
# ======================================================================

@dataclass
class MappingOutcome:
    instrument_id: str
    status: MappingStatus
    conid: str = ""
    detail: str = ""
    requested: bool = False


def _existing_conid(conn: sqlite3.Connection, instrument_id: str,
                    broker_id: str = "ibkr") -> str:
    from src.marketdata.universe import _conid_of
    row = conn.execute(
        "SELECT broker_payload_json, tradable FROM broker_instrument_mapping "
        "WHERE broker_id = ? AND canonical_instrument_id = ?",
        (broker_id, instrument_id)).fetchone()
    if row is None or not row[1]:
        return ""
    return _conid_of(row[0])


def _record(conn: sqlite3.Connection, outcome: MappingOutcome, now: datetime,
            next_retry: Optional[datetime]) -> None:
    previous = conn.execute(
        "SELECT attempts FROM capture_mappings WHERE instrument_id = ?",
        (outcome.instrument_id,)).fetchone()
    attempts = (previous[0] if previous else 0) + (1 if outcome.requested else 0)
    conn.execute("""
        INSERT INTO capture_mappings (instrument_id, status, conid, attempts,
            last_attempt_at, next_retry_at, detail)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(instrument_id) DO UPDATE SET
            status = excluded.status, conid = excluded.conid,
            attempts = excluded.attempts,
            last_attempt_at = COALESCE(excluded.last_attempt_at,
                                       capture_mappings.last_attempt_at),
            next_retry_at = excluded.next_retry_at, detail = excluded.detail
    """, (outcome.instrument_id, outcome.status.value, outcome.conid or None,
          attempts, now.isoformat() if outcome.requested else None,
          next_retry.isoformat() if next_retry else None, outcome.detail))


def _classify(resolution: Any) -> MappingStatus:
    from src.domain.broker_models import ExecutionRejectCode
    if resolution.ok:
        return MappingStatus.RESOLVED
    if resolution.ambiguous:
        return MappingStatus.AMBIGUOUS
    if resolution.code is ExecutionRejectCode.NO_INSTRUMENT_MAPPING:
        return MappingStatus.UNSUPPORTED
    return MappingStatus.FAILED


def map_universe(conn: sqlite3.Connection, gateway: Any, repository: Any,
                 definition: UniverseDefinition, now: datetime,
                 budget: Optional[RequestBudget] = None
                 ) -> Dict[str, MappingOutcome]:
    """
    Classify every member, asking IBKR only where it is due and affordable.

    Returns the outcome per instrument. Never raises for a mapping
    failure: an instrument that cannot be mapped is an instrument that
    is not captured today, recorded with its reason.
    """
    outcomes: Dict[str, MappingOutcome] = {}
    for member in definition.members:
        conid = _existing_conid(conn, member.instrument_id)
        if conid:
            outcome = MappingOutcome(member.instrument_id, MappingStatus.RESOLVED,
                                     conid, "mapping already persisted")
            _record(conn, outcome, now, None)
            outcomes[member.instrument_id] = outcome
            continue

        row = conn.execute(
            "SELECT status, next_retry_at, detail, attempts FROM capture_mappings "
            "WHERE instrument_id = ?", (member.instrument_id,)).fetchone()
        if row is not None and row[0] == MappingStatus.AMBIGUOUS.value:
            outcomes[member.instrument_id] = MappingOutcome(
                member.instrument_id, MappingStatus.AMBIGUOUS, "",
                row[2] or "ambiguous; narrow it with run_ibkr.py --resolve")
            continue
        if row is not None and row[1]:
            due = datetime.fromisoformat(row[1])
            if due > now:
                outcomes[member.instrument_id] = MappingOutcome(
                    member.instrument_id, MappingStatus(row[0]), "",
                    f"{row[2]} (next retry {row[1]})")
                continue
        if budget is not None and not budget.allow_discretionary(now):
            outcomes[member.instrument_id] = MappingOutcome(
                member.instrument_id,
                MappingStatus(row[0]) if row else MappingStatus.FAILED, "",
                "deferred: request budget reserved for market data")
            continue

        if budget is not None:
            budget.spend(now)
        try:
            resolution = gateway.resolve_contract(
                member.instrument_id, member.ticker, sec_type=member.sec_type,
                currency=member.currency, persist=True)
        except Exception as error:                        # noqa: BLE001
            outcome = MappingOutcome(member.instrument_id, MappingStatus.FAILED,
                                     "", f"{type(error).__name__}: {error}", True)
        else:
            status = _classify(resolution)
            outcome = MappingOutcome(member.instrument_id, status, "",
                                     resolution.explain(), True)
            if status is MappingStatus.RESOLVED:
                mapping = resolution.contract.as_mapping(member.instrument_id)
                repository.save_mapping(mapping)
                from src.execution.adapters.ibkr.contracts import conid_of
                outcome.conid = str(conid_of(mapping) or "")

        attempts = (row[3] if row else 0) + 1
        next_retry: Optional[datetime] = None
        if outcome.status is MappingStatus.FAILED:
            delay = min(FAILED_RETRY_BASE_SECONDS * (2 ** (attempts - 1)),
                        FAILED_RETRY_CAP_SECONDS)
            next_retry = now + timedelta(seconds=delay)
        elif outcome.status is MappingStatus.UNSUPPORTED:
            next_retry = now + timedelta(seconds=UNSUPPORTED_RETRY_SECONDS)
        _record(conn, outcome, now, next_retry)
        outcomes[member.instrument_id] = outcome
    conn.commit()
    return outcomes


def snapshot_membership(conn: sqlite3.Connection, session_id: str,
                        definition: UniverseDefinition,
                        outcomes: Dict[str, MappingOutcome]) -> None:
    """Freeze what this session expected. Existing rows are updated only
    while the session is being opened, never after it is finalized."""
    for member in definition.members:
        outcome = outcomes.get(member.instrument_id)
        status = outcome.status.value if outcome else MappingStatus.FAILED.value
        conn.execute("""
            INSERT INTO capture_session_members (session_id, instrument_id,
                ticker, mapping_status, conid, detail) VALUES (?,?,?,?,?,?)
            ON CONFLICT(session_id, instrument_id) DO UPDATE SET
                mapping_status = excluded.mapping_status,
                conid = excluded.conid, detail = excluded.detail
            WHERE (SELECT status FROM capture_sessions
                    WHERE session_id = excluded.session_id) != 'finalized'
        """, (session_id, member.instrument_id, member.ticker, status,
              (outcome.conid if outcome else "") or None,
              outcome.detail if outcome else "not attempted"))
    conn.commit()


def resolved_ids(outcomes: Dict[str, MappingOutcome]) -> List[str]:
    return sorted(i for i, o in outcomes.items()
                  if o.status is MappingStatus.RESOLVED)


def mapping_summary(outcomes: Dict[str, MappingOutcome]) -> Dict[str, int]:
    counts = {s.value: 0 for s in MappingStatus}
    for outcome in outcomes.values():
        counts[outcome.status.value] += 1
    return counts
