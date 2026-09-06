#!/usr/bin/env python3
"""
scripts/run_experiment.py
-------------------------------
The Experiment Lab, from the command line (Phase 22).

    hypothesis -> baseline vs candidate -> chronological split
        -> out-of-sample effect -> robustness -> decision

WHAT IT WILL NOT DO
-----------------------
It changes no production model, strategy, threshold, feature, risk
limit or capital figure (§79). A PASS is a research verdict, not a
deployment: promotion remains the separate, human decision Phase 18
built.

It also does not generate experiments on its own. `--propose` writes
DRAFTS from memory patterns and recurring errors and stops there —
§76 allows exposing the pathway and requires execution to stay
human-controlled.

    python scripts/run_experiment.py --list
    python scripts/run_experiment.py --propose
    python scripts/run_experiment.py --propose --apply
    python scripts/run_experiment.py --run exp-abc123
    python scripts/run_experiment.py --detail exp-abc123
    python scripts/run_experiment.py --sweep exp-abc123 --parameter threshold \\
        --values 0.3,0.4,0.5,0.6,0.7,0.8
    python scripts/run_experiment.py --families
    python scripts/run_experiment.py --compare exp-a,exp-b,exp-c
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.experiment_models import ExperimentStatus
from src.experiments import api, engine, evaluators, templates

DEFAULT_DB = os.path.join("data", "marketlens.db")


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def wrap(text: str, width: int = 68, indent: str = "  "):
    words, current = text.split(), ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            print(indent + current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        print(indent + current)


def show_result(result: dict) -> None:
    if not result:
        return
    line("RESULT")
    base = result.get("baseline_out_of_sample") or {}
    cand = result.get("candidate_out_of_sample") or {}
    fmt = lambda v, p=4: f"{v:+.{p}f}" if isinstance(v, (int, float)) else "—"
    print(f"  metric                     {result.get('metric')}")
    print(f"  baseline  out-of-sample    n={base.get('sample_size', 0):<6} "
          f"{fmt(base.get('directional_accuracy'))}")
    print(f"  candidate out-of-sample    n={cand.get('sample_size', 0):<6} "
          f"{fmt(cand.get('directional_accuracy'))}")
    print(f"  effect    out-of-sample    {fmt(result.get('effect'))}")
    print(f"  effect    in-sample        {fmt(result.get('effect_in_sample'))}")
    gap = result.get("overfitting_gap")
    if gap is not None:
        print(f"  overfitting gap            {fmt(gap)}"
              + ("   <-- fitted its training half" if gap > 0.05 else ""))
    if result.get("effect_low") is not None:
        print(f"  95% interval               [{fmt(result['effect_low'])}, "
              f"{fmt(result['effect_high'])}]")
    print(f"  robust across slices       {result.get('robust_slices_passing')}"
          f"/{result.get('robust_slices')}")
    print(f"  complexity ratio           {result.get('complexity_ratio')}")

    decision = str(result.get("decision", "")).upper()
    line(f"DECISION: {decision}")
    for reason in result.get("reasons", ()):
        wrap(reason)
        print()
    if result.get("limitations"):
        line("LIMITATIONS")
        for limitation in result["limitations"]:
            wrap(limitation)
            print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--status", default=None)
    parser.add_argument("--detail", default=None, metavar="EXPERIMENT_ID")
    parser.add_argument("--propose", action="store_true",
                        help="Draft experiments from memory patterns and "
                             "recurring errors. Never runs them.")
    parser.add_argument("--run", default=None, metavar="EXPERIMENT_ID")
    parser.add_argument("--cancel", default=None, metavar="EXPERIMENT_ID")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true",
                        help="Recompute even if an identical run exists.")
    parser.add_argument("--sweep", default=None, metavar="EXPERIMENT_ID",
                        help="Sensitivity sweep over one parameter.")
    parser.add_argument("--parameter", default="threshold")
    parser.add_argument("--values", default="0.3,0.4,0.5,0.6,0.7,0.8")
    parser.add_argument("--ablate", default=None, metavar="EXPERIMENT_ID")
    parser.add_argument("--components", default="")
    parser.add_argument("--families", action="store_true")
    parser.add_argument("--templates", action="store_true")
    parser.add_argument("--evaluators", action="store_true")
    parser.add_argument("--compare", default=None, metavar="ID,ID,ID")
    parser.add_argument("--apply", action="store_true",
                        help="Write. Without it, --propose is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_experiment_schema(conn)

    # ---- catalogue --------------------------------------------------
    if args.templates:
        line("TEMPLATES")
        for template in api.available_templates():
            mark = "" if template.get("runnable") else "   [cannot run here]"
            print(f"  {template['name']:20s} {template['description']}{mark}")
            if template.get("requires"):
                print(f"  {'':20s}   requires: {template['requires']}")
        conn.close()
        return 0

    if args.evaluators:
        line("EVALUATORS")
        for spec in api.available_evaluators():
            mark = "" if spec["runnable"] else "   [cannot run here]"
            print(f"  {spec['name']:28s} {spec['description'][:60]}{mark}")
            if spec["unavailable_reason"]:
                wrap(spec["unavailable_reason"], indent="    ")
        print()
        print("  Evaluators are named, never supplied as code. An arm that")
        print("  names an unregistered evaluator is refused (§80).")
        conn.close()
        return 0

    if args.families:
        line("HYPOTHESIS FAMILIES")
        rows = api.families(conn)
        if not rows:
            print("  No families yet.")
        for family in rows:
            print(f"  {family['name']}")
            print(f"    experiments {family['experiments']:<4} "
                  f"comparisons {family['comparisons']:<4} "
                  f"passed {family['passed']:<4} "
                  f"expected false positives {family['expected_false_positives']}")
        print()
        wrap("A family with one experiment is a pre-registered test. A family "
             "with fifty is a search, and its winner is not the same evidence.")
        conn.close()
        return 0

    # ---- propose (§21, §22, §69, §70, §76) --------------------------
    if args.propose:
        proposals = templates.propose_all(conn, created_by="cli")
        line("PROPOSED (DRAFT ONLY)")
        if not proposals:
            print("  Nothing to propose: no pattern clears the sample")
            print("  threshold and no error recurs often enough.")
        for proposal in proposals:
            print(f"  {proposal.experiment_id}  [{proposal.hypothesis.source.value}]")
            print(f"    {proposal.name}")
            print(f"    changes: {', '.join(proposal.changed_variables)}")
        print()
        print("  These are DRAFTS. Nothing was run. Generating is not")
        print("  executing, and a proposal from memory is a hypothesis mined")
        print("  from the record it will be tested against — which is stated")
        print("  in each mechanism.")
        if args.apply:
            for proposal in proposals:
                api.create(conn, proposal)
            print()
            print(f"  {len(proposals)} draft(s) written. Run one with --run.")
        else:
            print()
            print("  Add --apply to store them.")
        conn.close()
        return 0

    # ---- run --------------------------------------------------------
    if args.run:
        print("=" * 72)
        print("MarketLens - experiment lab")
        print("PASS means the predefined criteria were met. It does not mean")
        print("profitable, and it does not mean deploy.")
        print("=" * 72)
        experiment = api.load(conn, args.run)
        if experiment is None:
            print(f"No experiment {args.run!r}.")
            conn.close()
            return 1

        line("HYPOTHESIS")
        wrap(experiment.hypothesis.statement)
        print()
        print("  MECHANISM")
        wrap(experiment.hypothesis.mechanism, indent="    ")
        print()
        print(f"  metric      {experiment.hypothesis.metric}")
        print(f"  baseline    {experiment.baseline.name} "
              f"({experiment.baseline.evaluator})")
        print(f"  candidate   {experiment.candidate.name} "
              f"({experiment.candidate.evaluator})")
        print(f"  changes     {', '.join(experiment.changed_variables)}")
        print(f"  fingerprint {experiment.fingerprint[:16]}")

        line("CRITERIA (fixed before this run)")
        for key, value in experiment.criteria.as_dict().items():
            if value not in ("", None):
                print(f"  {key:32s} {value}")

        try:
            outcome = api.start(conn, args.run, seed=args.seed,
                                allow_cache=not args.no_cache)
        except engine.DefinitionChanged as error:
            line("REFUSED")
            wrap(str(error))
            conn.close()
            return 2

        run_record = outcome["run"]
        line("RUN")
        print(f"  {run_record['run_id']}  {run_record['status']}  "
              f"seed={run_record['seed']}  rows={run_record['rows_examined']:,}"
              f"  {run_record.get('duration_seconds') or 0:.2f}s")
        if run_record["cache_hit"]:
            print(f"  REUSED from {run_record['cached_from_run']} — identical")
            print("  fingerprint and seed. Nothing was recomputed.")
        if run_record["error"]:
            print(f"  ERROR: {run_record['error']}")

        show_result(outcome["result"])
        print()
        print("  Nothing in production changed. Promotion is a separate,")
        print("  human decision.")
        conn.close()
        return 0

    if args.cancel:
        result = api.cancel(conn, args.cancel)
        line("CANCELLED")
        print(f"  {result['runs_cancelled']} run(s) cancelled")
        wrap(result["note"])
        conn.close()
        return 0

    # ---- sensitivity (§51, §52) -------------------------------------
    if args.sweep:
        experiment = api.load(conn, args.sweep)
        if experiment is None:
            print(f"No experiment {args.sweep!r}.")
            conn.close()
            return 1
        values = []
        for raw in args.values.split(","):
            raw = raw.strip()
            try:
                values.append(float(raw))
            except ValueError:
                values.append(raw)
        try:
            surface = engine.sensitivity(conn, experiment, args.parameter, values)
        except engine.ResourceLimitExceeded as error:
            print(f"REFUSED: {error}")
            conn.close()
            return 2

        line(f"SENSITIVITY — {args.parameter}")
        print(f"  {'value':>8}  {'n':>6}  {'metric':>10}  {'effect':>10}")
        for point in surface["surface"]:
            if "error" in point:
                print(f"  {str(point['value']):>8}  {point['error'][:50]}")
                continue
            fmt = lambda v: f"{v:+.4f}" if isinstance(v, (int, float)) else "—"
            print(f"  {str(point['value']):>8}  {point['sample_size']:>6}  "
                  f"{fmt(point['metric']):>10}  {fmt(point['effect']):>10}")
        print()
        print(f"  shape: {surface['shape'].upper()}  "
              f"({surface['values_clearing_threshold']} of "
              f"{surface['values_tested']} clear the threshold)")
        if surface["note"]:
            wrap(surface["note"])
        conn.close()
        return 0

    # ---- ablation (§53) ---------------------------------------------
    if args.ablate:
        experiment = api.load(conn, args.ablate)
        if experiment is None:
            print(f"No experiment {args.ablate!r}.")
            conn.close()
            return 1
        components = [c.strip() for c in args.components.split(",") if c.strip()]
        if not components:
            components = list(experiment.candidate.parameters)
        report = engine.ablation(conn, experiment, components)
        line("ABLATION")
        print(f"  full candidate {report['metric']}: {report['full_value']}")
        for component, data in report["components"].items():
            if "error" in data:
                print(f"  without {component:20s} {data['error']}")
                continue
            print(f"  without {component:20s} {data['without_value']}"
                  f"   contribution {data['contribution']}")
            if data.get("note"):
                wrap(data["note"], indent="    ")
        conn.close()
        return 0

    # ---- comparison (§64, §65) --------------------------------------
    if args.compare:
        ids = [i.strip() for i in args.compare.split(",") if i.strip()]
        report = api.compare(conn, ids)
        line("COMPARISON")
        for row in report["experiments"]:
            fmt = lambda v: f"{v:+.4f}" if isinstance(v, (int, float)) else "—"
            print(f"  {row['experiment_id']}  {str(row['decision']).upper():<13}"
                  f" effect {fmt(row['effect'])}  gap {fmt(row['overfitting_gap'])}"
                  f"  n={row['sample']}  robust {row['robust']}")
            print(f"    {row['name'][:64]}")
        print()
        wrap(report["note"])
        conn.close()
        return 0

    # ---- detail -----------------------------------------------------
    if args.detail:
        record = api.detail(conn, args.detail)
        if record is None:
            print(f"No experiment {args.detail!r}.")
            conn.close()
            return 1
        line("EXPERIMENT")
        print(f"  {record['experiment_id']}  {record['status']}")
        print(f"  {record['name']}")
        print()
        print("  HYPOTHESIS")
        wrap(record["statement"], indent="    ")
        print("  MECHANISM")
        wrap(record["mechanism"], indent="    ")
        print()
        print(f"  baseline   {record['baseline_name']} ({record['baseline_evaluator']})")
        print(f"  candidate  {record['candidate_name']} ({record['candidate_evaluator']})")
        print(f"  changed    {record.get('changed_variables')}")
        print(f"  dataset    {record['dataset_snapshot_id']}")
        print(f"  versions   dataset={record['dataset_version'] or '—'} "
              f"features={record['feature_version'] or '—'} "
              f"code={record['code_version'] or '—'}")
        if record.get("family"):
            family = record["family"]
            print(f"  family     {family['experiments']} experiment(s), "
                  f"{family['comparisons']} comparison(s)")
        for result in record["results"][:1]:
            show_result(result)
        conn.close()
        return 0

    # ---- default: the lab -------------------------------------------
    line("EXPERIMENT LAB")
    stats = api.summary(conn)
    print(f"  experiments                {stats['experiments']:,}")
    print(f"  runs                       {stats['runs']:,}  "
          f"(cache hits {stats['cache_hits']:,})")
    print(f"  families                   {stats['families']:,}")
    print(f"  comparisons                {stats['total_comparisons']:,}")
    print(f"  expected false positives   {stats['expected_false_positives']}")
    print(f"  evaluators                 {stats['runnable_evaluators']} runnable "
          f"of {stats['declared_evaluators']} declared")
    if stats["by_decision"]:
        print(f"  decisions                  {stats['by_decision']}")
    if stats["by_status"]:
        print(f"  statuses                   {stats['by_status']}")

    rows = api.list_experiments(conn, status=args.status, limit=25)
    if rows:
        line("EXPERIMENTS")
        for row in rows:
            print(f"  {row['experiment_id']}  {row['status']:<13} "
                  f"{row['name'][:52]}")

    line("INTEGRITY (§88 — every count must be zero)")
    problems = api.integrity_check(conn)
    for name, count in problems.items():
        flag = "" if count == 0 else "   <-- INVESTIGATE"
        print(f"  {name:44s} {count}{flag}")
    conn.close()
    return 1 if any(problems.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
