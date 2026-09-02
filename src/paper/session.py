"""
src/paper/session.py
-------------------------
The paper trading session runner (Phase 13, spec §10, §11, §31, §49, §79).

WHAT A TICK IS
------------------
One advance of the session. Each tick runs the real pipeline once:

    freshness check
      -> signals live at this moment
      -> the REAL Phase 11 risk engine over the paper book
      -> order intents
      -> control gate (pauses, rate limits, circuit breakers)
      -> paper orders
      -> fill attempts against the bar
      -> snapshot, reconciliation, health

and returns a `TickResult` describing exactly what happened.

WHY A TICK RATHER THAN A LOOP
---------------------------------
This repository has no persistent runtime — every phase runs as a batch
job under GitHub Actions cron. A `while True` here would be a daemon
nothing runs. So the session is DURABLE rather than resident: its state
lives in the database, `tick()` advances it, and the caller decides the
cadence. A cron schedule, a CLI invocation and a test loop all drive it
identically.

That is a real constraint, not a shortcut, and the freshness monitor is
what keeps it honest: a session ticking against four-day-old bars
reports exactly that instead of presenting them as live.

RISK CANNOT BE BYPASSED
---------------------------
Spec §31 forbids the paper executor bypassing risk, and the structure
enforces it rather than the discipline: orders are only ever created
from `evaluation.intents`, and intents only exist when
`PortfolioService.evaluate()` returned an approving decision. There is
no code path from a signal to an order that does not pass through the
same Phase 11 call the live pipeline uses.

IDEMPOTENCY IS THE OTHER STRUCTURAL GUARANTEE
-------------------------------------------------
A scheduled system will re-run a tick: a retried workflow, a restart
mid-tick, a duplicated invocation. Every order carries a key derived
from its deciding inputs, so a re-run recognises its own previous work
instead of doubling the position (spec §12).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import MarketCalendar
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from src.domain.paper_models import (
    DataFreshness, HealthState, OrderSide, PaperAccount, PaperAccountStatus,
    PaperEvent, PaperEventKind, PaperFill, PaperOrder, PaperOrderState,
    PaperOrderType, PaperRejectReason, PaperSession, PaperSessionConfig,
    PaperSessionStatus, PaperSnapshot, ReconciliationResult, SystemHealth,
    TickResult, TimeInForce, finite_or_none, safe_ratio,
)
from src.domain.signal_models import Signal, SignalDirection
from src.paper.clock import Clock, SystemClock, require_utc
from src.paper.controls import CircuitBreakers, ControlLedger, RateLimits
from src.paper.executor import PaperExecutor
from src.paper.freshness import FreshnessMonitor, FreshnessReport
from src.paper.health import (
    DECISION_COMPONENTS, HealthMonitor, HeartbeatMonitor, LatencyTracker,
    PIPELINE_COMPONENTS,
)
from src.paper.reconciliation import Reconciler
from src.portfolio.exposure import InstrumentClassifier
from src.portfolio.service import PortfolioService
from src.portfolio.sizing import FixedFractionSizing, VolatilityTargetSizing

CODE_VERSION = "phase13-v1"

#: The paper book is evaluated under this id. It is never written to
#: the live `portfolios` table — the id exists only so Phase 11's types
#: have something to carry, exactly as Phase 12 does.
PAPER_PORTFOLIO_ID = "__paper__"


class PaperTradingSession:
    """
    One durable, resumable paper trading session.

    Holds the ledger, the executor, the control gate and the monitors.
    State that must survive a restart is written by the repository; what
    lives here is the working set for the current tick.
    """

    def __init__(self, conn: sqlite3.Connection, account: PaperAccount,
                 session: PaperSession, clock: Optional[Clock] = None,
                 ledger: Optional[PortfolioLedger] = None,
                 signals: Optional[Sequence[Signal]] = None):
        self.conn = conn
        self.account = account
        self.session = session
        self.config = session.config
        self.clock = clock or SystemClock()

        self.calendar = MarketCalendar(conn)
        self.classifier = InstrumentClassifier(conn)
        self.service = PortfolioService(
            conn, constraint_version=self.config.constraint_set_version)

        self.ledger = ledger or PortfolioLedger(
            account.initial_capital, run_id=session.session_id,
            base_currency=account.base_currency)

        self.costs = CostModel(version=self.config.cost_model_version,
                               commission_bps=self.config.commission_bps)
        self.slippage = SlippageModel(
            version=self.config.slippage_model_version,
            method=(SlippageMethod.NONE if self.config.slippage_bps == 0
                    else SlippageMethod.FIXED_BPS),
            base_bps=self.config.slippage_bps)

        self.executor = PaperExecutor(
            self.calendar, self.ledger, self.costs, self.slippage,
            account_id=account.account_id, session_id=session.session_id,
            max_participation=self.config.max_participation,
            allow_shorting=account.allows_shorting)
        self.executor.connect()

        self.controls = ControlLedger(
            RateLimits(max_orders_per_tick=self.config.max_orders_per_tick,
                       max_orders_per_day=self.config.max_orders_per_day),
            CircuitBreakers(daily_loss_limit_pct=self.config.daily_loss_limit_pct,
                            max_drawdown_pct=self.config.max_drawdown_pct))

        # Heartbeat staleness must exceed the tick cadence, or a
        # scheduled session declares itself stale between its own ticks.
        # Three intervals tolerates a missed tick and a weekend.
        self.health_monitor = HealthMonitor(HeartbeatMonitor(
            timeout_seconds=max(3600.0, self.config.tick_interval_seconds * 3)))
        self.reconciler = Reconciler(account.initial_capital)
        self.freshness: Optional[FreshnessMonitor] = None

        #: Explicit signals override the database — used by tests and by
        #: callers that generate their own.
        self._signal_override = list(signals) if signals is not None else None

        self.events: List[PaperEvent] = []
        self.fills: List[PaperFill] = []
        self.alerts: List[Any] = []
        self.snapshots: List[PaperSnapshot] = []
        self._event_seq = 0
        self._peak_equity: Optional[float] = None
        self._day_start_equity: Dict[str, float] = {}
        self._loaded = False

    # ---------------- setup ----------------

    def prepare(self) -> None:
        """Load the calendar and classify the universe. Idempotent."""
        if self._loaded:
            return
        universe = list(self.config.universe)
        self.calendar.load(universe)
        classifications = self.classifier.classify(universe)
        self.freshness = FreshnessMonitor(
            self.calendar,
            asset_class_by_instrument={i: c.asset_class
                                       for i, c in classifications.items()})
        self._loaded = True

    def restore_state(self, repository, at: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Rebuild the working set after a restart (spec §78, §79).

        The ledger is restored by the repository from its newest
        checkpoint plus the fills that followed. What this adds is the
        rest of the working set: the fill HISTORY and the still-open
        order book.

        Loading the fill history matters more than it looks.
        Reconciliation derives expected positions and cash from the
        fills it can see, so a recovered session with an empty fill list
        would compare a fully-restored ledger against no fills at all
        and report the whole book as corruption — a false alarm on every
        tick after every restart, which would train an operator to
        ignore the one that was real.
        """
        moment = require_utc(at or self.clock.now(), "at")
        ledger, restored_to, method = repository.restore_ledger(
            self.session.session_id, self.account.initial_capital,
            self.account.base_currency)
        self.ledger = ledger
        self.executor.ledger = ledger

        # The fills reconciliation will measure against.
        self.fills = repository.paper_fills_for(self.session.session_id)

        # The FULL order history, not just the open ones: reconciliation
        # checks every fill against a known order, and the executor's
        # own `open_only` filter still picks out what can receive fills.
        restored_orders = 0
        working = 0
        for order in repository.all_orders_for(self.session.session_id):
            self.executor._orders[order.order_id] = order
            if order.idempotency_key:
                self.executor._by_idempotency[order.idempotency_key] = order.order_id
            restored_orders += 1
            if order.state.is_working:
                working += 1

        self.log(PaperEventKind.RECOVERY, moment,
                 f"restored via {method}",
                 method=method,
                 restored_to=restored_to.isoformat() if restored_to else None,
                 fills=len(self.fills), orders=restored_orders,
                 working_orders=working)

        return {"method": method, "restored_to": restored_to,
                "fills": len(self.fills), "orders": restored_orders,
                "working_orders": working}

    # ---------------- events ----------------

    def log(self, kind: PaperEventKind, at: datetime, message: str = "",
            **payload: Any) -> PaperEvent:
        self._event_seq += 1
        event = PaperEvent(
            session_id=self.session.session_id, at=at, kind=kind,
            sequence=self._event_seq, message=message,
            instrument_id=payload.pop("instrument_id", None),
            order_id=payload.pop("order_id", None),
            fill_id=payload.pop("fill_id", None),
            signal_id=payload.pop("signal_id", None),
            payload=payload)
        self.events.append(event)
        return event

    # ---------------- signals ----------------

    def _all_signals(self) -> List[Signal]:
        if self._signal_override is not None:
            return list(self._signal_override)
        from src.data_access.signal_repository import SignalRepository
        try:
            found = SignalRepository(self.conn).signals_as_of(self.clock.now())
        except sqlite3.OperationalError:
            return []
        if self.config.strategy_id:
            found = [s for s in found
                     if s.provenance.strategy_id == self.config.strategy_id]
        return found

    def signals_live_at(self, moment: datetime) -> List[Signal]:
        """
        Signals actionable right now (spec §30).

        Reconstructed from timestamps rather than stored status, the
        same rule Phase 12 established: a signal's `status` column is
        its state TODAY, and a live session asking "what is active now"
        must not inherit a lifecycle decision made later.
        """
        live: List[Signal] = []
        for signal in self._all_signals():
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
            live.append(signal)
        return live

    # ---------------- the tick ----------------

    def tick(self, now: Optional[datetime] = None) -> TickResult:
        """
        Advance the session one step.

        Every stage is timed and heartbeats, so a tick that ran
        partially is distinguishable from one that never started.
        """
        self.prepare()
        moment = require_utc(now or self.clock.now(), "now")
        latency = LatencyTracker()
        result = TickResult(session_id=self.session.session_id, at=moment)

        self.controls.begin_tick()
        self.log(PaperEventKind.TICK, moment, "tick started")

        # --- stage 1: market data and freshness ---
        with latency.measure("market_data", moment):
            report = self.freshness.evaluate(list(self.config.universe), moment)
            self.health_monitor.beat("market_data", moment,
                                     f"{len(report.statuses)} instruments")
            self.health_monitor.beat("freshness", moment, report.worst.value)
        result.freshness = report.worst

        prices = report.prices()
        if report.worst in (DataFreshness.STALE, DataFreshness.INVALID,
                            DataFreshness.UNAVAILABLE):
            self.log(PaperEventKind.DATA_STALE, moment,
                     f"market data is {report.worst.value}",
                     blocked=len(report.blocked_instruments))

        # --- stage 2: signals ---
        with latency.measure("signals", moment):
            live = self.signals_live_at(moment)
            self.health_monitor.beat("signals", moment, f"{len(live)} live")
        result.signals_observed = len(live)
        if live:
            self.log(PaperEventKind.SIGNAL_OBSERVED, moment,
                     f"{len(live)} signal(s) live", count=len(live))

        # --- stage 3: expire and re-attempt resting orders ---
        with latency.measure("executor", moment):
            for expired in self.executor.expire_stale_orders(moment):
                self.log(PaperEventKind.ORDER_EXPIRED, moment,
                         f"{expired.instrument_id} order expired unfilled",
                         order_id=expired.order_id,
                         instrument_id=expired.instrument_id)
            resting_fills = self._fill_working_orders(moment, report)
            self.health_monitor.beat("executor", moment,
                                     f"{len(resting_fills)} resting fill(s)")

        # --- stage 4: the REAL risk engine over the paper book ---
        with latency.measure("risk", moment):
            evaluation = self._evaluate_risk(moment, live)
            self.health_monitor.beat("risk", moment,
                                     evaluation.decision.state.value
                                     if evaluation else "unavailable")
        if evaluation is None:
            result.blocked_reason = "risk engine unavailable"
            self.health_monitor.fail("risk", "evaluation raised")
            self.log(PaperEventKind.RISK_EVALUATED, moment,
                     "risk evaluation failed; no orders created")
        else:
            decision = evaluation.decision
            result.risk_state = decision.state.value
            self.log(PaperEventKind.RISK_EVALUATED, moment, decision.summary,
                     state=decision.state.value)
            if not decision.is_approved:
                # Surface WHY nothing traded. A tick showing "8 signals,
                # 0 orders" with no explanation is the state an operator
                # most needs described — and a soft breach blocking
                # automated exposure is a legitimate outcome, not a
                # fault, so it has to read as a reason rather than as
                # silence.
                blocking = [v.constraint_id for v in decision.blocking_violations]
                soft = [v.constraint_id for v in decision.soft_violations]
                result.blocked_reason = (
                    f"risk {decision.state.value}: "
                    + (", ".join(blocking or soft) or decision.summary))
                self.log(PaperEventKind.RISK_REJECTED, moment, decision.summary,
                         state=decision.state.value,
                         blocking=blocking, soft=soft)

        # --- stage 5: the decision gate ---
        # Gated on the stages that must have run to make a decision.
        # See DECISION_COMPONENTS for why ledger and persistence cannot
        # gate a decision they follow. Health across ALL stages is
        # judged at the end of the tick, once those stages have run.
        gate_health = self.health_monitor.evaluate(
            moment, freshness=report.worst, latencies=latency.by_stage(),
            components=DECISION_COMPONENTS)

        # --- stage 6: intents -> orders -> fills ---
        new_fills: List[PaperFill] = []
        if evaluation is not None and evaluation.intents:
            with latency.measure("orders", moment):
                created, rejected, filled = self._place_intents(
                    moment, evaluation, report, gate_health, prices)
            result.orders_created = created
            result.orders_rejected = rejected
            new_fills = filled

        result.fills = len(new_fills) + len(resting_fills)
        result.orders_filled = sum(
            1 for o in self.executor.get_orders()
            if o.state == PaperOrderState.FILLED and o.terminal_at == moment)

        # --- stage 7: snapshot, reconcile, persist ---
        with latency.measure("ledger", moment):
            snapshot = self._snapshot(moment, prices, report, gate_health)
            result.snapshot = snapshot
            self.snapshots.append(snapshot)
            self.health_monitor.beat("ledger", moment,
                                     f"equity {snapshot.equity:,.2f}")

        with latency.measure("persistence", moment):
            reconciliation = self.reconciler.reconcile(
                self.session.session_id, moment,
                self.executor.get_orders(), self.fills, self.ledger)
            result.reconciliation = reconciliation
            self.health_monitor.beat("persistence", moment,
                                     "clean" if reconciliation.is_clean
                                     else f"{len(reconciliation.discrepancies)} issue(s)")

        if not reconciliation.is_clean:
            # Spec §32: surface, never silently repair.
            self.health_monitor.enter_safe_mode(
                f"reconciliation found {len(reconciliation.discrepancies)} "
                f"discrepancy(ies)")
            self.log(PaperEventKind.RECONCILIATION, moment,
                     f"{len(reconciliation.discrepancies)} discrepancy(ies)",
                     kinds=[d.kind for d in reconciliation.discrepancies])
        else:
            self.log(PaperEventKind.RECONCILIATION, moment, "clean",
                     checks=reconciliation.checks_performed)

        # --- stage 8: judge the tick as a whole ---
        # Deliberately last. Every stage has now reported, so a
        # component that shows as stale here really has gone quiet
        # rather than simply not having run yet — on the first tick the
        # earlier ordering alerted that the ledger was "stale" before
        # the ledger had ever had a turn, which is exactly the kind of
        # false alarm that teaches people to ignore alerts. Judging
        # last also means this tick reflects its own reconciliation
        # result rather than the previous one.
        health = self.health_monitor.evaluate(
            moment, freshness=report.worst, latencies=latency.by_stage())
        result.health = health.overall
        snapshot.health = health.overall
        for alert in self.health_monitor.alerts_for(
                health, self.session.session_id, moment):
            self.alerts.append(alert)
            self.log(PaperEventKind.ALERT, moment, alert.message,
                     code=alert.code, severity=alert.severity.value)

        result.latencies = list(latency.samples)
        self.session.ticks_processed += 1
        self.session.last_tick_at = moment
        if self.session.status == PaperSessionStatus.CREATED:
            self.session.status = PaperSessionStatus.RUNNING
            self.session.started_at = self.session.started_at or moment

        self.log(PaperEventKind.SNAPSHOT, moment,
                 f"equity {snapshot.equity:,.2f}",
                 equity=round(snapshot.equity, 6),
                 cash=round(snapshot.cash, 6),
                 positions=snapshot.open_positions)
        return result

    # ---------------- stages ----------------

    def _fill_working_orders(self, moment: datetime,
                             report: FreshnessReport) -> List[PaperFill]:
        """
        Try to fill orders resting from earlier ticks.

        This is what makes limit orders meaningful: they were accepted
        on one tick and may only become fillable several ticks later,
        which a backtest's fill-or-reject sweep never has to model.
        """
        produced: List[PaperFill] = []
        for order in self.executor.get_orders(open_only=True):
            if not report.is_tradeable(order.instrument_id):
                continue
            fills = self.executor.try_fill(order, moment)
            for fill in fills:
                self.fills.append(fill)
                produced.append(fill)
                self.log(PaperEventKind.FILL, fill.filled_at,
                         f"{fill.side.value} {fill.quantity:.6f} "
                         f"{fill.instrument_id} @ {fill.price:.4f}",
                         order_id=order.order_id, fill_id=fill.fill_id,
                         instrument_id=fill.instrument_id,
                         resting=True)
        return produced

    def _evaluate_risk(self, moment: datetime, live: Sequence[Signal]):
        """
        Run the REAL Phase 11 risk engine over the paper book.

        Failure returns None rather than raising, so the tick can
        continue to snapshot and reconcile — but with no evaluation
        there are no intents, so no orders can be created. Spec §76 case
        4 requires exactly that.
        """
        sizing = (VolatilityTargetSizing()
                  if self.config.sizing_strategy_id == "volatility_target"
                  else FixedFractionSizing(self.config.sizing_target_weight))
        try:
            return self.service.evaluate(
                PAPER_PORTFOLIO_ID, moment, sizing=sizing, signals=list(live),
                positions=self.ledger.to_positions(PAPER_PORTFOLIO_ID, moment),
                cash=self.ledger.cash, persist=False)
        except Exception:      # noqa: BLE001 — a broken risk engine must not trade
            return None

    def _place_intents(self, moment: datetime, evaluation, report: FreshnessReport,
                       health: SystemHealth,
                       prices: Dict[str, Optional[float]]
                       ) -> Tuple[int, int, List[PaperFill]]:
        created = 0
        rejected = 0
        produced: List[PaperFill] = []

        equity = self.ledger.equity(prices)
        day_key = moment.date().isoformat()
        day_start = self._day_start_equity.setdefault(day_key, equity)
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        gross = sum(abs(p.market_value(prices.get(p.instrument_id) or 0.0))
                    for p in self.ledger.open_positions()
                    if prices.get(p.instrument_id))

        order_time = moment + timedelta(
            seconds=self.config.signal_to_order_seconds)

        for intent in evaluation.intents:
            quantity = self._target_quantity(intent, equity,
                                             prices.get(intent.instrument_id))
            if quantity is None or abs(quantity) <= 1e-9:
                continue

            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
            current = self.ledger.positions.get(intent.instrument_id)
            held = current.quantity if current else 0.0
            is_increase = abs(held + quantity) > abs(held) + 1e-12

            self.log(PaperEventKind.ORDER_INTENT, moment,
                     f"{side.value} {abs(quantity):.6f} {intent.instrument_id}",
                     instrument_id=intent.instrument_id,
                     signal_id=intent.source_signal_id,
                     target_weight=intent.target_weight)

            # --- the control gate (spec §38, §39, §40, §61) ---
            gate = self.controls.may_create_order(
                at=moment, account_status=self.account.status,
                session_status=self.session.status, is_increase=is_increase,
                instrument_id=intent.instrument_id,
                health_allows=health.allows_new_orders,
                health_detail=health.safe_mode_reason,
                equity=equity, day_start_equity=day_start,
                peak_equity=self._peak_equity, gross_exposure=gross)
            if not gate.allowed:
                rejected += 1
                self.log(PaperEventKind.ORDER_REJECTED, moment,
                         f"{intent.instrument_id}: {gate.detail}",
                         instrument_id=intent.instrument_id,
                         control=gate.control,
                         reason=gate.reason.value if gate.reason else "")
                continue

            key = PaperExecutor.idempotency_key(
                self.session.session_id, intent.instrument_id, moment,
                intent.source_signal_id, intent.target_weight)
            if self.executor.find_by_idempotency(key) is not None:
                self.log(PaperEventKind.ORDER_REJECTED, moment,
                         f"{intent.instrument_id}: duplicate decision, already ordered",
                         instrument_id=intent.instrument_id, reason="duplicate")
                continue

            order = PaperOrder(
                order_id=f"po-{hashlib.sha1(key.encode()).hexdigest()[:16]}",
                session_id=self.session.session_id,
                account_id=self.account.account_id,
                instrument_id=intent.instrument_id, side=side,
                quantity=abs(quantity),
                order_type=self.config.default_order_type,
                time_in_force=self.config.default_time_in_force,
                idempotency_key=key,
                signal_id=intent.source_signal_id,
                decision_id=evaluation.decision.decision_id,
                intent_id=intent.intent_id,
                target_weight=intent.target_weight,
                decided_at=moment, created_at=order_time,
                execution_model_version=self.executor.version)

            placed = self.executor.place_order(
                order, order_time,
                freshness=(report.status_for(intent.instrument_id).freshness
                           if report.status_for(intent.instrument_id) else None),
                available_cash=self.ledger.cash)

            if placed.state == PaperOrderState.REJECTED:
                rejected += 1
                self.log(PaperEventKind.ORDER_REJECTED, moment,
                         f"{intent.instrument_id}: {placed.reject_detail}",
                         order_id=placed.order_id,
                         instrument_id=intent.instrument_id,
                         reason=placed.reject_reason.value
                         if placed.reject_reason else "")
                continue

            created += 1
            self.controls.record_order(moment)
            self.log(PaperEventKind.ORDER_ACCEPTED, order_time,
                     f"{side.value} {abs(quantity):.6f} {intent.instrument_id}",
                     order_id=placed.order_id,
                     instrument_id=intent.instrument_id,
                     signal_id=intent.source_signal_id)

            # A market order may fill on the next available session; a
            # resting order will be retried on later ticks.
            following = self.calendar.next_bar_after(
                intent.instrument_id, order_time)
            if following is not None:
                for fill in self.executor.try_fill(placed, following.timestamp):
                    self.fills.append(fill)
                    produced.append(fill)
                    self.log(PaperEventKind.FILL, fill.filled_at,
                             f"{fill.side.value} {fill.quantity:.6f} "
                             f"{fill.instrument_id} @ {fill.price:.4f}",
                             order_id=placed.order_id, fill_id=fill.fill_id,
                             instrument_id=fill.instrument_id)

        return created, rejected, produced

    def _target_quantity(self, intent, equity: float,
                         price: Optional[float]) -> Optional[float]:
        """Signed delta from what is held to the intent's target weight."""
        if price is None or price <= 0 or equity <= 0:
            return None
        if intent.target_weight is None:
            return intent.target_quantity
        target = (intent.target_weight * equity) / price
        current = self.ledger.positions.get(intent.instrument_id)
        held = current.quantity if current else 0.0
        return finite_or_none(target - held)

    def _snapshot(self, moment: datetime, prices: Dict[str, Optional[float]],
                  report: FreshnessReport, health: SystemHealth) -> PaperSnapshot:
        state = self.ledger.mark_to_market(moment, prices)
        equity = state.equity
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = (safe_ratio(equity - self._peak_equity, self._peak_equity)
                    if self._peak_equity and self._peak_equity > 0 else None)

        unrealized = 0.0
        saw = False
        for position in self.ledger.open_positions():
            price = prices.get(position.instrument_id)
            if price is not None and price > 0:
                unrealized += position.unrealized(price)
                saw = True

        raw = f"{self.session.session_id}|{moment.isoformat()}"
        return PaperSnapshot(
            snapshot_id=f"ps-{hashlib.sha1(raw.encode()).hexdigest()[:16]}",
            session_id=self.session.session_id,
            account_id=self.account.account_id, at=moment,
            equity=equity, cash=state.cash, positions_value=state.positions_value,
            gross_exposure=state.gross_exposure, net_exposure=state.net_exposure,
            long_exposure=state.long_exposure, short_exposure=state.short_exposure,
            leverage=safe_ratio(state.gross_exposure, equity) if equity > 0 else None,
            realized_pnl=self.ledger.realized_pnl,
            unrealized_pnl=unrealized if saw else None,
            drawdown=drawdown, open_positions=state.open_positions,
            unpriced_positions=state.unpriced_positions,
            data_freshness=report.worst, health=health.overall)

    # ---------------- control surface ----------------

    def pause(self, at: Optional[datetime] = None, actor: str = "operator",
              reason: str = "") -> None:
        moment = require_utc(at or self.clock.now(), "at")
        previous = self.session.status.value
        self.session.status = PaperSessionStatus.PAUSED
        self.account.status = PaperAccountStatus.PAUSED
        self.controls.record_configuration_change(
            self.session.session_id, "session_status", previous,
            self.session.status.value, moment, actor, reason)
        self.log(PaperEventKind.SESSION_PAUSED, moment, reason or "paused")

    def resume(self, at: Optional[datetime] = None, actor: str = "operator",
               reason: str = "") -> None:
        moment = require_utc(at or self.clock.now(), "at")
        previous = self.session.status.value
        self.session.status = PaperSessionStatus.RUNNING
        self.account.status = PaperAccountStatus.ACTIVE
        self.controls.record_configuration_change(
            self.session.session_id, "session_status", previous,
            self.session.status.value, moment, actor, reason)
        self.log(PaperEventKind.SESSION_RESUMED, moment, reason or "resumed")

    def emergency_stop(self, at: Optional[datetime] = None,
                       actor: str = "operator", reason: str = "") -> None:
        """
        Halt new exposure immediately (spec §38).

        Does NOT liquidate. Forced liquidation carries its own risks and
        is not a decision a safety control should take by itself; the
        position stays observable and can be exited deliberately.
        """
        moment = require_utc(at or self.clock.now(), "at")
        previous = self.account.status.value
        self.account.status = PaperAccountStatus.EMERGENCY_STOP
        self.health_monitor.enter_safe_mode(reason or "emergency stop")
        self.controls.record_configuration_change(
            self.session.session_id, "account_status", previous,
            self.account.status.value, moment, actor, reason)
        self.log(PaperEventKind.CONTROL, moment,
                 f"EMERGENCY STOP: {reason}", actor=actor)

    def stop(self, at: Optional[datetime] = None, actor: str = "operator",
             reason: str = "") -> None:
        moment = require_utc(at or self.clock.now(), "at")
        self.session.status = PaperSessionStatus.COMPLETED
        self.session.ended_at = moment
        self.log(PaperEventKind.SESSION_STOPPED, moment, reason or "stopped")

    # ---------------- reporting ----------------

    def describe(self) -> Dict[str, Any]:
        return {
            "session_id": self.session.session_id,
            "account_id": self.account.account_id,
            "status": self.session.status.value,
            "ticks": self.session.ticks_processed,
            "clock": self.clock.describe(),
            "config_fingerprint": self.config.fingerprint(),
            "executor": self.executor.describe(),
            "code_version": CODE_VERSION,
            "is_paper": True,
            "connects_to_broker": False,
        }
