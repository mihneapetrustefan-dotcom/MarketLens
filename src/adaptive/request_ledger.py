"""
src/adaptive/request_ledger.py
--------------------------------------------
The local intraday meter for billable market-data requests
(Phase 25.9I-C, spec sections 23, 24, 35, 36, 37).

WHY THIS IS THE ONLY METER THAT WORKS
-----------------------------------------
IBKR updates its snapshot counter once per business day, at end of day.
During a session the provider's meter reads zero no matter what is
happening, so "watch the portal and stop if it climbs" is not a
control -- it is a next-morning autopsy.

Everything that bounds a validation run (request ceiling, cost ceiling,
timeout) is enforced against THIS ledger. The portal figure arrives the
next day and is written into the same rows as a reconciliation, beside
the local estimate rather than over it.

DEDUPLICATION IS COST SAFETY, NOT TIDINESS (section 37)
-----------------------------------------------------------
A thesis near its target, an event, a risk review and an entry scan can
all want the same instrument in the same cycle. Four requests would be
four charges for one piece of information. `coalesce` merges them into
ONE activation that remembers all four requesters, so the later
question "who wanted this, and was it worth it" stays answerable.

FAIL CLOSED, ALWAYS
-----------------------
Every limit refuses rather than sleeping or trimming. A throttle that
silently drops the last eight instruments of a batch produces a session
whose coverage nobody can explain; a refusal produces an error someone
reads.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from src.adaptive.billing import BillingUsage, MarketDataBillingGovernor
from src.adaptive.exchange_map import UNKNOWN_EXCHANGE
from src.data_access.adaptive_market_data_schema import (
    initialize_adaptive_market_data_schema,
)
from src.domain.data_attention_models import (
    AttentionPriority, DataAttentionRequest,
)
from src.domain.market_data_billing_models import (
    BillingPeriod, CapState, RequestCountingBasis,
)

#: Request kinds the ledger knows. `probe` is the read-only entitlement
#: check: it is billable like any other snapshot and is recorded as
#: such, because a "free diagnostic" that quietly costs money is how a
#: budget is breached by something nobody was counting.
REQUEST_SNAPSHOT = "snapshot"
REQUEST_STREAM = "stream"
REQUEST_PROBE = "probe"

#: Minimum seconds between billable requests for the same instrument.
#: Not a calibrated number: it is the capture cadence, which is the
#: fastest rate anything in this repository has ever needed.
DEFAULT_MIN_REFRESH_SECONDS = 60.0

#: A local ceiling that exists so a bug cannot become a bill. Sits
#: BELOW the transport's own 50/min budget so this layer is refused
#: first and the refusal names the adaptive layer.
DEFAULT_MAX_REQUESTS_PER_MINUTE = 40

DEFAULT_RETRY_BUDGET = 3


class RequestRefused(RuntimeError):
    """A billable request was not sent, and the reason it was not."""


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


@dataclass(frozen=True)
class LedgerEntry:
    """One billable request, as recorded. Immutable."""
    request_id: str
    provider: str
    exchange: str
    instrument_id: str
    requested_at: datetime
    billing_period: str
    request_type: str = REQUEST_SNAPSHOT
    counting_basis: str = RequestCountingBasis.UNKNOWN.value
    request_units: int = 1
    estimated_cost_before_cap_usd: Optional[float] = None
    estimated_billed_cost_usd: Optional[float] = None
    cap_state: str = CapState.UNKNOWN.value
    activation_reason: str = ""
    requester: str = ""
    purpose: str = ""
    priority: str = ""
    lineage_id: Optional[str] = None
    session_snapshot_id: Optional[str] = None
    admission_decision_id: Optional[str] = None
    allocation_plan_id: Optional[str] = None
    thesis_id: Optional[str] = None
    validation_run_id: Optional[str] = None
    policy_version: str = ""
    note: str = ""


def request_id_for(provider: str, exchange: str, instrument_id: str,
                   requested_at: datetime, request_type: str,
                   purpose: str = "") -> str:
    """
    Content-derived, so a replay writes the same row.

    Includes the timestamp to the second: two genuine requests for the
    same instrument in the same second would collide, which the minimum
    refresh interval already forbids.
    """
    raw = "|".join((str(provider), str(exchange), str(instrument_id),
                    requested_at.isoformat(timespec="seconds"),
                    str(request_type), str(purpose)))
    return "mdr-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ======================================================================
# Deduplication
# ======================================================================


@dataclass(frozen=True)
class CoalescedRequest:
    """
    Several subsystems' requests for one instrument, merged into one.

    `state` is the HIGHEST requested (HOT beats WARM beats COLD) and
    `priority` the most urgent, because merging must never lose the
    strongest claim. Every requester and reason survives.
    """
    instrument_id: str
    state: object                       # MarketDataState
    priority: AttentionPriority
    requesters: Tuple[str, ...]
    reasons: Tuple[str, ...]
    activation_reasons: Tuple[object, ...] = ()
    lineage_id: Optional[str] = None
    thesis_id: Optional[str] = None
    session_snapshot_id: Optional[str] = None
    admission_decision_id: Optional[str] = None

    @property
    def requester_count(self) -> int:
        return len(self.requesters)


_STATE_RANK = {"cold": 0, "warm": 1, "hot": 2}


def coalesce(requests: Sequence[DataAttentionRequest]
             ) -> Tuple[CoalescedRequest, ...]:
    """
    One activation per instrument, preserving every reason (section 36).

    Deterministic: results are ordered by instrument id and the merged
    tuples are sorted, so the same inputs in a different order produce
    byte-identical records.
    """
    grouped: Dict[str, List[DataAttentionRequest]] = {}
    for request in requests:
        grouped.setdefault(request.instrument_id, []).append(request)

    merged: List[CoalescedRequest] = []
    for instrument_id in sorted(grouped):
        items = grouped[instrument_id]
        strongest = max(items, key=lambda r: _STATE_RANK[r.desired_state.value])
        priority = min((r.priority for r in items), key=lambda p: p.rank)
        merged.append(CoalescedRequest(
            instrument_id=instrument_id,
            state=strongest.desired_state,
            priority=priority,
            requesters=tuple(sorted({r.requester for r in items})),
            reasons=tuple(sorted({r.reason for r in items if r.reason})),
            activation_reasons=tuple(sorted(
                {r.activation_reason for r in items
                 if r.activation_reason is not None},
                key=lambda reason: reason.value)),
            lineage_id=next((r.lineage_id for r in items if r.lineage_id), None),
            thesis_id=next((r.thesis_id for r in items if r.thesis_id), None),
            session_snapshot_id=next(
                (r.session_snapshot_id for r in items if r.session_snapshot_id),
                None),
            admission_decision_id=next(
                (r.admission_decision_id for r in items
                 if r.admission_decision_id), None)))
    return tuple(merged)


# ======================================================================
# The ledger
# ======================================================================


class RequestLedger:
    """
    Append-only accounting for billable requests, with the local limits.

    Holds no transport and sends nothing. A caller asks `authorize`
    BEFORE making a request and `record` after; the ledger's job is to
    be able to refuse, and to know what was spent when the portal
    cannot say.
    """

    def __init__(self, governor: MarketDataBillingGovernor, *,
                 min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
                 max_requests_per_minute: int = DEFAULT_MAX_REQUESTS_PER_MINUTE,
                 retry_budget: int = DEFAULT_RETRY_BUDGET,
                 counting_basis: Optional[RequestCountingBasis] = None):
        if min_refresh_seconds < 0:
            raise ValueError("min_refresh_seconds must be >= 0")
        if max_requests_per_minute <= 0:
            raise ValueError("max_requests_per_minute must be > 0")
        self.governor = governor
        self.usage = BillingUsage(policy=governor.policy)
        self.min_refresh_seconds = float(min_refresh_seconds)
        self.max_requests_per_minute = int(max_requests_per_minute)
        self.retry_budget = int(retry_budget)
        # UNKNOWN means bill conservatively: one unit per instrument.
        self.counting_basis = counting_basis or governor.policy.counting_basis
        self.entries: List[LedgerEntry] = []
        self._last_request: Dict[str, datetime] = {}
        self._recent: Deque[datetime] = deque()
        self._retries: Dict[str, int] = {}

    # ---------------- units ----------------

    def units_for(self, instrument_count: int) -> int:
        """
        How many billable units a batched call of N instruments costs.

        UNKNOWN resolves to the CONSERVATIVE basis -- one unit per
        instrument. Under-counting here would make every ceiling in the
        validation harness 31x too loose.
        """
        basis = self.counting_basis
        if basis is RequestCountingBasis.PER_HTTP_CALL:
            return 1
        return max(0, int(instrument_count))

    # ---------------- limits ----------------

    def _check_rate(self, at: datetime, units: int) -> None:
        cutoff = at - timedelta(seconds=60)
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        if len(self._recent) + units > self.max_requests_per_minute:
            raise RequestRefused(
                f"local adaptive request ceiling of "
                f"{self.max_requests_per_minute}/min would be exceeded "
                f"({len(self._recent)} in the last minute, {units} more "
                f"requested); nothing was sent")

    def _check_refresh(self, instrument_ids: Sequence[str],
                       at: datetime) -> None:
        for instrument_id in instrument_ids:
            last = self._last_request.get(str(instrument_id))
            if last is None:
                continue
            elapsed = (at - last).total_seconds()
            if elapsed < self.min_refresh_seconds:
                raise RequestRefused(
                    f"{instrument_id} was requested {elapsed:.0f}s ago; the "
                    f"minimum refresh interval is {self.min_refresh_seconds:.0f}s")

    def note_retry(self, key: str) -> None:
        """Count a retry, refusing once the budget for `key` is spent."""
        self._retries[key] = self._retries.get(key, 0) + 1
        if self._retries[key] > self.retry_budget:
            raise RequestRefused(
                f"retry budget of {self.retry_budget} exhausted for {key}")

    def reset_retries(self, key: str) -> None:
        self._retries.pop(key, None)

    # ---------------- authorization ----------------

    def authorize(self, instrument_ids: Sequence[str], exchanges: Dict[str, str],
                  at: datetime, period: BillingPeriod, *,
                  priority: AttentionPriority = AttentionPriority.BROAD_SCREENING,
                  purpose: str = "") -> object:
        """
        Whether these requests may be made. Returns a `BillingVerdict`.

        Checks in order: unattributable venue, refresh interval, local
        rate ceiling, then cost. The cheap structural refusals come
        first so a budget question is never asked about a request that
        was never allowed to exist.
        """
        from src.adaptive.billing import BillableRequestPlan

        unattributed = [i for i in instrument_ids
                        if exchanges.get(str(i), UNKNOWN_EXCHANGE)
                        == UNKNOWN_EXCHANGE]
        if unattributed and not self.governor.conservative_unknown_pricing:
            raise RequestRefused(
                f"{len(unattributed)} instrument(s) cannot be attributed to a "
                f"billing exchange ({', '.join(sorted(unattributed)[:5])}); "
                "refusing rather than billing to an unknown venue")

        self._check_refresh(instrument_ids, at)

        by_exchange: Dict[str, int] = {}
        for instrument_id in instrument_ids:
            name = exchanges.get(str(instrument_id), UNKNOWN_EXCHANGE)
            by_exchange[name] = by_exchange.get(name, 0) + 1

        if self.counting_basis is RequestCountingBasis.PER_HTTP_CALL:
            plan_counts = {name: 1 for name in by_exchange}
        else:
            plan_counts = dict(by_exchange)

        self._check_rate(at, sum(plan_counts.values()))

        plan = BillableRequestPlan(
            requests_by_exchange=plan_counts, priority=priority,
            purpose=purpose, instrument_count=len(instrument_ids))
        return self.governor.assess(plan, self.usage, period)

    # ---------------- recording ----------------

    def record(self, instrument_ids: Sequence[str], exchanges: Dict[str, str],
               at: datetime, period: BillingPeriod, *,
               request_type: str = REQUEST_SNAPSHOT,
               priority: AttentionPriority = AttentionPriority.BROAD_SCREENING,
               purpose: str = "", requester: str = "",
               activation_reason: str = "",
               validation_run_id: Optional[str] = None,
               lineage_id: Optional[str] = None,
               session_snapshot_id: Optional[str] = None,
               thesis_id: Optional[str] = None) -> Tuple[LedgerEntry, ...]:
        """
        Record requests that WERE made. One row per instrument.

        A row per instrument even under the per-call basis, because the
        evidence of what was asked for should not change shape with a
        billing assumption that is still unknown. `request_units`
        carries the billing weight instead.
        """
        units_total = self.units_for(len(instrument_ids))
        per_instrument_units = (
            1 if self.counting_basis is not RequestCountingBasis.PER_HTTP_CALL
            else 0)

        written: List[LedgerEntry] = []
        for instrument_id in instrument_ids:
            exchange = exchanges.get(str(instrument_id), UNKNOWN_EXCHANGE)
            venue_policy = self.governor.policy.policy_for(exchange)
            before = self.usage.count_for(exchange, period)
            units = per_instrument_units
            if per_instrument_units:
                self.usage.record(exchange, period, 1, at)
            after = self.usage.count_for(exchange, period)

            cost_before = (venue_policy.cost_for(before)
                           if venue_policy is not None else None)
            cost_after = (venue_policy.cost_for(after)
                          if venue_policy is not None else None)
            billed = (None if cost_before is None or cost_after is None
                      else round(max(0.0, cost_after - cost_before), 6))
            uncapped = (None if venue_policy is None
                        or venue_policy.cost_per_request_usd is None
                        else round(units * venue_policy.cost_per_request_usd, 6))
            cap_state = (venue_policy.cap_state_for(after)
                         if venue_policy is not None else CapState.UNKNOWN)

            entry = LedgerEntry(
                request_id=request_id_for(
                    self.governor.policy.provider, exchange, str(instrument_id),
                    at, request_type, purpose),
                provider=self.governor.policy.provider, exchange=exchange,
                instrument_id=str(instrument_id), requested_at=at,
                billing_period=period.key, request_type=request_type,
                counting_basis=self.counting_basis.value, request_units=units,
                estimated_cost_before_cap_usd=uncapped,
                estimated_billed_cost_usd=billed,
                cap_state=cap_state.value, activation_reason=activation_reason,
                requester=requester, purpose=purpose, priority=priority.value,
                lineage_id=lineage_id, session_snapshot_id=session_snapshot_id,
                thesis_id=thesis_id, validation_run_id=validation_run_id,
                policy_version=self.governor.policy.policy_version)
            self.entries.append(entry)
            written.append(entry)
            self._last_request[str(instrument_id)] = at

        if self.counting_basis is RequestCountingBasis.PER_HTTP_CALL:
            # One billable unit for the whole batch, attributed to every
            # venue the batch touched -- the conservative reading of a
            # per-call charge that spans exchanges.
            for exchange in sorted({exchanges.get(str(i), UNKNOWN_EXCHANGE)
                                    for i in instrument_ids}):
                self.usage.record(exchange, period, 1, at)

        for _ in range(max(1, units_total)):
            self._recent.append(at)
        return tuple(written)

    # ---------------- questions ----------------

    def total_estimated_cost_usd(self, period: BillingPeriod
                                 ) -> Tuple[Optional[float], Tuple[str, ...]]:
        return self.usage.total_variable_cost_usd(period)

    def request_count(self, validation_run_id: Optional[str] = None) -> int:
        if validation_run_id is None:
            return len(self.entries)
        return sum(1 for e in self.entries
                   if e.validation_run_id == validation_run_id)

    def billed_units(self, validation_run_id: Optional[str] = None) -> int:
        return sum(e.request_units for e in self.entries
                   if validation_run_id is None
                   or e.validation_run_id == validation_run_id)

    def run_cost_usd(self, validation_run_id: str) -> float:
        """
        What one validation run has actually cost so far, locally.

        Sums the BILLED (post-cap) estimates, which is what the account
        is expected to be charged. Unpriceable rows contribute nothing
        to this number, which is why `authorize` refuses them upstream
        rather than letting them reach here.
        """
        return round(sum(e.estimated_billed_cost_usd or 0.0
                         for e in self.entries
                         if e.validation_run_id == validation_run_id), 6)

    def uncapped_run_cost_usd(self, validation_run_id: str) -> float:
        """What the run would have cost if the cap did not apply."""
        return round(sum(e.estimated_cost_before_cap_usd or 0.0
                         for e in self.entries
                         if e.validation_run_id == validation_run_id), 6)

    def describe(self, period: BillingPeriod) -> Dict[str, object]:
        total, unpriceable = self.total_estimated_cost_usd(period)
        return {
            "period": period.key,
            "entries": len(self.entries),
            "counting_basis": self.counting_basis.value,
            "estimated_cost_usd": total,
            "unpriceable_exchanges": list(unpriceable),
            "per_exchange": [s.describe() for s in self.usage.states_in(period)],
            "min_refresh_seconds": self.min_refresh_seconds,
            "max_requests_per_minute": self.max_requests_per_minute,
            "retry_budget": self.retry_budget,
        }

    # ---------------- persistence ----------------

    def flush(self, conn: sqlite3.Connection, now: datetime) -> Dict[str, int]:
        """
        Write the ledger to the store. Idempotent; nothing is updated.

        Request rows use `INSERT OR IGNORE` on a content-derived id, so
        a re-flush is a no-op. The usage rollup is REPLACEd from the
        tally rather than incremented, so it cannot drift away from the
        rows that justify it.
        """
        initialize_adaptive_market_data_schema(conn)
        written = 0
        for entry in self.entries:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO adaptive_market_data_requests ("
                "request_id, provider, exchange, instrument_id, requested_at, "
                "billing_period, request_type, counting_basis, request_units, "
                "estimated_cost_before_cap_usd, estimated_billed_cost_usd, "
                "cap_state, activation_reason, requester, purpose, priority, "
                "lineage_id, session_snapshot_id, admission_decision_id, "
                "allocation_plan_id, thesis_id, validation_run_id, "
                "policy_version, note) VALUES ("
                + ",".join("?" * 24) + ")",
                (entry.request_id, entry.provider, entry.exchange,
                 entry.instrument_id, _iso(entry.requested_at),
                 entry.billing_period, entry.request_type, entry.counting_basis,
                 entry.request_units, entry.estimated_cost_before_cap_usd,
                 entry.estimated_billed_cost_usd, entry.cap_state,
                 entry.activation_reason, entry.requester, entry.purpose,
                 entry.priority, entry.lineage_id, entry.session_snapshot_id,
                 entry.admission_decision_id, entry.allocation_plan_id,
                 entry.thesis_id, entry.validation_run_id, entry.policy_version,
                 entry.note))
            written += cursor.rowcount or 0

        periods = {e.billing_period for e in self.entries}
        rollups = 0
        for key in sorted(periods):
            period = BillingPeriod.parse(key)
            for state in self.usage.states_in(period):
                conn.execute(
                    "INSERT OR REPLACE INTO adaptive_market_data_exchange_usage ("
                    "provider, billing_period, exchange, request_count, "
                    "estimated_cost_usd, uncapped_cost_usd, cap_state, "
                    "requests_until_cap, first_request_at, last_request_at, "
                    "portal_reported_count, portal_reported_cost_usd, "
                    "reconciled_at, reconciliation_note, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,"
                    "(SELECT portal_reported_count FROM "
                    " adaptive_market_data_exchange_usage "
                    " WHERE provider=? AND billing_period=? AND exchange=?),"
                    "(SELECT portal_reported_cost_usd FROM "
                    " adaptive_market_data_exchange_usage "
                    " WHERE provider=? AND billing_period=? AND exchange=?),"
                    "(SELECT reconciled_at FROM "
                    " adaptive_market_data_exchange_usage "
                    " WHERE provider=? AND billing_period=? AND exchange=?),"
                    "COALESCE((SELECT reconciliation_note FROM "
                    " adaptive_market_data_exchange_usage "
                    " WHERE provider=? AND billing_period=? AND exchange=?),''),"
                    "?)",
                    (self.governor.policy.provider, key, state.exchange,
                     state.request_count, state.estimated_cost_usd,
                     state.uncapped_cost_usd, state.cap_state.value,
                     state.requests_until_cap, _iso(state.first_request_at),
                     _iso(state.last_request_at),
                     self.governor.policy.provider, key, state.exchange,
                     self.governor.policy.provider, key, state.exchange,
                     self.governor.policy.provider, key, state.exchange,
                     self.governor.policy.provider, key, state.exchange,
                     _iso(now)))
                rollups += 1
        conn.commit()
        return {"requests_written": written, "usage_rows": rollups}

    @classmethod
    def replay(cls, conn: sqlite3.Connection,
               governor: MarketDataBillingGovernor,
               period: BillingPeriod, **kwargs) -> "RequestLedger":
        """
        Rebuild this period's tally from the stored request rows.

        Reads the ROWS, not the rollup: the rollup is a convenience and
        the rows are the evidence. A tally rebuilt from evidence is the
        one that can be trusted to bound a spend.
        """
        ledger = cls(governor, **kwargs)
        try:
            rows = conn.execute(
                "SELECT exchange, requested_at FROM adaptive_market_data_requests "
                "WHERE billing_period = ? ORDER BY requested_at", (period.key,)
            ).fetchall()
        except sqlite3.DatabaseError:
            return ledger
        for exchange, requested_at in rows:
            moment = None
            try:
                moment = datetime.fromisoformat(requested_at)
            except (TypeError, ValueError):
                moment = None
            ledger.usage.record(str(exchange), period, 1, moment)
        return ledger


def reconcile_portal_counter(conn: sqlite3.Connection, *, provider: str,
                             period: BillingPeriod, exchange: str,
                             portal_count: Optional[int],
                             portal_cost_usd: Optional[float],
                             at: datetime, note: str = "") -> Dict[str, object]:
    """
    Write the provider's next-day counter beside the local estimate.

    Deliberately does NOT overwrite the local numbers. A disagreement is
    the single most valuable observation this phase can produce -- it
    would mean the counting basis, the cap, or both, are not what the
    support reply described -- and an overwrite would erase it.
    """
    initialize_adaptive_market_data_schema(conn)
    row = conn.execute(
        "SELECT request_count, estimated_cost_usd FROM "
        "adaptive_market_data_exchange_usage "
        "WHERE provider=? AND billing_period=? AND exchange=?",
        (provider, period.key, str(exchange).upper())).fetchone()
    local_count = row[0] if row else None
    local_cost = row[1] if row else None
    conn.execute(
        "UPDATE adaptive_market_data_exchange_usage "
        "SET portal_reported_count=?, portal_reported_cost_usd=?, "
        "reconciled_at=?, reconciliation_note=? "
        "WHERE provider=? AND billing_period=? AND exchange=?",
        (portal_count, portal_cost_usd, _iso(at), note,
         provider, period.key, str(exchange).upper()))
    conn.commit()
    agrees: Optional[bool] = None
    if portal_count is not None and local_count is not None:
        agrees = int(portal_count) == int(local_count)
    return {
        "provider": provider, "period": period.key,
        "exchange": str(exchange).upper(),
        "local_request_count": local_count,
        "local_estimated_cost_usd": local_cost,
        "portal_reported_count": portal_count,
        "portal_reported_cost_usd": portal_cost_usd,
        "counts_agree": agrees,
        "note": note,
    }
