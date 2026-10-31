"""
scripts/validate_market_data_billing.py
-----------------------------------------------------
The controlled, bounded test of IBKR's capped snapshot billing
(Phase 25.9I-C, spec sections 25, 40, 41). READ-ONLY market data. NO ORDERS.

    # plan only -- the default, and what an ordinary invocation does
    python scripts/validate_market_data_billing.py --plan

    # the real thing; every ceiling is required and has no default
    python scripts/validate_market_data_billing.py --execute \
        --max-requests 1000 --max-estimated-cost-usd 4.00 \
        --timeout-seconds 1800

    # the morning after
    python scripts/validate_market_data_billing.py --reconcile \
        --exchange NYSE --portal-count 150 --portal-cost-usd 1.50

WHAT THIS TEST IS FOR
-------------------------
IBKR stated (ticket #561227) that snapshot requests cap at USD 1.50 per
exchange per calendar month, after which realtime streaming is free.
That is a support description, not a statement of this account. This
script is the only way to turn it into evidence, and it is built so the
cost of being wrong is small and known in advance.

WHY EVERY CEILING IS REQUIRED AND NONE HAS A DEFAULT
--------------------------------------------------------
The provider's counter updates once per business day. During the
session there is no external brake at all. A default ceiling would be a
number this file invented for a spend the operator has to justify, so
`--max-requests`, `--max-estimated-cost-usd` and `--timeout-seconds`
must all be supplied explicitly (spec section 41). The run stops at
whichever arrives first, and stopping is the normal ending.

WHAT IT REFUSES TO DO
-------------------------
Runs only through `capture_only()`, whose gateway and transport both
refuse every venue write. Checks `ExecutionSafety.allow_real_orders` is
False before the first request. Refuses a live (U-prefix) account.
Buys no subscription -- there is no code here that could.

THE PAPER-ACCOUNT CHECK HERE IS THIS TOOL'S OWN GUARD
---------------------------------------------------------
It is NOT a resolution of the separate, still-open question of whether
the production capture runner should hard-guard on a DU account. That
decision belongs to the operator and has not been made.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive.billing import (                                  # noqa: E402
    MarketDataBillingGovernor, ibkr_snapshot_policy,
)
from src.adaptive.entitlement import (                              # noqa: E402
    EntitlementVerifier, MarkerObservation, entitlement_report,
)
from src.adaptive.exchange_map import ExchangeMap                   # noqa: E402
from src.adaptive.request_ledger import (                           # noqa: E402
    REQUEST_PROBE, RequestLedger, RequestRefused, reconcile_portal_counter,
)
from src.domain.data_attention_models import AttentionPriority      # noqa: E402
from src.domain.market_data_billing_models import (                 # noqa: E402
    BillingPeriod, CapState, EntitlementStatus,
)

DEFAULT_DB = os.path.join(ROOT, "data", "capture", "intraday_capture.db")
CAPTURE_UNIVERSE = os.path.join(ROOT, "config", "capture_universe_v1.json")

#: IBKR field 6509 plus last price: the minimum that answers both
#: questions this test asks (did it bill, and is it realtime).
FIELDS = ("31", "6509")


def _members():
    with open(CAPTURE_UNIVERSE, encoding="utf-8") as handle:
        payload = json.load(handle)
    return [str(m.get("instrument_id"))
            for m in payload.get("instruments", []) if m.get("instrument_id")]


# ======================================================================
# Stage A -- pre-flight (spec section 25)
# ======================================================================


def preflight(conn, transport, ledger, period, exchange_map,
              instrument_ids) -> dict:
    """
    Everything that must be true before one billable request is sent.

    Returns a report with `ready`. A failed check is never a warning to
    be clicked through: `--execute` refuses unless every check passes.
    """
    checks = {}
    problems = []

    from src.execution.safety import ExecutionSafety
    checks["broker_writes_blocked"] = not ExecutionSafety().allow_real_orders
    if not checks["broker_writes_blocked"]:
        problems.append("ExecutionSafety.allow_real_orders is True")

    checks["capture_only_transport"] = bool(
        getattr(transport, "submission_forbidden", False))
    if not checks["capture_only_transport"]:
        problems.append("transport is not capture_only(); venue writes reachable")

    account_id, account_kind = "", "UNKNOWN"
    try:
        status = transport.is_authenticated()
        checks["gateway_authenticated"] = bool(status.authenticated)
        checks["gateway_competing"] = bool(status.competing)
        if not status.authenticated:
            problems.append("gateway is not authenticated; log in first")
        if status.competing:
            problems.append("another session holds this account's market data; "
                            "log out of the Client Portal in the browser")
        accounts = [a.get("accountId") for a in transport.accounts()]
        account_id = str(accounts[0]) if accounts else ""
        account_kind = ("PAPER" if account_id.upper().startswith("DU")
                        else "LIVE" if account_id else "UNKNOWN")
    except Exception as error:                                  # noqa: BLE001
        checks["gateway_authenticated"] = False
        problems.append(f"gateway did not answer: {str(error)[:160]}")

    checks["paper_account"] = account_kind == "PAPER"
    if account_kind != "PAPER":
        problems.append(
            f"account {account_id or '<unknown>'} is {account_kind}; this "
            "read-only billing test runs on paper only")

    unknown = exchange_map.unknown_instruments(instrument_ids)
    checks["all_instruments_attributable"] = not unknown
    if unknown:
        problems.append(f"{len(unknown)} instrument(s) have no billing exchange")

    spent, unpriceable = ledger.total_estimated_cost_usd(period)
    checks["period_priceable"] = not unpriceable
    checks["period_already_spent_usd"] = spent

    now = datetime.now(timezone.utc)
    checks["clock_utc"] = now.isoformat()
    checks["billing_period"] = period.key

    return {
        "ready": not problems,
        "account_id": account_id,
        "account_kind": account_kind,
        "checks": checks,
        "problems": problems,
        "exchange_distribution": exchange_map.distribution(instrument_ids),
    }


# ======================================================================
# Stage B/C -- the bounded run, and its deliberate stop
# ======================================================================


def run_bounded(transport, ledger, governor, exchange_map, period,
                instrument_ids, conids, *, max_requests, max_cost_usd,
                timeout_seconds, interval_seconds, run_id, verbose=True) -> dict:
    """
    Poll until a ceiling is hit, then stop. Stopping is success.

    The three ceilings are checked BEFORE each cycle, never after, so a
    cycle that would cross a limit is not sent. The local ledger is the
    meter; the provider's counter will not move until tomorrow.
    """
    started = time.monotonic()
    exchanges = {i: exchange_map.exchange_of(i) for i in instrument_ids}
    observations = []
    cycles = 0
    stop_reason = ""

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout_seconds:
            stop_reason = f"timeout reached ({timeout_seconds:.0f}s)"
            break
        if ledger.billed_units(run_id) + len(instrument_ids) > max_requests:
            stop_reason = (f"request ceiling reached "
                           f"({ledger.billed_units(run_id)}/{max_requests})")
            break
        projected = ledger.run_cost_usd(run_id)
        if projected >= max_cost_usd:
            stop_reason = (f"cost ceiling reached "
                           f"({projected:.2f}/{max_cost_usd:.2f} USD)")
            break

        try:
            verdict = ledger.authorize(
                instrument_ids, exchanges, datetime.now(timezone.utc), period,
                priority=AttentionPriority.BROAD_SCREENING,
                purpose="billing validation")
        except RequestRefused as refusal:
            stop_reason = f"refused by the local ledger: {refusal}"
            break
        if not verdict.permits_spending:
            stop_reason = f"refused by the cost governor: {verdict.reason}"
            break

        now = datetime.now(timezone.utc)
        try:
            payloads = transport.market_snapshot(conids, fields=FIELDS)
        except Exception as error:                              # noqa: BLE001
            stop_reason = f"transport error: {str(error)[:160]}"
            break

        ledger.record(instrument_ids, exchanges, now, period,
                      request_type=REQUEST_PROBE,
                      purpose="billing validation",
                      requester="validate_market_data_billing",
                      validation_run_id=run_id)
        cycles += 1

        by_conid = {str(i): str(c) for i, c in zip(instrument_ids, conids)}
        markers = {str(p.get("conid")): str(p.get("6509", "")) for p in payloads}
        for instrument_id in instrument_ids:
            marker = markers.get(by_conid.get(instrument_id, ""), "")
            observations.append(MarkerObservation(
                instrument_id=instrument_id, marker=marker, observed_at=now))

        if verbose:
            realtime = sum(1 for p in payloads if "R" in str(p.get("6509", "")))
            delayed = sum(1 for p in payloads if "D" in str(p.get("6509", "")))
            print("  cycle %3d  units %5d  est %.2f USD  realtime %d delayed %d"
                  % (cycles, ledger.billed_units(run_id),
                     ledger.run_cost_usd(run_id), realtime, delayed))

        remaining = interval_seconds - (time.monotonic() - started) % interval_seconds
        time.sleep(max(0.0, min(remaining, interval_seconds)))

    return {
        "cycles": cycles,
        "stop_reason": stop_reason,
        "billed_units": ledger.billed_units(run_id),
        "estimated_cost_usd": ledger.run_cost_usd(run_id),
        "uncapped_cost_usd": ledger.uncapped_run_cost_usd(run_id),
        "observations": observations,
    }


# ======================================================================
# CLI
# ======================================================================


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded, read-only validation of capped snapshot "
                    "billing. Makes NO orders and buys NO subscription.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true",
                      help="show the plan and the pre-flight; send nothing "
                           "(the default)")
    mode.add_argument("--execute", action="store_true",
                      help="actually send billable requests; every ceiling "
                           "below is then required")
    mode.add_argument("--reconcile", action="store_true",
                      help="record the provider's next-day counter beside "
                           "the local estimate")

    parser.add_argument("--max-requests", type=int, default=None,
                        help="hard ceiling on billable units. No default.")
    parser.add_argument("--max-estimated-cost-usd", type=float, default=None,
                        help="hard ceiling on locally estimated spend. "
                             "No default.")
    parser.add_argument("--timeout-seconds", type=float, default=None,
                        help="hard ceiling on wall-clock duration. No default.")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--budget-usd", type=float, default=20.0)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="use only the first N instruments")
    parser.add_argument("--exchange", default="")
    parser.add_argument("--portal-count", type=int, default=None)
    parser.add_argument("--portal-cost-usd", type=float, default=None)
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    period = BillingPeriod.for_moment(now)
    governor = MarketDataBillingGovernor(
        ibkr_snapshot_policy(), monthly_budget_usd=args.budget_usd)

    # ---------------- reconcile ----------------
    if args.reconcile:
        if not args.exchange:
            parser.error("--reconcile requires --exchange")
        conn = sqlite3.connect(args.db)
        try:
            result = reconcile_portal_counter(
                conn, provider=governor.policy.provider, period=period,
                exchange=args.exchange, portal_count=args.portal_count,
                portal_cost_usd=args.portal_cost_usd, at=now, note=args.note)
        finally:
            conn.close()
        print(json.dumps(result, indent=2) if args.json else
              "RECONCILED %s %s\n  local  %s requests, %s USD\n"
              "  portal %s requests, %s USD\n  agree: %s"
              % (result["exchange"], result["period"],
                 result["local_request_count"],
                 result["local_estimated_cost_usd"],
                 result["portal_reported_count"],
                 result["portal_reported_cost_usd"], result["counts_agree"]))
        return 0

    # ---------------- shared setup ----------------
    read_only = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        exchange_map = ExchangeMap.from_connection(read_only)
        ledger = RequestLedger.replay(read_only, governor, period)
    finally:
        read_only.close()

    instrument_ids = _members()
    if args.limit:
        instrument_ids = instrument_ids[:args.limit]
    conids = [exchange_map.venue_of(i).conid for i in instrument_ids]
    missing = [i for i, c in zip(instrument_ids, conids) if not c]
    if missing:
        print("REFUSED: %d instrument(s) have no resolved conid: %s"
              % (len(missing), ", ".join(missing[:5])))
        return 2

    exposure = governor.project_exposure(
        exchange_map.distribution(instrument_ids),
        minutes=(args.timeout_seconds or 1800) / 60.0,
        interval_seconds=args.interval_seconds)

    print("CONTROLLED MARKET-DATA BILLING VALIDATION")
    print("  period %s | %d instruments | %s"
          % (period.key, len(instrument_ids),
             exchange_map.distribution(instrument_ids)))
    print("  if the cap holds : %s USD" % exposure.capped_cost_usd)
    print("  if it does not   : %s USD" % exposure.worst_case_uncapped_usd)
    for name, minutes in sorted(exposure.minutes_until_cap.items()):
        print("  %-7s threshold after %s minutes" % (name, minutes))

    # ---------------- gateway ----------------
    import warnings
    warnings.filterwarnings("once", message="Unverified HTTPS request")
    from src.capture.stack import build_capture_gateway
    gateway = build_capture_gateway(sqlite3.connect(":memory:"))
    transport = gateway.transport

    flight = preflight(None, transport, ledger, period, exchange_map,
                       instrument_ids)
    print("\nPRE-FLIGHT: %s  (account %s / %s)"
          % ("READY" if flight["ready"] else "NOT READY",
             flight["account_id"] or "?", flight["account_kind"]))
    for problem in flight["problems"]:
        print("  BLOCKED: %s" % problem)

    if not args.execute:
        print("\nPLAN ONLY. Nothing was sent. To run the bounded test supply "
              "every ceiling:\n"
              "  --execute --max-requests N --max-estimated-cost-usd X "
              "--timeout-seconds S")
        return 0 if flight["ready"] else 1

    # ---------------- execute ----------------
    if args.max_requests is None or args.max_estimated_cost_usd is None \
            or args.timeout_seconds is None:
        parser.error("--execute requires --max-requests, "
                     "--max-estimated-cost-usd and --timeout-seconds; "
                     "these have no defaults on purpose")
    if not flight["ready"]:
        print("\nREFUSED: pre-flight did not pass. Nothing was sent.")
        return 1

    run_id = "mdv-" + now.strftime("%Y%m%dT%H%M%S")
    print("\nRUN %s  ceilings: %d units / %.2f USD / %.0fs"
          % (run_id, args.max_requests, args.max_estimated_cost_usd,
             args.timeout_seconds))
    result = run_bounded(
        transport, ledger, governor, exchange_map, period, instrument_ids,
        conids, max_requests=args.max_requests,
        max_cost_usd=args.max_estimated_cost_usd,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds, run_id=run_id)

    print("\nSTOPPED: %s" % result["stop_reason"])
    print("  cycles %d | billed units %d | estimated %.2f USD "
          "(uncapped would be %.2f)"
          % (result["cycles"], result["billed_units"],
             result["estimated_cost_usd"], result["uncapped_cost_usd"]))

    # ---------------- persist + entitlement ----------------
    writable = sqlite3.connect(args.db)
    try:
        written = ledger.flush(writable, datetime.now(timezone.utc))
    finally:
        writable.close()
    print("  ledger: %s" % written)

    cap_states = {state.exchange: state.cap_state
                  for state in ledger.usage.states_in(period)}
    expectations = {name: EntitlementStatus.REALTIME_VERIFIED
                    for name in governor.policy.known_exchanges}
    entitlements = EntitlementVerifier(exchange_map).verify(
        result["observations"], cap_states=cap_states,
        expected_after_cap=expectations)
    report = entitlement_report(entitlements, period)

    print("\nENTITLEMENT AFTER THE RUN")
    for name, info in sorted(report["exchanges"].items()):
        print("  %-7s cap=%-16s observed=%-18s execution-grade=%s"
              % (name, info["cap_state"], info["observed"],
                 info["execution_grade"]))
    if report["expectation_unmet_exchanges"]:
        print("  EXPECTATION UNMET on %s: the cap was reached and realtime "
              "did NOT appear. Record it; do not assume either way."
              % ", ".join(report["expectation_unmet_exchanges"]))

    print("\nNEXT BUSINESS DAY: read the portal snapshot counter, then")
    print("  python scripts/validate_market_data_billing.py --reconcile "
          "--exchange <NAME> --portal-count N --portal-cost-usd X")
    if args.json:
        print(json.dumps({"run": result["stop_reason"],
                          "billed_units": result["billed_units"],
                          "estimated_cost_usd": result["estimated_cost_usd"],
                          "entitlement": report}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
