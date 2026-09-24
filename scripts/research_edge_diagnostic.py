"""
scripts/research_edge_diagnostic.py
-------------------------------------------
Phase 25.9A Stage 1: does the feature set contain information at all,
and in what shape?

Implements docs/PHASE_25_9A_PREREGISTRATION.md Stage 1, committed as
1938923 before this script was run.

EXPLORATORY, NOT A MODEL
----------------------------
This fits nothing. It measures rank correlation between each feature
and each target, pooled and within-date, and asks whether the best of
468 such correlations is larger than the best of 468 correlations on
shuffled data. If it is not, there is no detectable information, and
no model complexity will manufacture some.

IMPLEMENTATION CHOICE, DISCLOSED
------------------------------------
Missing feature values (0-6% per feature) are median-imputed over the
research region. The permutation null is computed through the IDENTICAL
pipeline, so any effect of imputation is present in both the observed
statistic and the null distribution; the test remains valid. Target
rows with no label are dropped per target, never imputed.

The protected window (cutoff >= 2026-08-15T01:23:31) is never read.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROTECTED_START = "2026-08-15T01:23:31"
TARGETS = ["d1", "d3", "d5", "d10", "d20",
           "intraday_5m", "intraday_15m", "intraday_30m", "intraday_60m"]
MIN_ROWS_PER_DATE = 10
PERMUTATIONS = 500
SEED = 20260913


def rank(values: np.ndarray) -> np.ndarray:
    """Average ranks; ties share the mean of the positions they occupy."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values), dtype=float)
    sorted_vals = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return ranks


def standardise(matrix: np.ndarray) -> np.ndarray:
    centred = matrix - matrix.mean(axis=0)
    scale = centred.std(axis=0)
    scale[scale == 0] = np.inf
    return centred / scale


def load(conn):
    obs = conn.execute("""
        SELECT observation_id, information_cutoff FROM research_observations
        WHERE information_cutoff IS NOT NULL AND information_cutoff < ?
          AND quality_level != 'invalid'
        ORDER BY information_cutoff
    """, (PROTECTED_START,)).fetchall()
    ids = [o[0] for o in obs]
    index = {oid: i for i, oid in enumerate(ids)}
    cutoffs = [o[1] for o in obs]

    features: Dict[str, np.ndarray] = {}
    for name, oid, value in conn.execute(
            "SELECT qualified_name, observation_id, value_json FROM research_features"):
        if oid not in index:
            continue
        try:
            parsed = json.loads(value) if value else None
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
            continue
        column = features.setdefault(name, np.full(len(ids), np.nan))
        column[index[oid]] = float(parsed)

    targets: Dict[str, np.ndarray] = {}
    for horizon in TARGETS:
        column = np.full(len(ids), np.nan)
        for oid, value in conn.execute(
                "SELECT observation_id, value_json FROM research_labels WHERE name = ?",
                (f"{horizon}.abnormal_return",)):
            if oid not in index:
                continue
            try:
                parsed = json.loads(value) if value else None
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
                column[index[oid]] = float(parsed)
        targets[horizon] = column
    return ids, cutoffs, features, targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--permutations", type=int, default=PERMUTATIONS)
    args = parser.parse_args()

    rng = np.random.default_rng(SEED)
    conn = sqlite3.connect(args.db)
    ids, cutoffs, features, targets = load(conn)
    names = sorted(features)
    F = np.column_stack([features[n] for n in names])
    medians = np.nanmedian(F, axis=0)
    missing = np.isnan(F)
    F = np.where(missing, medians, F)
    dates = np.array([c[:10] for c in cutoffs])
    weeks = np.array([datetime.fromisoformat(c).isocalendar()[1] for c in cutoffs])

    print("=" * 76)
    print("Phase 25.9A Stage 1 edge diagnostic  (pre-registration 1938923)")
    print("=" * 76)
    print(f"research rows {len(ids)}   features {len(names)}   targets {len(TARGETS)}")
    print(f"feature missingness imputed: {missing.mean() * 100:.1f}% of cells")

    # Per target, precompute what the permutations need.
    prepared = {}
    for horizon in TARGETS:
        y = targets[horizon]
        rows = np.where(~np.isnan(y))[0]
        yr, Fr, dr = y[rows], F[rows], dates[rows]

        # pooled: standardised global ranks
        pooled_F = standardise(np.column_stack([rank(Fr[:, j])
                                                for j in range(Fr.shape[1])]))
        pooled_y = rank(yr)
        pooled_y = (pooled_y - pooled_y.mean()) / (pooled_y.std() or np.inf)

        # cross-sectional: standardised ranks within each eligible date
        groups = defaultdict(list)
        for k, d in enumerate(dr):
            groups[d].append(k)
        eligible = [np.array(g) for g in groups.values()
                    if len(g) >= MIN_ROWS_PER_DATE]
        cs_F, cs_y = [], []
        for g in eligible:
            block = np.column_stack([rank(Fr[g, j]) for j in range(Fr.shape[1])])
            cs_F.append(standardise(block))
            yy = rank(yr[g])
            cs_y.append((yy - yy.mean()) / (yy.std() or np.inf))
        prepared[horizon] = dict(rows=rows, groups=eligible, pooled_F=pooled_F,
                                 pooled_y=pooled_y, cs_F=cs_F, cs_y=cs_y,
                                 dates=dr)

    def statistics(permute: bool) -> np.ndarray:
        """IC for every (target, formulation, feature): shape (9, 2, 26)."""
        out = np.zeros((len(TARGETS), 2, len(names)))
        for t, horizon in enumerate(TARGETS):
            p = prepared[horizon]
            y = p["pooled_y"]
            if permute:
                y = y.copy()
                groups = defaultdict(list)
                for k, d in enumerate(p["dates"]):
                    groups[d].append(k)
                for g in groups.values():
                    g = np.array(g)
                    y[g] = y[rng.permutation(g)]
            out[t, 0] = (p["pooled_F"] * y[:, None]).mean(axis=0)
            per_date = []
            for block_F, block_y in zip(p["cs_F"], p["cs_y"]):
                yy = rng.permutation(block_y) if permute else block_y
                per_date.append((block_F * yy[:, None]).mean(axis=0))
            out[t, 1] = np.mean(per_date, axis=0) if per_date else 0.0
        return out

    observed = statistics(permute=False)
    observed_best = np.abs(observed).max()
    t_i, f_i, n_i = np.unravel_index(np.abs(observed).argmax(), observed.shape)

    print(f"\nrunning {args.permutations} within-date permutations ...")
    null_max = np.array([np.abs(statistics(permute=True)).max()
                         for _ in range(args.permutations)])
    threshold = float(np.percentile(null_max, 95))
    p_value = float((1 + (null_max >= observed_best).sum()) / (1 + len(null_max)))
    present = observed_best > threshold

    print("\n--- GLOBAL DECISION (pre-registered) ---")
    print(f"observed best |IC|      {observed_best:.4f}  "
          f"({TARGETS[t_i]}, {'POOLED' if f_i == 0 else 'CROSS-SECTIONAL'}, "
          f"{names[n_i]})")
    print(f"null 95th pct of max    {threshold:.4f}")
    print(f"permutation p-value     {p_value:.3f}")
    print(f"INFORMATION             {'PRESENT' if present else 'NOT DETECTED'}")

    print("\n--- best |IC| per target and formulation (descriptive) ---")
    print(f"{'target':14s} {'POOLED':>9s}  {'feature':30s} {'X-SECT':>9s}  feature")
    for t, horizon in enumerate(TARGETS):
        pj = np.abs(observed[t, 0]).argmax()
        cj = np.abs(observed[t, 1]).argmax()
        print(f"{horizon:14s} {observed[t, 0, pj]:>+9.4f}  {names[pj]:30s} "
              f"{observed[t, 1, cj]:>+9.4f}  {names[cj]}")

    pooled_mean = float(np.abs(observed[:, 0]).mean())
    cross_mean = float(np.abs(observed[:, 1]).mean())
    print("\n--- H1: pooled vs cross-sectional ---")
    print(f"mean |IC| pooled          {pooled_mean:.4f}")
    print(f"mean |IC| cross-sectional {cross_mean:.4f}")

    # Weekly sign stability for the single best comparison.
    horizon = TARGETS[t_i]
    p = prepared[horizon]
    row_weeks = weeks[p["rows"]]
    col = names[n_i]
    sign = np.sign(observed[t_i, f_i, n_i])
    stable = []
    for wk in sorted(set(row_weeks)):
        mask = row_weeks == wk
        if mask.sum() < MIN_ROWS_PER_DATE:
            continue
        fx = rank(F[p["rows"]][mask, n_i])
        yx = rank(targets[horizon][p["rows"]][mask])
        if fx.std() == 0 or yx.std() == 0:
            continue
        ic = float(np.corrcoef(fx, yx)[0, 1])
        stable.append((int(wk), round(ic, 4)))
    agree = sum(1 for _w, ic in stable if np.sign(ic) == sign)
    print(f"\n--- weekly stability of the best comparison ({horizon} / {col}) ---")
    for wk, ic in stable:
        print(f"  week {wk}: IC {ic:+.4f}")
    print(f"  sign agrees in {agree}/{len(stable)} weeks")

    if args.out:
        report = {
            "preregistration": "1938923", "seed": SEED,
            "permutations": args.permutations, "research_rows": len(ids),
            "comparisons": int(observed.size),
            "observed_best_abs_ic": float(observed_best),
            "best": {"target": TARGETS[t_i],
                     "formulation": "pooled" if f_i == 0 else "cross_sectional",
                     "feature": names[n_i]},
            "null_95th_percentile": threshold, "p_value": p_value,
            "information_present": bool(present),
            "mean_abs_ic_pooled": pooled_mean,
            "mean_abs_ic_cross_sectional": cross_mean,
            "weekly_stability": stable, "weekly_sign_agreement": agree,
            "ic": {TARGETS[t]: {
                "pooled": dict(zip(names, map(float, observed[t, 0]))),
                "cross_sectional": dict(zip(names, map(float, observed[t, 1]))),
            } for t in range(len(TARGETS))},
        }
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nreport written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
