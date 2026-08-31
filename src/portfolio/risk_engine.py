"""
src/portfolio/risk_engine.py
---------------------------------
The layer that decides whether a proposal may become exposure
(Phase 11, spec §21–§24, §40, §56).

WHAT THIS ENGINE JUDGES
---------------------------
The PROJECTED state, not the request in isolation. "May I take NVDA to
18%?" cannot be answered by looking at NVDA: the answer depends on what
the book would look like afterwards — its sector concentration, its
gross exposure, its correlation structure. So every check runs against
a projection of the portfolio as it would be if the proposal were
accepted, and the current value is carried alongside so the arithmetic
in an explanation can be verified (spec §22).

A consequence worth stating: an EMPTY proposal against a book that
already breaches a hard limit is REJECTED, not APPROVED. The verdict
describes the state, and that state is not acceptable. Nothing is lost
by this — there is no change to authorize either way — and it means
`is_approved` can never be true while a hard limit is broken.

APPROVAL IS EARNED, NEVER ASSUMED (spec §56)
------------------------------------------------
The engine starts from "cannot confirm this is safe" and requires
evidence to move. Anything that prevents a check from running — a
position that could not be priced, a stale price, a constraint whose
input is unmeasurable, a multi-currency book with no FX data — yields
INSUFFICIENT_DATA. Missing data is never read as absence of risk.

This is why `evaluated_scopes` and `skipped_scopes` are recorded. An
approval that silently skipped the sector check because sector data was
incomplete would look identical to one that passed it. Here it does
not: the skip is on the decision, with its reason.

THE WATERFALL
-----------------
    0  data sufficiency        -> INSUFFICIENT_DATA
    1  trading state           -> REJECTED (kill switch, reduce-only)
    2  signal confidence       -> per-change eligibility
    3  position weight         -> clamp candidates (REDUCED)
    4  sector weight           -> hard
    5  asset class weight      -> soft
    6  gross / net / leverage  -> hard
    7  liquidity               -> soft
    8  volatility              -> soft
    9  concentration + corr.   -> soft
   10  drawdown                -> soft

Hard breaches reject. Soft breaches downgrade to REQUIRES_REVIEW. A
proposal that only needed trimming to fit comes back REDUCED with the
trimmed changes attached.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from src.domain.portfolio_models import (
    AllocationChange, AllocationProposal, ConstraintScope, ConstraintSeverity,
    ConstraintSet, ExposureBreakdown, ExposureDimension, PortfolioSnapshot,
    RiskDecision, RiskDecisionState, RiskMetrics, RiskProvenance, RiskViolation,
    TradingState, finite_or_none, safe_ratio,
)
from src.domain.signal_models import Signal

RISK_ENGINE_VERSION = "v1"


@dataclass
class EvaluationInputs:
    """
    Everything the engine reads. Assembled by the caller, never fetched
    here.

    The engine holds no database connection on purpose: a component
    that can query freely can also query past its anchor, and the
    point-in-time guarantee would then rest on this file remembering to
    behave. Handing it finished inputs makes the guarantee the
    assembler's single responsibility — and makes the engine trivially
    testable without a database.
    """
    snapshot: PortfolioSnapshot
    constraint_set: ConstraintSet
    #: instrument_id -> sector_id, for projecting sector weights.
    sector_by_instrument: Dict[str, Optional[str]] = field(default_factory=dict)
    #: instrument_id -> asset_class.
    asset_class_by_instrument: Dict[str, Optional[str]] = field(default_factory=dict)
    #: Current exposure breakdowns, for reporting current values.
    exposures: Dict[ExposureDimension, ExposureBreakdown] = field(default_factory=dict)
    metrics: Optional[RiskMetrics] = None
    signals_by_id: Dict[str, Signal] = field(default_factory=dict)
    #: instrument_id -> position size as a fraction of average daily volume.
    liquidity_participation: Dict[str, Optional[float]] = field(default_factory=dict)
    information_cutoff: Optional[datetime] = None


@dataclass
class _Projection:
    """The portfolio as it would be if a change set were accepted."""
    weights: Dict[str, float] = field(default_factory=dict)      # signed
    sector_weights: Dict[str, float] = field(default_factory=dict)
    asset_class_weights: Dict[str, float] = field(default_factory=dict)
    gross: float = 0.0
    net: float = 0.0
    #: Exposure that could not be attributed to a sector — a sector cap
    #: checked against a partial map is not a real cap.
    unclassified_sector_weight: float = 0.0
    has_unclassified_sector: bool = False


class RiskEngine:
    """Evaluates allocation proposals against a versioned constraint set."""

    def __init__(self, constraint_set: ConstraintSet,
                 engine_version: str = RISK_ENGINE_VERSION):
        self.constraint_set = constraint_set
        self.engine_version = engine_version

    # ---------------- identity ----------------

    def _decision_id(self, portfolio_id: str, as_of: datetime,
                     proposal_id: Optional[str]) -> str:
        raw = (f"{self.engine_version}|{self.constraint_set.version}|"
               f"{portfolio_id}|{as_of.isoformat()}|{proposal_id or 'none'}")
        return f"risk-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    # ---------------- projection ----------------

    def _project(self, inputs: EvaluationInputs,
                 changes: Sequence[AllocationChange]) -> _Projection:
        """
        Signed weights per instrument after applying `changes`, then
        aggregated by sector and asset class.

        Equity is held constant: the proposal reallocates the book, it
        does not add or withdraw capital. Any future funding flow is a
        different operation and would need its own handling rather than
        being smuggled in as a weight change.
        """
        snapshot = inputs.snapshot
        projection = _Projection()
        equity = snapshot.equity

        # Start from what is currently held (signed: shorts negative).
        for valuation in snapshot.valuations:
            value = valuation.market_value
            if value is None or equity <= 0:
                continue
            weight = safe_ratio(value, equity)
            if weight is None:
                continue
            key = valuation.position.instrument_id
            projection.weights[key] = projection.weights.get(key, 0.0) + weight

        # Overwrite with targets. A target weight REPLACES the current
        # weight for that instrument rather than adding to it — that is
        # what "take NVDA to 18%" means.
        for change in changes:
            if change.target_weight is None:
                continue
            target = finite_or_none(change.target_weight)
            if target is None:
                continue
            projection.weights[change.instrument_id] = target

        for instrument_id, weight in projection.weights.items():
            projection.gross += abs(weight)
            projection.net += weight

            sector = inputs.sector_by_instrument.get(instrument_id)
            if sector:
                projection.sector_weights[sector] = (
                    projection.sector_weights.get(sector, 0.0) + abs(weight))
            elif abs(weight) > 0:
                projection.unclassified_sector_weight += abs(weight)
                projection.has_unclassified_sector = True

            asset_class = inputs.asset_class_by_instrument.get(instrument_id)
            if asset_class:
                projection.asset_class_weights[asset_class] = (
                    projection.asset_class_weights.get(asset_class, 0.0) + abs(weight))

        return projection

    # ---------------- helpers ----------------

    def _violation(self, constraint, observed: Optional[float],
                   current: Optional[float], message: str,
                   applies_to: Optional[str] = None) -> RiskViolation:
        return RiskViolation(
            constraint_id=constraint.constraint_id, scope=constraint.scope,
            severity=constraint.severity, message=message,
            observed_value=observed, current_value=current,
            limit_value=(constraint.max_value if constraint.max_value is not None
                         else constraint.min_value),
            applies_to=applies_to)

    def _check(self, decision: RiskDecision, constraint, observed: Optional[float],
               current: Optional[float] = None, applies_to: Optional[str] = None,
               label: str = "") -> bool:
        """
        Run one constraint. Returns True when it held or was not
        applicable, False when it was breached.

        An unmeasurable input does NOT count as a pass: it is recorded
        as a skipped scope, which downgrades the final verdict to
        INSUFFICIENT_DATA if the constraint was hard. A hard limit that
        silently evaporates whenever its input is missing is not a
        limit.
        """
        scope_label = label or f"{constraint.scope.value}" + (
            f"[{applies_to}]" if applies_to else "")

        if observed is None:
            if constraint.severity == ConstraintSeverity.HARD:
                decision.skipped_scopes[scope_label] = (
                    f"{constraint.constraint_id} could not be evaluated: "
                    f"no measurement available")
            else:
                decision.skipped_scopes[scope_label] = (
                    f"{constraint.constraint_id} not evaluated (soft, unmeasurable)")
            return True

        decision.evaluated_scopes.append(scope_label)
        breach = constraint.evaluate(observed)
        if breach is None:
            return True

        decision.add_violation(self._violation(
            constraint, observed, current,
            f"{constraint.constraint_id}: {breach}", applies_to))
        return False

    def _hard_scope_skipped(self, decision: RiskDecision) -> bool:
        return any("could not be evaluated" in reason
                   for reason in decision.skipped_scopes.values())

    # ---------------- the waterfall ----------------

    def evaluate(self, inputs: EvaluationInputs,
                 proposal: Optional[AllocationProposal],
                 as_of: datetime) -> RiskDecision:
        """Run every stage and return a decision that explains itself."""
        snapshot = inputs.snapshot
        constraint_set = inputs.constraint_set
        changes = list(proposal.changes) if proposal else []

        decision = RiskDecision(
            decision_id=self._decision_id(
                snapshot.portfolio_id, as_of, proposal.proposal_id if proposal else None),
            portfolio_id=snapshot.portfolio_id,
            state=RiskDecisionState.INSUFFICIENT_DATA,   # earned, not assumed
            as_of=as_of,
            proposal_id=proposal.proposal_id if proposal else None,
            metrics=inputs.metrics,
            provenance=RiskProvenance(
                risk_engine_version=self.engine_version,
                constraint_set_version=constraint_set.version,
                sizing_version=proposal.sizing_version if proposal else None,
                portfolio_snapshot_as_of=snapshot.as_of,
                information_cutoff=inputs.information_cutoff,
                price_data_as_of=snapshot.as_of,
                inputs={
                    "proposed_changes": len(changes),
                    "position_count": len(snapshot.valuations),
                    "trading_state": constraint_set.trading_state.value,
                },
            ),
        )

        # --- stage 0: is the portfolio measurable at all? ---
        if not self._data_is_sufficient(decision, snapshot):
            return decision

        # --- stage 1: global safety state ---
        if not self._trading_state_allows(decision, constraint_set, changes):
            return decision

        # --- stage 2: per-change eligibility and position caps ---
        accepted, reduced = self._screen_changes(decision, inputs, changes)

        # --- stages 4-6: projected portfolio limits ---
        projection = self._project(inputs, accepted)
        self._check_portfolio_limits(decision, inputs, projection)

        # --- stages 7-10: measured risk ---
        self._check_measured_risk(decision, inputs, accepted)

        return self._resolve(decision, proposal, accepted, reduced)

    # ---------------- stage 0 ----------------

    def _data_is_sufficient(self, decision: RiskDecision,
                            snapshot: PortfolioSnapshot) -> bool:
        """
        An empty portfolio is measurable — it is simply empty, and a
        proposal against it can be judged normally. What is NOT
        measurable is a portfolio whose positions could not be priced,
        or priced only from stale data, because every weight in every
        later check divides by an equity figure those gaps corrupt.
        """
        # Negative equity is checked FIRST, before the empty-portfolio
        # shortcut. A book with no positions but a negative cash
        # balance is a debt, not a clean slate, and every limit stated
        # as a fraction of equity is meaningless against it — so it
        # must not fall through the "nothing to measure" path and come
        # back approved.
        if snapshot.equity < 0:
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = (f"equity is negative ({snapshot.equity:.2f}); "
                                f"limits expressed as a fraction of equity are undefined")
            decision.add_reason("no fraction-of-equity limit can be evaluated")
            decision.skipped_scopes["all"] = "negative equity"
            return False

        if snapshot.is_empty:
            decision.add_reason("portfolio holds no positions")
            return True

        if not snapshot.is_complete:
            missing = ", ".join(sorted(
                v.position.instrument_id for v in snapshot.unvalued_positions)[:5])
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = (
                f"{len(snapshot.unvalued_positions)} position(s) could not be priced "
                f"at {snapshot.as_of.isoformat()}")
            decision.add_reason(f"no price at the anchor for: {missing}")
            decision.add_reason(
                "equity is the denominator of every weight, so an unpriced "
                "position makes every limit check unreliable")
            decision.skipped_scopes["all"] = "portfolio could not be fully priced"
            return False

        if snapshot.has_stale_prices:
            stale = ", ".join(sorted(
                v.position.instrument_id for v in snapshot.stale_valuations)[:5])
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = "portfolio priced from stale data"
            decision.add_reason(f"most recent price is older than the freshness limit for: {stale}")
            decision.skipped_scopes["all"] = "stale prices"
            return False

        if snapshot.is_multi_currency:
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = "portfolio spans multiple currencies and no FX data exists"
            decision.add_reason(
                f"currencies held: {sorted(set(snapshot.currencies))}; totals would "
                f"sum mixed units, so no weight can be trusted")
            decision.skipped_scopes["all"] = "multi-currency without FX rates"
            return False

        if snapshot.equity <= 0:
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = f"equity is {snapshot.equity:.2f}; weights are undefined"
            decision.add_reason("no limit expressed as a fraction of equity can be evaluated")
            decision.skipped_scopes["all"] = "non-positive equity"
            return False

        return True

    # ---------------- stage 1 ----------------

    def _trading_state_allows(self, decision: RiskDecision,
                              constraint_set: ConstraintSet,
                              changes: Sequence[AllocationChange]) -> bool:
        state = constraint_set.trading_state
        if state == TradingState.ENABLED:
            return True

        increases = [c for c in changes if c.is_increase]

        if state in (TradingState.EMERGENCY_STOP, TradingState.PAUSED):
            if not changes:
                decision.add_reason(f"trading state is {state.value}; no changes requested")
                return True
            decision.state = RiskDecisionState.REJECTED
            decision.summary = f"trading state is {state.value}"
            decision.add_reason(
                f"{len(changes)} proposed change(s) refused while trading is {state.value}")
            return False

        if state == TradingState.REDUCE_ONLY:
            if not increases:
                decision.add_reason("trading state is reduce_only; no increases requested")
                return True
            decision.state = RiskDecisionState.REJECTED
            decision.summary = "trading state is reduce_only"
            decision.add_reason(
                f"{len(increases)} proposed increase(s) refused; reductions remain allowed")
            return False

        return True

    # ---------------- stage 2-3 ----------------

    def _screen_changes(self, decision: RiskDecision, inputs: EvaluationInputs,
                        changes: Sequence[AllocationChange]
                        ) -> tuple:
        """
        Filter and, where possible, trim individual changes.

        Two outcomes are distinguished, and they are deliberately not
        the same kind of thing:

        DROPPED — the signal behind the change does not clear the
        confidence floor. This is an ELIGIBILITY failure, not a
        statement about the portfolio's state, so it removes that one
        line and lets the rest proceed. Recording it as a violation
        would reject an entire proposal because one of twenty signals
        was weak, which is not what the limit is for.

        TRIMMED — the change is eligible but too large, so it is capped
        and the decision comes back REDUCED. This is what makes REDUCED
        a real outcome rather than a softer rejection.
        """
        confidence_constraint = inputs.constraint_set.first(
            ConstraintScope.MIN_SIGNAL_CONFIDENCE)
        position_constraint = inputs.constraint_set.first(ConstraintScope.POSITION_WEIGHT)

        accepted: List[AllocationChange] = []
        reduced = False

        for change in changes:
            # --- confidence gate: filter, do not reject ---
            if confidence_constraint is not None and change.signal_id:
                signal = inputs.signals_by_id.get(change.signal_id)
                confidence = signal.confidence if signal else None
                floor = confidence_constraint.min_value
                scope_label = f"min_signal_confidence[{change.instrument_id}]"

                if confidence is None:
                    # Unknown confidence is not "good enough" — an
                    # unmeasured input never passes a gate (spec §56).
                    decision.skipped_scopes[scope_label] = (
                        f"signal {change.signal_id} has no confidence recorded")
                    decision.add_reason(
                        f"{change.instrument_id} dropped: signal confidence unknown")
                    continue

                decision.evaluated_scopes.append(scope_label)
                if floor is not None and confidence < floor:
                    decision.add_reason(
                        f"{change.instrument_id} dropped: signal confidence "
                        f"{confidence:.2f} is below the {floor:.2f} floor")
                    continue

            # --- position cap: trim rather than drop ---
            if position_constraint is not None and change.target_weight is not None:
                cap = position_constraint.max_value
                magnitude = abs(change.target_weight)
                if cap is not None and magnitude > cap:
                    decision.evaluated_scopes.append(
                        f"position_weight[{change.instrument_id}]")
                    breach = self._violation(
                        position_constraint, magnitude, change.current_weight,
                        f"max_position_weight: {magnitude:.4f} exceeds maximum "
                        f"{cap:.4f} — trimmed to the cap",
                        applies_to=change.instrument_id)
                    # Recorded so the binding cap stays queryable, but
                    # marked resolved: the engine fixed it here, so it
                    # must not also reject the proposal it just fixed.
                    breach.remediated = True
                    decision.add_violation(breach)

                    sign = -1.0 if change.target_weight < 0 else 1.0
                    scale = safe_ratio(cap, magnitude) or 0.0
                    trimmed = AllocationChange(
                        instrument_id=change.instrument_id,
                        current_weight=change.current_weight,
                        target_weight=sign * cap,
                        current_quantity=change.current_quantity,
                        target_quantity=(change.target_quantity * scale
                                         if change.target_quantity is not None else None),
                        signal_id=change.signal_id,
                        reason=f"{change.reason} (trimmed to {cap:.2%} position cap)",
                    )
                    accepted.append(trimmed)
                    reduced = True
                    continue
                decision.evaluated_scopes.append(f"position_weight[{change.instrument_id}]")

            accepted.append(change)

        return accepted, reduced

    # ---------------- stages 4-6 ----------------

    def _check_portfolio_limits(self, decision: RiskDecision, inputs: EvaluationInputs,
                                projection: _Projection) -> None:
        constraint_set = inputs.constraint_set
        current_exposures = inputs.exposures

        # --- per-instrument weight, over the PROJECTED book ---
        # Screening only checks proposed changes, which would let a
        # position that is ALREADY oversized pass unexamined whenever
        # nobody proposed to touch it. The engine judges the projected
        # state, so the state is what gets checked here. A change that
        # screening already trimmed sits exactly at the cap and passes.
        position_constraint = constraint_set.first(ConstraintScope.POSITION_WEIGHT)
        if position_constraint is not None:
            for instrument_id, weight in sorted(projection.weights.items()):
                scope_label = f"position_weight[{instrument_id}]"
                if scope_label in decision.evaluated_scopes:
                    continue        # already screened as a proposed change
                self._check(decision, position_constraint, abs(weight),
                            inputs.snapshot.weight_of(instrument_id),
                            instrument_id, label=scope_label)

        # --- sector ---
        sector_current = current_exposures.get(ExposureDimension.SECTOR)
        for sector_id, weight in sorted(projection.sector_weights.items()):
            constraint = constraint_set.first(ConstraintScope.SECTOR_WEIGHT, sector_id)
            if constraint is None:
                continue
            current = None
            if sector_current is not None:
                bucket = sector_current.bucket_for(sector_id)
                current = bucket.weight if bucket else None
            self._check(decision, constraint, weight, current, sector_id,
                        label=f"sector_weight[{sector_id}]")

        # A sector cap evaluated over a partial map is not a real cap.
        if projection.has_unclassified_sector:
            sector_constraint = constraint_set.first(ConstraintScope.SECTOR_WEIGHT)
            if sector_constraint is not None and sector_constraint.severity == ConstraintSeverity.HARD:
                decision.skipped_scopes["sector_weight[unclassified]"] = (
                    f"{projection.unclassified_sector_weight:.2%} of projected exposure "
                    f"has no sector in the canonical tables and could not be evaluated")

        # --- asset class ---
        asset_current = current_exposures.get(ExposureDimension.ASSET_CLASS)
        for asset_class, weight in sorted(projection.asset_class_weights.items()):
            constraint = constraint_set.first(ConstraintScope.ASSET_CLASS_WEIGHT, asset_class)
            if constraint is None:
                continue
            current = None
            if asset_current is not None:
                bucket = asset_current.bucket_for(asset_class)
                current = bucket.weight if bucket else None
            self._check(decision, constraint, weight, current, asset_class,
                        label=f"asset_class_weight[{asset_class}]")

        # --- gross / net / leverage ---
        snapshot = inputs.snapshot
        equity = snapshot.equity

        gross_constraint = constraint_set.first(ConstraintScope.GROSS_EXPOSURE)
        if gross_constraint is not None:
            self._check(decision, gross_constraint, projection.gross,
                        safe_ratio(snapshot.gross_exposure, equity))

        net_constraint = constraint_set.first(ConstraintScope.NET_EXPOSURE)
        if net_constraint is not None:
            # Absolute: a net SHORT book of -1.4x is as exposed as a
            # net long one of +1.4x, and a signed comparison against a
            # maximum would wave it straight through.
            self._check(decision, net_constraint, abs(projection.net),
                        abs(snapshot.net_exposure / equity) if equity > 0 else None)

        leverage_constraint = constraint_set.first(ConstraintScope.LEVERAGE)
        if leverage_constraint is not None:
            self._check(decision, leverage_constraint, projection.gross, snapshot.leverage)

    # ---------------- stages 7-10 ----------------

    def _check_measured_risk(self, decision: RiskDecision, inputs: EvaluationInputs,
                             changes: Sequence[AllocationChange]) -> None:
        constraint_set = inputs.constraint_set
        metrics = inputs.metrics

        # --- liquidity, per instrument being increased ---
        liquidity_constraint = constraint_set.first(ConstraintScope.MIN_LIQUIDITY)
        if liquidity_constraint is not None:
            for change in changes:
                if not change.is_increase:
                    continue
                participation = inputs.liquidity_participation.get(change.instrument_id)
                self._check(decision, liquidity_constraint, participation,
                            applies_to=change.instrument_id,
                            label=f"liquidity[{change.instrument_id}]")

        if metrics is None:
            decision.skipped_scopes["measured_risk"] = "no risk metrics were supplied"
            return

        # --- volatility ---
        volatility_constraint = constraint_set.first(ConstraintScope.PORTFOLIO_VOLATILITY)
        if volatility_constraint is not None:
            observed = (None if metrics.volatility.insufficient_data
                        else metrics.volatility.value)
            if observed is None:
                decision.skipped_scopes["portfolio_volatility"] = (
                    metrics.volatility.note or "volatility not measurable")
            else:
                self._check(decision, volatility_constraint, observed)

        # --- concentration ---
        hhi_constraint = constraint_set.first(ConstraintScope.CONCENTRATION_HHI)
        if hhi_constraint is not None:
            self._check(decision, hhi_constraint, metrics.concentration.hhi)

        # --- drawdown ---
        drawdown_constraint = constraint_set.first(ConstraintScope.DRAWDOWN)
        if drawdown_constraint is not None:
            if metrics.drawdown.insufficient_data or metrics.drawdown.max_drawdown is None:
                decision.skipped_scopes["drawdown"] = (
                    "no stored equity history for this portfolio yet")
            else:
                # Stored as a negative fraction; limits are stated as
                # positive magnitudes.
                self._check(decision, drawdown_constraint,
                            abs(metrics.drawdown.max_drawdown))

        # --- correlation: reported, never a hard gate ---
        pairs = metrics.correlation.highly_correlated_pairs
        if pairs:
            names = ", ".join(f"{a}/{b} {c:.2f}" for a, b, c in pairs[:3])
            decision.add_reason(
                f"{len(pairs)} highly correlated pair(s) held — {names}; "
                f"these move together and diversify less than position count suggests")

    # ---------------- resolution ----------------

    def _resolve(self, decision: RiskDecision, proposal: Optional[AllocationProposal],
                 accepted: Sequence[AllocationChange], reduced: bool) -> RiskDecision:
        """Fold the accumulated evidence into one verdict."""
        blocking = decision.blocking_violations
        soft = decision.soft_violations

        if blocking:
            decision.state = RiskDecisionState.REJECTED
            decision.summary = blocking[0].message
            for violation in blocking:
                decision.add_reason(violation.message)
            return decision

        if self._hard_scope_skipped(decision):
            decision.state = RiskDecisionState.INSUFFICIENT_DATA
            decision.summary = "a hard constraint could not be evaluated"
            for scope, reason in decision.skipped_scopes.items():
                if "could not be evaluated" in reason:
                    decision.add_reason(f"{scope}: {reason}")
            return decision

        if soft:
            decision.state = RiskDecisionState.REQUIRES_REVIEW
            decision.summary = (f"{len(soft)} soft limit(s) exceeded — "
                                f"allowed, but worth a look")
            for violation in soft:
                decision.add_reason(violation.message)
            decision.approved_changes = list(accepted)
            return decision

        if reduced:
            decision.state = RiskDecisionState.REDUCED
            decision.summary = "approved after trimming to position limits"
            decision.approved_changes = list(accepted)
            return decision

        decision.state = RiskDecisionState.APPROVED
        decision.approved_changes = list(accepted)
        if proposal is None or not proposal.changes:
            decision.summary = "no changes proposed; current state is within all limits"
        else:
            dropped = len(proposal.changes) - len(accepted)
            decision.summary = (
                f"{len(accepted)} change(s) approved within all limits"
                + (f"; {dropped} dropped on eligibility" if dropped > 0 else ""))
        return decision
