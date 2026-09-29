"""
src/domain/market_data_billing_models.py
--------------------------------------------------------
Capped, exchange-aware market-data billing (Phase 25.9I-C).

WHY A NEW MODULE RATHER THAN MORE FIELDS ON THE OLD ONE
-----------------------------------------------------------
Phase 25.9I-A's `MarketDataCostModel` understands a fixed subscription,
a per-request price and a flat free allowance. That shape cannot say
the one thing IBKR's answer to ticket #561227 actually says:

    a price that stops applying, per exchange, part-way through a month

A free allowance is subtracted once, globally. A cap applies per
billing venue, resets per calendar month, and -- in IBKR's telling --
changes what the account is ENTITLED TO once it is reached. Bending the
old dataclass around that would have produced a field whose meaning
depended on which provider was configured.

So the old model is left intact and still governs uncapped plans, and
capped billing gets contracts of its own.

PROVIDER-AGNOSTIC ON PURPOSE (spec section 8)
-------------------------------------------------
Nothing here names IBKR. `ExchangeBillingPolicy` describes "per-request
price, request threshold, maximum variable cost per venue per period",
which is a billing shape several vendors use. The IBKR numbers live in
`src/adaptive/billing.py` as configuration carrying its evidence, so a
second provider is a new constant rather than a new branch.

THE THREE SEPARATIONS THIS MODULE EXISTS TO ENFORCE
-------------------------------------------------------
1.  POLICY vs OBSERVATION. What the provider said should happen is not
    what this account did. `PricingConfidence` never reaches
    ACCOUNT_VERIFIED from a support reply, only from a measurement.

2.  CAP_REACHED vs REALTIME_VERIFIED. Crossing a request threshold is
    an arithmetic fact about a counter. Having realtime data is a fact
    about a quote. `ExchangeBillingState.cap_state` answers the first,
    `ExchangeEntitlement.observed` the second, and no code path lets
    the first imply the second.

3.  DESIRED state vs ACTUAL availability. Wanting HOT data does not
    produce it. Kept apart in `src/adaptive/attention.py`.

UNKNOWN IS NEVER ZERO, AND NEVER CHEAP
------------------------------------------
An exchange with no policy is not free: it is UNKNOWN, priced as
unpriceable, and the governor refuses it. The one number this module
will not produce is a confident-looking small one derived from missing
evidence.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Dict, Optional, Tuple

from src.domain.adaptive_models import (
    MarketDataState, _require_non_negative, require_utc,
)

# ======================================================================
# Evidence quality
# ======================================================================


class PricingConfidence(str, Enum):
    """
    How much is actually known about a price.

    The ladder is deliberately short and the top rung is deliberately
    hard to reach. A support reply -- however clear, however senior the
    signature -- is PROVIDER_STATED. Only a reconciled observation of
    this account's own billing may be ACCOUNT_VERIFIED.

    On 2026-09-24 IBKR's own assistant answered the same billing
    question two contradictory ways inside an hour. That is the reason
    the ladder exists.
    """
    UNKNOWN = "unknown"
    PROVIDER_STATED = "provider_stated"
    ACCOUNT_VERIFIED = "account_verified"


class RequestCountingBasis(str, Enum):
    """
    What the provider counts as ONE request. Materially unresolved.

    `transport.market_snapshot` sends the whole universe in a single
    HTTP call. Whether the provider's counter sees that as one request
    or as one per instrument changes worst-case exposure by a factor of
    31 for the current universe, and ticket #561227 did not say.

    UNKNOWN is the honest default, and callers that need a ceiling ask
    for the conservative basis explicitly.
    """
    PER_HTTP_CALL = "per_http_call"
    PER_INSTRUMENT = "per_instrument"
    UNKNOWN = "unknown"

    @staticmethod
    def conservative() -> "RequestCountingBasis":
        """The basis that bills most, used whenever the truth is unknown."""
        return RequestCountingBasis.PER_INSTRUMENT


# ======================================================================
# Billing periods
# ======================================================================


#: The timezone in which a provider's "calendar month" is evaluated is
#: NOT known for IBKR (ticket #561227 did not say, and the account, the
#: venue and the statement could each imply a different one). UTC is
#: used as a stated, overridable assumption rather than a fact -- see
#: `BillingPeriod.timezone_assumed`.
DEFAULT_BILLING_TIMEZONE = "UTC"


@dataclass(frozen=True)
class BillingPeriod:
    """
    One calendar month of billing, as a value rather than a date range.

    Deterministic and comparable: `BillingPeriod.for_moment(t)` always
    gives the same period for the same instant, so a ledger row written
    in one process lands in the same period as one written in another.
    """
    year: int
    month: int
    timezone_name: str = DEFAULT_BILLING_TIMEZONE
    timezone_assumed: bool = True

    def __post_init__(self):
        if not 1 <= int(self.month) <= 12:
            raise ValueError(f"month must be 1..12 (got {self.month})")
        if int(self.year) < 1970:
            raise ValueError(f"year looks wrong (got {self.year})")

    @property
    def key(self) -> str:
        """YYYY-MM. The stored form, and the join key for usage rows."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month,
                    calendar.monthrange(self.year, self.month)[1])

    def next(self) -> "BillingPeriod":
        year, month = ((self.year + 1, 1) if self.month == 12
                       else (self.year, self.month + 1))
        return BillingPeriod(year, month, self.timezone_name,
                             self.timezone_assumed)

    def previous(self) -> "BillingPeriod":
        year, month = ((self.year - 1, 12) if self.month == 1
                       else (self.year, self.month - 1))
        return BillingPeriod(year, month, self.timezone_name,
                             self.timezone_assumed)

    def contains(self, moment: datetime) -> bool:
        checked = require_utc(moment, "moment")
        return (checked.year, checked.month) == (self.year, self.month)

    @classmethod
    def for_moment(cls, moment: datetime,
                   timezone_name: str = DEFAULT_BILLING_TIMEZONE,
                   timezone_assumed: bool = True) -> "BillingPeriod":
        """
        The period an instant falls in.

        Only UTC is resolved without a timezone database; any other name
        is refused rather than silently treated as UTC, because a period
        boundary off by a day puts requests in the wrong month.
        """
        checked = require_utc(moment, "moment")
        if str(timezone_name).upper() not in ("UTC", "GMT", "Z"):
            raise ValueError(
                f"billing timezone {timezone_name!r} is not resolvable here; "
                "the period must be supplied explicitly rather than guessed")
        return cls(checked.year, checked.month, timezone_name, timezone_assumed)

    @classmethod
    def parse(cls, key: str,
              timezone_name: str = DEFAULT_BILLING_TIMEZONE) -> "BillingPeriod":
        parts = str(key).split("-")
        if len(parts) != 2:
            raise ValueError(f"billing period {key!r} must look like YYYY-MM")
        return cls(int(parts[0]), int(parts[1]), timezone_name)


# ======================================================================
# Policy
# ======================================================================


class CapState(str, Enum):
    """
    Where one exchange stands against its own monthly threshold.

    NO_CAP_CONFIGURED is not "uncapped and therefore fine": it is the
    state that makes a plan unpriceable, because nothing is known about
    where the charging stops.
    """
    BELOW_THRESHOLD = "below_threshold"
    CAP_REACHED = "cap_reached"
    NO_CAP_CONFIGURED = "no_cap_configured"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExchangeBillingPolicy:
    """
    What one billing venue charges, per period, as far as is known.

    `cost_per_request_usd` left None means the venue is unpriceable, so
    every plan touching it is refused. That is the intended behaviour
    for an exchange nobody has priced: 109 of the 130 parent-universe
    members currently have no resolved contract at all, and inventing a
    price for them would be the exact failure this phase prevents.
    """
    exchange: str
    cost_per_request_usd: Optional[float] = None
    request_threshold: Optional[int] = None
    max_variable_cost_usd: Optional[float] = None
    entitlement_expected_after_cap: Optional[MarketDataState] = None
    note: str = ""

    def __post_init__(self):
        if not str(self.exchange).strip():
            raise ValueError("exchange is required")
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        _require_non_negative(self.cost_per_request_usd, "cost_per_request_usd")
        _require_non_negative(self.max_variable_cost_usd, "max_variable_cost_usd")
        if self.request_threshold is not None and int(self.request_threshold) < 0:
            raise ValueError("request_threshold must be >= 0")
        # A threshold without a ceiling describes nothing: after the
        # threshold, what does it cost? Refuse the half-specified policy
        # rather than let a caller read the missing half as zero.
        if (self.request_threshold is None) != (self.max_variable_cost_usd is None):
            raise ValueError(
                f"{self.exchange}: request_threshold and max_variable_cost_usd "
                "must be configured together or not at all")

    @property
    def is_capped(self) -> bool:
        return self.max_variable_cost_usd is not None

    @property
    def is_priceable(self) -> bool:
        return self.cost_per_request_usd is not None

    def cost_for(self, request_count: int) -> Optional[float]:
        """
        The variable cost of `request_count` requests this period.

        None means unpriceable. The cap is applied to the COST, not to
        the count, so a provider that capped at a non-multiple of its
        unit price would still be represented correctly.
        """
        if request_count < 0:
            raise ValueError("request_count must be >= 0")
        if self.cost_per_request_usd is None:
            return None
        uncapped = request_count * self.cost_per_request_usd
        if self.max_variable_cost_usd is None:
            return round(uncapped, 6)
        return round(min(uncapped, self.max_variable_cost_usd), 6)

    def cap_state_for(self, request_count: int) -> CapState:
        if not self.is_capped or self.request_threshold is None:
            return CapState.NO_CAP_CONFIGURED
        return (CapState.CAP_REACHED if request_count >= self.request_threshold
                else CapState.BELOW_THRESHOLD)

    def requests_until_cap(self, request_count: int) -> Optional[int]:
        """How many more requests before the threshold. None if uncapped."""
        if self.request_threshold is None:
            return None
        return max(0, int(self.request_threshold) - int(request_count))


@dataclass(frozen=True)
class ProviderBillingPolicy:
    """
    A provider's exchange policies, with the evidence behind them.

    `evidence_source` and `evidence_date` are required rather than
    decorative: a price with no provenance is indistinguishable from a
    price someone typed, and this repository has already been burned
    once by an unattributed billing claim.
    """
    provider: str
    exchange_policies: Dict[str, ExchangeBillingPolicy] = field(default_factory=dict)
    fixed_monthly_subscription_cost_usd: Optional[float] = None
    counting_basis: RequestCountingBasis = RequestCountingBasis.UNKNOWN
    confidence: PricingConfidence = PricingConfidence.UNKNOWN
    evidence_source: str = ""
    evidence_date: Optional[str] = None
    policy_version: str = "unversioned"
    billing_timezone: str = DEFAULT_BILLING_TIMEZONE
    billing_timezone_assumed: bool = True

    def __post_init__(self):
        if not str(self.provider).strip():
            raise ValueError("provider is required")
        _require_non_negative(self.fixed_monthly_subscription_cost_usd,
                              "fixed_monthly_subscription_cost_usd")
        if self.confidence is not PricingConfidence.UNKNOWN and not (
                str(self.evidence_source).strip() and self.evidence_date):
            raise ValueError(
                "a policy claiming confidence must name its evidence source "
                "and date")
        object.__setattr__(self, "exchange_policies", {
            str(name).strip().upper(): policy
            for name, policy in dict(self.exchange_policies).items()})

    def policy_for(self, exchange: Optional[str]) -> Optional[ExchangeBillingPolicy]:
        """
        The policy for one venue, or None.

        There is deliberately NO fallback to a default policy. An
        exchange nobody configured is an exchange nobody priced, and
        returning a neighbour's price would be a guess with a decimal
        point on it.
        """
        if not exchange or not str(exchange).strip():
            return None
        return self.exchange_policies.get(str(exchange).strip().upper())

    @property
    def known_exchanges(self) -> Tuple[str, ...]:
        return tuple(sorted(self.exchange_policies))

    def maximum_known_variable_cost_usd(
            self, exchanges: Optional[Tuple[str, ...]] = None
    ) -> Tuple[Optional[float], Tuple[str, ...]]:
        """
        Worst case across venues, and the venues that cannot be bounded.

        Returns (ceiling, unbounded). `ceiling` is None when any named
        exchange is unpriceable or uncapped -- an unbounded term makes
        the whole sum unbounded, and reporting the bounded part alone
        would read as a total.
        """
        names = tuple(exchanges) if exchanges is not None else self.known_exchanges
        total, unbounded = 0.0, []
        for name in names:
            policy = self.policy_for(name)
            if policy is None or not policy.is_priceable or not policy.is_capped:
                unbounded.append(str(name).strip().upper() or "<blank>")
                continue
            total += float(policy.max_variable_cost_usd or 0.0)
        if unbounded:
            return None, tuple(sorted(set(unbounded)))
        return round(total, 6), ()


# ======================================================================
# Usage and state
# ======================================================================


@dataclass(frozen=True)
class ExchangeBillingState:
    """
    One exchange, one period: how many requests and what that cost.

    Frozen. Accumulation produces a NEW state (`with_requests`) rather
    than mutating, so a ledger replay and a live tally cannot diverge.
    """
    exchange: str
    period: BillingPeriod
    request_count: int = 0
    policy: Optional[ExchangeBillingPolicy] = None
    first_request_at: Optional[datetime] = None
    last_request_at: Optional[datetime] = None

    def __post_init__(self):
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        if self.request_count < 0:
            raise ValueError("request_count must be >= 0")
        object.__setattr__(self, "first_request_at",
                           require_utc(self.first_request_at, "first_request_at"))
        object.__setattr__(self, "last_request_at",
                           require_utc(self.last_request_at, "last_request_at"))

    @property
    def estimated_cost_usd(self) -> Optional[float]:
        """None when unpriceable -- which is not the same as 0.0."""
        return None if self.policy is None else self.policy.cost_for(self.request_count)

    @property
    def uncapped_cost_usd(self) -> Optional[float]:
        """What it would have cost without the cap. The size of the bet."""
        if self.policy is None or self.policy.cost_per_request_usd is None:
            return None
        return round(self.request_count * self.policy.cost_per_request_usd, 6)

    @property
    def cap_state(self) -> CapState:
        if self.policy is None:
            return CapState.UNKNOWN
        return self.policy.cap_state_for(self.request_count)

    @property
    def requests_until_cap(self) -> Optional[int]:
        return (None if self.policy is None
                else self.policy.requests_until_cap(self.request_count))

    def with_requests(self, count: int,
                      at: Optional[datetime] = None) -> "ExchangeBillingState":
        if count < 0:
            raise ValueError("count must be >= 0")
        moment = require_utc(at, "at")
        return ExchangeBillingState(
            exchange=self.exchange, period=self.period,
            request_count=self.request_count + count, policy=self.policy,
            first_request_at=self.first_request_at or moment,
            last_request_at=moment or self.last_request_at)

    def describe(self) -> Dict[str, object]:
        return {
            "exchange": self.exchange,
            "period": self.period.key,
            "request_count": self.request_count,
            "estimated_cost_usd": self.estimated_cost_usd,
            "uncapped_cost_usd": self.uncapped_cost_usd,
            "cap_state": self.cap_state.value,
            "requests_until_cap": self.requests_until_cap,
            "priceable": self.policy is not None and self.policy.is_priceable,
        }


# ======================================================================
# Entitlement -- what the account actually has
# ======================================================================


class EntitlementStatus(str, Enum):
    """
    What realtime access this account actually has, per exchange.

    REQUESTED_REALTIME is the trap this enum exists to make unspellable
    as success: it means somebody asked, and nothing has confirmed. Only
    REALTIME_VERIFIED is evidence, and only an observed realtime marker
    produces it.
    """
    REQUESTED_REALTIME = "requested_realtime"
    REALTIME_VERIFIED = "realtime_verified"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def is_execution_grade(self) -> bool:
        return self is EntitlementStatus.REALTIME_VERIFIED


@dataclass(frozen=True)
class ExchangeEntitlement:
    """
    Policy expectation and account observation, side by side, never merged.

    `expected_after_cap` is what the provider said would happen.
    `observed` is what a quote actually showed. The two are separate
    fields with separate provenance because the entire point of ticket
    #561227 is that the first does not imply the second.
    """
    exchange: str
    observed: EntitlementStatus = EntitlementStatus.UNKNOWN
    expected_after_cap: Optional[EntitlementStatus] = None
    cap_state: CapState = CapState.UNKNOWN
    observed_at: Optional[datetime] = None
    observed_marker: str = ""
    observation_count: int = 0
    note: str = ""

    def __post_init__(self):
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        object.__setattr__(self, "observed_at",
                           require_utc(self.observed_at, "observed_at"))
        if self.observation_count < 0:
            raise ValueError("observation_count must be >= 0")
        if (self.observed is EntitlementStatus.REALTIME_VERIFIED
                and not self.observed_at):
            raise ValueError(
                "REALTIME_VERIFIED requires the moment it was observed; a "
                "verification with no timestamp cannot be aged or audited")

    @property
    def is_execution_grade(self) -> bool:
        """The only property any execution-sensitive caller may read."""
        return self.observed.is_execution_grade

    @property
    def policy_expects_realtime(self) -> bool:
        return self.expected_after_cap is EntitlementStatus.REALTIME_VERIFIED

    @property
    def expectation_unmet(self) -> bool:
        """Cap reached, realtime expected, and still not observed."""
        return (self.cap_state is CapState.CAP_REACHED
                and self.policy_expects_realtime
                and not self.is_execution_grade)

    def describe(self) -> Dict[str, object]:
        return {
            "exchange": self.exchange,
            "policy_expects_realtime_after_cap": self.policy_expects_realtime,
            "cap_state": self.cap_state.value,
            "observed": self.observed.value,
            "observed_marker": self.observed_marker,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "observations": self.observation_count,
            "execution_grade": self.is_execution_grade,
            "expectation_unmet": self.expectation_unmet,
        }
