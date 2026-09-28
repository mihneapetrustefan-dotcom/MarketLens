"""
scripts/adaptive_status.py
---------------------------------------
What the adaptive decision layer is doing. In Phase 25.9I-A: nothing.

WHY A DIAGNOSTIC FOR AN INACTIVE LAYER
------------------------------------------
Because "it is off" is a claim, and a claim about safety is worth no
more than the command that checks it. This prints the resolved feature
flags, the cost model's configured prices, and the freshness policy, so
that the answer comes from the code rather than from a report written
the day the code was merged.

READ-ONLY. No database is written, no broker is contacted, no market
data is requested, and no account, token or credential is printed.

    python scripts/adaptive_status.py
    python scripts/adaptive_status.py --json

Exit 0 when the layer is fully inactive, 1 when any flag is on, 2 when
the configuration could not be read (in which case the flags are off
anyway -- see src/adaptive/flags.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import flags as adaptive_flags              # noqa: E402
from src.adaptive.cost_governor import (                      # noqa: E402
    CostConfigurationError, MarketDataCostGovernor, MarketDataCostModel,
)
from src.adaptive.freshness import ExecutionFreshnessPolicy   # noqa: E402

OK, FLAGS_ON, CONFIG_PROBLEM = 0, 1, 2


def collect() -> dict:
    resolved = adaptive_flags.resolve()
    report = {
        "phase": "25.9I-A",
        "adaptive_layer": resolved.describe(),
        "freshness_policy": ExecutionFreshnessPolicy().describe(),
        # Stated as facts about this phase, and asserted by the tests in
        # tests/adaptive/test_legacy_behaviour_unchanged.py.
        "production_paths": {
            "dynamic_universe": "disabled (config/capture_universe_v1.json is "
                                "still the only capture universe)",
            "adaptive_market_data": "disabled (capture transport and request "
                                    "cadence unchanged)",
            "capital_allocator": "legacy (src/portfolio/sizing.py)",
            "position_thesis": "disabled",
            "dynamic_exit": "disabled",
            "execution_freshness_gate": "disabled",
        },
        "safety": {
            "broker_writes_introduced": False,
            "live_trading_enabled": False,
            "paper_orders_created_by_this_layer": 0,
            "d20_touched": False,
        },
    }
    try:
        governor = MarketDataCostGovernor(MarketDataCostModel.from_environment())
        report["cost_governor"] = governor.describe()
        report["cost_governor_error"] = None
    except CostConfigurationError as error:
        # The prices are unusable, so nothing may be spent against them.
        report["cost_governor"] = None
        report["cost_governor_error"] = str(error)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = collect()
    layer = report["adaptive_layer"]

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("ADAPTIVE DECISION LAYER (phase %s)" % report["phase"])
        print("  layer_enabled            : %s" % layer["layer_enabled"])
        for name, value in sorted(layer["flags"].items()):
            print("  %-25s: %s" % (name, value))
        print("  any flag enabled         : %s" % layer["any_enabled"])
        print("PRODUCTION PATHS")
        for name, value in sorted(report["production_paths"].items()):
            print("  %-25s: %s" % (name, value))
        print("COST GOVERNOR")
        if report["cost_governor"] is None:
            print("  configuration refused    : %s" % report["cost_governor_error"])
        else:
            governor = report["cost_governor"]
            print("  monthly budget (USD)     : %s" % governor["monthly_budget_usd"])
            print("  model version            : %s" % governor["model_version"])
            print("  unknown prices           : %s"
                  % (", ".join(governor["unknown_prices"]) or "none"))
            print("  active                   : %s" % governor["active"])
        print("SAFETY")
        for name, value in sorted(report["safety"].items()):
            print("  %-25s: %s" % (name, value))
        for problem in layer["problems"]:
            print("  ! %s" % problem)
        for suppressed in layer["suppressed"]:
            print("  ! %s" % suppressed)

    if report["cost_governor_error"]:
        return CONFIG_PROBLEM
    if layer["any_enabled"] or layer["layer_enabled"]:
        return FLAGS_ON
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
