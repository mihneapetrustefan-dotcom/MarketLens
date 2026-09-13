"""
scripts/validate_d20_reversal.py
-------------------------------------------
The single, frozen protected-window test of the D20 reversal hypothesis
(Phase 25.9B).

Implements docs/PHASE_25_9B_PREREGISTRATION.md exactly. That file was
committed (754a7bd) before any protected statistic under anchor-v2
existed.

TWO GUARDS, IN THIS ORDER
-----------------------------
1. READINESS GATE. Sample requirements are checked from COUNTS alone.
   If they fail, no statistic is computed, the verdict is INSUFFICIENT
   DATA, and the protected window stays closed. This can be re-checked
   as often as needed, because counting resolved labels reveals nothing
   about the relationship being tested.

2. SINGLE EVALUATION. Computing the statistic OPENS the window. Once a
   SUPPORTED or NOT SUPPORTED verdict is recorded for this experiment,
   the script refuses to compute it again. There is no flag to override
   that, deliberately.

--research runs the identical statistic on the non-protected research
region, labelled NON-CONFIRMATORY. It is robustness evidence (§18), and
circular with respect to discovery, since the hypothesis was found
there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.research_edge_diagnostic import rank
from src.data_access.experiment_schema import initialize_experiment_schema

# ---------------- frozen (pre-registration 754a7bd) ----------------

EXPERIMENT_ID = "exp-d20-reversal-anchor-v2-protected"
PREREGISTRATION = "754a7bd"
PROTECTED_START = "2026-08-15T01:23:31"
PROTECTED_END = "2026-08-27T17:09:32"
FEATURE = "market.return_60d"
TARGET_LONG = "d20.abnormal_return.anchor-v2"
TARGET_SHORT = "d5.abnormal_return.anchor-v2"
EXPECTED_SIGN = -1
MIN_ROWS = 100
MIN_DATES = 8
MIN_ROWS_PER_DATE = 10
MAX_MDE = 0.20
ALPHA = 0.05
PERMUTATIONS = 2000
SEED = 20260913

FINAL_VERDICTS = ("SUPPORTED", "NOT SUPPORTED")


def minimum_detectable_effect(n: int, z_alpha=1.959964, z_beta=0.841621) -> float:
    """|IC| detectable at 80% power, two-sided alpha 0.05."""
    if n <= 3:
        return 1.0
    return math.tanh((z_alpha + z_beta) / math.sqrt(n - 3))


def code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            text=True).strip()
    except Exception:                                     # noqa: BLE001
        return "unknown"


def fingerprint() -> str:
    spec = json.dumps({
        "feature": FEATURE, "target": [TARGET_LONG, TARGET_SHORT],
        "sign": EXPECTED_SIGN, "min_rows": MIN_ROWS, "min_dates": MIN_DATES,
        "min_rows_per_date": MIN_ROWS_PER_DATE, "max_mde": MAX_MDE,
        "alpha": ALPHA, "permutations": PERMUTATIONS, "seed": SEED,
        "window": [PROTECTED_START, PROTECTED_END],
    }, sort_keys=True)
    return hashlib.sha256(spec.encode("utf-8")).hexdigest()[:24]


def load_rows(conn, protected: bool):
    """(date, feature, target) for rows with all three resolved."""
    if protected:
        region = "o.information_cutoff >= ? AND o.information_cutoff <= ?"
        params = (PROTECTED_START, PROTECTED_END)
    else:
        region = "o.information_cutoff < ?"
        params = (PROTECTED_START,)

    def column(name, table, key):
        out = {}
        for oid, value in conn.execute(f"""
            SELECT t.observation_id, t.value_json FROM {table} t
            JOIN research_observations o ON o.observation_id = t.observation_id
            WHERE t.{key} = ? AND {region}
              AND t.value_json IS NOT NULL AND t.value_json != 'null'
        """, (name, *params)):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
                out[oid] = float(parsed)
        return out

    feature = column(FEATURE, "research_features", "qualified_name")
    long_ = column(TARGET_LONG, "research_labels", "name")
    short = column(TARGET_SHORT, "research_labels", "name")
    dates = {oid: cutoff[:10] for oid, cutoff in conn.execute(
        f"SELECT o.observation_id, o.information_cutoff FROM research_observations o "
        f"WHERE {region}", params)}
    rows = []
    for oid in feature:
        if oid in long_ and oid in short and oid in dates:
            rows.append((dates[oid], feature[oid], long_[oid] - short[oid]))
    return rows


def readiness(rows) -> Dict[str, object]:
    """COUNTS ONLY. Computes nothing about the relationship."""
    per_date = defaultdict(int)
    for date, _f, _t in rows:
        per_date[date] += 1
    eligible = sum(1 for n in per_date.values() if n >= MIN_ROWS_PER_DATE)
    mde = minimum_detectable_effect(len(rows))
    failures = []
    if len(rows) < MIN_ROWS:
        failures.append(f"{len(rows)} resolved rows < {MIN_ROWS}")
    if eligible < MIN_DATES:
        failures.append(f"{eligible} eligible dates < {MIN_DATES}")
    if mde > MAX_MDE:
        failures.append(f"minimum detectable |IC| {mde:.3f} > {MAX_MDE}")
    return {"rows": len(rows), "eligible_dates": eligible,
            "mde": round(mde, 4), "ready": not failures, "failures": failures}


def statistic(rows, rng=None, permute=False) -> Optional[float]:
    groups = defaultdict(list)
    for date, f, t in rows:
        groups[date].append((f, t))
    ics = []
    for members in groups.values():
        if len(members) < MIN_ROWS_PER_DATE:
            continue
        f = np.array([m[0] for m in members])
        t = np.array([m[1] for m in members])
        if permute:
            t = rng.permutation(t)
        rf, rt = rank(f), rank(t)
        if rf.std() == 0 or rt.std() == 0:
            continue
        ics.append(float(np.corrcoef(rf, rt)[0, 1]))
    return float(np.mean(ics)) if ics else None


def tercile_spread(rows) -> Optional[float]:
    groups = defaultdict(list)
    for date, f, t in rows:
        groups[date].append((f, t))
    spreads = []
    for members in groups.values():
        if len(members) < MIN_ROWS_PER_DATE:
            continue
        members.sort(key=lambda m: m[0])
        third = len(members) // 3
        if third == 0:
            continue
        low = np.mean([m[1] for m in members[:third]])
        high = np.mean([m[1] for m in members[-third:]])
        spreads.append(float(high - low))
    return float(np.mean(spreads)) if spreads else None


def evaluate(rows) -> Dict[str, object]:
    rng = np.random.default_rng(SEED)
    observed = statistic(rows)
    null = np.array([statistic(rows, rng, permute=True)
                     for _ in range(PERMUTATIONS)], dtype=float)
    # One-sided in the frozen direction: how often chance is at least
    # as negative as what was observed.
    p = float((1 + np.sum(null <= observed)) / (1 + len(null)))
    if observed is None:
        verdict = "INSUFFICIENT DATA"
    elif observed < 0 and p < ALPHA:
        verdict = "SUPPORTED"
    else:
        verdict = "NOT SUPPORTED"
    return {"mean_ic": observed, "p_value_one_sided": p,
            "null_mean": float(np.mean(null)),
            "tercile_spread": tercile_spread(rows), "verdict": verdict}


def already_evaluated(conn) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM experiments WHERE experiment_id = ?",
        (EXPERIMENT_ID,)).fetchone()
    if row and row[0].upper().replace("_", " ") in FINAL_VERDICTS:
        return row[0]
    return None


def persist(conn, status: str, result: Dict[str, object]) -> None:
    initialize_experiment_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO experiments (
          experiment_id, method_version, name, experiment_type, status,
          description, created_by, statement, mechanism, expected_effect,
          population, metric, minimum_detectable_effect, hypothesis_source,
          source_reference, baseline_name, baseline_evaluator,
          candidate_name, candidate_evaluator, label_version, code_version,
          protocol_json, criteria_json, fingerprint, notes_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        EXPERIMENT_ID, "anchor-v2", "D20 reversal, protected validation",
        "hypothesis_validation", status,
        "Single frozen test of the 25.9A d20 reversal hypothesis under anchor-v2 labels.",
        "phase-25.9b",
        "Higher trailing 60-day return predicts lower abnormal return over trading days 5->20.",
        "Long-horizon reversal: past winners mean-revert relative to peers.",
        "negative cross-sectional rank IC",
        "instruments sharing a date in the protected window",
        "cross_sectional_rank_ic", MAX_MDE, "phase-25.9a",
        f"docs/PHASE_25_9B_PREREGISTRATION.md@{PREREGISTRATION}",
        "zero_edge", "within_date_permutation",
        "return_60d_reversal", "cross_sectional_spearman",
        "v2", code_version(),
        json.dumps({"window": [PROTECTED_START, PROTECTED_END],
                    "permutations": PERMUTATIONS, "seed": SEED,
                    "one_shot": True}),
        json.dumps({"min_rows": MIN_ROWS, "min_dates": MIN_DATES,
                    "max_mde": MAX_MDE, "alpha": ALPHA, "sign": EXPECTED_SIGN}),
        fingerprint(), json.dumps([result]), now))
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--research", action="store_true",
                        help="NON-CONFIRMATORY run on the research region")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    initialize_experiment_schema(conn)

    print("=" * 72)
    print("D20 reversal validation  (pre-registration 754a7bd)")
    print(f"fingerprint {fingerprint()}")
    print("=" * 72)

    if args.research:
        rows = load_rows(conn, protected=False)
        gate = readiness(rows)
        print("REGION  research  --  NON-CONFIRMATORY (hypothesis was found here)")
        print(f"rows {gate['rows']}  eligible dates {gate['eligible_dates']}  "
              f"MDE {gate['mde']}")
        result = evaluate(rows)
        for key, value in result.items():
            print(f"  {key:22s} {value}")
        return 0

    locked = already_evaluated(conn)
    if locked:
        print(f"REFUSED: the protected window was already evaluated ({locked}). "
              f"It is tested exactly once.")
        return 3

    rows = load_rows(conn, protected=True)
    gate = readiness(rows)
    print("REGION  protected")
    print(f"resolved rows          {gate['rows']}  (need {MIN_ROWS})")
    print(f"eligible dates         {gate['eligible_dates']}  (need {MIN_DATES})")
    print(f"min detectable |IC|    {gate['mde']}  (need <= {MAX_MDE})")

    if not gate["ready"]:
        print("\nPROTECTED WINDOW NOT READY -- no statistic computed, window stays closed")
        for failure in gate["failures"]:
            print(f"  - {failure}")
        persist(conn, "insufficient_data", {"gate": gate, "opened": False})
        return 0

    print("\nREADY. Opening the protected window once.")
    result = evaluate(rows)
    for key, value in result.items():
        print(f"  {key:22s} {value}")
    persist(conn, result["verdict"].lower().replace(" ", "_"),
            {"gate": gate, "opened": True, **result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
