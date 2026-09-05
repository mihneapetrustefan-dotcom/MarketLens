#!/usr/bin/env python3
"""
scripts/promote_model.py
------------------------------
Inspect model eligibility, and promote a model to ACTIVE.

THE ONLY WAY A MODEL BECOMES PRODUCTION-FACING
--------------------------------------------------
Nothing else in this repository sets `trained_models.status` to
'active'. Not `train_models.py`, not `predict.py`, not the pipeline
workflow. This script is the whole surface, it is manual, and it
requires a name and a reason:

    python scripts/promote_model.py --list
    python scripts/promote_model.py --model tm-abc123 \\
        --approved-by "your name" --reason "why" --apply

WHY IT WILL PROBABLY REFUSE TODAY
-------------------------------------
As of 2026-09-05 every trained model in production carries
`beats_all_baselines = 0` and a negative r-squared. The gate reads the
evaluator's own `is_deployable`, so it will decline all four, and
`--list` will show why for each.

That is the correct outcome, not a configuration problem. The fix for
"no model passes" is a better model, and there is deliberately no flag
here that produces any other answer.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.model_promotion_schema import (
    initialize_model_promotion_schema,
)
from src.data_access.model_schema import initialize_model_schema
from src.modeling.promotion import PromotionRefused, demote, history, promote
from src.modeling.selection import candidates

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def show_candidates(conn, label_name) -> int:
    pool = candidates(conn, label_name)
    if not pool:
        print("  No trained model exists"
              + (f" for label {label_name!r}." if label_name else "."))
        return 0

    line("MODELS")
    for candidate in pool:
        print(f"  {candidate.summary()}")
        print(f"      label      {candidate.label_name}")
        print(f"      trained    {candidate.trained_at[:19]}")
        if candidate.evaluated_at:
            print(f"      evaluated  {candidate.evaluated_at[:19]}"
                  f"  ({candidate.baseline_count} baseline comparison(s))")
        for reason in candidate.reasons:
            print(f"      - {reason}")
        print()

    promotable = [c for c in pool if c.may_be_promoted]
    active = [c for c in pool if c.is_active]
    line("VERDICT")
    print(f"  active now          {len(active)}"
          + (f"  ({active[0].trained_model_id})" if active else ""))
    print(f"  eligible to promote {len(promotable)}")
    if not promotable and not active:
        print()
        print("  No model passes the gate. The evaluator is reporting that")
        print("  none has shown an edge over its baselines on a large enough")
        print("  effective sample. There is no flag here that changes that.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--label", default=None,
                        help="Restrict to models fitted against this label.")
    parser.add_argument("--list", action="store_true",
                        help="Show every model and why it passes or fails.")
    parser.add_argument("--history", action="store_true",
                        help="Show past promotions and demotions.")
    parser.add_argument("--model", default=None, metavar="TRAINED_MODEL_ID")
    parser.add_argument("--approved-by", default=None,
                        help="Who is taking responsibility. Required to "
                             "promote; deliberately has no default.")
    parser.add_argument("--reason", default=None,
                        help="Why. Recorded permanently.")
    parser.add_argument("--demote", action="store_true",
                        help="Withdraw the model from production instead.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_model_schema(conn)
    initialize_model_promotion_schema(conn)

    print("=" * 72)
    print("MarketLens - model promotion")
    print("A model becomes production-facing ONLY through this script.")
    print("=" * 72)

    if args.history:
        records = history(conn, args.model)
        line("PROMOTION HISTORY")
        if not records:
            print("  Nothing has ever been promoted or demoted.")
        for record in records:
            print(f"  {record['promoted_at'][:19]}  {record['action']:8s}"
                  f"  {record['trained_model_id']}"
                  f"  {record['from_status']} -> {record['to_status']}")
            print(f"      by {record['approved_by']}"
                  + (f" @ {record['code_version']}" if record['code_version'] else ""))
            print(f"      {record['reason']}")
        conn.close()
        return 0

    if args.list or not args.model:
        result = show_candidates(conn, args.label)
        if not args.model:
            print()
            print("  To promote:  --model <id> --approved-by <name> "
                  "--reason <why> --apply")
        conn.close()
        return result

    if not args.approved_by or not args.reason:
        line("REFUSED")
        print("  --approved-by and --reason are both required.")
        print("  A promotion without a name and a reason is an automatic")
        print("  promotion with extra steps, which is the thing this gate")
        print("  exists to prevent.")
        conn.close()
        return 2

    action = "DEMOTION" if args.demote else "PROMOTION"
    if not args.apply:
        line(f"DRY RUN - {action}")
        show_candidates(conn, args.label)
        print(f"  Would {action.lower()} {args.model}.")
        print("  Add --apply to write it.")
        conn.close()
        return 0

    try:
        if args.demote:
            record = demote(conn, args.model, approved_by=args.approved_by,
                            reason=args.reason)
            records = [record]
        else:
            records = promote(conn, args.model, approved_by=args.approved_by,
                              reason=args.reason)
    except PromotionRefused as error:
        line("REFUSED")
        print(f"  {error}")
        conn.close()
        return 2

    line("WRITTEN")
    for record in records:
        print(f"  {record.action:8s}  {record.trained_model_id}"
              f"  {record.from_status} -> {record.to_status}")
    print()
    print("  scripts/predict.py will now use this model by default.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
