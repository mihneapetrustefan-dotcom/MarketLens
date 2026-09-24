#!/usr/bin/env python3
"""
scripts/audit_research_integrity.py
-----------------------------------------------------------
Phase 25.9D — one command that says whether the research record can be
trusted.

    python scripts/audit_research_integrity.py --db data/marketlens.db
    python scripts/audit_research_integrity.py --negative-controls

WHAT IT CHECKS
------------------
1. The three subsystem integrity checks that already existed
   (experiments, autoresearch, challengers), unchanged.
2. The Phase 25.9D checks: a cache hit that reused a different
   cohort, experiments stranded RUNNING by a crash, queue items that
   finished without an experiment, snooping-ledger uses inside a
   protected window, research code that writes outside research scope.
3. The protected-test ledger: chain intact, and the D20 test still
   registered and unconsumed. The D20 statistic is never computed; the
   ledger records only states and hashes.

IT CANNOT CHANGE WHAT IT AUDITS
-----------------------------------
The database is opened read-only (`mode=ro`) and copied into memory;
every check runs on the copy. The subsystem checks create their schema
if missing, and on a production file that would be a write -- the copy
is what makes calling them safe.

A CHECK THAT NEVER FIRES PROVES NOTHING
-------------------------------------------
`--negative-controls` builds a clean database, confirms every check
reads zero on it, then injects each defect once and confirms the check
for it -- and only a check -- turns non-zero. A control that fails
exits 2.

Exit codes: 0 clean, 1 a finding, 2 a negative control failed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.research import protected_ledger as L

DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")
D20_TEST_ID = "exp-d20-reversal-anchor-v2-protected"

#: A RUNNING experiment older than this with no run row was abandoned
#: by a crash between `set_status(RUNNING)` and `save_run`.
STRANDED_AFTER_SECONDS = 3600

RESEARCH_PACKAGES = ("experiments", "autoresearch", "challengers")

#: Tables the research packages may write. Anything else is a boundary
#: breach, however it is reached.
RESEARCH_TABLE_PREFIXES = ("experiment", "autoresearch_", "challenger")
RESEARCH_TABLES = {"experiments", "hypothesis_families"}


# ======================================================================
# Read-only access
# ======================================================================

def open_copy(path: str) -> sqlite3.Connection:
    """The database, read-only, copied into memory."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    source = sqlite3.connect(uri, uri=True)
    try:
        copy = sqlite3.connect(":memory:")
        source.backup(copy)
    finally:
        source.close()
    return copy


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name=?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


# ======================================================================
# Phase 25.9D checks
# ======================================================================

def cache_hits_on_a_different_cohort(conn: sqlite3.Connection) -> int:
    """A reused result whose source read different rows, seed or definition."""
    total = 0
    for runs in ("experiment_runs", "challenger_runs"):
        if not _table_exists(conn, runs) or "cohort_digest" not in _columns(conn, runs):
            continue
        total += _scalar(conn, """
            SELECT COUNT(*) FROM %s hit JOIN %s src
              ON src.run_id = hit.cached_from_run
            WHERE hit.cache_hit = 1
              AND (hit.cohort_digest != src.cohort_digest
                   OR hit.fingerprint != src.fingerprint
                   OR hit.seed != src.seed
                   OR src.cohort_digest = '')
        """ % (runs, runs))
    return total


def cache_hits_without_a_source(conn: sqlite3.Connection) -> int:
    total = 0
    for runs in ("experiment_runs", "challenger_runs"):
        if _table_exists(conn, runs):
            total += _scalar(conn, """
                SELECT COUNT(*) FROM %s hit WHERE hit.cache_hit = 1
                  AND NOT EXISTS (SELECT 1 FROM %s src
                                  WHERE src.run_id = hit.cached_from_run)
            """ % (runs, runs))
    return total


def experiments_stranded_running(conn: sqlite3.Connection, now: datetime) -> int:
    """RUNNING, started over an hour ago, and no run was ever saved."""
    if not _table_exists(conn, "experiments"):
        return 0
    cutoff = (now - timedelta(seconds=STRANDED_AFTER_SECONDS)).isoformat()
    return _scalar(conn, """
        SELECT COUNT(*) FROM experiments e
        WHERE e.status = 'running'
          AND COALESCE(e.started_at, e.created_at) < ?
          AND NOT EXISTS (SELECT 1 FROM experiment_runs r
                          WHERE r.experiment_id = e.experiment_id)
    """, (cutoff,))


def queue_items_completed_without_an_experiment(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "autoresearch_queue"):
        return 0
    return _scalar(conn, """
        SELECT COUNT(*) FROM autoresearch_queue q
        WHERE q.state = 'completed' AND (
            q.experiment_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM experiments e
                WHERE e.experiment_id = q.experiment_id))
    """)


def window_uses_inside_protected_windows(conn: sqlite3.Connection) -> int:
    """A snooping-ledger entry whose period overlaps a protected window."""
    if not (_table_exists(conn, "autoresearch_window_usage")
            and _table_exists(conn, "autoresearch_protected_windows")):
        return 0
    from src.autoresearch import governance
    count = 0
    for (key,) in conn.execute("SELECT window_key FROM autoresearch_window_usage"):
        start, _sep, end = str(key).partition("..")
        if not start or not end:
            continue
        try:
            governance.assert_window_allowed(conn, starts_at=start, ends_at=end)
        except governance.ProtectedWindowRefused:
            count += 1
    return count


def _executed_sql(source: str):
    """SQL literals passed to execute / executemany / executescript."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in (
                "execute", "executemany", "executescript"):
            continue
        for argument in node.args:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    yield inner.value


_WRITE = re.compile(
    r"\b(?:INSERT\s+OR\s+REPLACE\s+INTO|INSERT\s+OR\s+IGNORE\s+INTO|"
    r"INSERT\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|"
    r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE)


def written_tables(source: str) -> set:
    """Every table named as a write target, by whole-word verb."""
    found = set()
    for text in _executed_sql(source):
        for match in _WRITE.finditer(text):
            found.add(match.group(1).lower())
    return found


def is_research_table(table: str) -> bool:
    return table in RESEARCH_TABLES or table.startswith(RESEARCH_TABLE_PREFIXES)


def research_writes_outside_scope(root: str = ROOT) -> List[str]:
    """Static half of the boundary; the rehearsal measures the dynamic half."""
    offenders = []
    for package in RESEARCH_PACKAGES:
        folder = os.path.join(root, "src", package)
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                source = handle.read()
            for table in sorted(written_tables(source)):
                if not is_research_table(table):
                    offenders.append("src/%s/%s writes %s" % (package, name, table))
    return offenders


# ======================================================================
# The ledger
# ======================================================================

def ledger_state(ledger_path: str) -> Dict[str, Any]:
    try:
        L.verify(ledger_path)
        intact = True
        error = ""
    except L.LedgerError as exc:
        intact, error = False, str(exc)
    try:
        state = L.status(D20_TEST_ID, ledger_path)["state"]
    except L.LedgerError as exc:
        state, error = "UNREADABLE", error or str(exc)
    return {"intact": intact, "d20_state": state, "error": error}


# ======================================================================
# The audit
# ======================================================================

def subsystem_checks(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    from src.autoresearch import api as research_api
    from src.challengers import api as challenger_api
    from src.experiments import api as experiment_api
    return {"experiments": experiment_api.integrity_check(conn),
            "autoresearch": research_api.integrity_check(conn),
            "challengers": challenger_api.integrity_check(conn)}


def phase_checks(conn: sqlite3.Connection, now: datetime) -> Dict[str, int]:
    return {
        "cache_hits_on_a_different_cohort": cache_hits_on_a_different_cohort(conn),
        "cache_hits_without_a_source": cache_hits_without_a_source(conn),
        "experiments_stranded_running": experiments_stranded_running(conn, now),
        "queue_items_completed_without_an_experiment":
            queue_items_completed_without_an_experiment(conn),
        "window_uses_inside_protected_windows":
            window_uses_inside_protected_windows(conn),
    }


def audit(conn: sqlite3.Connection, *, now: datetime,
          ledger_path: str = L.DEFAULT_LEDGER, root: str = ROOT) -> Dict[str, Any]:
    """Every check on `conn`, which the caller guarantees is a copy."""
    # Phase checks first: the subsystem checks initialise their schemas
    # on the copy, and must not be what makes a check find a table.
    phase = phase_checks(conn, now)
    subsystems = subsystem_checks(conn)
    boundary = research_writes_outside_scope(root)
    ledger = ledger_state(ledger_path)

    findings = [("%s.%s" % (group, key), value)
                for group, checks in subsystems.items()
                for key, value in checks.items() if value]
    findings += [("phase_25_9d.%s" % key, value)
                 for key, value in phase.items() if value]
    findings += [("write_boundary", item) for item in boundary]
    if not ledger["intact"]:
        findings.append(("ledger.chain", ledger["error"]))
    if ledger["d20_state"] != "NOT_CONSUMED":
        findings.append(("ledger.d20_state", ledger["d20_state"]))

    return {"subsystems": subsystems, "phase_25_9d": phase,
            "write_boundary_offenders": boundary, "ledger": ledger,
            "findings": findings, "clean": not findings}


# ======================================================================
# Negative controls
# ======================================================================

def _clean_database() -> sqlite3.Connection:
    from src.data_access.autoresearch_schema import initialize_autoresearch_schema
    from src.data_access.challenger_schema import initialize_challenger_schema
    from src.data_access.experiment_schema import initialize_experiment_schema
    from src.data_access.memory_schema import initialize_memory_schema
    conn = sqlite3.connect(":memory:")
    initialize_memory_schema(conn)
    initialize_experiment_schema(conn)
    initialize_autoresearch_schema(conn)
    initialize_challenger_schema(conn)
    return conn


def _run(conn, run_id, *, digest="cd-a", cache_hit=0, source=None,
         experiment_id="exp-1", seed=7, fingerprint="fp"):
    conn.execute("""
        INSERT INTO experiment_runs (run_id, experiment_id, status, seed,
            fingerprint, cohort_digest, cache_hit, cached_from_run)
        VALUES (?,?,?,?,?,?,?,?)
    """, (run_id, experiment_id, "completed", seed, fingerprint, digest,
          cache_hit, source))


def _experiment(conn, experiment_id="exp-1", status="passed",
                started_at="2026-09-01T00:00:00+00:00"):
    conn.execute("""
        INSERT INTO experiments (experiment_id, method_version, name,
            experiment_type, status, statement, mechanism, baseline_name,
            baseline_evaluator, candidate_name, candidate_evaluator,
            changed_variables_json, dataset_snapshot_id, fingerprint,
            created_at, started_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (experiment_id, "v1", "n", "signal", status, "s", "m", "b",
          "all_signals", "c", "signal_strength_threshold", '["x"]', "ds",
          "fp", started_at, started_at))


def _defects() -> List[Tuple[str, Callable[[sqlite3.Connection], None]]]:
    def stale_cache(conn):
        _experiment(conn)
        _run(conn, "run-src", digest="cd-old")
        _run(conn, "run-hit", digest="cd-new", cache_hit=1, source="run-src")

    def orphan_hit(conn):
        _experiment(conn)
        _run(conn, "run-hit", cache_hit=1, source="run-missing")

    def stranded(conn):
        _experiment(conn, status="running")

    def completed_without_experiment(conn):
        conn.execute("""INSERT INTO autoresearch_hypotheses (hypothesis_id,
            method_version, statement, mechanism, claim_fingerprint,
            family_id, family_name, created_at)
            VALUES ('h1','v1','s','m','fp','f','f','2026-09-01')""")
        conn.execute("""INSERT INTO autoresearch_queue (queue_id,
            method_version, hypothesis_id, state, queued_at)
            VALUES ('q1','v1','h1','completed','2026-09-01')""")

    def protected_use(conn):
        conn.execute("""INSERT INTO autoresearch_protected_windows VALUES
            ('w1','final','2026-08-15','2026-08-27','protected','',
             '2026-09-01')""")
        conn.execute("""INSERT INTO autoresearch_window_usage (usage_id,
            window_key, used_at) VALUES ('u1','2026-08-20..2026-09-01',
            '2026-09-02')""")

    return [
        ("phase_25_9d.cache_hits_on_a_different_cohort", stale_cache),
        ("phase_25_9d.cache_hits_without_a_source", orphan_hit),
        ("phase_25_9d.experiments_stranded_running", stranded),
        ("phase_25_9d.queue_items_completed_without_an_experiment",
         completed_without_experiment),
        ("phase_25_9d.window_uses_inside_protected_windows", protected_use),
    ]


def negative_controls(now: datetime, ledger_path: str = L.DEFAULT_LEDGER
                      ) -> List[Dict[str, Any]]:
    """Every check silent on clean data, and firing on its own defect."""
    import shutil
    import tempfile

    outcomes = []
    clean = audit(_clean_database(), now=now, ledger_path=ledger_path)
    outcomes.append({"control": "clean database reads clean",
                     "passed": clean["clean"], "detail": clean["findings"]})

    for expected, inject in _defects():
        conn = _clean_database()
        inject(conn)
        conn.commit()
        found = [name for name, _value in
                 audit(conn, now=now, ledger_path=ledger_path)["findings"]]
        # Only its own Phase 25.9D check may fire: a check that fires on
        # every defect is as blind as one that fires on none.
        phase_found = [n for n in found if n.startswith("phase_25_9d.")]
        outcomes.append({"control": "detects " + expected,
                         "passed": phase_found == [expected], "detail": found})

    # The scanner must find the writes the research code really makes. A
    # scanner that matches nothing reports a clean boundary every time --
    # which is exactly what a lost regex escape did while this was built.
    real = set()
    for package in RESEARCH_PACKAGES:
        folder = os.path.join(ROOT, "src", package)
        for name in os.listdir(folder):
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    real |= written_tables(handle.read())
    known = {"experiment_runs", "autoresearch_queue", "challenger_runs"}
    outcomes.append({"control": "finds the known research writes",
                     "passed": known <= real, "detail": sorted(real)})

    # The static boundary scanner must see a write it is shown.
    fake = tempfile.mkdtemp()
    try:
        for package in RESEARCH_PACKAGES:
            os.makedirs(os.path.join(fake, "src", package))
        with open(os.path.join(fake, "src", "autoresearch", "leak.py"), "w",
                  encoding="utf-8") as handle:
            handle.write('def f(conn):\n    conn.execute("UPDATE trained_models '
                         'SET status = \'active\'")\n')
        offenders = research_writes_outside_scope(fake)
        outcomes.append({"control": "detects a research write to trained_models",
                         "passed": any("trained_models" in o for o in offenders),
                         "detail": offenders})
    finally:
        shutil.rmtree(fake)

    # The ledger checks must see a tampered chain and a consumed test.
    fake = tempfile.mkdtemp()
    try:
        path = os.path.join(fake, "ledger.jsonl")
        L.register(D20_TEST_ID, "fp", {}, path)
        L.append(D20_TEST_ID, L.OPENING, {}, path)
        state = ledger_state(path)
        outcomes.append({"control": "detects a consumed D20 test",
                         "passed": state["d20_state"] == "CONSUMED",
                         "detail": state})
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines[:1] + [lines[1].replace("OPENING", "REGISTERED")])
        state = ledger_state(path)
        outcomes.append({"control": "detects a tampered ledger",
                         "passed": not state["intact"], "detail": state})
    finally:
        shutil.rmtree(fake)
    return outcomes


# ======================================================================
# CLI
# ======================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--ledger", default=L.DEFAULT_LEDGER)
    parser.add_argument("--negative-controls", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.negative_controls:
        outcomes = negative_controls(now, args.ledger)
        for item in outcomes:
            print("%-4s %s" % ("PASS" if item["passed"] else "FAIL", item["control"]))
        failed = [o for o in outcomes if not o["passed"]]
        print("negative controls: %d/%d passed" % (len(outcomes) - len(failed),
                                                    len(outcomes)))
        return 2 if failed else 0

    report = audit(open_copy(args.db), now=now, ledger_path=args.ledger)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for group, checks in report["subsystems"].items():
            for key, value in checks.items():
                print("%-13s %-55s %s" % (group, key, value))
        for key, value in report["phase_25_9d"].items():
            print("%-13s %-55s %s" % ("phase_25_9d", key, value))
        print("%-13s %-55s %s" % ("boundary", "research_writes_outside_scope",
                                  len(report["write_boundary_offenders"])))
        print("%-13s %-55s %s" % ("ledger", "chain_intact", report["ledger"]["intact"]))
        print("%-13s %-55s %s" % ("ledger", "d20_state", report["ledger"]["d20_state"]))
        print("RESEARCH INTEGRITY: %s" % ("CLEAN" if report["clean"] else
                                          "%d FINDING(S)" % len(report["findings"])))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
