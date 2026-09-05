#!/usr/bin/env python3
"""
scripts/predict.py
------------------------
Score current observations with a trained model (Phase 9, inference).

THE STAGE THAT WAS MISSING
------------------------------
`train_models.py` predicts on the held-out TEST SLICE of a
chronological split. That measures a model; it cannot use one. The
test slice ends in the past by construction, so every prediction in
the database was about something that had already happened.

Measured on 2026-09-04: newest observation 2026-09-03, newest
PREDICTED observation 2026-08-26, and all 190 signals suppressed as
`stale_prediction`. Thirteen observations from the previous week had
no prediction at all, because nothing existed to make one.

This is that thing. It loads a trained model, finds observations it
has not scored, builds the design matrix TO THE MODEL'S OWN STORED
COLUMN ORDER, and writes predictions carrying the observation's
information cutoff.

WHERE IT SITS
-----------------
    compute_features  ->  train_models  ->  [ predict ]  ->  generate_signals

Run it after training, before signal generation. It does not train.

IT DOES, SINCE PHASE 18, JUDGE
----------------------------------
The sentence that used to sit here read: *it does not judge -- a model
with no edge scores just as willingly as a good one*. That was
accurate and it was the bug. `beats_all_baselines` existed and nothing
consulted it, so a model worse than predicting the mean scored 422
observations that became signals on a public page.

Selection now goes through `src/modeling/selection.py`, which asks the
evaluator's own `is_deployable`. By default only a model a human
promoted may score. `--experimental` scores with an unpromoted one for
research, says so loudly, and marks the report.

WHAT IT REFUSES
-------------------
  - a model with no stored feature contract
  - observations carrying too little of that contract
  - features computed after the cutoff they claim to describe

All three are refusals rather than warnings, because each produces
confident numbers that nothing downstream can tell apart from real
ones. The logic and its reasoning live in `src/modeling/inference.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.model_schema import initialize_model_schema
from src.modeling.inference import (
    FeatureContractBroken, NoUsableModel, save_predictions, score,
)
from src.modeling.selection import NoValidatedModel, SelectionPolicy

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--model", default=None, metavar="TRAINED_MODEL_ID",
                        help="Pin a specific model. Default: the most "
                             "recently trained one for --label.")
    parser.add_argument("--label", default="d5.abnormal_return",
                        help="Only consider models fitted against this label, "
                             "so scoring cannot pick up a model trained on a "
                             "different target.")
    parser.add_argument("--max-age-days", type=float, default=30.0,
                        help="How far back to reach. Scoring an older event "
                             "produces a prediction the signal layer will "
                             "suppress as stale anyway. 0 = no bound.")
    parser.add_argument("--min-feature-coverage", type=float, default=0.5,
                        help="Fraction of the model's features an observation "
                             "must carry to be scored at all.")
    parser.add_argument("--rescore", action="store_true",
                        help="Also re-score observations this model has "
                             "already predicted.")
    parser.add_argument("--experimental", action="store_true",
                        help="Score with a model that has NOT been promoted. "
                             "For research and for keeping the chain "
                             "exercised while no model passes the gate. "
                             "Everything produced is marked experimental.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a "
                             "dry run.")
    args = parser.parse_args()

    policy = (SelectionPolicy.EXPERIMENTAL if args.experimental
              else SelectionPolicy.ACTIVE_ONLY)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_model_schema(conn)

    print("=" * 72)
    print("MarketLens - score observations with a trained model")
    print("This APPLIES a model. It does not train one and does not judge one.")
    if not args.apply:
        print("MODE: DRY RUN - nothing is written")
    print("=" * 72)

    try:
        predictions, report = score(
            conn,
            trained_model_id=args.model,
            label_name=args.label or None,
            max_age_days=(None if args.max_age_days == 0 else args.max_age_days),
            min_feature_coverage=args.min_feature_coverage,
            rescore=args.rescore,
            policy=policy)
    except NoValidatedModel as error:
        line("NO VALIDATED MODEL AVAILABLE")
        print(error.report())
        print()
        print("  Nothing was written. This is the gate working, not a")
        print("  failure to configure: scoring with a model that does not")
        print("  beat its baselines produces numbers indistinguishable from")
        print("  real ones. Promote a model (scripts/promote_model.py) or")
        print("  pass --experimental to score for research.")
        return 1
    except NoUsableModel as error:
        line("NO MODEL")
        print(f"  {error}")
        return 1
    except FeatureContractBroken as error:
        line("REFUSED - FEATURE CONTRACT")
        print(f"  {error}")
        print()
        print("  Not downgraded to a warning on purpose: applying positional")
        print("  coefficients to misaligned columns returns numbers that look")
        print("  exactly like real predictions.")
        return 1

    line("MODEL")
    print(f"  {report.model_qualified_id}  ({report.trained_model_id})")
    print(f"  status                   {report.model_status}"
          f"   verdict: {report.model_verdict}")
    if report.is_experimental:
        print()
        print("  " + "!" * 66)
        print("  EXPERIMENTAL. This model has not been promoted, so these")
        print("  predictions are research output, not production output.")
        for reason in report.eligibility_reasons:
            print(f"    - {reason}")
        print("  " + "!" * 66)

    line("SCORING")
    print(f"  candidate observations   {report.candidates:,}")
    print(f"  scored                   {report.scored:,}")
    print(f"  abstained                {report.abstained:,}")
    print(f"  skipped, thin features   {report.skipped_no_features:,}")
    if report.skipped_leaky_features:
        print(f"  skipped, LEAKY features  {report.skipped_leaky_features:,}"
              f"   <-- computed after their own cutoff")
    if report.newest_cutoff:
        age = (datetime.now(timezone.utc) - report.newest_cutoff).days
        print(f"  newest cutoff scored     {report.newest_cutoff.isoformat()[:19]}"
              f"  ({age}d old)")

    if not predictions:
        line("NOTHING TO SCORE")
        print("  Every eligible observation already carries a prediction from")
        print("  this model. Use --rescore to redo them, or run the upstream")
        print("  stages to produce new observations.")
        return 0

    if not args.apply:
        line("DRY RUN")
        print(f"  {len(predictions):,} prediction(s) would be written.")
        print("  Add --apply to write them.")
        return 0

    written = save_predictions(conn, predictions)
    line("WRITTEN")
    print(f"  {written:,} prediction(s)")
    print()
    print("  Next: scripts/generate_signals.py --apply")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
