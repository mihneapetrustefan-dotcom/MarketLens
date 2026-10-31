"""
scripts/check_gateway_account.py
---------------------------------------------
Which IBKR account is the gateway logged into, and is its data real-time?

WHY. Capture polls whatever account the Client Portal Gateway happens to
hold, and does not record which one. On 2026-09-24 that turned out to be
DUT101249 -- a standalone paper account predating the live one -- while
the funded account's paper is DUT168120, so no shared subscription could
ever have reached it. A login that "succeeds" tells you nothing about
WHICH account succeeded: ask the gateway.

READ-ONLY. Goes through the same capture-only transport the runner uses,
so no venue write is reachable. Costs a handful of requests, which is
safe alongside a running session (the budget is 50/minute).

    python scripts/check_gateway_account.py
    python scripts/check_gateway_account.py --conids 756733,265598

Exit 0 when the gateway answers, 1 when it does not, 2 when it answers
but serves no market data at all.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.capture.stack import build_capture_gateway          # noqa: E402

#: SPY, AAPL, VZ: one ARCA, one NASDAQ, one NYSE listing.
DEFAULT_CONIDS = "756733,265598,4901"
#: IBKR field 6509: R real-time, D delayed, Z frozen, P snapshot, B book.
AVAILABILITY = "6509"


def verdict(marker: str) -> str:
    if "R" in marker:
        return "REAL-TIME"
    if "D" in marker:
        return "DELAYED"
    if "Z" in marker or "Y" in marker:
        return "FROZEN"
    return "UNKNOWN"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--conids", default=DEFAULT_CONIDS)
    parser.add_argument("--attempts", type=int, default=8,
                        help="the snapshot endpoint primes on the first call; "
                             "a fresh session can take a minute to serve")
    args = parser.parse_args(argv)

    import warnings
    warnings.filterwarnings("once", message="Unverified HTTPS request")

    transport = build_capture_gateway(sqlite3.connect(":memory:")).transport
    try:
        status = transport.is_authenticated()
    except Exception as error:                             # noqa: BLE001
        print("gateway did not answer: %s" % str(error)[:200])
        print("start it, then log in at https://localhost:5000")
        return 1
    print("authenticated %s | connected %s | competing %s"
          % (status.authenticated, status.connected, status.competing))
    if not status.authenticated:
        print("log in at https://localhost:5000")
        return 1

    try:
        accounts = [a.get("accountId") for a in transport.accounts()]
    except Exception as error:                             # noqa: BLE001
        print("accounts unavailable: %s" % str(error)[:200])
        return 1
    for account in accounts:
        kind = "PAPER" if str(account).upper().startswith("DU") else "LIVE"
        print("account: %s  (%s)" % (account, kind))

    conids = [c.strip() for c in args.conids.split(",") if c.strip()]
    payloads = []
    for attempt in range(args.attempts):
        try:
            payloads = transport.market_snapshot(conids, fields=("31", AVAILABILITY))
        except Exception as error:                         # noqa: BLE001
            print("  snapshot: %s" % str(error)[:120])
            payloads = []
        if any(str(p.get(AVAILABILITY, "")) for p in payloads):
            break
        time.sleep(5)
    else:
        print("no market data after %d attempts: the session may still be priming, "
              "or this account has no market data entitlement" % args.attempts)
        return 2

    for payload in payloads:
        marker = str(payload.get(AVAILABILITY, ""))
        print("  conid %-10s 6509=%-6s %-9s last=%s"
              % (payload.get("conid"), repr(marker), verdict(marker), payload.get("31")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
