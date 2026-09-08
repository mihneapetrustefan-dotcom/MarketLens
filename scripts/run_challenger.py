#!/usr/bin/env python3
"""
scripts/run_challenger.py
-----------------------------------
Phase 24 — turn research candidates into challengers and compare them
fairly against a versioned baseline.

    --candidates        Phase 23 candidates, and whether each qualifies
    --create ID         build a challenger from a candidate
    --queue-run ID      queue it for evaluation
    --work              evaluate what is queued, within the limits
    --list              every challenger, rejected ones included
    --compare ID        baseline beside challenger, not ranked
    --detail ID         the full chain: candidate to verdict
    --review ID         record a human decision (the only exit)
    --queue             queue depth and state
    --audit             who did what, and why
    --check             the §82 integrity queries

NOTHING WRITES WITHOUT --apply, except a review, which is an explicit
human act and says so.

WHAT THIS CANNOT DO
-----------------------
Promote anything to production, change a model, strategy, threshold,
risk limit or capital figure, or place an order. There is no flag for
it and no code path behind one. The furthest it reaches is
PAPER_CANDIDATE, which is a label a person applies and which executes
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from src.challengers import api, evaluation, registry, workflow  # noqa: E402
from src.domain.challenger_models import (  # noqa: E402
    ChallengerLimits, ReviewOutcome,
)

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")
WIDTH = 74


def head(title: str) -> None:
    print("\n--- %s %s" % (title, "-" * max(0, WIDTH - len(title) - 5)))


def wrap(text: str, indent: str = "  ") -> str:
    return textwrap.fill(" ".join(str(text).split()), width=WIDTH,
                         initial_indent=indent, subsequent_indent=indent)


def num(value, digits=4) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return "{:,}".format(value)
    try:
        return "%+0.*f" % (digits, float(value))
    except (TypeError, ValueError):
        return str(value)


# ======================================================================

def show_candidates(conn) -> None:
    rows = api.candidates(conn)
    head("PHASE 23 CANDIDATES")
    if not rows:
        print("  none. Autonomous research has produced no candidate yet.")
        return
    for row in rows:
        mark = "qualifies" if row["qualifies"] else "does NOT qualify"
        built = " · challenger exists" if row["has_challenger"] else ""
        print("\n  %s  [%s%s]" % (row["candidate_id"], mark, built))
        print(wrap(row["name"][:200], "    "))
        for problem in row["blocking_reasons"]:
            print(wrap("blocked: " + problem, "      "))
    print()
    print(wrap("Not every candidate becomes a challenger. Building one costs "
               "walk-forward folds, robustness slices and a sweep; spending "
               "that on evidence that cannot support it fills the record "
               "with work nobody can act on.", "  "))


def show_create(conn, candidate_id, apply_it, force) -> None:
    if not apply_it:
        from src.domain.challenger_models import validate_candidate
        from src.autoresearch import api as research_api
        found = {row["candidate_id"]: row
                 for row in research_api.candidates(conn, limit=500)}
        candidate = found.get(candidate_id)
        head("DRY RUN")
        if candidate is None:
            print("  no candidate %s exists" % candidate_id)
            return
        problems = validate_candidate(candidate)
        print("  would create a challenger from %s" % candidate_id)
        for problem in problems:
            print(wrap("blocked: " + problem, "    "))
        print("\n" + wrap("Nothing was written. Pass --apply.", "  "))
        return

    challenger = api.create(conn, candidate_id, force=force)
    head("CHALLENGER CREATED")
    print("  %s v%d" % (challenger["challenger_id"], challenger["version"]))
    print(wrap(challenger["name"][:200], "    "))
    print("\n  baseline   %s  [%s]" % (challenger["baseline"]["name"],
                                       challenger["baseline"]["version"]))
    print("  change     %s" % challenger["change"]["summary"])
    print("  complexity %s x baseline" % challenger["complexity_ratio"])
    print("  cutoff     %s" % challenger["dataset_cutoff"])
    for note in challenger["notes"]:
        print(wrap(note, "    "))


def show_work(conn, apply_it, limits) -> None:
    if not apply_it:
        print(wrap("Dry run: would evaluate the queued challengers. Pass "
                   "--apply to run them.", "  "))
        return
    report = workflow.run_queued(conn, limits=limits)
    head("EVALUATION PASS")
    if report["reclaimed"]:
        print("  reclaimed %d abandoned item(s)" % len(report["reclaimed"]))
    for item in report["evaluated"]:
        print("  %s  ->  %s  (effect %s)"
              % (item["challenger_id"], item["decision"].upper(),
                 num(item["effect"])))
    for queue_id in report["failed"]:
        print("  %s failed" % queue_id)
    if not report["evaluated"] and not report["failed"]:
        print("  nothing ran.")
    print()
    print(wrap(report["termination_reason"]))


def show_list(conn) -> None:
    rows = api.challengers(conn)
    head("CHALLENGERS (%d)" % len(rows))
    if not rows:
        print("  none.")
        return
    for row in rows:
        print("\n  %s v%d  [%s]" % (row["challenger_id"], row["version"],
                                    row["status"]))
        print(wrap(row["name"][:180], "    "))
        print("    baseline %s (%s)" % (row["baseline_name"],
                                        row["baseline_version"]))
        if row["decision"]:
            print("    verdict  %s   effect %s   interval [%s, %s]"
                  % (row["decision"].upper(), num(row["effect"]),
                     num(row["effect_low"]), num(row["effect_high"])))
        else:
            print("    verdict  not evaluated")
    rejected = sum(1 for r in rows if r["status"] == "rejected")
    print()
    print(wrap("%d of %d are rejected and remain listed. A challenger record "
               "filtered to its winners is not a record."
               % (rejected, len(rows)), "  "))


def show_compare(conn, challenger_id) -> None:
    report = api.comparison(conn, challenger_id)
    if report is None:
        print("  no challenger %s" % challenger_id)
        return

    head("BASELINE vs CHALLENGER")
    print("  baseline   %s  [%s]" % (report["baseline"]["name"],
                                     report["baseline"]["version"]))
    print("  challenger %s" % report["challenger"]["name"][:60])
    print("  change     %s" % report["change"]["summary"])

    if report.get("result") is None:
        print("\n" + wrap("Not evaluated yet.", "  "))
        return

    print()
    print("  %-24s %14s %14s %14s" % ("metric", "baseline", "challenger",
                                      "difference"))
    for row in report["side_by_side"]:
        print("  %-24s %14s %14s %14s"
              % (row["metric"][:24], num(row["baseline"]),
                 num(row["challenger"]), num(row["difference"])))

    result = report["result"]
    head("SCORECARD (six dimensions, no total)")
    for name, dimension in (result.get("scorecard") or {}).items():
        print("  %-12s %-9s %s" % (name, dimension["verdict"],
                                   dimension["detail"][:44]))
    print()
    print(wrap("There is deliberately no overall score. A challenger that "
               "wins on return and loses on complexity is a trade-off for a "
               "person to weigh; one number would hide which half you are "
               "buying.", "  "))

    head("CONTEXTS (preserved, never averaged)")
    for context in report["contexts"]:
        print("  %-11s %-26s base %s  chal %s  diff %s  n=%s"
              % (context["kind"], str(context["label"])[:26],
                 num(context["baseline_metric"]),
                 num(context["challenger_metric"]),
                 num(context["effect"]), context["sample_size"]))

    head("DECISION: %s" % result["decision"].upper())
    for reason in result["reasons"]:
        print(wrap(reason))
    head("LIMITATIONS")
    for limitation in result["limitations"]:
        print(wrap(limitation))
    print()
    print(wrap(report["research_result_is_not_production_approval"], "  "))


def show_detail(conn, challenger_id) -> None:
    record = api.detail(conn, challenger_id)
    if record is None:
        print("  no challenger %s" % challenger_id)
        return
    head("CHALLENGER %s v%d" % (record["challenger_id"], record["version"]))
    print(wrap(record["name"][:200], "  "))
    print("\n  status     %s" % record["status"])
    print("  baseline   %s  [%s]" % (record["baseline"]["name"],
                                     record["baseline"]["version"]))
    print("  change     %s" % record["change"]["summary"])
    print("  cutoff     %s" % record["dataset_cutoff"])
    print("  code       %s" % record["code_version"])
    print("  family     %s (%d challenger(s))"
          % (record["family_id"] or "—", record["family_challenger_count"]))

    head("LINEAGE")
    lineage = record["lineage"]
    print("  candidate   %s" % record["candidate_id"])
    print("  conclusion  %s" % record["conclusion_id"])
    print("  hypothesis  %s" % record["hypothesis_id"])
    print("  experiment  %s" % record["experiment_id"])
    hypothesis = lineage.get("hypothesis") or {}
    if hypothesis.get("statement"):
        print(wrap("claim: " + hypothesis["statement"], "    "))
    if hypothesis.get("mechanism"):
        print(wrap("mechanism: " + hypothesis["mechanism"][:400], "    "))

    head("VERSIONS")
    for version in record["versions"]:
        print("  v%-3d %-16s %s" % (version["version"], version["status"],
                                    version["change_summary"][:44]))

    head("RUNS")
    for run in record["runs"]:
        print("  %s  %-10s %-9s rows %-7s %s"
              % (run["run_id"], run["environment"], run["status"],
                 run["rows_examined"],
                 "CACHED" if run["cache_hit"] else ""))

    head("REVIEWS")
    if not record["reviews"]:
        print("  none. This challenger has not been reviewed by a person.")
    for review in record["reviews"]:
        print("  %s  %s  by %s" % (review["reviewed_at"][:19],
                                   review["outcome"], review["reviewer"]))
        print(wrap(review["reason"], "      "))


def show_review(conn, challenger_id, outcome, reviewer, reason) -> None:
    record = api.detail(conn, challenger_id)
    if record is None:
        print("  no challenger %s" % challenger_id)
        return
    result = workflow.review(conn, challenger_id, record["version"],
                             outcome=ReviewOutcome(outcome),
                             reviewer=reviewer, reason=reason)
    head("REVIEW RECORDED")
    print("  %s -> %s" % (result["outcome"], result["status"]))
    print(wrap(result["note"], "  "))


def show_queue(conn) -> None:
    head("CHALLENGER QUEUE")
    print("  " + "  ".join("%s=%d" % (k, v)
                           for k, v in workflow.depth(conn).items() if v))
    for item in api.queue(conn, limit=20):
        print("\n  [%s] %s v%s" % (item["state"], item["challenger_id"],
                                   item["challenger_version"]))
        if item["reason"]:
            print(wrap(item["reason"], "      "))


def show_audit(conn) -> None:
    head("AUDIT TRAIL")
    for row in workflow.trail(conn, limit=30):
        print("  %s  %-18s %-8s %s"
              % (row["occurred_at"][:19], row["action"], row["actor"],
                 (row["decision"] or "")[:20]))
        if row["reason"]:
            print(wrap(row["reason"][:200], "      "))


def show_check(conn) -> None:
    head("INTEGRITY (every count must be zero)")
    failures = 0
    for key, value in api.integrity_check(conn).items():
        print("  %-46s %s%s" % (key, value, "   <-- FAIL" if value else ""))
        failures += 1 if value else 0
    print()
    print("  %s" % ("all clear" if not failures
                    else "%d check(s) failed" % failures))


# ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="create from a candidate that does not qualify")

    parser.add_argument("--candidates", action="store_true")
    parser.add_argument("--create", metavar="CANDIDATE_ID")
    parser.add_argument("--queue-run", metavar="CHALLENGER_ID")
    parser.add_argument("--work", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--compare", metavar="CHALLENGER_ID")
    parser.add_argument("--detail", metavar="CHALLENGER_ID")
    parser.add_argument("--queue", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--check", action="store_true")

    parser.add_argument("--review", metavar="CHALLENGER_ID")
    parser.add_argument("--outcome",
                        choices=[o.value for o in ReviewOutcome])
    parser.add_argument("--reviewer")
    parser.add_argument("--reason")

    parser.add_argument("--max-runs", type=int, default=10)
    parser.add_argument("--max-concurrent", type=int, default=1)

    args = parser.parse_args()
    if not os.path.exists(args.db):
        print("EROARE: baza nu exista: %s" % args.db)
        return 1

    conn = sqlite3.connect(args.db)
    limits = ChallengerLimits(max_runs_per_challenger=args.max_runs,
                              max_concurrent_jobs=args.max_concurrent)
    try:
        if args.review:
            if not (args.outcome and args.reviewer and args.reason):
                print("a review needs --outcome, --reviewer and --reason. "
                      "An approval nobody signed is not an approval.")
                return 1
            show_review(conn, args.review, args.outcome, args.reviewer,
                        args.reason)
            return 0

        chose = any([args.candidates, args.create, args.queue_run, args.work,
                     args.list, args.compare, args.detail, args.queue,
                     args.audit, args.check])
        if not chose:
            parser.print_help()
            return 0

        if args.candidates:
            show_candidates(conn)
        if args.create:
            show_create(conn, args.create, args.apply, args.force)
        if args.queue_run:
            if not args.apply:
                print(wrap("Dry run: would queue %s. Pass --apply."
                           % args.queue_run, "  "))
            else:
                queued = api.run(conn, args.queue_run)
                print(wrap("queued %s v%s" % (queued["challenger_id"],
                                              queued["version"]), "  "))
        if args.work:
            show_work(conn, args.apply, limits)
        if args.list:
            show_list(conn)
        if args.compare:
            show_compare(conn, args.compare)
        if args.detail:
            show_detail(conn, args.detail)
        if args.queue:
            show_queue(conn)
        if args.audit:
            show_audit(conn)
        if args.check:
            show_check(conn)

        print("\n" + wrap("Research only. No production model, strategy, "
                          "threshold, risk limit or capital figure changed. "
                          "The furthest this reaches is PAPER_CANDIDATE, "
                          "which executes nothing.", "  "))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
