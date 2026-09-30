"""
scripts/adaptive_event_reassessment.py
----------------------------------------------------
What event intelligence WOULD tell a thesis (Phase 25.9I-F). Shadow only.

    python scripts/adaptive_event_reassessment.py --scenario all
    python scripts/adaptive_event_reassessment.py --scenario c --json
    python scripts/adaptive_event_reassessment.py --real --limit 200
    python scripts/adaptive_event_reassessment.py --scenario all --persist --db copy.db

NOTHING HERE EXECUTES ANYTHING
----------------------------------
No order is created, cancelled or modified. No broker is contacted. No
market-data subscription is opened and no billable request is made -- a
scenario may REQUEST a WARM or HOT state, which is a statement about
what would be needed before acting, not an activation. 25.9I-C owns
market-data attention and this account has never served a realtime
marker.

Every recommendation carries `is_executable = False`.

THE FIXTURES ARE FIXTURES
-----------------------------
The 117-122 target, the 12-day horizon and the 40% fraction in
scenario B are **inputs supplied by the scenario**, exactly as an
external estimator would supply them. The event engine has no field
that could carry any of the three. That is the point of scenario B: it
proves the separation by showing the same event producing no numbers in
scenario A and the caller's numbers in scenario B.

`--real` runs the assessment over this repository's actual 581 canonical
events, read-only and as-of bounded, and reports coverage. It selects
events by recency, never by what happened to the price afterwards.
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

from src.adaptive import flags as adaptive_flags                     # noqa: E402
from src.adaptive.event_assessment import (                          # noqa: E402
    AssessmentPolicy, EventImpactAssessor,
)
from src.adaptive.event_reassessment import (                        # noqa: E402
    ConsumptionRecord, EventDrivenReassessmentEngine, ReassessmentPolicy,
    suppression_reason,
)
from src.adaptive.event_relevance import (                           # noqa: E402
    EventRelevanceClassifier, RelevancePolicy,
)
from src.adaptive.event_sources import (                             # noqa: E402
    CanonicalEventReader, EventInstrumentResolver, InstrumentResolution,
)
from src.adaptive.event_thesis_repository import (                   # noqa: E402
    EventThesisRepository,
)
from src.adaptive.thesis import PositionThesisEngine, ThesisPolicy   # noqa: E402
from src.domain.event_position_models import (                       # noqa: E402
    EventEvidenceBundle,
)
from src.domain.impact_models import ImpactDirection, ImpactLevel    # noqa: E402
from src.domain.thesis_models import (                               # noqa: E402
    ExpectedHorizon, InvalidationZone, PositionDirection, TargetZone,
)

RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")

UTC = timezone.utc
ENTRY_AT = datetime(2026, 9, 26, 13, 30, tzinfo=UTC)
DAY4 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)

#: The only assumption mapping in this file. Supplied, not derived --
#: `PositionThesis` assumptions are free text and the event taxonomy has
#: no mapping to them, so a caller states the connection or relevance
#: stays UNKNOWN.
ASSUMPTION_MAP = {
    "margin expansion": frozenset({"guidance", "earnings"}),
    "deleveraging": frozenset({"debt_issuance", "capital_raise", "bankruptcy"}),
    "unrestricted export market": frozenset({
        "regulatory_restriction", "regulatory_action"}),
}


def _resolver() -> EventInstrumentResolver:
    return EventInstrumentResolver({
        "ent-aa": InstrumentResolution("ent-aa", ("inst-aa",), "semiconductors"),
        "ent-bb": InstrumentResolution("ent-bb", ("inst-bb",), "semiconductors"),
        "ent-cc": InstrumentResolution("ent-cc", ("inst-cc",), "semiconductors"),
        "ent-zz": InstrumentResolution("ent-zz", ("inst-zz",), "airlines"),
    })


def _engine(assumption_map=ASSUMPTION_MAP, stale_after_seconds=None):
    classifier = EventRelevanceClassifier(
        _resolver(), policy=RelevancePolicy(assumption_event_types=assumption_map))
    return EventDrivenReassessmentEngine(
        PositionThesisEngine(ThesisPolicy()), classifier,
        EventImpactAssessor(AssessmentPolicy(
            stale_after_seconds=stale_after_seconds)),
        ReassessmentPolicy())


def _thesis(engine, instrument_id="inst-aa", assumptions=("margin expansion",)):
    return engine.thesis_engine.open_thesis(
        instrument_id=instrument_id, direction=PositionDirection.LONG,
        as_of=ENTRY_AT, entry_price=100.0,
        target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(94.0, 96.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=ENTRY_AT),
        entry_notional=1000.0, event_assumptions=tuple(assumptions))


def _bundle(**kw) -> EventEvidenceBundle:
    base = dict(
        canonical_event_id="ce-fixture", event_type="guidance",
        event_category="corporate", as_of=DAY4, title="Guidance raised",
        lifecycle_state="confirmed", first_reported_at=DAY4,
        entity_ids=("ent-aa",), primary_entity_id="ent-aa",
        instrument_ids=("inst-aa",), sector_ids=("semiconductors",),
        total_report_count=1, revision_key="rev-1", quality_confidence=0.55,
        report_ids=("rep-1",))
    base.update(kw)
    return EventEvidenceBundle(**base)


# ======================================================================
# Scenarios
# ======================================================================


def scenario_a(engine):
    """Material positive, no target model anywhere."""
    thesis = _thesis(engine)
    outcome = engine.reassess(_bundle(), thesis, as_of=DAY4,
                              subtype="guidance_raised", price=106.0)
    return "A material positive (no target model)", [outcome], {}


def scenario_b(engine):
    """Same event, but an EXTERNAL estimator supplies the numbers."""
    thesis = _thesis(engine)
    outcome = engine.reassess(
        _bundle(), thesis, as_of=DAY4, subtype="guidance_raised", price=106.0,
        # Supplied by the caller. The event engine has no field for any
        # of these three and never produced them.
        proposed_target=TargetZone(117.0, 122.0),
        proposed_horizon=ExpectedHorizon(calendar_days=12, set_at=DAY4),
        exit_fraction=0.40)
    return ("B positive + EXTERNAL estimate 117-122 / 12d / 40%", [outcome],
            {"supplied_by_caller": ["target 117-122", "horizon 12d",
                                    "fraction 0.40"]})


def scenario_c(engine):
    """Direct negative event invalidating a core assumption."""
    thesis = _thesis(engine)
    outcome = engine.reassess(
        _bundle(canonical_event_id="ce-neg", revision_key="rev-neg",
                title="Guidance cut; margin outlook withdrawn"),
        thesis, as_of=DAY4, subtype="guidance_cut",
        invalidates_assumption=True, price=99.0)
    return "C negative invalidation", [outcome], {}


def scenario_d(engine):
    """Positive headline, low materiality, touches no stated assumption."""
    thesis = _thesis(engine)
    outcome = engine.reassess(
        _bundle(canonical_event_id="ce-minor", revision_key="rev-minor",
                event_type="product_launch", lifecycle_state="reported",
                title="Company refreshes mobile app icon"),
        thesis, as_of=DAY4, price=104.0)
    return "D minor positive news", [outcome], {}


def scenario_e(engine):
    """Five articles, one canonical event, one reaction."""
    thesis = _thesis(engine)
    bundle = _bundle(canonical_event_id="ce-dup", revision_key="rev-dup",
                     total_report_count=5,
                     report_ids=("rep-1", "rep-2", "rep-3", "rep-4", "rep-5"),
                     title="Acquisition agreed (5 reports, 1 event)")
    first = engine.reassess(bundle, thesis, as_of=DAY4,
                            subtype="guidance_raised", price=106.0)
    history = [ConsumptionRecord(
        canonical_event_id=bundle.canonical_event_id, thesis_id=thesis.thesis_id,
        revision_key=bundle.revision_key,
        assessment_id=first.assessment.assessment_id, assessed_at=DAY4,
        lifecycle_state=bundle.lifecycle_state,
        corroboration_state=bundle.corroboration_state.value,
        total_report_count=bundle.total_report_count)]
    # The same event, re-crawled the next day with two more copies.
    later = DAY4 + timedelta(days=1)
    again = engine.reassess(
        _bundle(canonical_event_id="ce-dup", revision_key="rev-dup-2",
                as_of=later, first_reported_at=DAY4, total_report_count=7,
                report_ids=tuple(f"rep-{n}" for n in range(1, 8)),
                title="Acquisition agreed (7 reports, still 1 event)"),
        thesis, as_of=later, subtype="guidance_raised", history=history,
        price=106.0)
    return ("E duplicate articles / re-crawl", [first, again],
            {"reports_in_one_event": 7,
             "independence_established": bundle.source_independence_known})


def scenario_f(engine):
    """Credible evidence pointing both ways."""
    thesis = _thesis(engine)
    outcome = engine.reassess(
        _bundle(canonical_event_id="ce-conflict", revision_key="rev-conf",
                title="Guidance raised, but a second source reports a cut"),
        thesis, as_of=DAY4, subtype="guidance_raised",
        opposing_directions=(ImpactDirection.NEGATIVE,), price=105.0)
    return "F conflicting evidence", [outcome], {}


def scenario_g(engine):
    """Scheduled earnings ahead of an open position."""
    thesis = _thesis(engine)
    advisory = engine.pre_event_advisory(
        thesis, as_of=DAY4, event_type="earnings",
        scheduled_for=DAY4 + timedelta(hours=20), exposure_notional=1000.0,
        sector_id="semiconductors", gap_risk_relevant=True)
    return "G scheduled earnings advisory", [], {"advisory": advisory.describe()}


def scenario_h(engine):
    """One sector event, three theses, three independent assessments."""
    # Each thesis states the assumption the restriction bears on, so the
    # sector event is genuinely relevant to all three -- and each is
    # still assessed on its own.
    theses = [_thesis(engine, instrument_id=i,
                      assumptions=("margin expansion",
                                   "unrestricted export market"))
              for i in ("inst-aa", "inst-bb", "inst-cc")]
    bundle = _bundle(canonical_event_id="ce-sector", revision_key="rev-sec",
                     event_type="regulatory_restriction",
                     entity_ids=(), primary_entity_id=None, instrument_ids=(),
                     sector_ids=("semiconductors",),
                     title="New export restrictions on the sector")
    report, outcomes = engine.reassess_many(
        bundle, theses, as_of=DAY4,
        sector_ids={i: "semiconductors" for i in
                    ("inst-aa", "inst-bb", "inst-cc")},
        exposures={"inst-aa": 1000.0, "inst-bb": 800.0, "inst-cc": 1200.0},
        supplied_direction=ImpactDirection.NEGATIVE,
        supplied_materiality=ImpactLevel.MEDIUM)
    return ("H sector event, 3 theses", list(outcomes),
            {"multi_position_report": report.describe()})


def scenario_i(engine):
    """Rumour, then a confirmed filing: a material revision."""
    thesis = _thesis(engine)
    rumour = _bundle(canonical_event_id="ce-rev", revision_key="rev-rumour",
                     lifecycle_state="rumored",
                     title="Report: guidance may be raised")
    first = engine.reassess(rumour, thesis, as_of=DAY4, price=105.0)
    history = [ConsumptionRecord(
        canonical_event_id=rumour.canonical_event_id,
        thesis_id=thesis.thesis_id, revision_key=rumour.revision_key,
        assessment_id=first.assessment.assessment_id, assessed_at=DAY4,
        lifecycle_state="rumored",
        corroboration_state=rumour.corroboration_state.value,
        total_report_count=1)]
    later = DAY4 + timedelta(hours=6)
    confirmed = engine.reassess(
        _bundle(canonical_event_id="ce-rev", revision_key="rev-filed",
                as_of=later, first_reported_at=DAY4, lifecycle_state="confirmed",
                total_report_count=2, report_ids=("rep-1", "rep-2"),
                title="Company filing confirms raised guidance"),
        thesis, as_of=later, subtype="guidance_raised", history=history,
        price=107.0)
    return "I event revision (rumour -> filing)", [first, confirmed], {}


def scenario_j(engine):
    """An old event reprocessed today."""
    stale_engine = _engine(stale_after_seconds=48 * 3600)
    thesis = _thesis(stale_engine)
    old = DAY4 - timedelta(days=9)
    outcome = stale_engine.reassess(
        _bundle(canonical_event_id="ce-old", revision_key="rev-old",
                first_reported_at=old, title="Guidance raised (nine days ago)"),
        thesis, as_of=DAY4, subtype="guidance_raised", price=106.0)
    return "J stale event reprocessed", [outcome], {}


def scenario_k(engine):
    """A positive revision 10 minutes ago, then an invalidating event."""
    thesis = _thesis(engine)
    outcome = engine.reassess(
        _bundle(canonical_event_id="ce-cool", revision_key="rev-cool",
                title="Guidance cut; margin thesis contradicted"),
        thesis, as_of=DAY4, subtype="guidance_cut",
        invalidates_assumption=True, price=97.0,
        # A revision happened 10 minutes ago and one has already
        # occurred today: the cooldown is fully engaged.
        since_last_revision=timedelta(minutes=10), revisions_today=1)
    return "K cooldown vs safety override", [outcome], {}


SCENARIOS = {
    "a": scenario_a, "b": scenario_b, "c": scenario_c, "d": scenario_d,
    "e": scenario_e, "f": scenario_f, "g": scenario_g, "h": scenario_h,
    "i": scenario_i, "j": scenario_j, "k": scenario_k,
}


# ======================================================================
# Real-data shadow pass
# ======================================================================


def real_pass(db_path: str, limit: int, as_of: datetime) -> dict:
    """
    Run the assessor over real canonical events. Read-only, as-of bounded.

    Events are selected by RECENCY, never by what the price did
    afterwards. No market data is requested and no price is read.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        reader = CanonicalEventReader(conn)
        coverage = reader.coverage(as_of)
        bundles = reader.events_as_of(as_of, limit=limit)
    finally:
        conn.close()

    engine = _engine()
    counts = {"assessed": 0, "skipped": 0}
    tallies = {"relation": {}, "direction": {}, "materiality": {},
               "urgency": {}, "thesis_relevance": {}, "recommendation": {},
               "market_data_request": {}}
    executable = 0

    for bundle in bundles:
        for instrument_id in bundle.instrument_ids[:1]:
            resolver = EventInstrumentResolver({
                entity: InstrumentResolution(entity, (instrument_id,))
                for entity in bundle.entity_ids})
            classifier = EventRelevanceClassifier(
                resolver, policy=RelevancePolicy(
                    assumption_event_types=ASSUMPTION_MAP))
            local = EventDrivenReassessmentEngine(
                engine.thesis_engine, classifier, engine.assessor)
            thesis = local.thesis_engine.open_thesis(
                instrument_id=instrument_id, direction=PositionDirection.LONG,
                as_of=as_of - timedelta(days=3), entry_price=100.0,
                target_zone=TargetZone(108.0, 112.0),
                invalidation_zone=InvalidationZone(94.0, 96.0),
                horizon=ExpectedHorizon(calendar_days=7,
                                        set_at=as_of - timedelta(days=3)),
                event_assumptions=("margin expansion",))
            outcome = local.reassess(bundle, thesis, as_of=as_of,
                                     subtype=bundle.subtype)
            assessment = outcome.assessment
            if outcome.evidence is None:
                counts["skipped"] += 1
            else:
                counts["assessed"] += 1
            if outcome.is_executable:
                executable += 1
            for key, value in (
                    ("relation", assessment.relevance.relation.value),
                    ("direction", assessment.direction.value),
                    ("materiality", assessment.materiality.value),
                    ("urgency", assessment.urgency.value),
                    ("thesis_relevance",
                     assessment.relevance.thesis_relevance.value),
                    ("recommendation",
                     outcome.action.value if outcome.action else "none"),
                    ("market_data_request",
                     outcome.data_request.desired_state.value
                     if outcome.data_request else "none")):
                tallies[key][value] = tallies[key].get(value, 0) + 1

    return {
        "as_of": as_of.isoformat(),
        "events_evaluated": len(bundles),
        "coverage": coverage,
        "counts": counts,
        "tallies": tallies,
        "executable_outcomes": executable,
        "billable_market_data_requests": 0,
        "note": ("events selected by recency, never by later returns; no "
                 "profitability claim is made or implied"),
    }


# ======================================================================
# CLI
# ======================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shadow event-driven position reassessment. Read-only; "
                    "creates no order and makes no billable request.")
    parser.add_argument("--scenario",
                        choices=sorted(SCENARIOS) + ["all"], default="all")
    parser.add_argument("--real", action="store_true",
                        help="run over this repository's real canonical events")
    parser.add_argument("--db", default=RESEARCH_DB)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--persist", action="store_true",
                        help="write assessments to --store (never the "
                             "research database)")
    parser.add_argument("--store", default=":memory:")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    flags = adaptive_flags.resolve()
    suppressed = suppression_reason(flags)

    if args.real:
        report = real_pass(args.db, args.limit, datetime.now(timezone.utc))
        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return 0
        print("REAL-DATA SHADOW PASS  (read-only, 0 billable requests)")
        print("  events evaluated : %d" % report["events_evaluated"])
        coverage = report["coverage"]
        print("  corpus visible   : %d events | event_time on %d | "
              "independence established on %d"
              % (coverage["events_visible"], coverage["with_event_time"],
                 coverage["with_established_independence"]))
        print("  evidence offered : %d | skipped: %d"
              % (report["counts"]["assessed"], report["counts"]["skipped"]))
        for name in ("relation", "thesis_relevance", "direction", "materiality",
                     "urgency", "recommendation", "market_data_request"):
            print("  %-18s %s" % (name, report["tallies"][name]))
        print("  executable outcomes: %d" % report["executable_outcomes"])
        print("\n  %s" % report["note"])
        return 0

    engine = _engine()
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    repository = None
    connection = None
    if args.persist:
        connection = sqlite3.connect(args.store)
        repository = EventThesisRepository(connection)

    for name in names:
        title, outcomes, extra = SCENARIOS[name](engine)
        payload = {"scenario": name, "title": title,
                   "outcomes": [o.describe() for o in outcomes]}
        payload.update(extra)
        results.append(payload)
        if repository is not None:
            for outcome in outcomes:
                repository.save_outcome(outcome)
                repository.record_consumption(
                    outcome.assessment,
                    lifecycle_state="", total_report_count=0)

    report = {
        "as_of": DAY4.isoformat(),
        "adaptive_layer": flags.describe(),
        "suppressed_in_production_because": suppressed,
        "scenarios": results,
        "broker_writes": 0, "orders_created": 0,
        "billable_market_data_requests": 0,
        "subscriptions_purchased": 0,
    }
    if repository is not None:
        report["persistence"] = repository.counts()
        report["rows_claiming_executable"] = repository.executable_rows()
        connection.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print("EVENT-DRIVEN POSITION REASSESSMENT  (shadow; nothing executes)")
    print("  flags: %s" % (suppressed or "would run"))
    for payload in results:
        print("\n%s" % payload["title"].upper())
        for extra_key in ("supplied_by_caller", "reports_in_one_event"):
            if extra_key in payload:
                print("  %-22s %s" % (extra_key, payload[extra_key]))
        for outcome in payload["outcomes"]:
            assessment = outcome["assessment"]
            print("  %-10s rel=%-11s relevance=%-12s dir=%-8s mat=%-7s "
                  "urgency=%-8s conflict=%s"
                  % (assessment["consumption_status"], assessment["relation"],
                     assessment["thesis_relevance"], assessment["direction"],
                     assessment["materiality"], assessment["urgency"],
                     assessment["conflict_state"]))
            print("             recommendation=%-13s target_changed=%-5s "
                  "horizon_changed=%-5s fraction=%s"
                  % (outcome["recommendation"], outcome["target_changed"],
                     outcome["horizon_changed"],
                     outcome["partial_exit_fraction"]))
            print("             data=%-6s executable=%-5s  %s"
                  % (outcome["market_data_request"], outcome["is_executable"],
                     outcome["skipped_reason"][:70]))
            if assessment["invalidates_thesis"]:
                print("             INVALIDATES THESIS")
            if assessment["target_reestimation_required"]:
                print("             TARGET_REESTIMATION_REQUIRED "
                      "(no number produced here)")
        if "advisory" in payload:
            advisory = payload["advisory"]
            print("  advisory: %s in %.1fh | review=%s risk_review=%s data=%s "
                  "executable=%s"
                  % (advisory["event_type"], advisory["hours_until"] or 0.0,
                     advisory["recommend_thesis_review"],
                     advisory["recommend_risk_review"],
                     advisory["requested_market_data_state"],
                     advisory["is_executable"]))
        if "multi_position_report" in payload:
            multi = payload["multi_position_report"]
            print("  multi-position: %d theses assessed independently | "
                  "aggregate notional %s | executable %s"
                  % (len(multi["assessments"]), multi["aggregate_notional"],
                     multi["is_executable"]))
    if repository is not None:
        print("\nPERSISTED %s | rows claiming executable: %d"
              % (report["persistence"], report["rows_claiming_executable"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
