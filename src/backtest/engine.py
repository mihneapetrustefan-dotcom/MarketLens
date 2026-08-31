"""
src/backtest/engine.py
---------------------------
The historical replay loop (Phase 12, spec §7, §8, §9, §26, §115).

WHAT THIS ENGINE ACTUALLY DOES
----------------------------------
At every historical moment T it reconstructs the decision the system
would have made:

    mark the simulated book to prices known at T
        -> collect the signals that were live at T
        -> hand the SIMULATED positions and cash to the REAL Phase 11
           risk engine
        -> take the allocation it approved
        -> cut orders, delayed by the configured latency
        -> fill them against bars strictly after the order
        -> apply the fills to the ledger

then moves to the next T. Nothing is vectorised over the whole period,
because the portfolio state at T+1 depends on what was filled at T.

THE RISK ENGINE IS THE REAL ONE
-----------------------------------
Spec §25 forbids a simplified "fake risk" layer for simulation, and
§29 forbids substituting fixed sizing for the real sizing logic. So
this engine builds no risk logic of its own: it calls
`PortfolioService.evaluate()`, the same entry point the live path uses,
handing it the simulated book through the `positions` and `cash`
overrides. If a constraint would have blocked a trade in production, it
blocks it here, because it is literally the same code.

HISTORICAL SIGNAL STATUS IS RECONSTRUCTED, NOT READ
-------------------------------------------------------
This is subtle and it matters. A signal's stored `status` is its status
TODAY — a signal that has since expired or been superseded reads as
EXPIRED now, but was ACTIVE at the moment being replayed. Filtering on
the stored status would silently hide every signal the system has since
moved past, which on a long enough history is most of them.

So `_signals_live_at` derives the historical state from timestamps:
information cutoff at or before T, validity window containing T, a
directional claim, and no suppression reasons. Suppression IS
historically accurate — it was decided when the signal was validated —
so it is the one stored field that can be trusted for replay.

WHAT THIS ENGINE WILL NOT DO
--------------------------------
It will not fabricate a signal, a price, or a fill. On this database
every stored signal is suppressed, so a real run over it produces zero
orders and reports exactly that, with a NO_SIGNALS warning. That is the
correct output for the data, and dressing it up would defeat the point
of building it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.backtest.accounting import PortfolioLedger
from src.backtest.attribution import AttributionEngine, assess_quality
from src.backtest.calendar import MarketCalendar
from src.backtest.execution import ExecutionContext, SimulationExecutor
from src.backtest.guards import TemporalGuard, TemporalViolation
from src.backtest.performance import (
    PerformanceEngine, annotate_drawdown, compute_drawdown_episodes,
)
from src.data_access.signal_repository import SignalRepository
from src.domain.backtest_models import (
    BacktestConfiguration, BacktestResult, BacktestStatus, EquityPoint,
    ExecutionTiming, OrderSide, OrderState, QualityAssessment, ReplayTrigger,
    RunIdentity, SimulatedOrder, SlippageMethod, WarningCode, finite_or_none,
    safe_ratio,
)
from src.domain.portfolio_models import RiskDecisionState
from src.domain.signal_models import Signal, SignalDirection
from src.portfolio.exposure import InstrumentClassifier
from src.portfolio.service import PortfolioService
from src.portfolio.sizing import FixedFractionSizing, VolatilityTargetSizing

RISK_ENGINE_VERSION = "v1"
CODE_VERSION = "phase12-v1"

#: The simulated book is evaluated under this portfolio id. It is never
#: written to the `portfolios` table — the id exists only so Phase 11's
#: types have something to carry.
SIMULATED_PORTFOLIO_ID = "__backtest__"


def _require_utc(moment: datetime, name: str = "moment") -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {moment.utcoffset()})")
    return moment


class BacktestEngine:
    """Runs one configuration over one historical period."""

    def __init__(self, conn: sqlite3.Connection,
                 configuration: BacktestConfiguration,
                 backtest_id: Optional[str] = None,
                 signals: Optional[Sequence[Signal]] = None):
        self.conn = conn
        self.configuration = configuration
        self.backtest_id = backtest_id or f"bt-{configuration.fingerprint()}"
        #: Explicit signals override the database — used by tests and by
        #: strategy-replay callers that generate candidates themselves.
        self._signal_override = list(signals) if signals is not None else None

        self.guard = TemporalGuard(run_id=self.backtest_id)
        self.calendar = MarketCalendar(conn)
        self.classifier = InstrumentClassifier(conn)
        self.service = PortfolioService(
            conn, constraint_version=configuration.constraint_set_version)

    # ---------------- identity ----------------

    def _run_id(self) -> str:
        raw = (f"{self.backtest_id}|{self.configuration.fingerprint()}|"
               f"{CODE_VERSION}")
        return f"run-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    def _identity(self) -> RunIdentity:
        config = self.configuration
        return RunIdentity(
            backtest_id=self.backtest_id,
            run_id=self._run_id(),
            config_fingerprint=config.fingerprint(),
            risk_engine_version=RISK_ENGINE_VERSION,
            constraint_set_version=config.constraint_set_version,
            sizing_version="v1",
            execution_model_version=config.execution.version,
            cost_model_version=config.costs.version,
            slippage_model_version=config.slippage.version,
            calendar_version=self.calendar.version,
            strategy_version=config.strategy_version,
            code_version=CODE_VERSION,
            created_at=datetime.now(timezone.utc),
        )

    # ---------------- signals ----------------

    def _all_signals(self) -> List[Signal]:
        if self._signal_override is not None:
            return list(self._signal_override)
        try:
            repository = SignalRepository(self.conn)
            found = repository.signals_as_of(self.configuration.end)
        except sqlite3.OperationalError:
            return []
        config = self.configuration
        if config.strategy_id:
            found = [s for s in found
                     if s.provenance.strategy_id == config.strategy_id]
        if config.strategy_version:
            found = [s for s in found
                     if s.provenance.strategy_version == config.strategy_version]
        return found

    def _signals_live_at(self, signals: Sequence[Signal],
                         moment: datetime) -> List[Signal]:
        """
        The signals that were actionable at `moment`, reconstructed from
        timestamps rather than from today's stored status.

        See the module docstring for why the stored status cannot be
        trusted for replay. The conditions are: its information existed
        by then, its validity window contains the moment, it makes a
        directional claim, and it was not suppressed at validation.
        """
        live: List[Signal] = []
        for signal in signals:
            cutoff = signal.provenance.source_information_cutoff
            if cutoff is None or cutoff > moment:
                continue
            if signal.suppression_reasons:
                continue
            if signal.direction not in (SignalDirection.LONG, SignalDirection.SHORT):
                continue
            if signal.valid_from is not None and signal.valid_from > moment:
                continue
            if signal.valid_until is not None and signal.valid_until < moment:
                continue
            self.guard.check_feature_not_future(cutoff, moment, "signal information")
            self.guard.check_signal_after_information(signal.created_at, cutoff)
            live.append(signal)
        return live

    # ---------------- universe ----------------

    def _resolve_universe(self, signals: Sequence[Signal]) -> Tuple[List[str], bool]:
        """
        The instruments this run may trade.

        Returns (universe, is_explicit). An empty configured universe
        falls back to whatever the signals reference, which is recorded
        as implicit — an implicit universe derived from today's signals
        is a survivorship-bias risk, and the run is warned accordingly
        rather than being quietly accepted as clean (spec §31, §32).
        """
        configured = [i for i in self.configuration.universe if i]
        if configured:
            return sorted(set(configured)), True
        return sorted({s.instrument_id for s in signals}), False

    # ---------------- pricing helpers ----------------

    def _prices_at(self, instrument_ids: Sequence[str],
                   moment: datetime) -> Dict[str, Optional[float]]:
        """Closing marks knowable at `moment`. Never reads a later bar."""
        out: Dict[str, Optional[float]] = {}
        for instrument_id in instrument_ids:
            bar = self.calendar.bar_at_or_before(instrument_id, moment)
            if bar is None or bar.close is None:
                out[instrument_id] = None
                continue
            self.guard.check_bar_not_future(bar.timestamp, moment, "valuation bar")
            out[instrument_id] = bar.close
        return out

    def _benchmark_series(self, anchors: Sequence[datetime]
                          ) -> List[Tuple[datetime, float]]:
        instrument_id = self.configuration.benchmark_instrument_id
        if not instrument_id:
            return []
        series: List[Tuple[datetime, float]] = []
        for anchor in anchors:
            bar = self.calendar.bar_at_or_before(instrument_id, anchor)
            if bar is not None and bar.close:
                series.append((anchor, bar.close))
        return series

    # ---------------- the loop ----------------

    def run(self) -> BacktestResult:
        config = self.configuration
        started = datetime.now(timezone.utc)
        identity = self._identity()

        result = BacktestResult(
            run_id=identity.run_id, backtest_id=self.backtest_id,
            status=BacktestStatus.RUNNING, configuration=config,
            identity=identity, started_at=started)

        try:
            self._execute(result)
        except TemporalViolation as exc:
            result.status = BacktestStatus.FAILED
            result.add_error("temporal_violation", str(exc), fatal=True)
        except Exception as exc:      # noqa: BLE001 — a run must fail loudly, not vanish
            result.status = BacktestStatus.FAILED
            result.add_error("engine_error", f"{type(exc).__name__}: {exc}", fatal=True)

        result.finished_at = datetime.now(timezone.utc)
        if result.status == BacktestStatus.RUNNING:
            result.status = (BacktestStatus.COMPLETED_WITH_WARNINGS
                             if result.warnings else BacktestStatus.COMPLETED)
        return result

    def _execute(self, result: BacktestResult) -> None:
        config = self.configuration
        result.status = BacktestStatus.VALIDATING

        # --- inputs ---
        all_signals = self._all_signals()
        universe, explicit_universe = self._resolve_universe(all_signals)

        if not universe:
            result.add_warning(
                WarningCode.NO_SIGNALS,
                "no instruments to trade",
                "the configured universe was empty and no signal referenced an "
                "instrument, so there was nothing to simulate")

        load_ids = list(universe)
        if config.benchmark_instrument_id:
            load_ids.append(config.benchmark_instrument_id)
        self.calendar.load(load_ids)

        anchors = self.calendar.evaluation_dates(universe, config.start, config.end)
        if not anchors:
            # Without sessions there is no clock. Fall back to the
            # benchmark's calendar so an empty run still produces an
            # honest, dated equity curve instead of nothing at all.
            if config.benchmark_instrument_id:
                anchors = self.calendar.evaluation_dates(
                    [config.benchmark_instrument_id], config.start, config.end)

        coverage = self.calendar.coverage(universe)
        with_data = sum(1 for count in coverage.values() if count > 0)
        if universe and with_data < len(universe):
            result.add_warning(
                WarningCode.MISSING_PRICES,
                f"{len(universe) - with_data} of {len(universe)} instruments have "
                f"no cached price history",
                "orders for those instruments cannot fill and are rejected")

        self._add_configuration_warnings(result, explicit_universe, len(anchors))

        # --- simulation state ---
        result.status = BacktestStatus.RUNNING
        ledger = PortfolioLedger(config.initial_capital, run_id=result.run_id,
                                 base_currency=config.base_currency)
        executor = SimulationExecutor(
            self.calendar, config.costs, config.slippage, config.execution, self.guard)
        sizing = (VolatilityTargetSizing()
                  if config.sizing_strategy_id == "volatility_target"
                  else FixedFractionSizing(config.sizing_target_weight))

        classifications = self.classifier.classify(universe)
        sector_by_instrument = {i: c.sector_id for i, c in classifications.items()}
        signals_by_id = {s.signal_id: s for s in all_signals}

        last_evaluated: Optional[datetime] = None
        previous_anchor: Optional[datetime] = None
        slippage_bps_samples: List[float] = []
        fill_delays: List[float] = []

        # --- the replay ---
        for anchor in anchors:
            self.guard.check_within_horizon(anchor, config.end)
            result.observations_processed += 1

            prices = self._prices_at(universe, anchor)
            snapshot = ledger.mark_to_market(anchor, prices)

            benchmark_value = None
            if config.benchmark_instrument_id:
                bar = self.calendar.bar_at_or_before(
                    config.benchmark_instrument_id, anchor)
                benchmark_value = bar.close if bar else None

            result.equity_curve.append(EquityPoint(
                timestamp=anchor, equity=snapshot.equity, cash=snapshot.cash,
                positions_value=snapshot.positions_value,
                gross_exposure=snapshot.gross_exposure,
                net_exposure=snapshot.net_exposure,
                benchmark_value=benchmark_value,
                open_positions=snapshot.open_positions))

            live = self._signals_live_at(all_signals, anchor)
            trigger = self._trigger_for(anchor, previous_anchor, last_evaluated, live)
            previous_anchor = anchor
            if trigger is None:
                continue
            last_evaluated = anchor

            self._evaluate_and_trade(
                result, ledger, executor, sizing, anchor, live, prices,
                sector_by_instrument, signals_by_id, trigger,
                slippage_bps_samples, fill_delays)

        # --- close out ---
        if anchors:
            final_prices = self._prices_at(universe, anchors[-1])
            closed = ledger.close_all(anchors[-1], final_prices)
            if closed:
                result.log(anchors[-1], "liquidation", trades=len(closed))

        result.trades = list(ledger.trades)
        annotate_drawdown(result.equity_curve)
        result.drawdowns = compute_drawdown_episodes(result.equity_curve)

        self._finalize(result, ledger, universe, with_data, anchors,
                       slippage_bps_samples, fill_delays, sector_by_instrument,
                       signals_by_id)

    # ---------------- triggering ----------------

    def _trigger_for(self, anchor: datetime, previous_anchor: Optional[datetime],
                     last_evaluated: Optional[datetime],
                     live: Sequence[Signal]) -> Optional[ReplayTrigger]:
        """
        Whether to evaluate at this moment, and why (spec §8, §9).

        Event-driven fires when a signal's information became knowable
        since the previous anchor — the strategy reacting to news.
        Scheduled fires on the configured cadence. Both are recorded so
        a run can be read as "how much of this was reaction versus
        routine".
        """
        config = self.configuration

        if config.event_driven and previous_anchor is not None:
            for signal in live:
                cutoff = signal.provenance.source_information_cutoff
                if cutoff is not None and previous_anchor < cutoff <= anchor:
                    return ReplayTrigger.EVENT

        if last_evaluated is None:
            return ReplayTrigger.SCHEDULED
        elapsed = (anchor - last_evaluated).total_seconds() / 86400.0
        if elapsed >= config.rebalance_days:
            return ReplayTrigger.SCHEDULED
        return None

    # ---------------- one evaluation ----------------

    def _evaluate_and_trade(self, result: BacktestResult, ledger: PortfolioLedger,
                            executor: SimulationExecutor, sizing,
                            anchor: datetime, live: Sequence[Signal],
                            prices: Dict[str, Optional[float]],
                            sector_by_instrument: Dict[str, Optional[str]],
                            signals_by_id: Dict[str, Signal],
                            trigger: ReplayTrigger,
                            slippage_samples: List[float],
                            fill_delays: List[float]) -> None:
        config = self.configuration

        # --- the REAL risk engine, over the SIMULATED book ---
        evaluation = self.service.evaluate(
            SIMULATED_PORTFOLIO_ID, anchor,
            sizing=sizing, signals=list(live),
            positions=ledger.to_positions(SIMULATED_PORTFOLIO_ID, anchor),
            cash=ledger.cash, persist=False)

        decision = evaluation.decision
        state = decision.state.value
        result.risk_decision_counts[state] = result.risk_decision_counts.get(state, 0) + 1

        # --- keep what risk refused or trimmed (spec §27, §28) ---
        if decision.state == RiskDecisionState.REJECTED and evaluation.proposal:
            result.rejected_allocations.append({
                "at": anchor.isoformat(),
                "proposal_id": evaluation.proposal.proposal_id,
                "changes": [{"instrument_id": c.instrument_id,
                             "target_weight": c.target_weight}
                            for c in evaluation.proposal.changes],
                "reason": decision.summary,
                "violations": [v.message for v in decision.blocking_violations],
                "constraint_set_version": decision.provenance.constraint_set_version,
            })
        if decision.state == RiskDecisionState.REDUCED and evaluation.proposal:
            proposed = {c.instrument_id: c.target_weight
                        for c in evaluation.proposal.changes}
            result.modified_allocations.append({
                "at": anchor.isoformat(),
                "proposal_id": evaluation.proposal.proposal_id,
                "proposed": proposed,
                "approved": {c.instrument_id: c.target_weight
                             for c in decision.approved_changes},
                "reason": decision.summary,
            })

        result.log(anchor, "risk_decision", state=state, trigger=trigger.value,
                   signals=len(live), summary=decision.summary)

        equity = ledger.equity(prices)
        order_time = anchor + timedelta(seconds=config.execution.signal_to_order_seconds)

        # --- exits for positions whose signal is no longer live ---
        # Generated here rather than inside sizing because sizing only
        # speaks about instruments that HAVE a signal. These reduce
        # exposure, so they need no risk approval — Phase 11's own
        # REDUCE_ONLY state exists on exactly that reasoning — but each
        # one is logged so the exits are visible in the audit trail.
        work: List[Tuple[str, float, Optional[str], Optional[float], str]] = []
        for intent in evaluation.intents:
            work.append((intent.instrument_id,
                         self._target_quantity(intent, ledger, equity,
                                               prices.get(intent.instrument_id)) or 0.0,
                         intent.source_signal_id, intent.target_weight, "signal"))

        if config.exit_when_signal_expires:
            supported = {s.instrument_id for s in live}
            proposed = {i.instrument_id for i in evaluation.intents}
            for position in ledger.open_positions():
                if position.instrument_id in supported or position.instrument_id in proposed:
                    continue
                work.append((position.instrument_id, -position.quantity,
                             position.entry_signal_id, 0.0, "signal_expired"))

        if not work:
            return

        for instrument_id, quantity, signal_id, target_weight, origin in work:
            price = prices.get(instrument_id)
            if price is None or price <= 0:
                continue
            if quantity is None or abs(quantity) <= 1e-9:
                continue

            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
            signal = signals_by_id.get(signal_id or "")
            cutoff = (signal.provenance.source_information_cutoff
                      if signal else None)

            seed = f"{result.run_id}|{instrument_id}|{anchor.isoformat()}|{origin}"
            order = SimulatedOrder(
                order_id=f"or-{hashlib.sha1(seed.encode()).hexdigest()[:16]}",
                run_id=result.run_id, instrument_id=instrument_id,
                side=side, quantity=abs(quantity),
                information_cutoff=cutoff, decision_at=anchor,
                created_at=order_time, signal_id=signal_id,
                decision_id=decision.decision_id, intent_id=None,
                target_weight=target_weight, note=origin)

            self.guard.check_order_after_signal(order_time, cutoff)
            result.orders.append(order)

            current = ledger.positions.get(instrument_id)
            context = ExecutionContext(
                available_cash=ledger.cash,
                volatility=None,
                horizon_end=config.end,
                allow_shorting=config.execution.allow_shorting,
                current_quantity=current.quantity if current else 0.0)

            fills = executor.execute(order, context)
            for fill in fills:
                self.guard.check_outcome_after_decision(fill.filled_at, anchor)
                ledger.apply_fill(
                    fill,
                    sector_id=sector_by_instrument.get(fill.instrument_id),
                    strategy_id=(signal.provenance.strategy_id if signal else None),
                    signal_id=signal_id,
                    decision_id=decision.decision_id,
                    exit_reason=origin)
                result.fills.append(fill)
                fill_delays.append(
                    (fill.filled_at - order_time).total_seconds() / 86400.0)
                if fill.reference_price:
                    slippage_samples.append(
                        abs(fill.price - fill.reference_price)
                        / fill.reference_price * 10_000.0)
                result.log(fill.filled_at, "fill", instrument=fill.instrument_id,
                           side=fill.side.value, quantity=round(fill.quantity, 6),
                           price=round(fill.price, 6))

            if order.state == OrderState.REJECTED:
                result.log(anchor, "order_rejected", instrument=order.instrument_id,
                           reason=order.reject_reason.value if order.reject_reason else "",
                           note=order.note)

    def _target_quantity(self, intent, ledger: PortfolioLedger,
                         equity: float, price: Optional[float]) -> Optional[float]:
        """
        The signed quantity that moves the position to the intent's
        target weight.

        Computed as a DELTA from what is already held, not as the target
        itself: an intent to hold 5% of a book that already holds 4%
        should trade 1%, not 5%. Getting this wrong is how a backtest
        accumulates enormous phantom turnover.
        """
        if price is None or price <= 0 or equity <= 0:
            return None
        target_weight = intent.target_weight
        if target_weight is None:
            return intent.target_quantity
        target_quantity = (target_weight * equity) / price
        current = ledger.positions.get(intent.instrument_id)
        held = current.quantity if current else 0.0
        return finite_or_none(target_quantity - held)

    # ---------------- warnings and results ----------------

    def _add_configuration_warnings(self, result: BacktestResult,
                                    explicit_universe: bool, anchor_count: int) -> None:
        config = self.configuration

        if config.execution.timing == ExecutionTiming.SAME_BAR_CLOSE:
            result.add_warning(
                WarningCode.SAME_BAR_EXECUTION,
                "orders fill at the close of the bar that produced the decision",
                "the decision used information including that close, so these "
                "fills were not achievable — use only for comparison")

        if config.costs.is_zero and config.slippage.method == SlippageMethod.NONE:
            result.add_warning(
                WarningCode.ZERO_COSTS,
                "no commission and no slippage are modelled",
                "results are an unreachable upper bound")

        if not config.benchmark_instrument_id:
            result.add_warning(
                WarningCode.NO_BENCHMARK, "no benchmark was configured",
                "returns cannot be read as out- or under-performance")

        if not explicit_universe:
            result.add_warning(
                WarningCode.SURVIVORSHIP_RISK,
                "the universe was derived from instruments that have signals today",
                "point-in-time universe membership is not available in this "
                "database, so delisted or dropped instruments are absent")

        # Polygon bars are fetched with adjusted=true, so a split today
        # restates every historical bar. Real, and worth stating.
        result.add_warning(
            WarningCode.RETROACTIVE_ADJUSTMENT,
            "prices are retroactively split- and dividend-adjusted",
            "a historical bar is not the price quoted at the time; corporate "
            "actions are embedded in the series rather than paid as cash events")

        if anchor_count < 60:
            result.add_warning(
                WarningCode.SHORT_HISTORY,
                f"only {anchor_count} evaluation date(s) in the period",
                "too short for the risk-adjusted metrics to mean much")

        result.add_warning(
            WarningCode.NO_REGIME_DATA,
            "market_regime is not populated in this database",
            "performance cannot be broken down by regime")

    def _finalize(self, result: BacktestResult, ledger: PortfolioLedger,
                  universe: Sequence[str], instruments_with_data: int,
                  anchors: Sequence[datetime], slippage_samples: List[float],
                  fill_delays: List[float],
                  sector_by_instrument: Dict[str, Optional[str]],
                  signals_by_id: Dict[str, Signal]) -> None:
        config = self.configuration

        # --- execution statistics ---
        stats = result.execution_stats
        stats.orders_created = len(result.orders)
        stats.orders_filled = sum(1 for o in result.orders
                                  if o.state == OrderState.FILLED)
        stats.orders_partially_filled = sum(
            1 for o in result.orders if o.state == OrderState.PARTIALLY_FILLED)
        stats.orders_rejected = sum(1 for o in result.orders
                                    if o.state == OrderState.REJECTED)
        for order in result.orders:
            if order.reject_reason is not None:
                key = order.reject_reason.value
                stats.reject_reasons[key] = stats.reject_reasons.get(key, 0) + 1
        stats.total_commission = ledger.total_costs
        stats.total_slippage_cost = ledger.total_slippage
        if slippage_samples:
            stats.average_slippage_bps = finite_or_none(
                sum(slippage_samples) / len(slippage_samples))
        if fill_delays:
            stats.average_fill_delay_days = finite_or_none(
                sum(fill_delays) / len(fill_delays))

        # --- performance ---
        engine = PerformanceEngine(config.risk_free_rate, config.risk_free_source)
        result.metrics = engine.compute(
            result.equity_curve, result.trades, config.initial_capital,
            traded_notional=ledger.traded_notional,
            total_costs=ledger.total_costs, total_slippage=ledger.total_slippage,
            benchmark_points=self._benchmark_series(anchors))

        # --- attribution ---
        attribution = AttributionEngine(sector_by_instrument, signals_by_id)
        if attribution.is_meaningful(result.trades):
            result.attribution = attribution.all_dimensions(result.trades)
        elif result.trades:
            result.add_warning(
                WarningCode.SMALL_SAMPLE,
                f"{len(result.trades)} closed trade(s) — too few to attribute",
                "per-bucket breakdowns over a handful of trades are anecdote")

        if not result.trades:
            result.add_warning(
                WarningCode.NO_SIGNALS if not result.orders else WarningCode.SMALL_SAMPLE,
                "the run produced no closed trades",
                "no performance metric can be computed from an untraded book")

        if (result.metrics.annualized_turnover is not None
                and result.metrics.annualized_turnover > 10):
            result.add_warning(
                WarningCode.HIGH_TURNOVER,
                f"annualized turnover is {result.metrics.annualized_turnover:.1f}x",
                "results will be very sensitive to the cost assumptions")

    # ---------------- quality ----------------

    def assess(self, result: BacktestResult) -> QualityAssessment:
        """Grade the run's research quality — never its profitability."""
        universe, _ = self._resolve_universe(
            self._signal_override if self._signal_override is not None
            else self._all_signals())
        coverage = self.calendar.coverage(universe)
        with_data = sum(1 for count in coverage.values() if count > 0)
        return assess_quality(
            self.configuration, result.metrics,
            [w.code for w in result.warnings],
            trading_days=len(result.equity_curve),
            instruments_with_data=with_data,
            instruments_requested=max(1, len(universe)))
