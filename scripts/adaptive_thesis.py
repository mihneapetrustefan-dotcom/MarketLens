"""
scripts/adaptive_thesis.py
---------------------------------------
What the thesis engine WOULD recommend (Phase 25.9I-E). Shadow only.

    python scripts/adaptive_thesis.py --scenario positive
    python scripts/adaptive_thesis.py --scenario all --json
    python scripts/adaptive_thesis.py --scenario all --persist --db <copy.db>

EVERY RECOMMENDATION IS NON-EXECUTABLE
------------------------------------------
No order is created, cancelled, modified or sent. No broker is
contacted. No market-data subscription is opened -- a scenario may
REQUEST a HOT state, which is a statement about what would be needed
before acting, not an activation. 25.9I-C owns subscriptions and is
externally blocked.

The scenarios are deterministic fixtures with explicit numbers. The 40%
partial exit, the 117-122 target and the 5-day extension in the positive
scenario are FIXTURE INPUTS, not engine policy: the engine has no
opinion about any of them and will say so if asked to invent one.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import flags as adaptive_flags                  # noqa: E402
from src.adaptive.risk_budget import RiskConfig                   # noqa: E402
from src.adaptive.thesis import (                                 # noqa: E402
    PositionThesisEngine, ThesisPolicy,
)
from src.domain.thesis_models import (                            # noqa: E402
    EvidenceDirection, ExpectedHorizon, InvalidationZone, PositionDirection,
    RevisionPolicy, TargetZone, ThesisEvidence,
)

UTC = timezone.utc
DAY0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
INSTRUMENT = "us_and_intl-demo"


def base_thesis(engine):
    """entry 100, target 108-112, invalidation 93-94, horizon 7 days."""
    return engine.open_thesis(
        INSTRUMENT, as_of=DAY0, direction=PositionDirection.LONG,
        target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(93.0, 94.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=DAY0),
        entry_price=100.0, entry_notional=50.0,
        summary="entry: liquidity and coverage qualified it for the session",
        risk_assumptions=("volatility stays near its trailing level",),
        session_snapshot_id="su-demo", admission_decision_id="adm-demo",
        allocation_plan_id="alloc-demo")


def scenario_positive(engine):
    """A: material positive evidence -> partial exit + extension (fixture)."""
    thesis = base_thesis(engine)
    evidence = ThesisEvidence(
        "ev-positive", EvidenceDirection.POSITIVE, DAY0 + timedelta(days=4),
        summary="material positive development, supplied by the fixture",
        materiality=0.8, confidence=0.7, source="fixture")
    return thesis, engine.reassess(
        thesis, DAY0 + timedelta(days=4), price=109.0, evidence=[evidence],
        proposed_target=TargetZone(117.0, 122.0),
        proposed_invalidation=InvalidationZone(101.0, 102.0),
        proposed_horizon=ExpectedHorizon(calendar_days=12, set_at=DAY0),
        exit_fraction=0.40)


def scenario_negative(engine):
    """B: the thesis is invalidated -> full exit, without waiting."""
    thesis = base_thesis(engine)
    evidence = ThesisEvidence(
        "ev-negative", EvidenceDirection.NEGATIVE, DAY0 + timedelta(days=4),
        summary="the reason for holding stopped being true",
        materiality=0.9, invalidates_thesis=True, source="fixture")
    return thesis, engine.reassess(
        thesis, DAY0 + timedelta(days=4), price=104.0, evidence=[evidence])


def scenario_price_only(engine):
    """C: price near the target, no evidence -> reassess, never extend."""
    thesis = base_thesis(engine)
    return thesis, engine.reassess(thesis, DAY0 + timedelta(days=2), price=109.0)


def scenario_time_decay(engine):
    """D: horizon ends with nothing new -> no automatic extension."""
    thesis = base_thesis(engine)
    return thesis, engine.reassess(thesis, DAY0 + timedelta(days=8), price=101.0)


def scenario_price_only_extension_refused(engine):
    """C2: a target extension asked for on price alone is refused."""
    thesis = base_thesis(engine)
    return thesis, engine.reassess(
        thesis, DAY0 + timedelta(days=2), price=109.0,
        evidence=[ThesisEvidence("ev-noise", EvidenceDirection.POSITIVE,
                                 DAY0 + timedelta(days=2),
                                 summary="price rose", source="fixture")],
        proposed_target=TargetZone(120.0, 125.0))


def scenario_cooldown(engine):
    """G: a second upside revision inside the cooldown is withheld."""
    policy = ThesisPolicy(revision_policy=RevisionPolicy(
        min_revision_interval=timedelta(hours=6)))
    restrained = PositionThesisEngine(policy)
    thesis = base_thesis(restrained)
    evidence = ThesisEvidence(
        "ev-second", EvidenceDirection.POSITIVE, DAY0 + timedelta(days=4),
        summary="another positive item, minutes after the last revision",
        materiality=0.7, source="fixture")
    return thesis, restrained.reassess(
        thesis, DAY0 + timedelta(days=4), evidence=[evidence],
        proposed_target=TargetZone(118.0, 123.0),
        since_last_revision=timedelta(minutes=10))


def scenario_safety_override(engine):
    """H: an invalidation inside the same cooldown is still allowed."""
    policy = ThesisPolicy(revision_policy=RevisionPolicy(
        min_revision_interval=timedelta(hours=6)))
    restrained = PositionThesisEngine(policy)
    thesis = base_thesis(restrained)
    evidence = ThesisEvidence(
        "ev-urgent", EvidenceDirection.NEGATIVE, DAY0 + timedelta(days=4),
        summary="thesis invalidated, minutes after the last revision",
        materiality=0.95, invalidates_thesis=True, source="fixture")
    return thesis, restrained.reassess(
        thesis, DAY0 + timedelta(days=4), evidence=[evidence],
        since_last_revision=timedelta(minutes=10))


SCENARIOS = {
    "positive": scenario_positive,
    "negative": scenario_negative,
    "price_only": scenario_price_only,
    "price_only_extension": scenario_price_only_extension_refused,
    "time_decay": scenario_time_decay,
    "cooldown": scenario_cooldown,
    "safety_override": scenario_safety_override,
}


def render(name, thesis, assessment) -> dict:
    revised = assessment.revised_thesis
    return {
        "scenario": name,
        "before": {
            "thesis_id": thesis.thesis_id, "status": thesis.status.value,
            "target": [thesis.target_zone.low, thesis.target_zone.high],
            "invalidation": [thesis.invalidation_zone.low,
                             thesis.invalidation_zone.high],
            "horizon_days": thesis.horizon.calendar_days,
            "remaining_fraction": thesis.remaining_fraction,
        },
        "assessment": assessment.describe(),
        "after": ({
            "thesis_id": revised.thesis_id, "status": revised.status.value,
            "target": [revised.target_zone.low, revised.target_zone.high],
            "invalidation": [revised.invalidation_zone.low,
                             revised.invalidation_zone.high],
            "horizon_days": revised.horizon.calendar_days,
            "remaining_fraction": revised.remaining_fraction,
            "realized_fraction": revised.realized_fraction,
        } if revised else None),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="all",
                        choices=sorted(SCENARIOS) + ["all"])
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    engine = PositionThesisEngine(ThesisPolicy(), RiskConfig())
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]

    results = []
    for name in names:
        thesis, assessment = SCENARIOS[name](engine)
        results.append((name, thesis, assessment))

    report = {
        "flags": {
            "position_thesis_enabled":
                adaptive_flags.resolve().position_thesis_enabled,
            "dynamic_exit_enabled":
                adaptive_flags.resolve().dynamic_exit_enabled,
        },
        "mode": "persisted" if args.persist else "dry-run",
        "scenarios": [render(name, thesis, assessment)
                      for name, thesis, assessment in results],
    }

    if args.persist:
        if not args.db:
            print("--persist needs --db (use a scratch copy, not production)")
            return 2
        from src.adaptive.thesis_repository import AdaptiveThesisRepository
        conn = sqlite3.connect(args.db)
        try:
            repository = AdaptiveThesisRepository(conn)
            written = 0
            for _, thesis, assessment in results:
                repository.save_thesis(thesis)
                written += 1 if repository.save_assessment(assessment) else 0
            report["persisted_new_recommendations"] = written
        finally:
            conn.close()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    print("POSITION THESIS ENGINE (%s) — RECOMMENDATIONS ONLY" % report["mode"])
    print("  position_thesis_enabled : %s | dynamic_exit_enabled : %s"
          % (report["flags"]["position_thesis_enabled"],
             report["flags"]["dynamic_exit_enabled"]))
    for name, thesis, assessment in results:
        risk = engine.risk_estimate(thesis)
        print("\n%s" % name.upper())
        print("  before : target %.0f-%.0f | invalidation %.0f-%.0f | horizon %sd "
              "| remaining %.2f"
              % (thesis.target_zone.low, thesis.target_zone.high,
                 thesis.invalidation_zone.low, thesis.invalidation_zone.high,
                 thesis.horizon.calendar_days, thesis.remaining_fraction))
        print("  action : %s%s" % (
            assessment.action.value,
            " %.0f%%" % (100 * assessment.recommendation.exit_fraction)
            if assessment.recommendation.exit_fraction else ""))
        print("  data   : requires %s%s | executable %s"
              % (assessment.required_state.value,
                 " (%s)" % assessment.activation_reason.value
                 if assessment.activation_reason else "",
                 assessment.is_executable))
        print("  risk   : %s %s (from the thesis invalidation)"
              % (risk.status.value, risk.risk_per_dollar))
        after = assessment.revised_thesis
        if after:
            print("  after  : target %.0f-%.0f | invalidation %.0f-%.0f | horizon %sd "
                  "| remaining %.2f realized %.2f | status %s"
                  % (after.target_zone.low, after.target_zone.high,
                     after.invalidation_zone.low, after.invalidation_zone.high,
                     after.horizon.calendar_days, after.remaining_fraction,
                     after.realized_fraction, after.status.value))
        if assessment.capital_release:
            print("  frees  : %.0f%% of the position%s (not reallocated here)"
                  % (100 * assessment.capital_release.released_fraction,
                     " = %.2f" % assessment.capital_release.released_notional
                     if assessment.capital_release.released_notional else ""))
        for reason in assessment.reasons[:2]:
            print("  why    : %s" % reason[:100])
        for blocked in assessment.blocked_by_policy:
            print("  held   : %s" % blocked)
    if args.persist:
        print("\nPERSISTED %d new recommendation(s)"
              % report.get("persisted_new_recommendations", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
