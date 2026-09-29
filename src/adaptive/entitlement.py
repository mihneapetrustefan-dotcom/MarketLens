"""
src/adaptive/entitlement.py
-----------------------------------------
What this account actually receives, as opposed to what it should
(Phase 25.9I-C, spec sections 6, 17, 22, 33, 34, 50).

THE ONE SENTENCE THIS MODULE IS FOR
---------------------------------------
    COST CAP REACHED DOES NOT MEAN EXECUTION-GRADE REALTIME IS VERIFIED.

IBKR stated that realtime streaming follows once an exchange's 150
snapshot requests are reached. That is a provider EXPECTATION. This
module is the only thing in the phase that may turn it into a
verification, and it will only do so from an observed field-6509
marker. `CapState.CAP_REACHED` never reaches into
`EntitlementStatus.REALTIME_VERIFIED`; there is no code path between
them, by construction.

WHAT THE TRANSPORT AUDIT FOUND, AND WHAT FOLLOWS FROM IT
------------------------------------------------------------
Audited 2026-09-29. `ClientPortalTransport` implements ONE market-data
call:

    market_snapshot(conids, fields) -> GET /iserver/marketdata/snapshot

There is no WebSocket client, no subscribe/unsubscribe pair, no stream
reader and no persistent runtime anywhere in the project to hold one
(`src/marketdata/service.py` documents that absence deliberately).

So this phase does NOT build a streaming stack. Spec section 16 says to
reuse streaming if it exists and otherwise implement only the minimum
abstraction required, and a second market-data stack with its own
daemon and reconnect state machine is the opposite of minimum. What is
built instead is `MarketDataChannel`, which lets a HOT instrument state
"streaming was wanted and is NOT available here" and fail closed,
rather than quietly being served a snapshot and believing it streamed.

SUBSCRIPTIONS EXIST ANYWAY
------------------------------
Measured live on 2026-09-11 and recorded in `src/marketdata/quotes.py`:
IBKR's first snapshot for a conid it has not seen returns no fields --
the request OPENS a subscription and data arrives on a later call. So
there IS subscription state at the gateway, even without a WebSocket,
and it does NOT survive a gateway restart. `SubscriptionRegistry`
reconciles the desired HOT set against it (spec section 34), which is a
real problem this repository has already hit in production, not a
hypothetical one.

READ-ONLY, ALWAYS
---------------------
Nothing here places, modifies or cancels anything. Verification reads
markers from quotes that were acquired anyway; it never acquires on its
own initiative, because a "free diagnostic" that costs a billable
request is how a budget is breached by something nobody counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.exchange_map import UNKNOWN_EXCHANGE, ExchangeMap
from src.domain.adaptive_models import MarketDataState
from src.domain.market_data_billing_models import (
    BillingPeriod, CapState, EntitlementStatus, ExchangeEntitlement,
)
from src.domain.market_data_models import MarketDataAvailability


class MarketDataChannel(str, Enum):
    """
    How data would physically arrive. Audited, not aspirational.

    STREAM is declared so a HOT requirement can name it and be refused
    honestly. Declaring it does not implement it.
    """
    SNAPSHOT = "snapshot"
    STREAM = "stream"


@dataclass(frozen=True)
class ChannelAvailability:
    """
    What the configured transport can actually do.

    Defaults record the 2026-09-29 audit of `ClientPortalTransport`:
    batched snapshots yes, streaming no. A future transport that gains
    a WebSocket flips `stream_supported` here and nothing else in the
    phase has to learn a new shape.
    """
    snapshot_supported: bool = True
    stream_supported: bool = False
    batched_snapshot: bool = True
    audited_at: str = "2026-09-29"
    transport: str = "ClientPortalTransport"
    note: str = ("no WebSocket client, no subscribe/unsubscribe pair and no "
                 "persistent runtime to hold one")

    def supports(self, channel: MarketDataChannel) -> bool:
        return (self.snapshot_supported if channel is MarketDataChannel.SNAPSHOT
                else self.stream_supported)

    def channel_for(self, state: MarketDataState
                    ) -> Tuple[Optional[MarketDataChannel], str]:
        """
        The channel a desired state would use, and why.

        HOT prefers STREAM (spec section 42: once a venue is capped,
        polling it further is request churn for staler data). With no
        stream available it falls back to SNAPSHOT and SAYS SO, so the
        freshness obligation that HOT carries is evaluated against what
        is really arriving.
        """
        if state is MarketDataState.COLD:
            return None, "delayed, daily or cached data is sufficient"
        if state is MarketDataState.WARM:
            return MarketDataChannel.SNAPSHOT, "a bounded fresh check"
        if self.stream_supported:
            return MarketDataChannel.STREAM, "execution-sensitive; streaming"
        return MarketDataChannel.SNAPSHOT, (
            "execution-sensitive, but this transport has no streaming "
            "channel; snapshot polling is the only path and its age must be "
            "measured at consumption")


#: IBKR field 6509 characters. 'R' realtime, 'D' delayed, 'Z'/'Y' frozen,
#: 'P' snapshot, 'B' book. Only 'R' is execution grade.
def entitlement_from_marker(marker: Optional[str]) -> EntitlementStatus:
    """
    Read the provider's own live/delayed marker.

    Absent or unreadable is UNKNOWN, never assumed live. Mirrors
    `src/marketdata/quotes.py::_availability_of` rather than replacing
    it: that function owns the canonical mapping for quote acquisition,
    and this one answers the entitlement question in entitlement terms.
    """
    if not isinstance(marker, str) or not marker.strip():
        return EntitlementStatus.UNKNOWN
    upper = marker.upper()
    if "D" in upper:
        return EntitlementStatus.DELAYED
    if "R" in upper:
        return EntitlementStatus.REALTIME_VERIFIED
    if "Z" in upper or "Y" in upper:
        return EntitlementStatus.DELAYED
    return EntitlementStatus.UNKNOWN


def entitlement_from_availability(availability: MarketDataAvailability
                                  ) -> EntitlementStatus:
    """Translate an already-normalised availability into entitlement terms."""
    return {
        MarketDataAvailability.AVAILABLE: EntitlementStatus.REALTIME_VERIFIED,
        MarketDataAvailability.DELAYED: EntitlementStatus.DELAYED,
        MarketDataAvailability.UNAVAILABLE: EntitlementStatus.UNAVAILABLE,
        MarketDataAvailability.RESTRICTED: EntitlementStatus.UNAVAILABLE,
    }.get(availability, EntitlementStatus.UNKNOWN)


@dataclass(frozen=True)
class MarkerObservation:
    """One instrument's observed marker at one moment. Evidence."""
    instrument_id: str
    marker: str = ""
    availability: Optional[MarketDataAvailability] = None
    observed_at: Optional[datetime] = None

    @property
    def entitlement(self) -> EntitlementStatus:
        if self.marker:
            return entitlement_from_marker(self.marker)
        if self.availability is not None:
            return entitlement_from_availability(self.availability)
        return EntitlementStatus.UNKNOWN


class EntitlementVerifier:
    """
    Turns observed markers into per-exchange entitlement, and nothing else.

    Never calls a transport. The observations are handed in, because the
    only quotes this phase may reason about are ones some other,
    already-budgeted activity acquired.
    """

    def __init__(self, exchange_map: ExchangeMap):
        self.exchange_map = exchange_map

    def verify(self, observations: Sequence[MarkerObservation], *,
               cap_states: Optional[Mapping[str, CapState]] = None,
               expected_after_cap: Optional[Mapping[str, EntitlementStatus]] = None,
               ) -> Dict[str, ExchangeEntitlement]:
        """
        exchange -> entitlement, from what was actually seen.

        An exchange is REALTIME_VERIFIED only if a realtime marker was
        observed on it and NO delayed marker was. One delayed instrument
        on a venue means the venue is not uniformly realtime, and the
        safe reading of a mixed feed is the pessimistic one -- a caller
        about to price an action cannot act on the average of its
        instruments.
        """
        caps = dict(cap_states or {})
        expectations = dict(expected_after_cap or {})
        by_exchange: Dict[str, List[MarkerObservation]] = {}
        for observation in observations:
            exchange = self.exchange_map.exchange_of(observation.instrument_id)
            by_exchange.setdefault(exchange, []).append(observation)

        results: Dict[str, ExchangeEntitlement] = {}
        for exchange in sorted(by_exchange):
            items = by_exchange[exchange]
            statuses = [item.entitlement for item in items]
            realtime = [i for i in items
                        if i.entitlement is EntitlementStatus.REALTIME_VERIFIED]
            delayed = any(s is EntitlementStatus.DELAYED for s in statuses)

            if realtime and not delayed:
                observed = EntitlementStatus.REALTIME_VERIFIED
                stamped = [i.observed_at for i in realtime if i.observed_at]
                observed_at = max(stamped) if stamped else None
                if observed_at is None:
                    # REALTIME_VERIFIED without a timestamp is refused by
                    # the contract. An untimed observation is downgraded
                    # rather than dropped: something was seen, but it
                    # cannot be aged, so it cannot price an action.
                    observed = EntitlementStatus.UNKNOWN
                marker = realtime[0].marker
            elif delayed:
                observed = EntitlementStatus.DELAYED
                observed_at = None
                marker = next((i.marker for i in items
                               if i.entitlement is EntitlementStatus.DELAYED), "")
            elif all(s is EntitlementStatus.UNAVAILABLE for s in statuses):
                observed, observed_at, marker = (
                    EntitlementStatus.UNAVAILABLE, None, "")
            else:
                observed, observed_at, marker = (
                    EntitlementStatus.UNKNOWN, None, "")

            results[exchange] = ExchangeEntitlement(
                exchange=exchange, observed=observed,
                expected_after_cap=expectations.get(exchange),
                cap_state=caps.get(exchange, CapState.UNKNOWN),
                observed_at=observed_at, observed_marker=marker,
                observation_count=len(items),
                note=("mixed markers on this venue; the pessimistic reading "
                      "applies" if realtime and delayed else ""))
        return results


# ======================================================================
# Subscriptions and recovery (spec sections 33, 34)
# ======================================================================


@dataclass
class SubscriptionRegistry:
    """
    What the gateway is believed to be serving, and how stale that belief is.

    A BELIEF, deliberately named as one. The gateway holds the truth and
    does not report it, so this is reconciled against observed markers
    rather than trusted. After a restart the belief is wrong and
    `reconcile` says so.
    """
    active: Dict[str, MarketDataChannel] = field(default_factory=dict)
    opened_at: Dict[str, datetime] = field(default_factory=dict)
    last_data_at: Dict[str, datetime] = field(default_factory=dict)
    generation: int = 0

    def open(self, instrument_id: str, channel: MarketDataChannel,
             at: datetime) -> None:
        self.active[str(instrument_id)] = channel
        self.opened_at.setdefault(str(instrument_id), at)

    def close(self, instrument_id: str) -> None:
        self.active.pop(str(instrument_id), None)
        self.opened_at.pop(str(instrument_id), None)
        self.last_data_at.pop(str(instrument_id), None)

    def note_data(self, instrument_id: str, at: datetime) -> None:
        self.last_data_at[str(instrument_id)] = at

    def invalidate(self, reason: str = "") -> Dict[str, object]:
        """
        Forget everything after a gateway restart or a lost connection.

        Spec section 34: do not assume prior streaming state survived.
        The count is returned so a caller can record how much belief was
        discarded rather than discovering it later from a gap.
        """
        dropped = len(self.active)
        self.active.clear()
        self.opened_at.clear()
        self.last_data_at.clear()
        self.generation += 1
        return {"dropped_subscriptions": dropped, "generation": self.generation,
                "reason": reason}

    def stale(self, now: datetime, max_silence_seconds: float
              ) -> Tuple[str, ...]:
        """Subscriptions that have gone quiet for too long."""
        if max_silence_seconds <= 0:
            raise ValueError("max_silence_seconds must be > 0")
        out: List[str] = []
        for instrument_id in sorted(self.active):
            last = self.last_data_at.get(instrument_id)
            if last is None:
                reference = self.opened_at.get(instrument_id)
            else:
                reference = last
            if reference is None:
                continue
            if (now - reference).total_seconds() > max_silence_seconds:
                out.append(instrument_id)
        return tuple(out)

    def reconcile(self, desired: Mapping[str, MarketDataState],
                  observed: Mapping[str, EntitlementStatus],
                  availability: ChannelAvailability, *, now: datetime,
                  max_silence_seconds: float = 300.0) -> Dict[str, object]:
        """
        Desired vs believed vs observed. Read-only; opens nothing.

        Returns the work a caller would have to do, and the instruments
        that are HOT by intention but not execution-grade in fact. That
        second list is the whole point: it is the state Phase 25.9H's
        four real sessions were in, and it must be visible rather than
        inferred from a missing fill.
        """
        wanted = {instrument_id: state for instrument_id, state in desired.items()
                  if state is not MarketDataState.COLD}
        to_open = sorted(set(wanted) - set(self.active))
        to_close = sorted(set(self.active) - set(wanted))
        stale = self.stale(now, max_silence_seconds)

        not_execution_grade: List[str] = []
        for instrument_id, state in sorted(wanted.items()):
            if state is not MarketDataState.HOT:
                continue
            if observed.get(instrument_id) is not EntitlementStatus.REALTIME_VERIFIED:
                not_execution_grade.append(instrument_id)
            elif instrument_id in stale:
                not_execution_grade.append(instrument_id)

        channel, channel_reason = availability.channel_for(MarketDataState.HOT)
        return {
            "generation": self.generation,
            "desired_non_cold": len(wanted),
            "believed_active": len(self.active),
            "to_open": to_open,
            "to_close": to_close,
            "stale": list(stale),
            "hot_desired_but_not_execution_grade": not_execution_grade,
            "hot_channel": channel.value if channel else None,
            "hot_channel_reason": channel_reason,
            "stream_supported": availability.stream_supported,
        }


def entitlement_report(entitlements: Mapping[str, ExchangeEntitlement],
                       period: BillingPeriod) -> Dict[str, object]:
    """
    Policy expectation beside account observation, per venue (section 22).

    `realtime_verified_exchanges` is the only list a consumer may treat
    as permission, and `expectation_unmet` names the venues where the
    provider's promise and this account's behaviour disagree -- the most
    informative thing this phase can produce.
    """
    verified = sorted(name for name, e in entitlements.items()
                      if e.is_execution_grade)
    unmet = sorted(name for name, e in entitlements.items() if e.expectation_unmet)
    return {
        "period": period.key,
        "exchanges": {name: e.describe()
                      for name, e in sorted(entitlements.items())},
        "realtime_verified_exchanges": verified,
        "expectation_unmet_exchanges": unmet,
        "account_realtime_verified": bool(verified),
        "note": ("policy expectation and account observation are reported "
                 "separately and are never merged"),
    }
