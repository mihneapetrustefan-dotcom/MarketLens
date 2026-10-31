"""
scripts/signal_horizon_status.py
----------------------------------------------
Why every instrument has the answer it has (Phase 25.9I-F.5). Read-only.

    python scripts/signal_horizon_status.py
    python scripts/signal_horizon_status.py --horizons
    python scripts/signal_horizon_status.py --models
    python scripts/signal_horizon_status.py --instrument us_and_intl-aapl
    python scripts/signal_horizon_status.py --json
    python scripts/signal_horizon_status.py --scenario all

WHAT THIS REPLACES
----------------------
A dashboard cell reading `NO SIGNAL`, which today means any of: no
model, a model that failed its gates, no data, stale data, a sealed
horizon, or a genuine decision that there is no edge. Those are
different states with different repairs, and only the last one is a
finding about the market.

    NO SIGNAL IS A DECISION ONLY WHEN THE SYSTEM HAD ENOUGH
    INFORMATION TO MAKE THE DECISION.

NOTHING HERE CHANGES ANYTHING
---------------------------------
Opens the research store READ-ONLY (`mode=ro`). Trains nothing,
evaluates nothing, promotes nothing, weakens no threshold, creates no
order and contacts no broker. It reports what is already true.

The protected D20 horizon is reported as SEALED with its readiness
metadata only. No label value is read for any horizon.
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

from src.adaptive.horizon_intelligence import (                    # noqa: E402
    HorizonIntelligence, HorizonPolicy, horizon_answer_for_request,
)
from src.adaptive.horizon_inventory import build_horizon_inventory  # noqa: E402
from src.adaptive.model_inventory import build_model_inventory      # noqa: E402
from src.adaptive.signal_availability import (                      # noqa: E402
    AvailabilityPolicy, build_coverage, build_engine, collect_inputs,
)
from src.adaptive.target_estimation import (                        # noqa: E402
    ExpectedReturnEstimate, TargetEstimator, target_answer_for_request,
)
from src.domain.signal_availability_models import (                 # noqa: E402
    HorizonStatus, InstrumentHorizonStatus, SignalAvailabilityClass,
)
from src.domain.signal_models import SignalDirection                # noqa: E402

RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")
UTC = timezone.utc


def _open(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"research store not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# ======================================================================
# Scenarios -- fixtures for states the real corpus cannot produce
# ======================================================================


def scenarios(as_of: datetime) -> dict:
    """
    The required states, several of which the real corpus cannot show.

    Marked as fixtures because they ARE fixtures: with zero qualified
    models, this repository can produce no valid no-signal, no
    multi-horizon agreement and no defensible target. Presenting a
    fixture as a real observation would be the exact dishonesty the
    phase exists to remove.
    """
    intelligence = HorizonIntelligence(HorizonPolicy())
    estimator = TargetEstimator()
    out = {}

    # A -- valid no signal: everything present, edge too small.
    usable = [InstrumentHorizonStatus(
        "fixture-a", "d5", HorizonStatus.AVAILABLE,
        direction=SignalDirection.LONG, confidence=0.31,
        model_qualified_id="fixture_model:v1", model_status="active",
        observations=180, explanation="qualified model, usable prediction")]
    out["A_valid_no_signal"] = {
        "horizon": intelligence.assess("fixture-a", usable,
                                       as_of=as_of).describe(),
        "note": ("a qualified model produced a prediction and confidence 0.31 "
                 "did not clear the threshold -- this IS a decision"),
    }

    # D -- two usable horizons agreeing.
    agree = [
        InstrumentHorizonStatus("fixture-d", "d1", HorizonStatus.AVAILABLE,
                                direction=SignalDirection.LONG, confidence=0.55,
                                model_qualified_id="fixture_model:v1",
                                explanation="fixture"),
        InstrumentHorizonStatus("fixture-d", "d5", HorizonStatus.AVAILABLE,
                                direction=SignalDirection.LONG, confidence=0.68,
                                model_qualified_id="fixture_model:v1",
                                explanation="fixture"),
    ]
    out["D_multi_horizon_agreement"] = intelligence.assess(
        "fixture-d", agree, as_of=as_of).describe()

    # E -- disagreement.
    disagree = [
        InstrumentHorizonStatus("fixture-e", "d1", HorizonStatus.AVAILABLE,
                                direction=SignalDirection.SHORT,
                                confidence=0.60, explanation="fixture"),
        InstrumentHorizonStatus("fixture-e", "d5", HorizonStatus.AVAILABLE,
                                direction=SignalDirection.LONG,
                                confidence=0.71, explanation="fixture"),
    ]
    out["E_horizon_disagreement"] = intelligence.assess(
        "fixture-e", disagree, as_of=as_of).describe()

    # F -- nothing usable.
    none_usable = [
        InstrumentHorizonStatus("fixture-f", "d5",
                                HorizonStatus.NO_QUALIFIED_MODEL,
                                explanation="fixture"),
        InstrumentHorizonStatus("fixture-f", "d20", HorizonStatus.PROTECTED,
                                explanation="sealed"),
    ]
    out["F_no_preferred_horizon"] = intelligence.assess(
        "fixture-f", none_usable, as_of=as_of).describe()

    # G -- target requested, no model. THE REAL CASE TODAY.
    out["G_target_without_model"] = target_answer_for_request(
        estimator.estimate("fixture-g", as_of=as_of, reference_price=100.0,
                           horizon_id="d5"))

    # H -- target with a supplied, validated estimate.
    supplied = ExpectedReturnEstimate(
        instrument_id="fixture-h", horizon_id="d5", expected_return=0.023,
        model_qualified_id="fixture_model:v1", as_of=as_of,
        model_is_qualified=True, is_calibrated=True, standard_error=0.011,
        confidence=0.64, uncertainty_basis="bootstrap standard error")
    out["H_target_with_valid_estimate"] = target_answer_for_request(
        estimator.estimate("fixture-h", as_of=as_of, reference_price=100.0,
                           expected_return=supplied))

    # I -- horizon requested, no evidence. THE REAL CASE TODAY.
    out["I_horizon_without_evidence"] = horizon_answer_for_request(
        intelligence.assess("fixture-i", none_usable, as_of=as_of))

    # J -- protected horizon.
    out["J_protected_horizon"] = {
        "target": target_answer_for_request(
            estimator.estimate("fixture-j", as_of=as_of, horizon_id="d20",
                               reference_price=100.0)),
        "horizon": intelligence.assess(
            "fixture-j",
            [InstrumentHorizonStatus("fixture-j", "d20",
                                     HorizonStatus.PROTECTED,
                                     explanation="sealed")],
            as_of=as_of).describe(),
    }
    return out


# ======================================================================
# CLI
# ======================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Signal and horizon availability diagnostics. Read-only; "
                    "changes no threshold and promotes no model.")
    parser.add_argument("--db", default=RESEARCH_DB)
    parser.add_argument("--instrument", default=None,
                        help="show one instrument in full")
    parser.add_argument("--limit", type=int, default=12,
                        help="how many per-instrument rows to print")
    parser.add_argument("--horizons", action="store_true",
                        help="print the horizon inventory only")
    parser.add_argument("--models", action="store_true",
                        help="print the model inventory only")
    parser.add_argument("--scenario", choices=("all",), default=None)
    parser.add_argument("--stale-after-days", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(UTC)

    if args.scenario:
        result = scenarios(now)
        print(json.dumps(result, indent=2, default=str) if args.json
              else _render_scenarios(result))
        return 0

    conn = _open(args.db)
    try:
        models = build_model_inventory(conn, as_of=now)
        horizons = build_horizon_inventory(
            conn, as_of=now, qualified_model_ids=models.qualified_ids)

        if args.horizons:
            payload = horizons.describe()
            print(json.dumps(payload, indent=2) if args.json
                  else _render_horizons(payload))
            return 0
        if args.models:
            payload = models.describe()
            print(json.dumps(payload, indent=2) if args.json
                  else _render_models(payload))
            return 0

        engine = build_engine(conn, as_of=now, policy=AvailabilityPolicy(
            stale_after_days=args.stale_after_days))
        instrument_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT instrument_id FROM research_observations")]
        if args.instrument:
            instrument_ids = [args.instrument]
        inputs = collect_inputs(conn, instrument_ids, as_of=now)
    finally:
        conn.close()

    verdicts = [engine.assess(inputs[i], as_of=now) for i in sorted(inputs)]
    coverage = build_coverage(verdicts, as_of=now)

    report = {
        "as_of": now.isoformat(),
        "read_only": True,
        "thresholds_changed": 0,
        "models_promoted": 0,
        "broker_writes": 0,
        "horizon_inventory": horizons.describe(),
        "model_inventory": models.describe(),
        "coverage": coverage.describe(),
        "instruments": [v.describe() for v in verdicts[:args.limit]],
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(_render_horizons(horizons.describe()))
    print()
    print(_render_models(models.describe()))
    print()
    print(_render_coverage(coverage.describe()))
    print()
    print("PER INSTRUMENT (first %d of %d)" % (min(args.limit, len(verdicts)),
                                               len(verdicts)))
    for verdict in verdicts[:args.limit]:
        described = verdict.describe()
        print("  %-26s %-16s %-22s pref=%-6s conf=%-6s %s"
              % (described["instrument_id"][:26], described["availability"],
                 described["reason"],
                 described["preferred_horizon"] or "none",
                 described["confidence"] if described["confidence"] is not None
                 else "n/a",
                 "DECISION" if described["is_decision"] else "cannot decide"))
        print("      %s" % described["explanation"][:96])
    return 0


def _render_horizons(payload: dict) -> str:
    lines = ["HORIZON INVENTORY  (%d horizons, from repository evidence)"
             % payload["horizon_count"],
             "  %-14s %8s %7s %7s %9s  %s"
             % ("horizon", "labels", "models", "qual", "protected",
                "limitation")]
    for h in payload["horizons"]:
        lines.append("  %-14s %8d %7d %7d %9s  %s"
                     % (h["horizon_id"], h["label_count"], h["models"],
                        h["qualified_models"],
                        "SEALED" if h["is_protected"] else "",
                        (h["limitations"][0][:46] if h["limitations"] else "")))
    lines.append("  signal-capable horizons: %s"
                 % (", ".join(payload["signal_capable"]) or "NONE"))
    return "\n".join(lines)


def _render_models(payload: dict) -> str:
    lines = ["MODEL INVENTORY",
             "  trained rows %d | distinct specifications %d | evaluated %d | "
             "qualified %d | promoted/active %d"
             % (payload["trained_rows"], payload["distinct_specifications"],
                payload["evaluated"], payload["qualified"],
                payload["promoted_active"]),
             "  by horizon: %s" % payload["by_horizon"],
             "  gate failures: %s" % payload["gate_failures"]]
    for m in payload["models"]:
        lines.append("  %-28s %-8s %-10s verdict=%-9s gates=%s"
                     % (m["model_qualified_id"][:28], m["horizon_id"],
                        m["status"], m["verdict"],
                        ",".join(m["gate_failures"])))
        if m["metrics"]:
            lines.append("      metrics %s | effective sample %s | beats "
                         "baselines %s"
                         % (m["metrics"], m["effective_sample_size"],
                            m["beats_all_baselines"]))
    return "\n".join(lines)


def _render_coverage(payload: dict) -> str:
    lines = ["COVERAGE  (universe %d, reconciles %s)"
             % (payload["universe_size"], payload["reconciles"]),
             "  decisions the system actually made: %d of %d"
             % (payload["decisions"], payload["universe_size"]),
             "  by class : %s" % payload["by_class"],
             "  by reason: %s" % payload["by_reason"],
             "", "  FUNNEL"]
    for stage in payload["funnel"]:
        lines.append("   %6d  %-36s %s"
                     % (stage["remaining"], stage["name"],
                        ("-%d %s" % (stage["excluded"], stage["reason"]))
                        if stage["excluded"] else ""))
    return "\n".join(lines)


def _render_scenarios(result: dict) -> str:
    lines = ["SCENARIOS  (fixtures where the real corpus cannot produce the "
             "state; labelled as such)"]
    for name, payload in result.items():
        lines.append("\n%s" % name.upper())
        lines.append("  " + json.dumps(payload, default=str)[:400])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
