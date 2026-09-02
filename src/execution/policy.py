"""
src/execution/policy.py
----------------------------
Execution policy and idempotency (Phase 14, spec §11, §20, §39, §42).

EXECUTION POLICY IS AN EXTENSION POINT, NOT AN ALGORITHM
------------------------------------------------------------
Spec §20 asks for the abstraction and §68 forbids building the
algorithms. So a policy here decides one narrow thing: given an intent
and a reference price, what order TYPE and price should be sent. That
is enough to express market, passive limit and aggressive limit — and
enough that a future TWAP or VWAP slices into this same interface
rather than around it.

`TwapPolicy` is deliberately absent rather than stubbed. A class named
after an algorithm it does not implement is worse than no class: it
reads as available in the registry, the UI and the docs.

IDEMPOTENCY IS DERIVED, NOT ASSIGNED
----------------------------------------
An idempotency key must be reproducible from the decision alone, so
that a retry after a crash — where nothing local survived — computes
the same key and recognises its own earlier work. A random uuid
assigned at submission time cannot do that, because the retry has no
way to learn the uuid the lost attempt used.

WHAT GOES INTO THE KEY, AND ONE THING THAT DELIBERATELY DOES NOT
--------------------------------------------------------------------
Included: account, instrument, side, quantity, order type, prices, time
in force, and the intent's own id and version. Excluded: the signal id.

Phase 13 learned this the hard way. Several live signals for one
instrument frequently ask for the same target at the same moment; keyed
on the signal, each produced its own order, and the "duplicate
protection" protected nothing. Keyed on what is actually being
requested, they are one order. The signal id is still carried on the
order for provenance — it just does not participate in identity.

INTENT VERSION IS THE ESCAPE HATCH
--------------------------------------
Two genuinely different orders for the same instrument, side and size
in the same second are rare but real: a scale-in, a correction after a
partial cancel. `intent_version` distinguishes them deliberately, so
the caller states that this is a new request rather than the system
guessing from timing.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Tuple

from collections import deque

from src.domain.broker_models import (
    CanonicalOrderSide, CanonicalOrderType, CanonicalTimeInForce,
    ExecutionRejectCode, finite_or_none,
)


# ============================================================
# Idempotency
# ============================================================

def idempotency_key(account_id: str, instrument_id: str,
                    side: CanonicalOrderSide, quantity: float,
                    order_type: CanonicalOrderType,
                    time_in_force: CanonicalTimeInForce,
                    limit_price: Optional[float] = None,
                    stop_price: Optional[float] = None,
                    intent_id: str = "", intent_version: int = 1) -> str:
    """
    Deterministic identity for one logical order.

    Quantities and prices are rounded to eight decimals before hashing.
    Without that, a float that round-trips through the database as
    100.00000000000001 produces a different key than the one that
    created it, and the duplicate check silently stops working.
    """
    def number(value: Optional[float]) -> str:
        cleaned = finite_or_none(value)
        return "" if cleaned is None else f"{cleaned:.8f}"

    raw = "|".join([
        account_id, instrument_id, side.value, number(quantity),
        order_type.value, time_in_force.value,
        number(limit_price), number(stop_price),
        intent_id, str(intent_version),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def client_order_id(key: str, prefix: str = "ml") -> str:
    """
    The id we ask the broker to echo back.

    Derived from the idempotency key so that a retry sends the SAME
    client id — which is what lets a venue that deduplicates on it do
    so, and what lets us find the order at a venue that does not.
    """
    return f"{prefix}-{key[:20]}"


# ============================================================
# Execution policy
# ============================================================

@dataclass
class PolicyDecision:
    """The order shape a policy chose, and why."""
    order_type: CanonicalOrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    rationale: str = ""


class ExecutionPolicy(ABC):
    """One way of turning an intent plus a price into an order shape."""

    name: str = "abstract"

    @abstractmethod
    def decide(self, side: CanonicalOrderSide,
               reference_price: Optional[float]) -> PolicyDecision:
        ...


class MarketPolicy(ExecutionPolicy):
    """
    Cross the spread. Certain execution, uncertain price.

    The default, because for a daily-bar system the alternative is an
    order that may simply not fill, and a strategy that expected
    exposure and got none is wrong in a way that is harder to see than
    slippage.
    """

    name = "market"

    def decide(self, side: CanonicalOrderSide,
               reference_price: Optional[float]) -> PolicyDecision:
        return PolicyDecision(
            CanonicalOrderType.MARKET,
            rationale="take the price available; fill certainty over price")


class LimitPolicy(ExecutionPolicy):
    """A limit at the reference price. Falls back to market without one."""

    name = "limit"

    def decide(self, side: CanonicalOrderSide,
               reference_price: Optional[float]) -> PolicyDecision:
        price = finite_or_none(reference_price)
        if price is None:
            return PolicyDecision(
                CanonicalOrderType.MARKET,
                rationale="no reference price, so no limit can be set")
        return PolicyDecision(CanonicalOrderType.LIMIT, limit_price=price,
                              rationale="limit at the reference price")


class OffsetLimitPolicy(ExecutionPolicy):
    """
    A limit offset from the reference by a number of basis points.

    Passive (positive offset) sits away from the market and may not
    fill; aggressive (negative) crosses toward it and usually does.
    Both are the same arithmetic with the sign flipped, so they are one
    class rather than two that could drift apart.
    """

    def __init__(self, offset_bps: float, name: str = "offset_limit"):
        self.offset_bps = float(offset_bps)
        self.name = name

    def decide(self, side: CanonicalOrderSide,
               reference_price: Optional[float]) -> PolicyDecision:
        price = finite_or_none(reference_price)
        if price is None:
            return PolicyDecision(
                CanonicalOrderType.MARKET,
                rationale="no reference price, so no limit can be set")
        # A buy improves by moving DOWN, a sell by moving UP, so the
        # offset is applied against the side's direction.
        adjusted = price * (1.0 - self.offset_bps / 10_000.0 * side.sign)
        return PolicyDecision(
            CanonicalOrderType.LIMIT, limit_price=adjusted,
            rationale=(f"limit {self.offset_bps:g} bps "
                       f"{'inside' if self.offset_bps > 0 else 'through'} "
                       f"the reference"))


def passive_limit(offset_bps: float = 10.0) -> OffsetLimitPolicy:
    return OffsetLimitPolicy(abs(offset_bps), name="passive_limit")


def aggressive_limit(offset_bps: float = 10.0) -> OffsetLimitPolicy:
    return OffsetLimitPolicy(-abs(offset_bps), name="aggressive_limit")


#: The policies that exist. TWAP, VWAP and participation are named in
#: the spec as future work and are deliberately not here — a registry
#: entry is a claim that something is available.
POLICIES: Dict[str, ExecutionPolicy] = {
    "market": MarketPolicy(),
    "limit": LimitPolicy(),
    "passive_limit": passive_limit(),
    "aggressive_limit": aggressive_limit(),
}

#: Declared so the UI and docs can say what is planned without
#: implying it works. Nothing dispatches on this.
PLANNED_POLICIES: Tuple[str, ...] = ("twap", "vwap", "participation")


def get_policy(name: str) -> ExecutionPolicy:
    """
    Look up a policy, refusing unknown names loudly.

    Falling back to market on a typo would execute a different strategy
    than the one configured, and would do it silently.
    """
    policy = POLICIES.get(name)
    if policy is None:
        planned = " (planned, not implemented)" if name in PLANNED_POLICIES else ""
        raise ValueError(
            f"unknown execution policy {name!r}{planned}; "
            f"available: {', '.join(sorted(POLICIES))}")
    return policy


# ============================================================
# Rate limiting
# ============================================================

class RateLimiter:
    """
    A sliding-window request budget (spec §42).

    Sliding rather than fixed-bucket: a fixed window lets twice the
    budget through across a boundary, which is exactly when a burst of
    orders at the open would trip a venue's limit.

    Refuses rather than sleeping. A blocking limiter inside a batch job
    turns a rate limit into a job that appears hung, and the caller is
    better placed to decide whether to defer or drop.
    """

    def __init__(self, max_per_minute: Optional[int] = None):
        self.max_per_minute = max_per_minute
        self._times: Deque[datetime] = deque()

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=60)
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

    def allow(self, now: datetime) -> bool:
        if self.max_per_minute is None:
            return True
        self._prune(now)
        return len(self._times) < self.max_per_minute

    def record(self, now: datetime) -> None:
        self._prune(now)
        self._times.append(now)

    def check(self, now: datetime) -> Optional[ExecutionRejectCode]:
        return None if self.allow(now) else ExecutionRejectCode.RATE_LIMITED

    def used(self, now: datetime) -> int:
        self._prune(now)
        return len(self._times)
