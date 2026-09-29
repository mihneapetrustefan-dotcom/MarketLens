"""
scripts/adaptive_allocation.py
-------------------------------------------
What the adaptive allocator WOULD propose (Phase 25.9I-D). Shadow only.

    python scripts/adaptive_allocation.py --as-of 2026-08-27 --equity 100 --cash 100
    python scripts/adaptive_allocation.py --as-of ... --equity 250 --cash 250 --compare
    python scripts/adaptive_allocation.py --as-of ... --hold us_and_intl-nvda:20 ...
    python scripts/adaptive_allocation.py --as-of ... --persist --db <copy.db>

THE OUTPUT IS A PROPOSAL, AND THAT IS ALL IT CAN BE
-------------------------------------------------------
No order is created, no share quantity is computed, no broker is
contacted, no Portfolio target is written. The canonical path is
unchanged: Portfolio, then Risk as the hard gate, then OrderIntent, then
Execution. This command shows what an allocator would say if anyone
asked it, while `smart_capital_allocation_enabled` stays off -- which it
does, and which this command reports rather than requires.

Capital is supplied on the command line rather than read from a broker.
That is deliberate for a shadow phase: reading live balances would make
this diagnostic depend on a session, and a proposal about money should
not need an account connection to be inspected.

`--compare` prints the equal-weight and all-cash baselines beside the
plan. It compares STRUCTURE -- deployment, concentration, risk, cash --
never profitability, which would need returns this phase does not have.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import flags as adaptive_flags                    # noqa: E402
from src.adaptive.admission import AdmissionConfig                  # noqa: E402
from src.adaptive.allocator import (                                # noqa: E402
    AllocationConfig, SmartCapitalAllocator, all_cash_baseline,
    equal_weight_baseline,
)
from src.adaptive.correlation import AsOfCorrelationProvider        # noqa: E402
from src.adaptive.desirability import DesirabilityEngine            # noqa: E402
from src.adaptive.risk_budget import PositionRiskEstimator, RiskConfig  # noqa: E402
from src.adaptive.selector import SessionUniverseSelector           # noqa: E402
from src.adaptive.universe import (                                 # noqa: E402
    DEFAULT_ARTIFACT, MarketHistory, ParentUniverse,
)
from src.domain.adaptive_models import DecisionLineage              # noqa: E402
from src.domain.allocation_models import (                          # noqa: E402
    AllocationCandidate, ExistingExposure, PortfolioCapitalState,
)

DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")


def build_candidates(selection, history, as_of, *, state, desirability, risk):
    """Turn an I-B session selection into allocation candidates."""
    candidates = []
    for instrument_id in selection.universe.members:
        assessment = selection.assessment(instrument_id)
        candles = history.history(instrument_id)
        volatility = candles.realized_volatility()
        liquidity = candles.median_dollar_volume()
        equity = state.account_equity or 1.0
        sector_exposure = state.sector_exposure().get(
            assessment.sector_id or "unclassified", 0.0)
        estimate = risk.estimate(instrument_id, daily_volatility=volatility)
        assessed = desirability.assess(
            instrument_id,
            opportunity_score=assessment.overall,
            score_completeness=(assessment.score.completeness
                                if assessment.score else None),
            liquidity_notional=liquidity, volatility=volatility,
            existing_exposure_fraction=state.exposure_of(instrument_id) / equity,
            sector_exposure_fraction=sector_exposure / equity,
            risk_is_known=estimate.is_usable)
        candidates.append(AllocationCandidate(
            instrument_id=instrument_id, as_of=as_of, desirability=assessed,
            risk=estimate, sector_id=assessment.sector_id,
            session_snapshot_id=selection.snapshot_id,
            admission_decision_id=next(
                (d.decision_id for d in selection.admissions
                 if d.instrument_id == instrument_id), None),
            opportunity_score=assessment.overall,
            score_completeness=(assessment.score.completeness
                                if assessment.score else None),
            liquidity_notional=liquidity))
    return candidates


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--equity", type=float, required=True)
    parser.add_argument("--cash", type=float, default=None,
                        help="defaults to equity when omitted")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--parent", default=DEFAULT_ARTIFACT)
    parser.add_argument("--max-capacity", type=int, default=None)
    parser.add_argument("--pass-threshold", type=float, default=None)
    parser.add_argument("--hold", action="append", default=[],
                        metavar="INSTRUMENT:NOTIONAL[:SECTOR]",
                        help="an existing position, repeatable")
    parser.add_argument("--account-risk-budget", type=float, default=None)
    parser.add_argument("--per-position-risk-budget", type=float, default=None)
    parser.add_argument("--unknown-risk-cap", type=float, default=None)
    parser.add_argument("--max-correlation", type=float, default=None)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    raw = args.as_of if "T" in args.as_of else args.as_of + "T13:25:00+00:00"
    as_of = datetime.fromisoformat(raw)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    holds = []
    for entry in args.hold:
        parts = entry.split(":")
        holds.append(ExistingExposure(
            instrument_id=parts[0], notional=float(parts[1]),
            sector_id=parts[2] if len(parts) > 2 else None))

    state = PortfolioCapitalState(
        as_of=as_of, account_equity=args.equity,
        cash=args.cash if args.cash is not None else args.equity,
        existing_exposures=tuple(holds), source="supplied on the command line")

    parent = ParentUniverse.load(args.parent)
    admission = AdmissionConfig(**{k: v for k, v in (
        ("max_capacity", args.max_capacity),
        ("pass_threshold", args.pass_threshold)) if v is not None})
    risk_config = RiskConfig(
        account_risk_budget_usd=args.account_risk_budget,
        per_position_risk_budget_usd=args.per_position_risk_budget,
        unknown_risk_notional_cap_usd=args.unknown_risk_cap)
    allocation_config = AllocationConfig(
        **({"max_pairwise_correlation": args.max_correlation}
           if args.max_correlation is not None else {}))

    with MarketHistory(args.db, as_of) as history:
        selection = SessionUniverseSelector(parent, admission=admission).select(
            history, as_of)
        candidates = build_candidates(
            selection, history, as_of, state=state,
            desirability=DesirabilityEngine(),
            risk=PositionRiskEstimator(risk_config))
        correlations = (AsOfCorrelationProvider(history)
                        if args.max_correlation is not None else None)
        allocator = SmartCapitalAllocator(allocation_config, risk_config)
        plan = allocator.allocate(
            candidates, state, as_of, correlations=correlations,
            lineage=DecisionLineage(
                lineage_id=selection.snapshot_id,
                session_date=selection.session_date,
                universe_decision_id=selection.snapshot_id,
                allocation_proposal_id=None, created_at=as_of))

    report = {
        "flag_smart_capital_allocation_enabled":
            adaptive_flags.resolve().smart_capital_allocation_enabled,
        "mode": "persisted" if args.persist else "dry-run",
        "session_snapshot_id": selection.snapshot_id,
        "candidates": len(candidates),
        "plan": plan.describe(),
        "identity_reconciles": plan.check_identity(),
        "allocation_config": allocation_config.describe(),
        "risk_config": risk_config.describe(),
        "lines": [
            {"instrument_id": line.instrument_id, "status": line.status.value,
             "sector": line.sector_id,
             "requested": line.requested_notional,
             "allocated": line.allocated_notional,
             "capital_at_risk": line.capital_at_risk,
             "risk_status": line.risk.status.value,
             "desirability": line.desirability.overall,
             "constraints": [
                 {"constraint": c.constraint, "removed": c.removed,
                  "detail": c.detail} for c in line.constraints_applied],
             "reasons": list(line.reasons)}
            for line in plan.lines],
    }
    if args.compare:
        deployable = plan.deployable_capital
        report["baselines"] = {
            "equal_weight": equal_weight_baseline(candidates, deployable),
            "all_cash": all_cash_baseline(candidates, deployable),
            "note": "structural comparison only: deployment, concentration and "
                    "risk. No profitability claim is made or possible here.",
        }
    if args.persist:
        from src.adaptive.allocation_repository import AdaptiveAllocationRepository
        conn = sqlite3.connect(args.db)
        try:
            repository = AdaptiveAllocationRepository(conn)
            report["persisted_new_plan"] = repository.save_plan(
                plan, session_snapshot_id=selection.snapshot_id)
        finally:
            conn.close()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    described = plan.describe()
    print("ADAPTIVE CAPITAL ALLOCATION (%s) — PROPOSAL ONLY" % report["mode"])
    print("  as_of                : %s" % as_of.isoformat())
    print("  smart allocator flag : %s (production control stays off)"
          % report["flag_smart_capital_allocation_enabled"])
    print("  session universe     : %s (%d selected)"
          % (selection.snapshot_id, selection.universe.occupied))
    print("  status               : %s" % described["status"])
    print("CAPITAL")
    print("  equity %.2f | cash %.2f | deployable %.2f"
          % (described["account_equity"], described["cash"],
             described["deployable_capital"]))
    print("  deployed %.2f | reserved %.2f | unallocated %.2f | above ceiling %.2f"
          % (described["deployed"], described["reserved"],
             described["unallocated"], plan.capital_above_ceiling))
    print("  retained cash %.2f | deployment %.1f%% of equity | identity ok %s"
          % (described["retained_cash"],
             100 * (described["deployment_fraction"] or 0.0),
             plan.check_identity()))
    risk = described["total_capital_at_risk"]
    print("  capital at risk      : %s"
          % ("%.2f" % risk if risk is not None else "UNKNOWN (a funded line has "
                                                    "no measurable downside)"))
    print("ALLOCATIONS")
    for line in plan.lines:
        if line.allocated_notional <= 0:
            continue
        print("  %-28s %-8s %7.2f (req %6.2f) risk %-8s des %.3f  %s"
              % (line.instrument_id, line.status.value, line.allocated_notional,
                 line.requested_notional,
                 "%.2f" % line.capital_at_risk if line.capital_at_risk is not None
                 else "UNKNOWN", line.desirability.overall,
                 line.sector_id or ""))
        for constraint in line.constraints_applied:
            print("      cut %6.2f by %s — %s"
                  % (constraint.removed, constraint.constraint, constraint.detail))
    zero = [line for line in plan.lines if line.allocated_notional <= 0]
    if zero:
        print("NOT FUNDED (%d)" % len(zero))
        for line in zero[:8]:
            reason = (line.reasons[0] if line.reasons else
                      (line.rejection_reasons[0].value if line.rejection_reasons
                       else "no reason recorded"))
            print("  %-28s %s" % (line.instrument_id, reason))
        if len(zero) > 8:
            print("  ... and %d more" % (len(zero) - 8))
    if plan.gaps:
        print("GAPS")
        for gap in plan.gaps:
            print("  - %s" % gap)
    for note in plan.notes:
        print("NOTE %s" % note)
    if args.compare:
        baseline = report["baselines"]["equal_weight"]
        print("BASELINES (structure only, no profitability claim)")
        print("  equal weight would deploy %.2f across %d candidates (%.2f each)"
              % (sum(baseline.values()), len(baseline),
                 list(baseline.values())[0] if baseline else 0.0))
        print("  all cash would deploy 0.00 — also a valid policy")
        print("  smart allocator deployed %.2f across %d of %d candidates"
              % (plan.deployed, len(plan.funded_lines), len(plan.lines)))
    if args.persist:
        print("PERSISTED plan %s (new=%s)"
              % (plan.plan_id, report.get("persisted_new_plan")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
