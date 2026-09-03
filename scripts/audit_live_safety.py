#!/usr/bin/env python3
"""
scripts/audit_live_safety.py
---------------------------------
The live-safety audit (Phase 16, spec 94, 101).

Sixteen questions about whether real-money execution can happen, each
answered by EXECUTING the code rather than reading it. A claim in a
document is a claim; this is a check.

It is a script rather than a one-off because the property it verifies
is not "was true when written" but "is true now". Run it before any
release, and after any change to the execution, safety, governance or
adapter layers.

Exits non-zero if any question fails, so it can gate a pipeline.
"""
import os
import re
import subprocess
import sys
import pathlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

FAIL = []


def check(question, ok, detail=""):
    mark = "PASS" if ok else "**FAIL**"
    print(f"  [{mark}] {question}")
    if detail:
        print(f"         {detail}")
    if not ok:
        FAIL.append(question)


print("=" * 70)
print("PHASE 16 FINAL LIVE-SAFETY AUDIT")
print("=" * 70)

# Q1 --------------------------------------------------------------
from src.domain.broker_models import (
    Broker, BrokerAccount, ExecutionEnvironment,
)
from src.execution.safety import ExecutionSafety

ok = True
try:
    Broker(broker_id="x", name="x", environment=ExecutionEnvironment.LIVE,
           adapter="a")
    ok = False
except Exception:
    pass
try:
    BrokerAccount(account_id="a", broker_id="x", name="a",
                  environment=ExecutionEnvironment.LIVE)
    ok = False
except Exception:
    pass
check("Q1  a real-money broker or account cannot be constructed", ok)

# Q2 --------------------------------------------------------------
safety = ExecutionSafety()
ok = safety.allow_real_orders is False
try:
    safety.allow_real_orders = True
    ok = False
except AttributeError:
    pass
check("Q2  allow_real_orders is False and has no setter", ok)

# Q3 --------------------------------------------------------------
os.environ["MARKETLENS_ALLOW_REAL_ORDERS"] = "1"
requested = ExecutionSafety.real_orders_requested_by_environment()
granted = ExecutionSafety().allow_real_orders
verdict = ExecutionSafety().check(ExecutionEnvironment.PAPER)
os.environ.pop("MARKETLENS_ALLOW_REAL_ORDERS", None)
check("Q3  setting MARKETLENS_ALLOW_REAL_ORDERS=1 grants nothing",
      granted is False,
      f"detected as an attempt: {requested} (reported, never honoured)")

# Q4 --------------------------------------------------------------
from src.execution.adapters.ibkr.config import (
    IBKRConfig, IBKRConfigurationError,
)
ok = True
for env in ("live", "LIVE", " live "):
    os.environ["IBKR_ENVIRONMENT"] = env
    try:
        IBKRConfig.from_environment(account_id="U1")
        ok = False
    except Exception:
        pass
os.environ.pop("IBKR_ENVIRONMENT", None)
check("Q4  IBKR_ENVIRONMENT=live is refused however it is spelled", ok)

# Q5 --------------------------------------------------------------
from src.execution.session import SessionConfiguration
ok = True
try:
    SessionConfiguration(environment=ExecutionEnvironment.LIVE)
    ok = False
except Exception:
    pass
check("Q5  a session cannot be configured for real money", ok)

# Q6 --------------------------------------------------------------
from src.execution.governance import ExecutionGovernor, ExecutionLevel
implemented_real = [l.label for l in ExecutionLevel
                    if l.is_implemented and l.is_real_money]
check("Q6  no implemented execution level is real money",
      not implemented_real, f"real+implemented: {implemented_real or 'none'}")

# Q7 --------------------------------------------------------------
from datetime import datetime, timezone
now = datetime(2026, 9, 3, tzinfo=timezone.utc)
gov = ExecutionGovernor()
req = gov.request(ExecutionLevel.PRODUCTION_LIVE, "alice", now)
req.approve("bob", now)
effective = gov.effective_level(now)
check("Q7  approving level 7 still yields a non-real-money level",
      not effective.is_real_money, f"effective: {effective.label}")

# Q8 --------------------------------------------------------------
check("Q8  the governor never reports real money as reachable",
      gov.state(now)["real_money_reachable"] is False)

# Q9 --------------------------------------------------------------
req2 = gov.request(ExecutionLevel.BROKER_PAPER, "carol", now)
ok = False
try:
    req2.approve("carol", now)
except ValueError:
    ok = True
check("Q9  nobody can approve their own promotion request", ok)

# Q10 -------------------------------------------------------------
from src.execution.adapters.disabled_gateway import planned_gateways
check("Q10 no second broker is planned or stubbed",
      planned_gateways() == {}, f"planned: {planned_gateways()}")

# Q11 -------------------------------------------------------------
# This file is excluded from its own search: a check that names what
# it forbids will always contain the word.
hits = [f for f in subprocess.run(
    ["git", "grep", "-riIl", "-e", "mt5", "-e", "metatrader",
     "--", "src", "tests", "scripts"],
    capture_output=True, text=True).stdout.split()
    if not f.endswith("audit_live_safety.py")]
check("Q11 no MT5 reference remains in src, tests or scripts",
      not hits, ", ".join(hits) or "clean")

# Q12 -------------------------------------------------------------
SECRET = re.compile(
    r"(password|passwd|secret|api[_-]?key|token)\s*=\s*[\"'][^\"']{6,}",
    re.I)
offenders = []
for path in pathlib.Path("src").rglob("*.py"):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if SECRET.search(line) and "os.environ" not in line:
            offenders.append(f"{path}:{n}")
check("Q12 no literal credential is assigned anywhere in src",
      not offenders, "; ".join(offenders) or "clean")

# Q13 -------------------------------------------------------------
cfg_fields = set(IBKRConfig.__dataclass_fields__)
forbidden = {"password", "username", "secret", "api_key", "token"}
check("Q13 the IBKR config carries no credential field at all",
      not (cfg_fields & forbidden),
      f"fields: {sorted(cfg_fields & forbidden) or 'none'}")

# Q14 -------------------------------------------------------------
ignored = pathlib.Path(".gitignore").read_text(encoding="utf-8")
check("Q14 .env is gitignored and .env.example is not",
      ".env" in ignored and "!.env.example" in ignored)

# Q15 -------------------------------------------------------------
tracked = subprocess.run(["git", "ls-files", ".env"],
                         capture_output=True, text=True).stdout.strip()
check("Q15 no .env file is tracked by git", not tracked, tracked or "none")

# Q16 -------------------------------------------------------------
from src.execution.limits import CapitalLimits
caps = CapitalLimits()
unset = all(getattr(caps, f) is None for f in
            ("max_live_capital", "max_order_notional",
             "max_position_notional", "max_daily_orders"))
check("Q16 no real-money capital default ships in the code",
      unset and not caps.configured_for_real_money)

print()
print("=" * 70)
if FAIL:
    print(f"AUDIT FAILED — {len(FAIL)} question(s):")
    for q in FAIL:
        print(f"  - {q}")
    sys.exit(1)
print("ALL 16 AUDIT QUESTIONS PASS")
print("INTERACTIVE BROKERS = ONLY BROKER")
print("MT5 = NOT IMPLEMENTED")
print("REAL MONEY EXECUTION = BLOCKED BY DEFAULT")
print("=" * 70)
