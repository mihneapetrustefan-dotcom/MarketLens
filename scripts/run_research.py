#!/usr/bin/env python3
"""
scripts/run_research.py
-----------------------------------
Phase 23 — drive the autonomous research loop from the command line.

    --observe     what the researcher can currently see, and cannot
    --questions   raise and triage questions, including the refusals
    --cycle       one bounded pass: observe -> conclude -> remember
    --queue       what is waiting, and why anything was skipped
    --conclusions every finding, negative ones included
    --candidates  promising results awaiting human review
    --families    how each line of research has actually done
    --governance  snooping ledger, protected windows, diversity
    --audit       who did what, and why
    --check       the integrity queries from §88

NOTHING WRITES WITHOUT --apply. The same discipline as every other
script in this project: a run you did not ask to persist does not
persist.

WHAT THIS CANNOT DO
-----------------------
Promote anything, change a model, a strategy, a threshold, a risk
limit or a capital figure, or place an order. There is no flag for it
and no code path behind one.
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

from src.autoresearch import (  # noqa: E402
    api, audit, candidates as candidate_registry, cycle as cycle_layer,
    governance, observations as observation_layer, prioritization,
    questions as question_layer, queue as queue_layer,
)
from src.domain.autoresearch_models import ResearchBudget  # noqa: E402

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")
WIDTH = 74


def head(title: str) -> None:
    print("\n--- %s %s" % (title, "-" * max(0, WIDTH - len(title) - 5)))


def wrap(text: str, indent: str = "  ") -> str:
    return textwrap.fill(" ".join(str(text).split()), width=WIDTH,
                         initial_indent=indent, subsequent_indent=indent)


def pct(value) -> str:
    return "—" if value is None else "%+0.4f" % value


# ======================================================================

def show_observations(conn) -> None:
    found, blind = observation_layer.observe_all(conn)
    head("WHAT THE RESEARCHER CAN SEE")
    for observation in found:
        print("  [%s] %s" % (observation.kind.value, observation.subject[:52]))
        print(wrap(observation.statement, "      "))
    if not found:
        print("  nothing: no detector found anything above its threshold.")

    head("WHAT IT CANNOT SEE")
    for gap in blind:
        print("  %s" % gap["detector"])
        print(wrap(gap["reason"], "      "))
    print()
    print(wrap("A research engine that reports only what it can see, "
               "without saying what it cannot, is describing its "
               "instruments and calling it the world.", "  "))


def show_questions(conn, apply: bool) -> None:
    found, _blind = observation_layer.observe_all(conn)
    triaged = question_layer.raise_questions(conn, found)
    if apply:
        observation_layer.save(conn, found)
        question_layer.save(conn, triaged)

    buckets = {}
    for question, _s, _c in triaged:
        buckets.setdefault(question.triage.value, []).append(question)

    head("QUESTIONS RAISED")
    for state in sorted(buckets):
        print("\n  %s (%d)" % (state.upper(), len(buckets[state])))
        for question in sorted(buckets[state], key=lambda q: -q.priority)[:6]:
            print("    p=%.3f  %s" % (question.priority, question.question[:60]))
            print(wrap(question.triage_reason, "           "))
    print()
    print(wrap("Seven of the eight triage states are ways of saying no. "
               "Recording the refusal with its reason is what separates a "
               "research programme from a backlog.", "  "))


def show_cycle(conn, apply: bool, budget: ResearchBudget) -> None:
    report = cycle_layer.run_cycle(conn, budget=budget, apply=apply)

    head("RESEARCH CYCLE %s" % report["cycle_id"])
    print("  observations       %d (%d blind spots)"
          % (report["observations"], len(report["blind_spots"])))
    print("  questions          %s" % json.dumps(report["questions"]))
    print("  hypotheses formed  %d" % report["hypotheses"])
    print("  duplicates skipped %d" % report["duplicates_skipped"])
    print("  selected to run    %d" % len(report["selected"]))
    print("  skipped            %d" % len(report["skipped"]))

    for item in report["skipped"][:5]:
        print(wrap("skipped: " + item.get("reason", ""), "      "))

    if report.get("unrunnable"):
        head("RAN INTO A WALL")
        for item in report["unrunnable"]:
            print("  %s produced no result (%s)"
                  % (item["experiment_id"], item["status"]))
        print(wrap("No conclusion was drawn from these. A run that did not "
                   "happen is not a finding.", "  "))

    if report["conclusions"]:
        head("CONCLUSIONS")
        for item in report["conclusions"]:
            print("  %-20s conf=%-12s effect=%s%s"
                  % (item["conclusion"], item["confidence"],
                     pct(item["effect"]),
                     "  PROMISING" if item["promising"] else ""))

    if report["candidates"]:
        head("CANDIDATES PROPOSED")
        for candidate_id in report["candidates"]:
            print("  %s" % candidate_id)
        print(wrap("Every candidate requires human review. Nothing in "
                   "production changed, and nothing here can change it.", "  "))

    head("WHY THE CYCLE STOPPED")
    print(wrap(report["termination_reason"]))
    print("\n  runtime %.1fs" % report.get("runtime_seconds", 0.0))
    if not apply:
        print("\n" + wrap("Dry run. Nothing was written and no experiment "
                          "ran. Pass --apply to act on this plan.", "  "))


def show_queue(conn) -> None:
    head("RESEARCH QUEUE")
    depth = queue_layer.depth(conn)
    print("  " + "  ".join("%s=%d" % (k, v) for k, v in depth.items() if v))
    for item in queue_layer.listing(conn, limit=20):
        print("\n  [%s] p=%.3f  %s"
              % (item["state"], item["priority"] or 0,
                 (item["statement"] or "")[:56]))
        if item["reason"]:
            print(wrap(item["reason"], "       "))


def show_conclusions(conn) -> None:
    rows = api.conclusions(conn, limit=50)
    head("CONCLUSIONS (%d)" % len(rows))
    if not rows:
        print("  none yet.")
        return
    print("  %-22s %-13s %10s %10s %6s" %
          ("conclusion", "confidence", "OOS", "in-sample", "n"))
    for row in rows:
        print("  %-22s %-13s %10s %10s %6s"
              % (row["conclusion"], row["confidence"], pct(row["effect"]),
                 pct(row["effect_in_sample"]), row["sample_size"]))
    negative = sum(1 for r in rows if r["conclusion"] != "supported")
    print()
    print(wrap("%d of %d conclusions are not SUPPORTED. They are kept, "
               "counted and shown: a rejected hypothesis is a research "
               "result, and a record filtered to its successes is not a "
               "record." % (negative, len(rows)), "  "))


def show_candidates(conn) -> None:
    rows = api.candidates(conn)
    head("CANDIDATES (%d)" % len(rows))
    for row in rows:
        print("\n  %s  [%s]  effect %s"
              % (row["name"][:52], row["status"], pct(row["effect"])))
        print("    base: %s" % row["base_version"])
        print(wrap(row["review_reason"], "    "))
    if not rows:
        print("  none. No conclusion has cleared the quality gate.")
    print()
    print(wrap("A candidate is a record, not a deployment. Promotion is a "
               "human decision through the Phase 18 gate.", "  "))


def show_families(conn) -> None:
    rows = api.families(conn)
    head("HYPOTHESIS FAMILIES")
    if not rows:
        print("  none recorded yet.")
        return
    for row in rows:
        print("\n  %s  [%s]" % (row.get("family_name") or row["family_id"],
                                row["status"]))
        print("    experiments %d  supported %d  rejected %d  inconclusive %d"
              % (row["experiments"], row["supported"], row["rejected"],
                 row["inconclusive"]))
        print("    best %s   median %s"
              % (pct(row["best_effect"]), pct(row["median_effect"])))
        print(wrap(row["reason"], "    "))
    print()
    print(wrap("Best AND median, always. A family whose best result is "
               "+0.4% and whose median is -0.2% is a weak family, and "
               "either number alone would not say so.", "  "))


def show_governance(conn) -> None:
    report = api.governance_report(conn)

    head("DATA SNOOPING LEDGER")
    if not report["snooping"]:
        print("  no evaluation window has been used yet.")
    for row in report["snooping"]:
        print("  %s  used %d time(s) by %d hypothesis/es"
              % (row["window"], row["uses"], row["distinct_hypotheses"]))
    print(wrap("A researcher permitted to retune against the same held-out "
               "period will eventually find something, and no correction "
               "applied afterwards undoes it.", "  "))

    head("PROTECTED WINDOWS")
    if not report["protected_windows"]:
        print("  none declared.")
        print(wrap("Nothing is reserved from the researcher on this "
                   "database. Declare one with --protect.", "  "))
    for row in report["protected_windows"]:
        print("  %s  %s..%s  [%s]" % (row["label"], row["starts_at"],
                                      row["ends_at"], row["policy"]))

    head("MULTIPLE TESTING")
    print(wrap(report["multiple_testing"]["note"]))

    head("RESEARCH PORTFOLIO")
    diversity = report["diversity"]
    print("  " + "  ".join("%s=%d" % (k, v)
                           for k, v in diversity["areas"].items() if v))
    if diversity["concentration"] is not None:
        print("  concentration %.2f (1.00 = every test is the same kind)"
              % diversity["concentration"])
    if diversity["untouched"]:
        print(wrap("untouched areas: " + ", ".join(diversity["untouched"]), "  "))
    exploration = report["exploration"]
    if exploration["ratio"] is not None:
        print("  exploration %.2f  (new families vs repeat tests)"
              % exploration["ratio"])


def show_audit(conn) -> None:
    head("AUDIT TRAIL")
    for row in audit.trail(conn, limit=25):
        print("  %s  %-24s %-12s %s"
              % (row["occurred_at"][:19], row["action"], row["actor"],
                 (row["decision"] or "")[:20]))
        if row["reason"]:
            print(wrap(row["reason"][:220], "      "))
    summary = audit.activity_summary(conn)
    print("\n  by actor: %s" % json.dumps(summary["by_actor"]))
    print(wrap("The LLM row reads zero because no LLM is used. The absence "
               "is measured rather than asserted.", "  "))


def show_check(conn) -> None:
    head("INTEGRITY (every count must be zero)")
    failures = 0
    for key, value in api.integrity_check(conn).items():
        print("  %-48s %s%s" % (key, value, "   <-- FAIL" if value else ""))
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
    parser.add_argument("--apply", action="store_true",
                        help="persist; without it nothing is written")

    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--questions", action="store_true")
    parser.add_argument("--cycle", action="store_true")
    parser.add_argument("--queue", action="store_true")
    parser.add_argument("--conclusions", action="store_true")
    parser.add_argument("--candidates", action="store_true")
    parser.add_argument("--families", action="store_true")
    parser.add_argument("--governance", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tools", action="store_true",
                        help="the controlled tool surface and its permissions")

    parser.add_argument("--protect", nargs=3, metavar=("LABEL", "FROM", "TO"),
                        help="reserve a period from autonomous research")

    parser.add_argument("--max-experiments", type=int, default=5)
    parser.add_argument("--max-variants", type=int, default=12)
    parser.add_argument("--max-runtime", type=float, default=900.0)

    args = parser.parse_args()

    if not os.path.exists(args.db):
        print("EROARE: baza nu exista: %s" % args.db)
        return 1

    conn = sqlite3.connect(args.db)
    budget = ResearchBudget(
        max_experiments_per_cycle=args.max_experiments,
        max_variants_per_experiment=args.max_variants,
        max_runtime_seconds=args.max_runtime)

    try:
        if args.protect:
            label, starts, ends = args.protect
            if not args.apply:
                print("dry run: would protect %r from %s to %s"
                      % (label, starts, ends))
            else:
                window_id = governance.declare_window(
                    conn, label=label, starts_at=starts, ends_at=ends,
                    reason="reserved so a final un-tuned-against test remains")
                print("protected %s (%s..%s)" % (window_id, starts, ends))
            return 0

        if args.tools:
            from src.autoresearch import tools
            head("RESEARCH TOOL SURFACE")
            for spec in tools.describe():
                print("  %-22s %-20s %s"
                      % (spec["name"], spec["permission"],
                         "" if spec["grantable"] else "[NEVER GRANTED]"))
                print(wrap(spec["description"], "      "))
            print()
            print(wrap("There is no tool that modifies production, submits "
                       "an order, changes risk or moves capital. The "
                       "enforcement is that no such function exists.", "  "))
            return 0

        chose_any = any([args.observe, args.questions, args.cycle, args.queue,
                         args.conclusions, args.candidates, args.families,
                         args.governance, args.audit, args.check])
        if not chose_any:
            parser.print_help()
            return 0

        if args.observe:
            show_observations(conn)
        if args.questions:
            show_questions(conn, args.apply)
        if args.cycle:
            show_cycle(conn, args.apply, budget)
        if args.queue:
            show_queue(conn)
        if args.conclusions:
            show_conclusions(conn)
        if args.candidates:
            show_candidates(conn)
        if args.families:
            show_families(conn)
        if args.governance:
            show_governance(conn)
        if args.audit:
            show_audit(conn)
        if args.check:
            show_check(conn)

        print("\n" + wrap("Research only. Nothing in production changed: no "
                          "model, strategy, threshold, risk limit or capital "
                          "figure. Promotion remains a human decision.", "  "))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
