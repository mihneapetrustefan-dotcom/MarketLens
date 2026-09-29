"""
scripts/adaptive_market_data.py
---------------------------------------------
What the market-data governor knows, and what a test would cost
(Phase 25.9I-C). Read-only. Dry-run by default and by construction.

    python scripts/adaptive_market_data.py
    python scripts/adaptive_market_data.py --json
    python scripts/adaptive_market_data.py --exposure-minutes 30
    python scripts/adaptive_market_data.py --universe parent
    python scripts/adaptive_market_data.py --db path/to/copy.db

THIS SCRIPT MAKES NO BILLABLE REQUEST
-----------------------------------------
It opens the capture store READ-ONLY (`mode=ro`), reads contract
metadata and configuration, and does arithmetic. It contacts no broker,
opens no gateway session, sends no snapshot and buys no subscription.
The only script in this phase that may spend money is
`scripts/validate_market_data_billing.py`, which requires explicit
ceilings on every axis before it will run.
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

from src.adaptive import flags as adaptive_flags                   # noqa: E402
from src.adaptive.billing import (                                 # noqa: E402
    MarketDataBillingGovernor, ibkr_snapshot_policy,
)
from src.adaptive.entitlement import (                             # noqa: E402
    ChannelAvailability, EntitlementVerifier, entitlement_report,
)
from src.adaptive.exchange_map import ExchangeMap                  # noqa: E402
from src.adaptive.request_ledger import RequestLedger              # noqa: E402
from src.domain.market_data_billing_models import BillingPeriod    # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "capture", "intraday_capture.db")
CAPTURE_UNIVERSE = os.path.join(ROOT, "config", "capture_universe_v1.json")
PARENT_UNIVERSE = os.path.join(ROOT, "config", "parent_universe_v1.json")


def _members(path: str):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    members = payload.get("instruments") or payload.get("members") or []
    return [str(m.get("instrument_id")) for m in members if m.get("instrument_id")]


def _open_readonly(path: str) -> sqlite3.Connection:
    """Read-only by URI. A diagnostic must not be able to write."""
    if not os.path.exists(path):
        raise SystemExit(f"store not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def build_report(db_path: str, universe: str, exposure_minutes: float,
                 interval_seconds: float, budget_usd: float,
                 subscription_cost_usd, now: datetime) -> dict:
    conn = _open_readonly(db_path)
    try:
        exchange_map = ExchangeMap.from_connection(conn)
        period = BillingPeriod.for_moment(now)
        ledger = RequestLedger.replay(
            conn, MarketDataBillingGovernor(
                ibkr_snapshot_policy(), monthly_budget_usd=budget_usd),
            period)
    finally:
        conn.close()

    governor = ledger.governor
    capture_ids = _members(CAPTURE_UNIVERSE)
    parent_ids = _members(PARENT_UNIVERSE)
    selected = capture_ids if universe == "capture" else parent_ids

    distribution = exchange_map.distribution(selected)
    priceable = {name: count for name, count in distribution.items()
                 if governor.policy.policy_for(name) is not None}
    exposure = governor.project_exposure(
        priceable, minutes=exposure_minutes, interval_seconds=interval_seconds)

    verifier = EntitlementVerifier(exchange_map)
    entitlements = verifier.verify([])

    return {
        "as_of": now.isoformat(),
        "billing_period": period.key,
        "dry_run": True,
        "billable_requests_made_by_this_script": 0,
        "adaptive_layer": adaptive_flags.resolve().describe(),
        "cost_model": governor.describe(),
        "channel": ChannelAvailability().__dict__,
        "universes": {
            "capture": exchange_map.describe(capture_ids),
            "parent": exchange_map.describe(parent_ids),
        },
        "selected_universe": universe,
        "exchange_distribution": distribution,
        "unpriced_exchanges": sorted(set(distribution) - set(priceable)),
        "usage_this_period": ledger.describe(period),
        "forecast": governor.forecast_month(tuple(priceable)),
        "exposure": exposure.describe(),
        "plan_comparison": governor.compare_plans(
            tuple(priceable), subscription_cost_usd=subscription_cost_usd,
            account_verified=False).describe(),
        "entitlement": entitlement_report(entitlements, period),
    }


def scenarios(db_path: str, now: datetime) -> dict:
    """
    Worked COLD/WARM/HOT examples on the real universe. Still no request.

    Fixtures, plainly labelled as such. The engine is given a situation
    and asked what state it would want; nothing is activated and no
    quote is acquired. The HOT-but-delayed case is the one that matters:
    it is the state every real session so far has been in.
    """
    from src.adaptive.attention import AttentionAllocator, AttentionConfig
    from src.domain.adaptive_models import (
        MarketDataState, RealtimeActivationReason,
    )
    from src.domain.data_attention_models import (
        AttentionPriority, DataAttentionRequest, InstrumentDataStatus,
    )
    from src.domain.market_data_billing_models import EntitlementStatus
    from src.domain.market_data_models import MarketDataAvailability
    from src.domain.paper_models import DataFreshness

    conn = _open_readonly(db_path)
    try:
        exchange_map = ExchangeMap.from_connection(conn)
    finally:
        conn.close()

    period = BillingPeriod.for_moment(now)
    governor = MarketDataBillingGovernor(ibkr_snapshot_policy())
    members = _members(CAPTURE_UNIVERSE)
    known = [i for i in members
             if exchange_map.exchange_of(i) != "UNKNOWN"]
    broad, approaching, exiting = known[0], known[1], known[2]

    allocator = AttentionAllocator(governor, exchange_map,
                                   AttentionConfig(max_hot_symbols=3))
    plan = allocator.plan([
        DataAttentionRequest(broad, MarketDataState.COLD, "screener",
                             "in the session universe; no decision near",
                             AttentionPriority.BROAD_SCREENING),
        DataAttentionRequest(approaching, MarketDataState.WARM, "thesis",
                             "price approaching the target zone",
                             AttentionPriority.POSITION_MONITOR),
        DataAttentionRequest(exiting, MarketDataState.HOT, "thesis",
                             "invalidation breached; exit recommended",
                             AttentionPriority.POSITION_EXIT,
                             RealtimeActivationReason.INVALIDATION_PROXIMITY),
        DataAttentionRequest(exiting, MarketDataState.WARM, "risk",
                             "periodic position verification",
                             AttentionPriority.POSITION_MONITOR),
    ], as_of=now, period=period, candidates_considered=len(members))

    delayed = InstrumentDataStatus(
        exiting, exchange=exchange_map.exchange_of(exiting),
        desired_state=MarketDataState.HOT,
        availability=MarketDataAvailability.DELAYED,
        entitlement=EntitlementStatus.DELAYED,
        freshness=DataFreshness.FRESH, age_seconds=2.0)
    live = InstrumentDataStatus(
        exiting, exchange=exchange_map.exchange_of(exiting),
        desired_state=MarketDataState.HOT,
        availability=MarketDataAvailability.AVAILABLE,
        entitlement=EntitlementStatus.REALTIME_VERIFIED,
        freshness=DataFreshness.FRESH, age_seconds=3.0)

    return {
        "plan": plan.describe(),
        "decisions": [d.describe() for d in plan.decisions],
        "hot_but_delayed": delayed.describe(),
        "hot_and_realtime_fixture": live.describe(),
        "session_universe_size": len(members),
        "hot_count": len(plan.hot),
        "note": ("fixtures; no quote was acquired and no state was "
                 "activated. The realtime case is a FIXTURE -- this "
                 "account has never served a realtime marker."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Market-data cost, exchange coverage and entitlement "
                    "status. Read-only; makes no billable request.")
    parser.add_argument("--scenarios", action="store_true",
                        help="worked COLD/WARM/HOT examples on the real "
                             "universe; still makes no request")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--universe", choices=("capture", "parent"),
                        default="capture")
    parser.add_argument("--exposure-minutes", type=float, default=30.0,
                        help="length of a hypothetical bounded test")
    parser.add_argument("--interval-seconds", type=float, default=60.0,
                        help="request cadence; the capture default is 60")
    parser.add_argument("--budget-usd", type=float, default=20.0)
    parser.add_argument("--subscription-cost-usd", type=float, default=None,
                        help="if omitted, the subscription price is reported "
                             "UNKNOWN rather than guessed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.scenarios:
        result = scenarios(args.db, datetime.now(timezone.utc))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0
        print("COLD / WARM / HOT  (fixtures on the real universe; 0 requests)")
        for decision in result["decisions"]:
            print("  %-22s %-5s  %-16s %s"
                  % (decision["instrument_id"], decision["granted_state"],
                     decision["priority"],
                     ", ".join(decision["requesters"])))
            for reason in decision["reasons"]:
                print("      why: %s" % reason)
        print("\n  session universe %d  ->  HOT %d   (unused HOT capacity %s)"
              % (result["session_universe_size"], result["hot_count"],
                 result["plan"]["unused_hot_capacity"]))
        print("\nEXECUTION FRESHNESS, same instrument, two worlds")
        for label, key in (("HOT + delayed  ", "hot_but_delayed"),
                           ("HOT + realtime ", "hot_and_realtime_fixture")):
            status = result[key]
            print("  %s ready=%-5s %s"
                  % (label, status["execution_freshness_ready"],
                     status["not_ready_because"] or "(fixture: never observed "
                                                    "on this account)"))
        return 0

    report = build_report(
        args.db, args.universe, args.exposure_minutes, args.interval_seconds,
        args.budget_usd, args.subscription_cost_usd,
        datetime.now(timezone.utc))

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    model = report["cost_model"]
    print("MARKET-DATA GOVERNOR  (period %s, dry run, 0 billable requests)"
          % report["billing_period"])
    print("  provider   : %s  policy %s  confidence %s"
          % (model["provider"], model["policy_version"], model["confidence"]))
    print("  evidence   : %s (%s)"
          % (model["evidence_source"], model["evidence_date"]))
    print("  ACCOUNT VERIFIED: %s" % ("YES" if model["account_verified"]
                                      else "NO -- provider policy only"))
    print("  counting   : %s  (batched transport; basis not stated by provider)"
          % model["counting_basis"])
    print("  budget     : %.2f USD/month" % model["monthly_budget_usd"])

    channel = report["channel"]
    print("\nTRANSPORT (audited %s)" % channel["audited_at"])
    print("  snapshot %s | streaming %s -- %s"
          % (channel["snapshot_supported"], channel["stream_supported"],
             channel["note"]))

    print("\nEXCHANGE COVERAGE")
    for name, info in report["universes"].items():
        print("  %-8s %3d instruments  %s%s"
              % (name, info["instruments"], info["distribution"],
                 "  UNKNOWN=%d" % info["unknown_count"]
                 if info["unknown_count"] else ""))
    if report["unpriced_exchanges"]:
        print("  unpriced venues (requests refused): %s"
              % ", ".join(report["unpriced_exchanges"]))

    forecast = report["forecast"]
    print("\nMONTHLY FORECAST (%s universe)" % report["selected_universe"])
    print("  %d capped exchange(s) x cap = %s USD variable ceiling"
          % (forecast["exchange_count"], forecast["variable_ceiling_usd"]))
    print("  subscription: %s  ->  worst case %s USD against %.2f budget: %s"
          % (forecast["fixed_subscription_usd"] if
             forecast["fixed_subscription_usd"] is not None else "none configured",
             forecast["total_worst_case_usd"], forecast["budget_usd"],
             "FITS" if forecast["fits_budget"] else "DOES NOT FIT"))

    exposure = report["exposure"]
    print("\nBOUNDED TEST EXPOSURE (%.0f minutes at %.0fs cadence, %d cycles)"
          % (exposure["minutes"], args.interval_seconds, exposure["cycles"]))
    print("  if the cap holds      : %s USD" % exposure["capped_cost_usd"])
    print("  if it does NOT hold   : %s USD  (worst of the two counting bases)"
          % exposure["worst_case_uncapped_usd"])
    print("    per-instrument basis: %s USD"
          % exposure["uncapped_cost_usd_per_instrument_basis"])
    print("    per-call basis      : %s USD"
          % exposure["uncapped_cost_usd_per_call_basis"])
    for name, minutes in sorted(exposure["minutes_until_cap"].items()):
        print("  %-7s reaches its 150-request threshold after %s minutes"
              % (name, minutes))

    entitlement = report["entitlement"]
    print("\nENTITLEMENT")
    print("  realtime verified on: %s"
          % (", ".join(entitlement["realtime_verified_exchanges"])
             or "NO EXCHANGE -- never observed on this account"))
    print("  cap reached does NOT imply realtime verified; only an observed "
          "marker does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
