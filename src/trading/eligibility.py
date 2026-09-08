"""
src/trading/eligibility.py
--------------------------------
Which signals may be considered for a trade, and why the others may not (§14).

WHY THIS IS NOT PART OF THE RISK ENGINE
-------------------------------------------
Risk answers "is this position safe for the book". Eligibility answers
"is this signal fit to be a candidate at all" — is it live, is its
model deployable, is its instrument tradeable, does an order for it
already exist. Those are different questions with different owners, and
merging them would put instrument resolution inside a component that
Phase 11 deliberately built without a database connection.

EVERY SIGNAL GETS A VERDICT
-------------------------------
Spec §14: *do not silently discard signals*. `evaluate()` returns one
`SignalEligibility` per signal it was handed, including the eligible
ones. A signal the loop never saw produces no row and that is
different from one it rejected — which is exactly the distinction
that makes the signal-to-trade conversion rate in §21 mean anything.

MODEL GOVERNANCE IS NOT BYPASSED, IT IS LABELLED (§37)
----------------------------------------------------------
Phase 18 is the authority on whether a model may be deployed. This
module asks it and records the answer on every eligibility row. A
signal from a model nobody promoted is not silently blocked and it is
not silently traded: it is marked `experimental=True`, and a session
that has not declared itself experimental refuses it.

That is the honest reading of §37. On this database NO model has been
promoted — `model_promotions` does not exist in production — so
without the experimental path the loop could never place an order at
all, and without the label it would place orders as if governance had
approved something. Both would be wrong in different directions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from src.domain.signal_models import Signal, SignalDirection, SignalStatus
from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, EligibilityCode, SignalEligibility, require_utc,
)

#: A signal whose information cutoff is older than this is stale for
#: trading purposes even if its `valid_until` has not passed. Two
#: different clocks: `valid_until` is the strategy's claim about how
#: long the view holds, this is the operator's claim about how old a
#: view may be before it should be re-derived rather than acted on.
DEFAULT_MAX_SIGNAL_AGE_HOURS = 48.0

#: A reference price older than this cannot support an order. The
#: execution validator has its own staleness rule; this one exists so
#: the refusal is attributed to the SIGNAL stage rather than surfacing
#: as a validation failure three layers later.
DEFAULT_MAX_PRICE_AGE_DAYS = 5.0


def model_of(signal: Signal) -> Optional[str]:
    """
    The trained model behind a signal.

    Phase 10 does not put one on `Signal` — it keeps a list of
    `ModelContribution`, because a signal may aggregate several. The
    heaviest non-abstaining contribution is the one governance is asked
    about; `contributions_summary` on the eligibility row would be a
    nicer answer and is not worth a second table yet.

    Returns None when the signal carries no contribution at all, and
    that is treated as NOT DEPLOYABLE upstream — an unknown model is
    not an approved one.
    """
    best: Optional[Any] = None
    for contribution in getattr(signal, "contributions", None) or []:
        if getattr(contribution, "is_abstention", False):
            continue
        if best is None or (contribution.weight or 0.0) > (best.weight or 0.0):
            best = contribution
    return getattr(best, "trained_model_id", None) if best else None


def strategy_of(signal: Signal) -> Optional[str]:
    provenance = getattr(signal, "provenance", None)
    return getattr(provenance, "strategy_id", None) if provenance else None


def cutoff_of(signal: Signal) -> Optional[datetime]:
    provenance = getattr(signal, "provenance", None)
    return (getattr(provenance, "source_information_cutoff", None)
            if provenance else None)


@dataclass
class EligibilityPolicy:
    """
    The thresholds, in one object with one version.

    Versioned because §29 requires a session's configuration to be
    pinned: a policy that changed mid-session would make the session's
    conversion rate uninterpretable.
    """
    version: str = "elig-v1"
    min_confidence: float = 0.0
    min_strength: float = 0.0
    max_signal_age_hours: float = DEFAULT_MAX_SIGNAL_AGE_HOURS
    max_price_age_days: float = DEFAULT_MAX_PRICE_AGE_DAYS
    allow_experimental_models: bool = False
    #: Instruments an operator has paused. Absent means allowed.
    paused_instruments: Set[str] = None            # type: ignore[assignment]
    #: Strategies an operator has disabled.
    disabled_strategies: Set[str] = None           # type: ignore[assignment]

    def __post_init__(self):
        if self.paused_instruments is None:
            self.paused_instruments = set()
        if self.disabled_strategies is None:
            self.disabled_strategies = set()

    def as_dict(self) -> Dict[str, Any]:
        return {"version": self.version,
                "min_confidence": self.min_confidence,
                "min_strength": self.min_strength,
                "max_signal_age_hours": self.max_signal_age_hours,
                "max_price_age_days": self.max_price_age_days,
                "allow_experimental_models": self.allow_experimental_models,
                "paused_instruments": sorted(self.paused_instruments),
                "disabled_strategies": sorted(self.disabled_strategies)}


@dataclass
class EligibilityContext:
    """
    Everything the gate needs that is not on the signal itself.

    Assembled once per cycle by the caller and passed in, rather than
    each check reaching for a connection. Same reasoning as Phase 11's
    `PortfolioService`: a component that can query freely can also
    query past its anchor.
    """
    as_of: datetime
    prices: Dict[str, float]
    price_ages_days: Dict[str, float]
    tradeable_instruments: Optional[Set[str]] = None
    #: instrument_id -> quantity of working (unfilled) orders
    open_order_quantity: Dict[str, float] = None      # type: ignore[assignment]
    #: instrument_id -> intent ids already created in this cycle
    existing_intents: Set[str] = None                 # type: ignore[assignment]
    #: Phase 18's verdict, per trained model id.
    model_deployable: Dict[str, bool] = None          # type: ignore[assignment]
    model_status: Dict[str, str] = None               # type: ignore[assignment]

    def __post_init__(self):
        require_utc(self.as_of, "as_of")
        if self.open_order_quantity is None:
            self.open_order_quantity = {}
        if self.existing_intents is None:
            self.existing_intents = set()
        if self.model_deployable is None:
            self.model_deployable = {}
        if self.model_status is None:
            self.model_status = {}


class EligibilityGate:
    """
    One signal in, one verdict out.

    The checks run in a fixed order and stop at the first failure.
    Order matters for the REASON, not the answer: a suppressed signal
    for an unsupported instrument should read "suppressed", because
    that is the fact an operator can act on.
    """

    def __init__(self, policy: Optional[EligibilityPolicy] = None,
                 method_version: str = LOOP_METHOD_VERSION):
        self.policy = policy or EligibilityPolicy()
        self.method_version = method_version

    def evaluate_all(self, signals: Sequence[Signal], cycle_id: str,
                     context: EligibilityContext) -> List[SignalEligibility]:
        return [self.evaluate(signal, cycle_id, context) for signal in signals]

    def evaluate(self, signal: Signal, cycle_id: str,
                 context: EligibilityContext) -> SignalEligibility:
        checks = 0
        model_id = model_of(signal)
        status = context.model_status.get(model_id or "", "")
        # A model nobody promoted is EXPERIMENTAL, not approved. The
        # flag travels onto the row whether the signal passes or not,
        # so §37 is answerable per signal rather than per session.
        experimental = not context.model_deployable.get(model_id or "", False)

        def verdict(code: EligibilityCode, detail: str = "") -> SignalEligibility:
            return SignalEligibility(
                cycle_id=cycle_id, signal_id=signal.signal_id,
                instrument_id=signal.instrument_id, code=code, detail=detail,
                checks_performed=checks, evaluated_at=context.as_of,
                method_version=self.method_version,
                trained_model_id=model_id, model_status=status or None,
                strategy_id=strategy_of(signal),
                experimental=experimental)

        # ---- lifecycle -------------------------------------------
        checks += 1
        if signal.status is not SignalStatus.ACTIVE:
            if signal.status is SignalStatus.SUPPRESSED:
                reasons = ", ".join(
                    getattr(r, "value", str(r))
                    for r in getattr(signal, "suppression_reasons", []))
                return verdict(EligibilityCode.SUPPRESSED,
                               reasons or "withheld by the signal engine")
            if signal.status is SignalStatus.EXPIRED:
                return verdict(EligibilityCode.EXPIRED,
                               "the signal engine marked it expired")
            return verdict(EligibilityCode.NOT_ACTIVE,
                           f"status is {signal.status.value}")

        checks += 1
        if signal.direction not in (SignalDirection.LONG, SignalDirection.SHORT):
            return verdict(EligibilityCode.NO_DIRECTION,
                           f"direction is {signal.direction.value}, which is "
                           f"not a position")

        checks += 1
        if getattr(signal, "suppression_reasons", None):
            reasons = ", ".join(getattr(r, "value", str(r))
                                for r in signal.suppression_reasons)
            return verdict(EligibilityCode.SUPPRESSED, reasons)

        # ---- validity window -------------------------------------
        checks += 1
        valid_from = getattr(signal, "valid_from", None)
        if valid_from is not None and context.as_of < valid_from:
            return verdict(EligibilityCode.NOT_YET_VALID,
                           f"valid from {valid_from.isoformat()}")

        checks += 1
        if signal.is_expired_at(context.as_of):
            return verdict(EligibilityCode.EXPIRED,
                           f"valid_until {signal.valid_until.isoformat()} "
                           f"is in the past")

        # ---- staleness of the information, not of the row --------
        checks += 1
        cutoff = cutoff_of(signal)
        if cutoff is not None:
            age_hours = (context.as_of - cutoff).total_seconds() / 3600.0
            if age_hours > self.policy.max_signal_age_hours:
                return verdict(
                    EligibilityCode.STALE,
                    f"the information behind it is {age_hours:.1f}h old, past "
                    f"the {self.policy.max_signal_age_hours:.0f}h policy")

        # ---- operator switches -----------------------------------
        checks += 1
        if signal.instrument_id in self.policy.paused_instruments:
            return verdict(EligibilityCode.INSTRUMENT_PAUSED,
                           "an operator paused this instrument")

        checks += 1
        strategy_id = strategy_of(signal)
        if strategy_id and strategy_id in self.policy.disabled_strategies:
            return verdict(EligibilityCode.STRATEGY_DISABLED,
                           f"strategy {strategy_id} is disabled")

        # ---- model governance (§37) ------------------------------
        checks += 1
        if experimental and not self.policy.allow_experimental_models:
            return verdict(
                EligibilityCode.MODEL_NOT_DEPLOYABLE,
                (f"model {model_id or 'unknown'} is not promoted"
                 + (f" ({status})" if status else "")
                 + "; this session did not declare itself experimental"))

        # ---- instrument resolution -------------------------------
        checks += 1
        if (context.tradeable_instruments is not None
                and signal.instrument_id not in context.tradeable_instruments):
            return verdict(EligibilityCode.INSTRUMENT_UNSUPPORTED,
                           "the broker has no contract for this instrument")

        # ---- price -----------------------------------------------
        checks += 1
        price = context.prices.get(signal.instrument_id)
        if price is None or price <= 0:
            return verdict(EligibilityCode.NO_PRICE,
                           "no reference price at the anchor")

        checks += 1
        age_days = context.price_ages_days.get(signal.instrument_id)
        if age_days is not None and age_days > self.policy.max_price_age_days:
            return verdict(
                EligibilityCode.STALE_PRICE,
                f"the reference price is {age_days:.1f} days old, past the "
                f"{self.policy.max_price_age_days:.0f}-day policy")

        # ---- policy thresholds -----------------------------------
        checks += 1
        confidence = getattr(signal, "confidence", None)
        if (self.policy.min_confidence > 0 and confidence is not None
                and confidence < self.policy.min_confidence):
            return verdict(
                EligibilityCode.BELOW_CONFIDENCE_POLICY,
                f"confidence {confidence:.2f} is below the "
                f"{self.policy.min_confidence:.2f} policy")

        checks += 1
        strength = getattr(signal, "strength", None)
        if (self.policy.min_strength > 0 and strength is not None
                and strength < self.policy.min_strength):
            return verdict(
                EligibilityCode.BELOW_STRENGTH_POLICY,
                f"strength {strength:.2f} is below the "
                f"{self.policy.min_strength:.2f} policy")

        # ---- conflicting work already in flight (§7, §14) --------
        checks += 1
        working = context.open_order_quantity.get(signal.instrument_id)
        if working:
            wanted_long = signal.direction is SignalDirection.LONG
            if (working > 0) != wanted_long:
                return verdict(
                    EligibilityCode.CONFLICTING_OPEN_ORDER,
                    f"an order for {working:+.4g} is already working against "
                    f"this direction")

        checks += 1
        if signal.signal_id in context.existing_intents:
            return verdict(EligibilityCode.DUPLICATE_INTENT,
                           "an intent for this signal already exists in this "
                           "cycle")

        return verdict(EligibilityCode.ELIGIBLE)


def summarize(verdicts: Sequence[SignalEligibility]) -> Dict[str, Any]:
    """
    Counts by code, plus the conversion §21 asks for.

    Returned as data rather than printed, so the CLI, the dashboard and
    the validation record all read the same numbers.
    """
    by_code: Dict[str, int] = {}
    for verdict in verdicts:
        by_code[verdict.code.value] = by_code.get(verdict.code.value, 0) + 1
    seen = len(verdicts)
    eligible = sum(1 for v in verdicts if v.is_eligible)
    return {"seen": seen, "eligible": eligible,
            "rate": (eligible / seen) if seen else None,
            "experimental": sum(1 for v in verdicts if v.experimental),
            "by_code": by_code}
