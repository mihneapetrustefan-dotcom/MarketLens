"""
src/trading/loop.py
-------------------------
One bounded cycle of the paper-trading operating loop (§2, §13).

    mode -> health -> market data -> signals -> eligibility
         -> portfolio -> risk -> targets -> intents -> submission
         -> broker poll -> fills -> positions -> reconciliation
         -> P&L -> outcomes -> persist

WHAT THIS FILE IS AND IS NOT
--------------------------------
It is an ORCHESTRATION. Every decision it appears to make is made
somewhere else:

    sizing and risk        Phase 11  `PortfolioService.evaluate`
    risk -> execution      Phase 17  `intake.from_decision`
    validation, limits     Phase 14  `ExecutionOrchestrator`
    the venue              Phase 15  `IBKRGateway`
    reconciliation         Phase 14  `BrokerReconciler`
    model deployability    Phase 18  `modeling.selection`

There is no second risk check, no second sizing rule and no second
order lifecycle in this file. The one thing it owns is ORDER: which
question is asked before which, and what happens when one cannot be
answered.

THE JOINT THAT WAS MISSING
------------------------------
`src/execution/intake.py` was written in Phase 17 to convert an
approved `RiskDecision` into execution requests, and until this phase
it had **zero callers** — both CLIs mentioned it in a help string and
built their requests by hand from a flag named
`--assume-risk-approved`. This module is its first caller, and that is
the single change that turns a set of subsystems into a loop.

FAIL CLOSED IS THE DEFAULT PATH, NOT THE EXCEPTION PATH
-----------------------------------------------------------
`CycleResult.mode` starts OFF and `health` starts BLOCKED. Every stage
that cannot establish its precondition records a `Block` and returns.
An exception escaping a stage is caught, recorded as a FAILED stage
with the error text, and the cycle ends without trading — because a
stage that crashed did not establish anything, and continuing past it
would be trading on an unknown state.

IDEMPOTENCY IS STRUCTURAL, NOT DEFENSIVE
--------------------------------------------
The cycle anchors to a quantized clock (`cycle_anchor`). Phase 11
derives `decision_id` from `as_of`; Phase 11 derives `intent_id` from
`decision_id`; Phase 14 derives the idempotency key from `intent_id`
plus quantity and shape. So a retried run recomputes the same ids all
the way down and the orchestrator recognises its own previous work.
Nothing in this file compares timestamps or counts attempts.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.domain.broker_models import (
    CanonicalTimeInForce, ExecutionFill, ExecutionOrder, PositionSnapshot,
)
from src.domain.portfolio_models import RiskDecisionState
from src.domain.signal_models import Signal
from src.domain.trading_loop_models import (
    LOOP_METHOD_VERSION, ActualPosition, Block, BlockReason,
    CanonicalAccountState, CycleResult, CycleStatus, LoopHealth, LoopStage,
    LoopTimestamps, ModeResolution, PaperSessionRecord, PositionOrigin,
    StageOutcome, StageResult, TradeLineage, TradingMode, cycle_anchor,
    cycle_id_for, require_utc,
)
from src.execution import intake
from src.execution.intake import LineageIncomplete, RiskNotApproved
from src.portfolio.service import PortfolioService
from src.portfolio.sizing import FixedFractionSizing
from src.trading import accounts as account_state
from src.trading import outcomes as loop_outcomes
from src.trading import targets as target_math
from src.trading.eligibility import (
    EligibilityContext, EligibilityGate, EligibilityPolicy, model_of,
)
from src.trading.mode import TradingModeStore
from src.trading.repository import TradingLoopRepository
from src.trading.stack import ExecutionStack, loop_caller

#: The portfolio id the loop evaluates under. Never written to the live
#: `portfolios` table — the id exists so Phase 11's types have
#: something to carry, exactly as Phase 12 and Phase 13 do.
LOOP_PORTFOLIO_ID = "__paper_loop__"

#: How far behind the wall clock a cycle's anchor may be before the
#: loop refuses to trade on it.
#:
#: `run_cycle(now=...)` takes the moment as an argument, which is what
#: makes the loop testable -- and also what would let a backfill or a
#: replay decide on month-old signals and send the resulting orders to
#: a live venue at today's prices. Point-in-time correctness protects
#: the DECISION; nothing protected the EXECUTION.
#:
#: Four hours rather than one: a scheduled run can be delayed by a slow
#: runner, and refusing a legitimate cycle is also a cost. A replay is
#: days or months out and is nowhere near this.
MAX_ANCHOR_DRIFT_SECONDS = 4 * 3600.0


def _validation_id(config: "LoopConfig", method_version: str) -> str:
    """The id `PaperValidator.start` would mint for this session."""
    from src.trading.validation import validation_id_for
    return validation_id_for(config.strategy_id or "",
                             config.strategy_version or "v1",
                             config.session_id, method_version)


@dataclass
class LoopConfig:
    """
    Everything one session runs under, in one versioned object (§29).

    Passed whole into `configuration_fingerprint`, so a change to any
    field produces a different fingerprint and a resumed session
    refuses rather than blending two configurations into one record.
    """
    session_id: str
    name: str = "paper loop"
    broker_id: str = "ibkr"
    account_id: str = ""
    actor: str = "trading-loop"
    cycle_seconds: int = 900
    universe_limit: int = 25
    constraint_version: str = ""
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    challenger_id: Optional[str] = None
    allow_paper_orders: bool = False
    experimental: bool = False
    eligibility: EligibilityPolicy = field(default_factory=EligibilityPolicy)
    max_price_age_days: float = 5.0
    #: How far behind the wall clock this session's anchors may be.
    #: Configuration rather than a constant because a deliberate replay
    #: is legitimate and a test must pin its clock -- and because a
    #: threshold nobody can see is a threshold nobody reviews. It is
    #: part of `as_dict()`, so it is in the session fingerprint.
    max_anchor_drift_seconds: float = MAX_ANCHOR_DRIFT_SECONDS
    dry_run: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {"broker_id": self.broker_id, "account_id": self.account_id,
                "cycle_seconds": self.cycle_seconds,
                "universe_limit": self.universe_limit,
                "constraint_version": self.constraint_version,
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "challenger_id": self.challenger_id,
                "experimental": self.experimental,
                "max_price_age_days": self.max_price_age_days,
                "max_anchor_drift_seconds": self.max_anchor_drift_seconds,
                "eligibility": json.dumps(self.eligibility.as_dict(),
                                          sort_keys=True)}


class TradingLoop:
    """
    One session's loop. `run_cycle()` advances it exactly once.

    Not a daemon, and deliberately so: this repository has no
    persistent runtime, every phase is a batch job under cron, and a
    `while True` here would be a process nothing starts. The state
    lives in the database; a scheduled `run_cycle` is a running paper
    account. That is the same decision Phase 13 made and it has held.
    """

    def __init__(self, conn: sqlite3.Connection, stack: ExecutionStack,
                 config: LoopConfig,
                 method_version: str = LOOP_METHOD_VERSION):
        self.conn = conn
        self.stack = stack
        self.config = config
        self.method_version = method_version
        self.repository = TradingLoopRepository(conn, method_version)
        self.modes = TradingModeStore(conn, method_version)
        self.gate = EligibilityGate(config.eligibility, method_version)
        self.caller = loop_caller(config.actor, config.allow_paper_orders)
        self.repository.initialize()
        initialize_portfolio_schema(conn)

    # ==================================================================
    # session
    # ==================================================================

    def open_session(self, now: datetime) -> PaperSessionRecord:
        """
        Create or resume the session, pinning its configuration.

        A resumed session whose configuration changed raises
        `ConfigurationChanged` (§29). That is a refusal rather than a
        warning because a session spanning two configurations produces
        numbers that cannot be attributed to either.
        """
        require_utc(now, "now")
        from src.domain.trading_loop_models import configuration_fingerprint

        fingerprint = configuration_fingerprint(self.config.as_dict())
        existing = self.repository.get_session(self.config.session_id)
        if existing is not None:
            existing.assert_configuration(fingerprint)
            return existing

        session = PaperSessionRecord(
            session_id=self.config.session_id, name=self.config.name,
            mode=TradingMode.PAPER, method_version=self.method_version,
            broker_id=self.config.broker_id,
            account_id=self.config.account_id or self.stack.account_id,
            strategy_id=self.config.strategy_id,
            strategy_version=self.config.strategy_version,
            challenger_id=self.config.challenger_id,
            experimental=self.config.experimental,
            constraint_version=self.config.constraint_version,
            configuration_fingerprint=fingerprint,
            configuration_json=json.dumps(self.config.as_dict(), sort_keys=True),
            cycle_seconds=self.config.cycle_seconds,
            status="open", started_at=now)
        self.repository.save_session(session)
        self.repository.audit(self.config.actor, "session_opened", now,
                              session_id=session.session_id,
                              detail=f"configuration {fingerprint}")
        return session

    def close_session(self, now: datetime, reason: str) -> None:
        session = self.repository.get_session(self.config.session_id)
        if session is None:
            return
        session.status = "closed"
        session.ended_at = now
        self.repository.save_session(session)
        self.repository.audit(self.config.actor, "session_closed", now,
                              session_id=session.session_id, detail=reason)

    # ==================================================================
    # one cycle
    # ==================================================================

    def run_cycle(self, now: Optional[datetime] = None,
                  worker: str = "") -> CycleResult:
        """
        Advance the loop once.

        Returns a `CycleResult` in every case, including the blocked
        and failed ones. A caller that only reads `orders_submitted`
        still behaves correctly; a caller that reads `blocks` can also
        explain the cycles that placed nothing, which spec §22 calls
        half the evidence.
        """
        now = require_utc(now or datetime.now(timezone.utc), "now")
        anchor = cycle_anchor(now, self.config.cycle_seconds)
        drift = (datetime.now(timezone.utc) - anchor).total_seconds()
        cycle_id = cycle_id_for(self.config.session_id, anchor,
                                self.method_version)
        worker = worker or f"{self.config.actor}"

        result = CycleResult(cycle_id=cycle_id,
                             session_id=self.config.session_id,
                             anchor=anchor, method_version=self.method_version)
        # The ANCHOR, not the wall clock. In a point-in-time system the
        # observation is "the world as of T", and T is the anchor -- so
        # `observed_at` and `decision_time` are the same instant, while
        # the wall-clock moment the process happened to read at lives on
        # the cycle row as `claimed_at`. Setting this to `now` made the
        # §24 chronology check report `decision_time precedes
        # observed_at` on every cycle, which was the check being right
        # about a field being wrong.
        result.timestamps.event_time = anchor
        result.timestamps.observed_at = anchor

        session = self.open_session(now)

        # -- the anchor must describe roughly now (§31) ----------------
        # A replay decides on old information, which is correct, and
        # would then trade at today's venue, which is not. Blocked
        # rather than refused outright, so the cycle still observes,
        # reconciles and records -- a replay that cannot trade is still
        # a useful read of the broker.
        if (drift > self.config.max_anchor_drift_seconds
                and not self.config.dry_run):
            result.block(
                BlockReason.STALE_SIGNAL,
                f"the cycle anchor is {drift / 3600.0:.1f}h behind the wall "
                f"clock, past the "
                f"{self.config.max_anchor_drift_seconds / 3600.0:.1f}h limit. "
                f"A replay may read the broker but may not trade on "
                f"information this old.")
            self.config = replace(self.config, dry_run=True)

        # -- the claim ------------------------------------------------
        self.repository.reclaim_stale(now)
        self.repository.open_cycle(cycle_id, self.config.session_id, anchor, now)
        if not self.repository.claim(cycle_id, worker, now):
            status = self.repository.cycle_status(cycle_id) or "unknown"
            result.status = CycleStatus.ABANDONED
            result.block(BlockReason.CYCLE_ALREADY_RUNNING,
                         f"cycle {cycle_id} is {status} and held by another "
                         f"worker; this run changed nothing")
            result.detail = "not claimed"
            return result

        try:
            self._advance(result, session, now, anchor)
        except Exception as error:                          # noqa: BLE001
            # The outermost net. A stage that raised has already been
            # recorded as FAILED by `_stage`; this catches anything
            # outside one and makes sure the cycle row still closes,
            # because a cycle stuck in CLAIMED would block its anchor
            # until `reclaim_stale` timed it out.
            result.status = CycleStatus.FAILED
            result.detail = f"{type(error).__name__}: {error}"
            self.repository.audit(self.config.actor, "cycle_failed", now,
                                  cycle_id=cycle_id,
                                  detail=traceback.format_exc(limit=3))
        else:
            if result.blocked and not result.traded:
                result.status = CycleStatus.BLOCKED
            elif result.status is CycleStatus.CLAIMED:
                result.status = CycleStatus.COMPLETED

        finished = datetime.now(timezone.utc)
        self.repository.save_cycle(result, finished)
        return result

    # ------------------------------------------------------------------

    @contextmanager
    def _stage(self, result: CycleResult, stage: LoopStage
               ) -> Iterator[StageResult]:
        """
        Record one stage, whatever happens to it.

        A stage that raises is recorded FAILED with the exception text
        and the exception is re-raised — the record must not swallow
        the error, and the error must not lose the record.
        """
        started = datetime.now(timezone.utc)
        entry = StageResult(cycle_id=result.cycle_id, stage=stage,
                            outcome=StageOutcome.RAN, started_at=started)
        result.stages.append(entry)
        try:
            yield entry
        except Exception as error:                          # noqa: BLE001
            entry.outcome = StageOutcome.FAILED
            entry.detail = f"{type(error).__name__}: {error}"
            entry.finished_at = datetime.now(timezone.utc)
            raise
        entry.finished_at = datetime.now(timezone.utc)

    def _blocked(self, result: CycleResult, entry: StageResult,
                 reason: BlockReason, detail: str) -> None:
        entry.outcome = StageOutcome.BLOCKED
        entry.detail = detail
        entry.block = result.block(reason, detail)

    # ------------------------------------------------------------------

    def _advance(self, result: CycleResult, session: PaperSessionRecord,
                 now: datetime, anchor: datetime) -> None:
        """
        One cycle: OBSERVE, then DECIDE. In that order, and it matters.

        Observing runs first because the fills this cycle is about were
        created by a PREVIOUS one. Deciding against a book that has not
        yet absorbed them double-counts: the position is already held at
        the venue AND the order that established it still reads as
        working, so the arithmetic sees the target met twice and
        proposes a trade to undo the difference.

        That is not a hypothetical -- deciding first produced, on the
        second cycle of the end-to-end test, a SELL of 500 shares
        against a target of 499.87 and a holding of 500, because the
        same 500 was counted as both `actual` and `pending`.

        The two halves also fail differently. Deciding may stop early --
        no eligible signal, risk declining, a gap too small to trade, a
        blocked health verdict -- and all of those are the system
        working. Observing must run REGARDLESS: a cycle that places no
        order still has to poll, fill, reconcile and record, or a filled
        order stays invisible until the loop happens to want another
        trade in the same instrument.
        """
        context: Dict[str, Any] = {"account": None, "prices": {},
                                   "submitted": [], "evaluation": None,
                                   "positions": [], "open_orders": []}

        # ---- 1. mode (§9) -------------------------------------------
        with self._stage(result, LoopStage.MODE) as entry:
            self.modes.apply_to_safety(self.stack.safety, now)
            resolution = self.modes.resolve(now)
            result.mode = resolution.mode
            entry.detail = f"{resolution.mode.value} ({resolution.source.value})"
            if not resolution.may_trade:
                self._blocked(result, entry,
                              BlockReason.MODE_NOT_PERMITTED,
                              resolution.reason or "mode is not PAPER")

        # ---- 2. broker + account state (§4) -------------------------
        with self._stage(result, LoopStage.HEALTH) as entry:
            # Beat the session first. `IBKRGateway.heartbeat` exists
            # because the Client Portal session "lapses when idle", and
            # until Phase 25.5 NOTHING called it -- not the loop, not
            # either CLI. A loop that ticks every fifteen minutes is
            # exactly the idle pattern it was written for. Its failure
            # is not fatal on its own: the account read that follows is
            # the real test of the session, and `connection_state`
            # records what the beat found.
            beat = None
            try:
                beat = self.stack.gateway.heartbeat()
            except Exception as error:                      # noqa: BLE001
                entry.detail = f"heartbeat failed: {error}; "
            account, broker_positions, open_orders = account_state.read_account_state(
                self.stack.gateway, self.stack.broker_id,
                self.config.account_id or self.stack.account_id,
                result.cycle_id, now)
            self.repository.save_account_state(account)
            context["account"] = account
            entry.count = len(broker_positions)
            entry.detail = (
                (entry.detail or "")
                + ("session beat ok, " if beat else
                   "session beat FAILED, " if beat is False else "")
                + f"account {account.source.value}, "
                + f"{len(broker_positions)} position(s), "
                + f"{len(open_orders)} open order(s)")

        self._observe(result, context, now, anchor)
        self._decide_and_submit(result, session, context, now, anchor)
        self._persist(result, context, now)

    def _decide_and_submit(self, result: CycleResult,
                           session: PaperSessionRecord,
                           context: Dict[str, Any], now: datetime,
                           anchor: datetime) -> Dict[str, Any]:
        """
        Decide, and submit what risk approved.

        Runs on the book the observation half has just refreshed, so
        `actual` and `pending` cannot describe the same shares. Every
        `return` in here means "this cycle will place no order", never
        "this cycle is over" -- the persist stage runs afterwards
        either way.
        """
        account = context.get("account")
        # Positions and open orders as they stand AFTER the poll. The
        # stale copies read at the top of the cycle are not used for
        # any decision.
        broker_positions = context.get("positions") or []
        open_orders = context.get("open_orders") or []

        # ---- 3. market data (§25) -----------------------------------
        prices: Dict[str, float] = {}
        price_ages: Dict[str, float] = {}
        newest_age: Optional[float] = None
        with self._stage(result, LoopStage.MARKET_DATA) as entry:
            newest_age = self._newest_bar_age_days(anchor)
            entry.detail = ("no bars" if newest_age is None
                            else f"newest bar {newest_age:.1f} days old")

        # ---- 4. signals ---------------------------------------------
        service = PortfolioService(
            self.conn,
            **({"constraint_version": self.config.constraint_version}
               if self.config.constraint_version else {}))
        signals: List[Signal] = []
        with self._stage(result, LoopStage.SIGNALS) as entry:
            signals = self._signals_at(service, anchor)
            result.signals_seen = len(signals)
            entry.count = len(signals)
            entry.detail = f"{len(signals)} live at the anchor"

        if signals:
            prices, price_ages = self._prices_for(
                service, [s.instrument_id for s in signals], anchor)
        context["prices"] = prices

        # ---- 5. health verdict (§27) --------------------------------
        kill, _ = self.modes.kill_switch()
        last_finished = self.repository.last_finished_at(self.config.session_id)
        report = account_state.assess_health(
            result.cycle_id, now,
            mode=self.modes.resolve(now), kill_switch=kill, account=account,
            connection_healthy=self.stack.connected,
            market_data_age_days=newest_age,
            signals_available=len(signals),
            portfolio_known=account is not None and account.is_known,
            risk_known=True,
            execution_ready=self.stack.may_submit,
            reconciliation_clean=context.get("reconciliation_clean"),
            database_ok=True,
            scheduler_age_seconds=((now - last_finished).total_seconds()
                                   if last_finished else None))
        result.health = report.overall
        for reason, detail in account_state.blocks_from(report):
            result.block(reason, detail)

        # ---- 6. eligibility (§14) -----------------------------------
        eligible: List[Signal] = []
        with self._stage(result, LoopStage.ELIGIBILITY) as entry:
            deployable, statuses, governance_detail = self._model_governance()
            if governance_detail:
                # A gate that could not answer is not a gate that said
                # no. Recorded on the stage so an operator sees it,
                # rather than every signal quietly reading experimental
                # for a reason nobody can find.
                entry.detail = "MODEL GATE UNREADABLE: " + governance_detail
                result.block(BlockReason.MODEL_NOT_DEPLOYABLE,
                             governance_detail)
            eligibility_context = EligibilityContext(
                as_of=anchor, prices=prices, price_ages_days=price_ages,
                tradeable_instruments=None,
                open_order_quantity=target_math.combined_pending(
                    open_orders, self._our_working_orders()),
                model_deployable=deployable, model_status=statuses)
            verdicts = self.gate.evaluate_all(signals, result.cycle_id,
                                              eligibility_context)
            self.repository.save_eligibility(verdicts)
            passed = {v.signal_id for v in verdicts if v.is_eligible}
            eligible = [s for s in signals if s.signal_id in passed]
            result.signals_eligible = len(eligible)
            # PROVENANCE, PER INSTRUMENT. Phase 25 passed none of this
            # and every order reached `trade_outcomes` with an empty
            # model, prediction and strategy -- so Phase 16 recorded
            # `lineage_complete = 0` on every paper trade while Phase
            # 25's own chain read complete, because that chain did not
            # include the model. Two lineage models disagreeing, and
            # the one that certified was the one that could not see.
            context["provenance"] = self._provenance_for(eligible)
            entry.count = len(eligible)
            entry.detail = ((entry.detail + "; " if entry.detail else "")
                            + f"{len(eligible)} of {len(signals)} eligible; "
                            + ", ".join(sorted(
                                {v.code.value for v in verdicts
                                 if not v.is_eligible})))

        # A blocked health verdict stops here. Everything above this
        # line is READING; everything below it can create an order.
        if result.health is LoopHealth.BLOCKED:
            return context

        if not eligible:
            return context

        # ---- 7. portfolio + risk (§5, §8) ---------------------------
        evaluation = None
        held = target_math.positions_from_broker(
            broker_positions, LOOP_PORTFOLIO_ID, anchor)
        with self._stage(result, LoopStage.PORTFOLIO) as entry:
            evaluation = service.evaluate(
                LOOP_PORTFOLIO_ID, anchor,
                sizing=FixedFractionSizing(), signals=eligible,
                positions=held,
                cash=(account.cash if account and account.cash is not None
                      else 0.0),
                # Persisted, and this is the first phase that does it.
                # §6 wants a portfolio decision that can be asked which
                # signal caused it and under which risk configuration;
                # §32 wants the risk decision auditable; and Phase 20's
                # risk detector joins `risk_decisions` through
                # `trade_outcomes`. Phase 11 owns these tables and its
                # own `_persist` writes them -- `portfolios` and
                # `positions` are NOT among them, which is why the
                # paper book stays out of the live portfolio tables.
                persist=True)
            result.timestamps.decision_time = anchor
            entry.count = len(evaluation.intents)

            # WHICH ELIGIBLE SIGNALS THE SIZING LAYER DROPPED, AND WHY.
            # Phase 11 applies its own gates inside `propose()` -- a
            # confidence floor from the constraint set, expiry, and
            # priceability -- and a signal it drops simply does not
            # appear in the proposal. Without this line those signals
            # vanish between "eligible" and "no changes proposed", and
            # §14's rule that no signal is silently discarded would hold
            # for the loop's own gate and not for the one after it.
            #
            # This is not a hypothetical: on the production database all
            # four eligible signals carry confidence 0.30 against a
            # `min_signal_confidence` of 0.40, so the honest report is
            # "risk declined to size them", not "risk approved".
            sized = set(getattr(evaluation.proposal, "source_signal_ids", [])
                        or [])
            dropped = [s for s in eligible if s.signal_id not in sized]
            floor = self._confidence_floor(service)
            entry.count = len(evaluation.intents)
            entry.detail = (
                f"{len(held)} held, {len(sized)} of {len(eligible)} eligible "
                f"signal(s) sized"
                + (f"; {len(dropped)} dropped by the sizing layer"
                   + (f" (confidence floor {floor:.2f}; dropped carry "
                      + ", ".join(sorted(
                          f"{s.confidence:.2f}" if s.confidence is not None
                          else "none" for s in dropped)) + ")"
                      if floor is not None else "")
                   if dropped else ""))

        with self._stage(result, LoopStage.RISK) as entry:
            decision = evaluation.decision
            result.timestamps.risk_time = anchor
            entry.detail = (f"{decision.state.value}: "
                            + (decision.summary or "; ".join(decision.reasons)
                               or "no reason recorded"))
            if not decision.is_approved:
                # NOT a block. Risk declining is the system working, and
                # recording it as a block would make a correct refusal
                # look like an outage on the health page.
                entry.outcome = StageOutcome.SKIPPED
                return context

        # ---- 8. targets and deltas (§7, §16) ------------------------
        with self._stage(result, LoopStage.TARGETS) as entry:
            targets = target_math.targets_from_intents(
                evaluation.intents, result.cycle_id, evaluation.decision,
                prices, anchor)
            self.repository.save_targets(targets)
            actuals = target_math.actuals_from_broker(
                broker_positions, result.cycle_id, now, reconciled=True)
            self.repository.save_actuals(actuals)
            deltas = target_math.compute_deltas(
                targets, actuals,
                pending=target_math.combined_pending(
                    open_orders, self._our_working_orders()),
                intents=evaluation.intents,
                mappings={t.instrument_id:
                          self.stack.gateway.resolve_instrument(t.instrument_id)
                          for t in targets})
            self.repository.save_deltas(result.cycle_id, deltas.deltas, now)
            result.targets_set = len(targets)
            entry.count = len(deltas.actionable)
            entry.detail = (f"{len(targets)} target(s), "
                            f"{len(deltas.actionable)} actionable, "
                            f"{len(deltas.rejected)} rejected")

        if not deltas.quantities:
            return context

        # ---- 9. intents -> execution requests (§8, the Phase 17 joint)
        provenance = context.get("provenance") or {
            "strategies": {}, "model_versions": {}, "predictions": {},
            "trained_models": {}}
        requests = []
        with self._stage(result, LoopStage.INTENTS) as entry:
            try:
                result_intake = intake.from_decision(
                    evaluation.decision, evaluation.intents,
                    broker_id=self.stack.broker_id,
                    account_id=self.config.account_id or self.stack.account_id,
                    now=now, prices=prices,
                    quantities=deltas.quantities,
                    strategy_id=self.config.strategy_id,
                    strategy_ids=provenance["strategies"],
                    model_versions=provenance["model_versions"],
                    predictions=provenance["predictions"],
                    time_in_force=CanonicalTimeInForce.DAY,
                    policy="market",
                    data_is_stale=(newest_age or 0.0) > self.config.max_price_age_days,
                    freshness_detail=(f"newest bar {newest_age:.1f} days old"
                                      if newest_age is not None
                                      else "no bars"))
            except (RiskNotApproved, LineageIncomplete) as error:
                # Both are refusals by design, and both mean the same
                # thing here: this cycle will not trade. Recorded as a
                # block rather than a crash so the reason survives.
                self._blocked(result, entry, BlockReason.RISK_STATE_UNKNOWN,
                              f"{type(error).__name__}: {error}")
                return context
            requests = result_intake.requests
            result.intents_created = len(requests)
            result.intents_rejected = len(result_intake.rejected)
            entry.count = len(requests)
            entry.detail = (f"{len(requests)} request(s), "
                            f"{len(result_intake.rejected)} rejected: "
                            + ", ".join(sorted({r.reason for r
                                                in result_intake.rejected})))

        # ---- 10. submission (§15) -----------------------------------
        submitted: List[ExecutionOrder] = []
        with self._stage(result, LoopStage.SUBMISSION) as entry:
            # THE LAST GATE, AND THE ONE THAT MAKES A BLOCK MEAN
            # SOMETHING.
            #
            # `result.block()` records a reason; until Phase 25.5 it did
            # not stop anything. The only thing that stopped a cycle
            # trading was `health is BLOCKED`, so any block recorded
            # after the health verdict -- an unreadable model gate, a
            # P&L disagreement between the broker and our own books --
            # was written into the record and then ignored, and the
            # cycle submitted anyway. Found by making the model gate
            # raise: the block appeared and the order still went.
            #
            # Checked here rather than earlier so everything before it
            # still runs and is recorded: a blocked cycle should still
            # be able to explain what it would have done.
            if result.blocks:
                self._blocked(
                    result, entry, result.blocks[0].reason,
                    "not submitted: " + "; ".join(
                        f"{b.reason.value} ({b.detail})"
                        for b in result.blocks[:3]))
                return context

            if self.config.dry_run:
                entry.outcome = StageOutcome.SKIPPED
                entry.detail = (f"dry run: {len(requests)} request(s) were "
                                f"validated and not sent")
                for request in requests:
                    self.stack.service.dry_run(self.caller, request)
                return context
            if not self.stack.may_submit:
                self._blocked(
                    result, entry, BlockReason.BROKER_UNHEALTHY,
                    "the gateway is connected but paper ordering is not "
                    "enabled (IBKR_PAPER_ORDERING_ENABLED)")
                return context

            result.timestamps.submission_time = now
            for request in requests:
                outcome = self.stack.service.submit(self.caller, request)
                if outcome.accepted and outcome.order is not None:
                    submitted.append(outcome.order)
                    result.orders_submitted += 1
                    if outcome.order.acknowledged_at:
                        result.timestamps.broker_ack_time = \
                            outcome.order.acknowledged_at
                else:
                    result.orders_rejected += 1
            entry.count = result.orders_submitted
            entry.detail = (f"{result.orders_submitted} submitted, "
                            f"{result.orders_rejected} rejected")
            context["submitted"] = submitted

        context["submitted"] = submitted
        context["evaluation"] = evaluation
        return context

    # ------------------------------------------------------------------

    def _observe(self, result: CycleResult, context: Dict[str, Any],
                 now: datetime, anchor: datetime) -> None:
        """
        Poll, fill, reconcile, price and record -- always.

        Runs whether or not the decision half placed anything, because
        the fills it is looking for were created by a PREVIOUS cycle.
        """
        account = context.get("account")

        # ---- 11-12. poll and fills (§11, §15) -----------------------
        # `poll_broker` and not `drain_events`: the venue reports status
        # and executions through two different calls, and an
        # ORDER_FILLED event whose execution has not been collected
        # yet is a filled status the fills do not support -- which the
        # processor correctly refuses, sending the order to
        # RECONCILIATION_REQUIRED. Collect, poll, pair.
        report = None
        with self._stage(result, LoopStage.BROKER_POLL) as entry:
            report = self.stack.orchestrator.poll_broker(
                self.stack.broker_id, now)
            entry.count = report.processed
            entry.detail = (f"{report.processed} event(s), "
                            f"{report.applied} applied, "
                            f"{report.duplicates} duplicate")

        with self._stage(result, LoopStage.FILLS) as entry:
            new_fills = list(report.fills) if report else []
            result.fills_recorded = len(new_fills)
            if new_fills:
                result.timestamps.fill_time = max(
                    (f.filled_at for f in new_fills if f.filled_at),
                    default=None)
            entry.count = len(new_fills)
            entry.detail = f"{len(new_fills)} new fill(s)"

        # ---- 13. positions, reconciled (§16) ------------------------
        with self._stage(result, LoopStage.POSITIONS) as entry:
            entry.detail = ""
            fresh = list(self.stack.gateway.get_positions(
                self.config.account_id or self.stack.account_id, now))
            self.repository.save_actuals(target_math.actuals_from_broker(
                fresh, result.cycle_id, now, reconciled=True))
            result.positions_reconciled = len(fresh)
            result.timestamps.position_time = now
            context["positions"] = fresh
            # Re-read the venue's working orders AFTER the poll, so the
            # decision half never sees an order that has since filled
            # counted as still pending.
            try:
                context["open_orders"] = list(
                    self.stack.gateway.get_open_orders(
                        self.config.account_id or self.stack.account_id))
            except Exception as error:                      # noqa: BLE001
                context["open_orders"] = []
                entry.detail = f"open-order re-read failed: {error}; "
            entry.count = len(fresh)
            entry.detail += f"{len(fresh)} position(s) from the broker"

        # ---- 14. reconciliation (§12) -------------------------------
        with self._stage(result, LoopStage.RECONCILIATION) as entry:
            # Reconciliation asks the gateway three more questions, and
            # a gateway that has stopped answering will raise on any of
            # them. Caught HERE and turned into a block, because §25
            # says a failed reconciliation must mean NO TRADE -- not a
            # crashed cycle that never reaches the health verdict and
            # therefore records no reason at all. Found exactly that
            # way: stubbing `get_account` to raise produced a FAILED
            # cycle with an empty `blocks` list.
            try:
                record = self.stack.orchestrator.reconcile(
                    self.stack.broker_id,
                    self.config.account_id or self.stack.account_id, now,
                    internal_positions={p.instrument_id: p.quantity
                                        for p in fresh},
                    internal_cash=account.cash if account else None)
            except Exception as error:                      # noqa: BLE001
                self._blocked(result, entry,
                              BlockReason.RECONCILIATION_FAILED,
                              f"{type(error).__name__}: {error}")
                context["reconciliation_clean"] = False
                return
            if record is None:
                entry.outcome = StageOutcome.SKIPPED
                entry.detail = "the broker is not registered"
            else:
                self.stack.repository.save_reconciliation(record)
                result.discrepancies = len(record.mismatches)
                entry.count = len(record.mismatches)
                entry.detail = (f"{len(record.mismatches)} mismatch(es)"
                                if record.mismatches else "clean")
                context["reconciliation_clean"] = not record.mismatches
                if record.mismatches:
                    # ASK the broker about the ones it can answer for
                    # before calling them unresolved. §12 forbids
                    # silently overwriting a discrepancy; it does not
                    # forbid resolving one by asking. `resolve_unknown_orders`
                    # never resubmits -- it queries and records.
                    try:
                        resolutions = self.stack.orchestrator.resolve_unknown_orders(
                            self.stack.broker_id, now)
                    except Exception as error:              # noqa: BLE001
                        resolutions = []
                        entry.detail += f"; unknown-order query failed: {error}"
                    resolved = sum(1 for r in resolutions if r.resolved)
                    if resolved:
                        entry.detail += f"; {resolved} unknown order(s) resolved"
                    outstanding = len(record.mismatches) - resolved
                    if outstanding > 0:
                        result.block(BlockReason.RECONCILIATION_UNRESOLVED,
                                     f"{outstanding} unresolved "
                                     f"discrepancy/ies at the broker")
                        context["reconciliation_clean"] = False

        # ---- 15. P&L, both sides (§17) ------------------------------
        with self._stage(result, LoopStage.PNL) as entry:
            # Marked at the BROKER's prices. The decision half's
            # reference prices are not available yet -- it has not run --
            # and using a price from a different source than the
            # positions would mix two books in one number.
            marks: Dict[str, float] = {}
            for position in fresh:
                if position.market_price:
                    marks[position.instrument_id] = float(position.market_price)
            pnl = loop_outcomes.compute_pnl(
                account, list(self.stack.orchestrator.fills), fresh, now,
                marks=marks)
            agreement = pnl.agrees_within()
            entry.detail = (
                "local realized "
                f"{(pnl.local_realized.value if pnl.local_realized else None)}, "
                + ("sources agree" if agreement is True
                   else "SOURCES DISAGREE" if agreement is False
                   else "not comparable"))
            if agreement is False:
                # A disagreement between the broker's number and ours is
                # a reconciliation finding, not a number to average.
                result.block(BlockReason.RECONCILIATION_UNRESOLVED,
                             "broker-reported and locally-calculated P&L "
                             "disagree")

        # ---- 16. trade outcomes and their lineage (§18) -------------
        with self._stage(result, LoopStage.OUTCOMES) as entry:
            produced = loop_outcomes.build_outcomes(
                [o for o in self.stack.orchestrator.orders.values()
                 if o.broker_id == self.stack.broker_id],
                list(self.stack.orchestrator.fills),
                session_id=self.config.session_id, marks=marks,
                strategy_version=self.config.strategy_version,
                models_by_signal=self._models_by_signal(
                    [o.signal_id for o
                     in self.stack.orchestrator.orders.values()
                     if o.signal_id]))
            written = loop_outcomes.persist_outcomes(
                self.conn, produced, session_id=self.config.session_id)
            result.outcomes_recorded = written
            if produced:
                result.timestamps.outcome_time = now
            context["outcome_by_order"] = {
                o.lineage.order_id: o.outcome_id
                for o in produced if o.lineage.order_id}
            entry.count = written
            entry.detail = (f"{written} trade outcome(s), "
                            f"{sum(1 for o in produced if o.is_open)} open")

    # ------------------------------------------------------------------

    def _persist(self, result: CycleResult, context: Dict[str, Any],
                 now: datetime) -> None:
        """
        Write the order book and the lineage, whatever the cycle did.

        Last, and unconditional. A cycle that decided nothing still has
        orders whose state moved during the poll, and a chain that
        gained a fill has to be written or it stays incomplete forever.
        """
        evaluation = context.get("evaluation")
        outcome_by_order = context.get("outcome_by_order") or {}

        # ---- 17. persist orders and lineage (§10, §18, §32) ---------
        with self._stage(result, LoopStage.PERSIST) as entry:
            self.stack.service.persist_all()
            # Every order this session knows about, not only the ones
            # submitted in THIS cycle. A fill arriving two cycles after
            # its order still has to reach its chain.
            chains = self._lineage(
                result, evaluation,
                [o for o in self.stack.orchestrator.orders.values()
                 if o.broker_id == self.stack.broker_id],
                now, outcome_by_order,
                models=self._models_by_signal(
                    [o.signal_id for o
                     in self.stack.orchestrator.orders.values()
                     if o.signal_id]))
            self.repository.save_lineage(chains)
            entry.count = len(chains)
            entry.detail = f"{len(chains)} lineage chain(s)"
            self.repository.audit(
                self.config.actor, "cycle_completed", now,
                session_id=self.config.session_id, cycle_id=result.cycle_id,
                detail=(f"{result.orders_submitted} order(s), "
                        f"{result.fills_recorded} fill(s)"))
            self._update_validation(result, now)

    # ==================================================================
    # helpers -- each does one lookup and nothing else
    # ==================================================================

    def _update_validation(self, result: CycleResult, now: datetime) -> None:
        """
        Fold this session's cycles into its paper validation record (§21).

        Only when a strategy is named. A run with no strategy is an
        operational exercise of the loop, and giving it a validation
        record would create evidence about a strategy nobody declared.

        Counts are RECOMPUTED from the stored cycles rather than
        incremented, so a re-run produces the same totals instead of
        doubling them -- the Phase 22 rule about keying results on their
        inputs.
        """
        if not self.config.strategy_id:
            return
        from src.trading.validation import PaperValidator

        validator = PaperValidator(self.conn, self.method_version)
        existing = validator.repository.get_validation(
            _validation_id(self.config, self.method_version))
        if existing is None:
            try:
                validation = validator.start(
                    strategy_id=self.config.strategy_id,
                    strategy_version=self.config.strategy_version or "v1",
                    session_id=self.config.session_id, at=now,
                    challenger_id=self.config.challenger_id)
            except Exception as error:                      # noqa: BLE001
                # A challenger that is not PAPER_CANDIDATE raises here.
                # Recorded rather than crashing the cycle: the trading
                # already happened and the refusal is about the RECORD,
                # not about the trade.
                self.repository.audit(
                    self.config.actor, "validation_refused", now,
                    session_id=self.config.session_id,
                    detail=f"{type(error).__name__}: {error}")
                return
        else:
            from src.domain.trading_loop_models import (
                PaperStrategyState, PaperValidation,
            )
            validation = PaperValidation(
                validation_id=existing["validation_id"],
                strategy_id=existing["strategy_id"],
                strategy_version=existing["strategy_version"],
                session_id=existing["session_id"],
                method_version=self.method_version,
                challenger_id=existing["challenger_id"],
                state=PaperStrategyState(existing["state"]),
                started_at=now)

        # The stored cycles PLUS the one still in flight. `save_cycle`
        # runs after `_advance` returns, so reading only the database
        # here would leave the validation permanently one cycle behind
        # -- it reported zero fills on the cycle that recorded the fill.
        cycles = [c for c in self.repository.recent_cycles(
            self.config.session_id, limit=1000)
            if c["cycle_id"] != result.cycle_id]
        cycles.append({
            "cycle_id": result.cycle_id, "status": result.status.value,
            "signals_seen": result.signals_seen,
            "signals_eligible": result.signals_eligible,
            "targets_set": result.targets_set,
            "orders_submitted": result.orders_submitted,
            "orders_rejected": result.orders_rejected,
            "fills_recorded": result.fills_recorded,
        })
        outcomes = [{"is_open": bool(r[0]), "net_pnl": r[1],
                     "gross_pnl": r[2], "quantity": r[3], "entry_price": r[4],
                     "slippage_bps": r[5]}
                    for r in self.conn.execute("""
                        SELECT is_open, net_pnl, gross_pnl, quantity,
                               entry_price, slippage_bps
                          FROM trade_outcomes WHERE session_id = ?
                    """, (self.config.session_id,))]
        failures = sum(1 for c in cycles if c["status"] == "failed")
        validator.update(validation, cycles, outcomes=outcomes,
                         operational_failures=failures, at=now)

    def _our_working_orders(self) -> List[ExecutionOrder]:
        """
        Our own orders that can still fill.

        NOT `orders_in_flight()`: Phase 14 defines in-flight as
        SUBMITTING or SUBMITTED — the crash-recovery window where the
        outcome is unknown. An ACKNOWLEDGED order sitting at the venue
        is not in flight by that definition and is very much still
        going to fill, so counting only in-flight orders would let the
        loop re-propose a working trade.
        """
        return [order for order in self.stack.orchestrator.orders.values()
                if order.broker_id == self.stack.broker_id
                and not order.state.is_terminal]

    def _models_by_signal(self, signal_ids: Sequence[str]) -> Dict[str, str]:
        """
        The trained model behind each signal, read from the record.

        `signal_contributions` is where Phase 10 stores it, so this is
        the source rather than a copy carried through the cycle. That
        matters because outcomes are built in the observation half,
        before this cycle has looked at a single signal -- a map built
        during the decision half is empty exactly when the outcome
        builder needs it, which is how `trade_outcomes.model_id` stayed
        None after the first attempt at this fix.
        """
        wanted = sorted({s for s in signal_ids if s})
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        try:
            rows = self.conn.execute(
                "SELECT signal_id, trained_model_id, weight FROM "
                "signal_contributions WHERE signal_id IN (%s) "
                "AND is_abstention = 0 ORDER BY weight DESC" % placeholders,
                wanted)
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return {}
            raise
        out: Dict[str, str] = {}
        for signal_id, trained_model_id, _weight in rows:
            if trained_model_id:
                out.setdefault(str(signal_id), str(trained_model_id))
        return out

    def _provenance_for(self, signals: Sequence[Signal]) -> Dict[str, Any]:
        """
        Model, prediction and strategy per instrument, from the signals.

        Read off Phase 10's own `ModelContribution` and
        `SignalProvenance` rather than from configuration, so the value
        recorded on an order is the one that actually produced the
        signal. Configuration is the fallback, not the source.

        A signal with two instruments cannot happen; a signal with no
        model contribution yields no model entry, and the lineage row
        then honestly shows a missing model instead of a borrowed one.
        """
        from src.trading.eligibility import model_of, strategy_of

        strategies: Dict[str, str] = {}
        model_versions: Dict[str, str] = {}
        predictions: Dict[str, str] = {}
        trained_models: Dict[str, str] = {}

        for signal in signals:
            instrument = signal.instrument_id
            strategy = strategy_of(signal)
            if strategy:
                strategies[instrument] = strategy

            trained = model_of(signal)
            if trained:
                trained_models[instrument] = trained

            best = None
            for contribution in getattr(signal, "contributions", None) or []:
                if getattr(contribution, "is_abstention", False):
                    continue
                if best is None or (contribution.weight or 0.0) > (
                        best.weight or 0.0):
                    best = contribution
            if best is not None:
                if best.model_qualified_id:
                    model_versions[instrument] = best.model_qualified_id
                if best.prediction_id:
                    predictions[instrument] = best.prediction_id

        return {"strategies": strategies, "model_versions": model_versions,
                "predictions": predictions, "trained_models": trained_models}

    def _confidence_floor(self, service: PortfolioService) -> Optional[float]:
        """
        Phase 11's `min_signal_confidence`, asked rather than assumed.

        Returns None when no constraint set declares one, which is
        different from a floor of zero and is reported as such.
        """
        try:
            from src.domain.portfolio_models import ConstraintScope
            constraint_set = service.constraints.load_or_default(
                service.constraint_version)
        except Exception:                                   # noqa: BLE001
            return None
        constraint = constraint_set.first(ConstraintScope.MIN_SIGNAL_CONFIDENCE)
        return constraint.min_value if constraint else None

    def _newest_bar_age_days(self, anchor: datetime) -> Optional[float]:
        """
        Age of the newest cached bar at or before the anchor.

        `at or before`, because a bar dated after the anchor must not
        make the data look fresh to a replay -- the point-in-time rule
        the whole project is built on.
        """
        try:
            row = self.conn.execute("""
                SELECT MAX(timestamp) FROM price_candle_cache
                 WHERE interval = '1d' AND timestamp <= ?
            """, (anchor.isoformat(),)).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise
        if not row or not row[0]:
            return None
        try:
            newest = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return max(0.0, (anchor - newest).total_seconds() / 86400.0)

    def _signals_at(self, service: PortfolioService,
                    anchor: datetime) -> List[Signal]:
        """
        Every signal the loop should CONSIDER, not just the actionable ones.

        `PortfolioService.actionable_signals` pre-filters to ACTIVE and
        unexpired, which is right for its own use and wrong here: §14
        requires a recorded verdict for every signal the loop saw, and
        a signal filtered out before the gate produces no row at all.
        """
        from src.data_access.signal_repository import SignalRepository
        try:
            return list(SignalRepository(self.conn).signals_as_of(anchor))
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return []
            raise

    def _prices_for(self, service: PortfolioService,
                    instrument_ids: Sequence[str], anchor: datetime
                    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        prices: Dict[str, float] = {}
        ages: Dict[str, float] = {}
        points = service.prices.prices_as_of(sorted(set(instrument_ids)), anchor)
        for instrument_id, point in points.items():
            if point.price is not None and point.price > 0:
                prices[instrument_id] = float(point.price)
                ages[instrument_id] = point.age_days(anchor)
        return prices, ages

    def _model_governance(self) -> Tuple[Dict[str, bool], Dict[str, str], str]:
        """
        Phase 18's verdict per trained model, asked ONCE.

        Returns `(deployable, statuses, detail)`. `detail` is empty when
        the gate answered and carries the failure when it did not --
        which is the whole point of this rewrite. The two halves used to
        be separate methods that each called Phase 18's `candidates()`
        and each swallowed every exception with a bare `except`, so a
        crashed governance query produced exactly the same empty dict as
        "nothing has been promoted".

        The direction was never unsafe: an unreadable gate marks a
        promoted model as experimental rather than the reverse. But an
        invisible failure in the component that decides what may trade
        is not something to leave silent, and calling it twice meant
        the two answers could disagree if the database moved between
        them.
        """
        try:
            from src.modeling.selection import candidates
            from src.domain.model_models import ModelStatus
        except ImportError as error:
            return {}, {}, f"Phase 18 is not importable here: {error}"

        try:
            verdicts = candidates(self.conn)
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                # Nothing has ever been trained or promoted here. That
                # is an answer, not a failure.
                return {}, {}, ""
            return {}, {}, f"the model gate could not be read: {error}"
        except Exception as error:                          # noqa: BLE001
            return {}, {}, (f"the model gate raised "
                            f"{type(error).__name__}: {error}")

        deployable = {v.trained_model_id: (v.status == ModelStatus.ACTIVE)
                      for v in verdicts if v.trained_model_id}
        statuses = {v.trained_model_id: getattr(v.status, "value", str(v.status))
                    for v in verdicts if v.trained_model_id}
        return deployable, statuses, ""

    def _lineage(self, result: CycleResult, evaluation: Any,
                 orders: Sequence[ExecutionOrder], now: datetime,
                 outcome_by_order: Optional[Dict[str, str]] = None,
                 models: Optional[Dict[str, str]] = None
                 ) -> List[TradeLineage]:
        """
        One chain per order, carrying every link that exists yet.

        `outcome_id` is filled only when a trade outcome actually
        exists for the order -- which means the order filled. An order
        still working has no outcome, and writing a placeholder would
        make an incomplete chain read as complete when `is_complete` is
        exactly what the integrity check reads.
        """
        outcome_by_order = outcome_by_order or {}
        models = models or {}
        by_order = {}
        for fill in self.stack.orchestrator.fills:
            by_order.setdefault(fill.order_id, fill)
        chains: List[TradeLineage] = []
        for order in orders:
            fill = by_order.get(order.order_id)
            chains.append(TradeLineage(
                cycle_id=result.cycle_id,
                instrument_id=order.instrument_id,
                trained_model_id=models.get(order.signal_id or ""),
                signal_id=order.signal_id,
                decision_id=order.decision_id,
                intent_id=order.intent_id,
                order_id=order.order_id,
                fill_id=(fill.fill_id if fill else None),
                position_instrument_id=(order.instrument_id if fill else None),
                outcome_id=outcome_by_order.get(order.order_id),
                model_version=order.model_version,
                strategy_id=order.strategy_id,
                strategy_version=self.config.strategy_version,
                challenger_id=self.config.challenger_id,
                recorded_at=now))
        return chains
