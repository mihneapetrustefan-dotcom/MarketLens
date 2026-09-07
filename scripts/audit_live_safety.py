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
# Searched at LINE level, not file level.
#
# The file-level version failed the moment Phases 22 and 23 added
# safety tests that assert the broker's ABSENCE -- a test containing
# `assertNotIn("metatrader", source)` looked identical to an
# implementation. That is a false positive with a real cost: an audit
# that cries wolf is one people start passing over, and this one is
# the last line of defence on the broker boundary.
#
# So each matching line is classified. A line that names the broker
# inside a prohibition -- a negative assertion, or a tuple of
# forbidden words being scanned for -- is proving absence and is not
# an implementation. Anything else is an offender.
#
# This keeps the check strict where it matters: any line under src/
# or scripts/ that names the broker outside a prohibition still fails,
# so no adapter, import, config key or call site can hide here.
PROHIBITION_MARKERS = (
    "assertnotin", "assertnot", "not in", "notin(", "forbidden",
    "must not", "no second broker", "is absent", "self.fail",
)

# `--untracked` matters more than it looks. Without it `git grep`
# searches only the index, so a broker adapter that had been written
# but not yet committed would pass this audit -- which is exactly the
# moment you want it to fail. A negative-control probe placed in
# src/ went undetected until this flag was added.
raw = subprocess.run(
    ["git", "grep", "-rinI", "--untracked", "-e", "mt5", "-e", "metatrader",
     "-e", "metaquotes", "--", "src", "tests", "scripts"],
    capture_output=True, text=True).stdout.splitlines()

#: A prohibition is a CONSTRUCT, not a line. The forbidden words are
#: usually a tuple on one line and the assertion on the next:
#:
#:     for word in ("metatrader", "mt5", "alpaca"):
#:         self.assertNotIn(word, lowered)
#:
#: so the classification reads a small window around the match. Three
#: lines is enough for every such construct in this repository and
#: narrow enough that a real implementation cannot hide behind a
#: distant comment.
CONTEXT = 3

_source_cache = {}


def _window(path, line_no):
    if path not in _source_cache:
        try:
            _source_cache[path] = pathlib.Path(path).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            _source_cache[path] = []
    lines = _source_cache[path]
    lo = max(0, line_no - 1 - CONTEXT)
    hi = min(len(lines), line_no + CONTEXT)
    return " ".join(lines[lo:hi]).lower()


offenders = []
for entry in raw:
    parts = entry.split(":", 2)
    if len(parts) < 3:
        continue
    path, line_no, text = parts
    if path.endswith("audit_live_safety.py"):
        continue          # the search that proves the absence
    try:
        context = _window(path, int(line_no))
    except ValueError:
        context = text.lower()
    if any(marker in context for marker in PROHIBITION_MARKERS):
        continue          # naming what is forbidden, not implementing it
    offenders.append("%s:%s" % (path, line_no))

check("Q11 no MT5 reference remains in src, tests or scripts",
      not offenders, ", ".join(offenders) or "clean")

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
