"""
src/portfolio/service.py
-----------------------------
The Phase 11 entry point: assembles a portfolio's state, measures it,
sizes a proposal from signals, and asks the risk engine for a verdict.

WHY THE ASSEMBLY LIVES IN ONE PLACE
---------------------------------------
The risk engine holds no database connection, by design — a component
that can query freely can also query past its anchor, and then
point-in-time correctness depends on that component remembering to
behave. Moving every read here makes the guarantee one file's single
responsibility, and this file has exactly one rule:

    every read is filtered by `as_of`, and nothing else is read.

Positions open at the anchor, prices at or before the anchor, return
history ending at the anchor, signals whose information cutoff is at or
before the anchor, snapshots recorded at or before the anchor. A
replay anchored at any past moment sees the world as it was, because
there is no accessor here that could show it otherwise.

WHAT `evaluate` DOES NOT DO
-------------------------------
It does not place an order, and it cannot. Its most action-like output
is a list of OrderIntent objects, which are inert records marked
`is_executable=False` with no venue, account or broker anywhere in
them. The boundary is the return type: this function's job ends with a
decision and an intent, and no code path continues past it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.data_access.portfolio_repository import PortfolioRepository
from src.data_access.signal_repository import SignalRepository
from src.domain.portfolio_models import (
    AllocationProposal, ConstraintSet, ExposureDimension, OrderIntent,
    PortfolioSnapshot, Position, PositionValuation, RiskDecision, RiskMetrics,
    ValuationStatus, finite_or_none,
)
from src.domain.signal_models import Signal, SignalDirection
from src.portfolio import analytics
from src.portfolio.constraints import ConstraintRepository, DEFAULT_CONSTRAINT_VERSION
from src.portfolio.exposure import ExposureEngine, InstrumentClassifier
from src.portfolio.risk_engine import EvaluationInputs, RiskEngine
from src.portfolio.sizing import (
    FixedFractionSizing, PositionSizingStrategy, SizingContext,
)
from src.portfolio.valuation import PortfolioValuator, PriceRepository

#: Calendar days of price history pulled for analytics. 365 covers the
#: full span currently cached (2025-11 onward) without pretending to
#: reach further back than the data goes.
DEFAULT_LOOKBACK_DAYS = 365


def _require_utc(moment: datetime, name: str = "as_of") -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {moment.utcoffset()})")
    return moment


@dataclass
class EvaluationResult:
    """Everything one evaluation produced, returned together so nothing is re-derived."""
    snapshot: PortfolioSnapshot
    metrics: RiskMetrics
    proposal: Optional[AllocationProposal]
    decision: RiskDecision
    intents: List[OrderIntent]


class PortfolioService:
    """Assembles portfolio state and risk decisions from stored data only."""

    def __init__(self, conn: sqlite3.Connection,
                 constraint_version: str = DEFAULT_CONSTRAINT_VERSION,
                 lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                 max_price_age_days: Optional[float] = None):
        self.conn = conn
        self.repository = PortfolioRepository(conn)
        self.prices = PriceRepository(
            conn, **({"max_price_age_days": max_price_age_days}
                     if max_price_age_days is not None else {}))
        self.valuator = PortfolioValuator(self.prices)
        self.classifier = InstrumentClassifier(conn)
        self.exposures = ExposureEngine(self.classifier)
        self.constraints = ConstraintRepository(conn)
        self.constraint_version = constraint_version
        self.lookback_days = lookback_days

    # ---------------- state ----------------

    def build_snapshot(self, portfolio_id: str, as_of: datetime,
                       positions: Optional[Sequence[Position]] = None,
                       cash: Optional[float] = None
                       ) -> PortfolioSnapshot:
        """
        Price the portfolio as it stood at the anchor.

        Positions may be supplied (for replay over a hypothetical book)
        or read from storage. Reading uses the anchor-aware accessor, so
        a position opened after the anchor is invisible and one closed
        after it is correctly still open.

        `cash` overrides the stored balance. This exists for Phase 12:
        a backtest's cash lives in its own in-memory ledger and changes
        with every simulated fill, so reading the stored balance would
        value a simulated book against a real portfolio's cash. Passing
        it in keeps the simulation from having to write into the live
        `portfolios` table to be evaluated.
        """
        _require_utc(as_of)
        portfolio = self.repository.get_portfolio(portfolio_id) or {}
        held = (list(positions) if positions is not None
                else self.repository.open_positions(portfolio_id, as_of))

        valuations = self.valuator.value_positions(held, as_of)

        priced: List[PositionValuation] = []
        unpriced: List[PositionValuation] = []
        for valuation in valuations:
            if valuation.price is None:
                unpriced.append(valuation)
            else:
                priced.append(valuation)

        snapshot = PortfolioSnapshot(
            portfolio_id=portfolio_id,
            as_of=as_of,
            base_currency=portfolio.get("base_currency", "USD"),
            cash=(float(cash) if cash is not None
                  else float(portfolio.get("cash", 0.0) or 0.0)),
            valuations=priced,
            unvalued_positions=unpriced,
            currencies=[p.currency for p in held if p.currency],
        )

        unrealized_total = 0.0
        saw_unrealized = False
        for valuation in priced:
            exposure = valuation.exposure or 0.0
            snapshot.gross_exposure += exposure
            if valuation.position.is_short:
                snapshot.short_exposure += exposure
            else:
                snapshot.long_exposure += exposure
            pnl = valuation.unrealized_pnl
            if pnl is not None:
                unrealized_total += pnl
                saw_unrealized = True

        snapshot.unrealized_pnl = unrealized_total if saw_unrealized else None

        realized = [p.realized_pnl for p in held if p.realized_pnl is not None]
        snapshot.realized_pnl = sum(realized) if realized else None
        return snapshot

    # ---------------- measurement ----------------

    def _signed_weights(self, snapshot: PortfolioSnapshot) -> Dict[str, float]:
        """Signed exposure weights, the input to a portfolio return series."""
        equity = snapshot.equity
        if equity <= 0:
            return {}
        weights: Dict[str, float] = {}
        for valuation in snapshot.valuations:
            value = valuation.market_value
            if value is None:
                continue
            weight = finite_or_none(value / equity)
            if weight is None:
                continue
            key = valuation.position.instrument_id
            weights[key] = weights.get(key, 0.0) + weight
        return weights

    def compute_metrics(self, snapshot: PortfolioSnapshot,
                        as_of: Optional[datetime] = None) -> RiskMetrics:
        """
        Measure the portfolio: volatility, VaR, ES, concentration,
        correlation, drawdown.

        Each metric independently reports insufficiency rather than the
        whole computation failing — a new portfolio has enough data for
        concentration and none for drawdown, and that is a normal state
        to be in, not an error.
        """
        anchor = _require_utc(as_of or snapshot.as_of)
        metrics = RiskMetrics(as_of=anchor)

        metrics.concentration = analytics.compute_concentration(snapshot)

        instrument_ids = sorted({v.position.instrument_id for v in snapshot.valuations})
        if not instrument_ids:
            # Mark the estimates themselves insufficient, not just the
            # `unavailable` map. A default estimate carrying value=None
            # with insufficient_data=False reads as "measured, and the
            # answer was nothing", which is a different claim.
            reason = "portfolio holds no priced positions"
            metrics.volatility.insufficient_data = True
            metrics.volatility.note = reason
            metrics.value_at_risk.insufficient_data = True
            metrics.value_at_risk.note = reason
            for name in ("volatility", "value_at_risk", "correlation"):
                metrics.mark_unavailable(name, reason)
        else:
            series = self.prices.return_series_batch(
                instrument_ids, anchor, self.lookback_days)

            missing = [i for i in instrument_ids if i not in series]
            if missing:
                metrics.mark_unavailable(
                    "return_history",
                    f"{len(missing)} instrument(s) have no cached return history: "
                    f"{', '.join(missing[:5])}")

            weights = self._signed_weights(snapshot)
            returns, observations = analytics.portfolio_return_series(weights, series)

            metrics.volatility = analytics.compute_volatility(returns, self.lookback_days)
            metrics.value_at_risk = analytics.compute_value_at_risk(returns)
            metrics.correlation = analytics.compute_correlation_summary(series)

            if observations == 0:
                metrics.mark_unavailable(
                    "portfolio_returns",
                    "no overlapping trading days across held instruments")

        # Drawdown reads REAL recorded equity only (spec §12).
        curve = self.repository.equity_curve(snapshot.portfolio_id, anchor)
        metrics.drawdown = analytics.compute_drawdown(curve)
        if metrics.drawdown.insufficient_data:
            metrics.mark_unavailable(
                "drawdown",
                f"{len(curve)} stored complete snapshot(s); a drawdown needs at "
                f"least 2 and is never synthesized from simulated equity")

        return metrics

    # ---------------- signals ----------------

    def actionable_signals(self, as_of: datetime,
                           instrument_ids: Optional[Sequence[str]] = None
                           ) -> List[Signal]:
        """
        Signals usable at the anchor.

        Read via `signals_as_of`, which filters on information cutoff
        rather than row-write time, then filtered to those actually
        actionable and unexpired at the anchor. A signal generated by a
        backfill today over last month's data must not appear to have
        been available last month.
        """
        _require_utc(as_of)
        repository = SignalRepository(self.conn)
        try:
            candidates = repository.signals_as_of(as_of)
        except sqlite3.OperationalError:
            return []      # signal tables absent in a partial database

        allowed = set(instrument_ids) if instrument_ids else None
        return [s for s in candidates
                if s.is_actionable
                and not s.is_expired_at(as_of)
                and (allowed is None or s.instrument_id in allowed)]

    # ---------------- evaluation ----------------

    def _liquidity(self, snapshot: PortfolioSnapshot,
                   proposal: Optional[AllocationProposal],
                   as_of: datetime) -> Dict[str, Optional[float]]:
        """Position size as a fraction of average daily volume, per instrument."""
        instrument_ids = {v.position.instrument_id for v in snapshot.valuations}
        if proposal:
            instrument_ids |= {c.instrument_id for c in proposal.changes}
        if not instrument_ids:
            return {}

        history = self.prices.close_series_batch(
            sorted(instrument_ids), as_of, self.lookback_days)

        quantities: Dict[str, float] = {}
        for valuation in snapshot.valuations:
            key = valuation.position.instrument_id
            quantities[key] = quantities.get(key, 0.0) + valuation.position.quantity
        if proposal:
            for change in proposal.changes:
                if change.target_quantity is not None:
                    quantities[change.instrument_id] = change.target_quantity

        out: Dict[str, Optional[float]] = {}
        for instrument_id in instrument_ids:
            points = history.get(instrument_id)
            quantity = quantities.get(instrument_id)
            out[instrument_id] = (
                analytics.compute_liquidity_participation(quantity, points)
                if points and quantity is not None else None)
        return out

    def build_inputs(self, snapshot: PortfolioSnapshot, metrics: RiskMetrics,
                     constraint_set: ConstraintSet,
                     signals: Sequence[Signal],
                     proposal: Optional[AllocationProposal],
                     as_of: datetime) -> EvaluationInputs:
        """Gather every read the engine needs, all anchored at `as_of`."""
        instrument_ids = {v.position.instrument_id for v in snapshot.valuations}
        if proposal:
            instrument_ids |= {c.instrument_id for c in proposal.changes}

        classifications = self.classifier.classify(sorted(instrument_ids))
        information_cutoff = max(
            (s.provenance.source_information_cutoff for s in signals
             if s.provenance.source_information_cutoff is not None),
            default=None)

        return EvaluationInputs(
            snapshot=snapshot,
            constraint_set=constraint_set,
            sector_by_instrument={i: c.sector_id for i, c in classifications.items()},
            asset_class_by_instrument={i: c.asset_class for i, c in classifications.items()},
            exposures=self.exposures.all_breakdowns(snapshot),
            metrics=metrics,
            signals_by_id={s.signal_id: s for s in signals},
            liquidity_participation=self._liquidity(snapshot, proposal, as_of),
            information_cutoff=information_cutoff,
        )

    def evaluate(self, portfolio_id: str, as_of: datetime,
                 sizing: Optional[PositionSizingStrategy] = None,
                 signals: Optional[Sequence[Signal]] = None,
                 proposal: Optional[AllocationProposal] = None,
                 positions: Optional[Sequence[Position]] = None,
                 cash: Optional[float] = None,
                 persist: bool = False) -> EvaluationResult:
        """
        The whole pipeline: state -> metrics -> proposal -> decision.

        A caller may supply a ready-made proposal (to evaluate a
        specific "what if"), or let a sizing strategy build one from
        signals. Supplying neither evaluates the CURRENT state against
        the limits, which is a legitimate and useful question on its
        own.

        `positions` and `cash` together let a caller evaluate a book
        this service does not store — which is how Phase 12 runs the
        real risk engine against a simulated portfolio without writing
        into the live tables.
        """
        _require_utc(as_of)
        constraint_set = self.constraints.load_or_default(self.constraint_version)

        snapshot = self.build_snapshot(portfolio_id, as_of, positions, cash)
        metrics = self.compute_metrics(snapshot, as_of)

        usable_signals = list(signals) if signals is not None else self.actionable_signals(as_of)

        if proposal is None and (sizing is not None or usable_signals):
            strategy = sizing or FixedFractionSizing()
            prices = self.prices.prices_as_of(
                [s.instrument_id for s in usable_signals], as_of)
            proposal = strategy.propose(SizingContext(
                as_of=as_of,
                snapshot=snapshot,
                signals=usable_signals,
                constraint_set=constraint_set,
                volatility_by_instrument=self._instrument_volatility(
                    [s.instrument_id for s in usable_signals], as_of),
                price_by_instrument={i: p.price for i, p in prices.items()},
            ))

        inputs = self.build_inputs(
            snapshot, metrics, constraint_set, usable_signals, proposal, as_of)
        engine = RiskEngine(constraint_set)
        decision = engine.evaluate(inputs, proposal, as_of)
        intents = self.build_intents(decision, proposal, as_of)

        if persist:
            self._persist(snapshot, metrics, proposal, decision, intents, as_of)

        return EvaluationResult(snapshot=snapshot, metrics=metrics,
                                proposal=proposal, decision=decision, intents=intents)

    def _instrument_volatility(self, instrument_ids: Sequence[str],
                               as_of: datetime) -> Dict[str, Optional[float]]:
        """Per-instrument annualized volatility, for volatility-targeted sizing."""
        if not instrument_ids:
            return {}
        series = self.prices.return_series_batch(
            sorted(set(instrument_ids)), as_of, self.lookback_days)
        out: Dict[str, Optional[float]] = {}
        for instrument_id in set(instrument_ids):
            returns = [value for _, value in series.get(instrument_id, [])]
            estimate = analytics.compute_volatility(
                returns, self.lookback_days, method="historical_instrument")
            out[instrument_id] = None if estimate.insufficient_data else estimate.value
        return out

    # ---------------- the execution boundary ----------------

    def build_intents(self, decision: RiskDecision,
                      proposal: Optional[AllocationProposal],
                      as_of: datetime) -> List[OrderIntent]:
        """
        Turn an APPROVED decision into inert order intents.

        Returns an empty list for any non-approving decision — the
        guard is `OrderIntent.require_approval`, enforced at the
        boundary rather than left to each future caller to remember.

        These objects are records, not instructions. Nothing here
        transmits them, and `is_executable` is False on every one.
        """
        if not decision.is_approved or not decision.approved_changes:
            return []
        OrderIntent.require_approval(decision)

        intents: List[OrderIntent] = []
        for change in decision.approved_changes:
            delta = change.weight_delta
            if delta is None or delta == 0:
                continue
            raw = f"{decision.decision_id}|{change.instrument_id}"
            intents.append(OrderIntent(
                intent_id=f"intent-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}",
                portfolio_id=decision.portfolio_id,
                instrument_id=change.instrument_id,
                side="buy" if delta > 0 else "sell",
                target_weight=change.target_weight,
                target_quantity=change.target_quantity,
                source_signal_id=change.signal_id,
                decision_id=decision.decision_id,
                reason=change.reason,
                created_at=as_of,
                is_executable=False,
            ))
        return intents

    # ---------------- persistence ----------------

    def _persist(self, snapshot: PortfolioSnapshot, metrics: RiskMetrics,
                 proposal: Optional[AllocationProposal], decision: RiskDecision,
                 intents: Sequence[OrderIntent], as_of: datetime) -> None:
        raw = f"{snapshot.portfolio_id}|{as_of.isoformat()}"
        snapshot_id = f"snap-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"
        self.repository.save_snapshot(
            snapshot_id, snapshot,
            metrics_json=self.repository._metrics_payload(decision),
            computed_at=datetime.now(timezone.utc))
        if proposal is not None:
            self.repository.save_proposal(proposal, created_at=datetime.now(timezone.utc))
        self.repository.save_decision(decision, created_at=datetime.now(timezone.utc))
        for intent in intents:
            self.repository.save_intent(intent)
