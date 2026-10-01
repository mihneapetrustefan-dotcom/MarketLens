#!/usr/bin/env python
"""
scripts/recover_historical_corpus.py
--------------------------------------------------
Phase 25.9I-F.7 historical corpus recovery.

    python scripts/recover_historical_corpus.py --plan
    python scripts/recover_historical_corpus.py --validate-slice
    python scripts/recover_historical_corpus.py --run

RECOVERED HISTORY CREATES A NEW DATASET VERSION.
IT DOES NOT REWRITE THE PAST EXPERIMENT.

WHY A SECOND DATABASE FILE
--------------------------
`build_research_observations.py` derives `observation_id` from
(event_id, instrument_id) and then DELETEs that observation's feature
and label rows before rewriting them. Pointing it at the existing
database would therefore overwrite snapshot v1 in place -- the single
thing this phase must not do.

So the new corpus is built in its own file. The old database is opened
`mode=ro` and never written. Its manifest hashes are measured from it
and recorded in the NEW file, which is a stronger guarantee than an
entry inside the protected artifact would have been: anyone can
recompute the hash against the untouched original and compare.

    data/marketlens.db          corpus v1 + snapshot v1   READ ONLY
    data/marketlens_corpus_v2.db  corpus v2 + snapshot v2   built here

WHAT IS REUSED, AND WHAT IS NEW
-------------------------------
Every pipeline stage is the existing script, invoked unchanged against
the new file. No second ingestion, entity, extraction, fusion, study,
observation or feature pipeline is created here; this module only
sequences them, records provenance and verifies the result.

  entity resolution   scripts/backfill_article_entities.py
  event extraction    scripts/populate_events.py
  fusion              scripts/fuse_events.py
  event studies       scripts/build_event_studies.py
  observations        scripts/build_research_observations.py
  features            scripts/compute_features.py

The one difference from the original run is the absence of `--since`.
That flag is the root cause (see docs/PHASE_25_9I_F7_ARCHITECTURE.md):
entity resolution was given a publication floor, so 17,789 articles
published before it were never scanned.

INFORMATION TIME IS NOT PROCESSING TIME
---------------------------------------
Every recovered record carries its historical `information_time` from
`articles.published_at`, with the recovery instant stored separately in
`recovery_provenance.recovered_at`. `InformationTiming` refuses any
record whose information time is at or after the recovery instant, and
`--run` applies that check to every recovered record before the corpus
may be frozen.

NO MONEY, NO BROKER, NO PROTECTED OUTCOME
-----------------------------------------
Every stage is local: no provider, no HTTP, no LLM, no paid API. This
script places no order, creates no OrderIntent, promotes no model, and
never runs or reads the protected D20 validation. The protected horizon
is excluded from every report and from the handoff.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adaptive import corpus_funnel as funnel                     # noqa: E402
from src.adaptive.corpus_versioning import (                         # noqa: E402
    CORPUS_V1, SCHEMA_VERSION, SNAPSHOT_V1, corpus_manifest,
    current_commit, freeze_corpus, freeze_snapshot,
    initialize_recovery_schema, measure_corpus, measure_snapshot,
    record_corpus_version, record_cross_version_mapping, record_provenance,
    record_recovery_run, record_snapshot, snapshot_manifest, compare_corpora,
)
from src.domain.corpus_recovery_models import (                      # noqa: E402
    ArtifactState, InformationTiming, PipelineStage, RecoveryRun,
    RecoveryStatus, manifest_hash,
)

UTC = timezone.utc

SOURCE_DB = os.path.join(ROOT, "data", "marketlens.db")
TARGET_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")

CORPUS_V2 = "canonical_corpus_v2"
SNAPSHOT_V2 = "research_snapshot_v2"
PIPELINE_VERSION = "f7.recovery.v1"

#: Derived tables cleared in the new file before the replay. Everything
#: here is reproducible from the source material that is kept, so
#: clearing it makes the new corpus a pure function of articles,
#: registries and prices rather than a mixture of old rows and new.
DERIVED_TABLES: Tuple[str, ...] = (
    "article_entities",
    "events", "event_evidence", "event_participants", "article_event_links",
    "event_corrections", "event_instruments", "event_sectors",
    "canonical_events", "canonical_event_reports", "canonical_event_participants",
    "fusion_decisions", "fusion_timeline", "fusion_review_cases",
    "fusion_contradictions",
    "event_studies", "event_study_returns", "event_study_volatility",
    "event_study_volume",
    "research_observations", "research_features", "research_labels",
)

#: Experiment output from corpus v1. It is historical evidence about v1
#: and must not travel into v2, where a stale evaluation sample would
#: otherwise look like the new snapshot's capacity -- the exact mistake
#: Phase 25.9I-F.6 spent itself diagnosing.
V1_EXPERIMENT_TABLES: Tuple[str, ...] = (
    "trained_models", "model_evaluations", "model_baseline_comparisons",
    "predictions", "signals", "signal_contributions", "signal_evaluations",
    "signal_outcomes", "signal_suppressions", "recommendations",
)

#: Sequenced exactly as the original pipeline runs. `--since` is absent
#: from every one of them, deliberately.
STAGES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("entity resolution", ("scripts/backfill_article_entities.py", "--apply")),
    ("event extraction", ("scripts/populate_events.py", "--apply")),
    ("fusion", ("scripts/fuse_events.py", "--apply")),
    ("event studies", ("scripts/build_event_studies.py", "--apply")),
    ("research observations", ("scripts/build_research_observations.py", "--apply")),
    ("features", ("scripts/compute_features.py", "--apply")),
)

#: The protected horizon. Named so it can be excluded; never queried for
#: a value.
PROTECTED_HORIZON = "d20"


class RecoveryError(RuntimeError):
    """A recovery stage failed. The run is marked FAILED, never COMPLETE."""


# ======================================================================
# Helpers
# ======================================================================

def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mb(path: str) -> float:
    return round(os.path.getsize(path) / 1024 / 1024, 2) if os.path.exists(path) else 0.0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


# ======================================================================
# Plan
# ======================================================================

def build_plan(source_db: str) -> Dict[str, object]:
    """
    Everything needed to decide whether to run, measured read-only.

    Section 15 of the brief: no large recovery before a plan showing
    records affected, date range, stages, runtime, growth and rollback.
    """
    with _ro(source_db) as conn:
        measured = funnel.measure_funnel(conn, period_label="all")
        losses = funnel.classify_losses(conn)
        recoverable = funnel.recoverable_detail(conn)
        entity_cut = funnel.entity_cutoff(conn)
        earliest_article = _parse(conn.execute(
            "SELECT MIN(published_at) FROM articles").fetchone()[0])
        latest_article = _parse(conn.execute(
            "SELECT MAX(published_at) FROM articles").fetchone()[0])
        prices = funnel.price_coverage_audit(conn)
        records = funnel.record_counts(conn)
        entity_audit = funnel.entity_resolution_audit(conn)

    skipped = int(recoverable.get("skipped") or 0)
    with_mentions = int(recoverable.get("skipped_with_mentions") or 0)

    # Observed yield in the region the pipeline did process: entity-linked
    # articles per canonical event, and per observation. Used to project
    # rather than to promise.
    linked = measured.stage(PipelineStage.ENTITY_RESOLVED)
    canonical_articles = measured.stage(PipelineStage.CANONICAL_EVENT)
    observed_rate = (canonical_articles.count_exiting / linked.count_exiting
                     if linked and linked.count_exiting else 0.0)

    with _ro(source_db) as conn:
        canonical_events = conn.execute(
            "SELECT COUNT(*) FROM canonical_events").fetchone()[0]
        observations = conn.execute(
            "SELECT COUNT(*) FROM research_observations").fetchone()[0]

    projected_articles = int(round(with_mentions * observed_rate))
    per_article_events = (canonical_events / canonical_articles.count_exiting
                          if canonical_articles and canonical_articles.count_exiting
                          else 0.0)
    per_article_observations = (observations / canonical_articles.count_exiting
                               if canonical_articles and canonical_articles.count_exiting
                               else 0.0)

    return {
        "source_db": source_db,
        "target_db": TARGET_DB,
        "source_size_mb": _mb(source_db),
        "root_cause_boundary": entity_cut.isoformat() if entity_cut else None,
        "recovery_range": {
            "start": earliest_article.isoformat() if earliest_article else None,
            "end": entity_cut.isoformat() if entity_cut else None,
            "days": ((entity_cut - earliest_article).days
                     if entity_cut and earliest_article else None),
        },
        "corpus_range": {
            "earliest_article": earliest_article.isoformat() if earliest_article else None,
            "latest_article": latest_article.isoformat() if latest_article else None,
        },
        "records_affected": {
            "articles_never_scanned": skipped,
            "of_those_with_resolvable_mentions": with_mentions,
            "of_those_without_mentions": skipped - with_mentions,
            "recoverable_share": recoverable.get("recoverable_share"),
        },
        "projection": {
            "basis": "observed yield in the region that was processed",
            "entity_linked_to_canonical_rate": round(observed_rate, 4),
            "canonical_events_per_evidencing_article": round(per_article_events, 4),
            "observations_per_evidencing_article": round(per_article_observations, 4),
            "projected_evidencing_articles": projected_articles,
            "projected_new_canonical_events": int(round(
                projected_articles * per_article_events)),
            "projected_new_observations": int(round(
                projected_articles * per_article_observations)),
            "caveat": "a projection from one period's yield, not a promise; "
                      "the measured result replaces it",
        },
        "stages_to_rerun": [name for name, _ in STAGES],
        "derived_tables_cleared": list(DERIVED_TABLES),
        "v1_experiment_tables_cleared": list(V1_EXPERIMENT_TABLES),
        "estimated_storage": {
            "approach": "copy the source file, clear derived and v1 experiment "
                        "tables, VACUUM, then replay",
            "source_mb": _mb(source_db),
            "note": "the new file keeps articles, registries and the price cache; "
                    "measured growth is reported by --run",
        },
        "external_cost": {
            "paid_api_calls": 0,
            "llm_calls": 0,
            "network_calls": 0,
            "evidence": "every stage script imports no provider, http or llm module; "
                        "extraction_tier is deterministic_rule for all existing events",
        },
        "rollback": {
            "strategy": "the new file is standalone; deleting it restores the "
                        "prior state exactly",
            "source_mutated": False,
        },
        "price_coverage": prices,
        "funnel": measured.describe(),
        "loss_accounting": losses.describe(),
        "entity_audit": entity_audit,
        "record_counts": records,
    }


# ======================================================================
# Target preparation
# ======================================================================

def prepare_target(source_db: str, target_db: str, *,
                   clear_experiment_tables: bool = True) -> Dict[str, object]:
    """
    Create the new file from the source and strip everything derived.

    Non-destructive by construction: the source is copied, never
    modified. If the target already exists it is replaced, because a
    half-built corpus is not something to append to.
    """
    if os.path.abspath(source_db) == os.path.abspath(target_db):
        raise RecoveryError("target must not be the source database")

    started = time.time()
    if os.path.exists(target_db):
        os.remove(target_db)
    for suffix in ("-wal", "-shm"):
        stale = target_db + suffix
        if os.path.exists(stale):
            os.remove(stale)
    shutil.copy2(source_db, target_db)
    copied_mb = _mb(target_db)

    cleared: Dict[str, int] = {}
    conn = sqlite3.connect(target_db)
    try:
        tables = list(DERIVED_TABLES)
        if clear_experiment_tables:
            tables += list(V1_EXPERIMENT_TABLES)
        for table in tables:
            if not _table_exists(conn, table):
                continue
            before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            cleared[table] = before
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    return {
        "source_mb": _mb(source_db),
        "copied_mb": copied_mb,
        "after_vacuum_mb": _mb(target_db),
        "rows_cleared": cleared,
        "total_rows_cleared": sum(cleared.values()),
        "seconds": round(time.time() - started, 2),
    }


# ======================================================================
# Stage execution
# ======================================================================

def run_stage(name: str, argv: Sequence[str], target_db: str, *,
              extra: Sequence[str] = (), verbose: bool = True) -> Dict[str, object]:
    """Invoke one existing pipeline script against the new file."""
    command = [sys.executable, os.path.join(ROOT, argv[0]),
               "--db", target_db, *argv[1:], *extra]
    started = time.time()
    if verbose:
        print(f"  [{name}] {' '.join(argv)}", flush=True)
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    elapsed = round(time.time() - started, 2)
    tail = (result.stdout or "").strip().splitlines()[-6:]
    if verbose:
        for line in tail:
            print(f"      {line}", flush=True)
        print(f"      -> exit {result.returncode} in {elapsed}s", flush=True)
    if result.returncode != 0:
        raise RecoveryError(
            f"stage '{name}' failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()[-600:]}")
    return {"stage": name, "command": " ".join(argv), "seconds": elapsed,
            "exit_code": result.returncode, "tail": tail}


# ======================================================================
# Integrity verification
# ======================================================================

def verify_temporal_integrity(target_db: str, *,
                              recovered_at: datetime) -> Dict[str, object]:
    """
    Apply `InformationTiming` to every recovered record.

    This is the check that makes the recovery trustworthy rather than
    merely larger. A single record whose information time reaches the
    recovery instant would mean today's knowledge had entered a
    historical observation.
    """
    violations: List[Dict[str, object]] = []
    checked = 0

    with _ro(target_db) as conn:
        for kind, sql in (
            ("canonical_event",
             "SELECT canonical_event_id, first_reported_at, last_updated_at "
             "FROM canonical_events"),
            ("event_report",
             "SELECT event_id, publication_time, detection_time FROM events"),
            ("research_observation",
             "SELECT observation_id, information_cutoff, observation_created_at "
             "FROM research_observations"),
        ):
            for record_id, information, processing in conn.execute(sql):
                info = _parse(information)
                if info is None:
                    violations.append({"kind": kind, "record_id": record_id,
                                       "problem": "missing information time"})
                    continue
                checked += 1
                try:
                    InformationTiming(information_time=info,
                                      processing_time=_parse(processing),
                                      recovered_at=recovered_at,
                                      recovery_run_id="verification",
                                      source=kind)
                except ValueError as exc:
                    violations.append({"kind": kind, "record_id": record_id,
                                       "problem": str(exc),
                                       "information_time": info.isoformat()})

        future_labels = conn.execute("""
            SELECT COUNT(*) FROM research_labels l
            JOIN research_observations o ON o.observation_id = l.observation_id
            WHERE l.measured_at IS NOT NULL
              AND l.measured_at <= o.information_cutoff
        """).fetchone()[0]
        leaked_features = conn.execute("""
            SELECT COUNT(*) FROM research_features f
            JOIN research_observations o ON o.observation_id = f.observation_id
            WHERE f.as_of IS NOT NULL AND f.as_of > o.information_cutoff
        """).fetchone()[0]

    return {
        "records_checked": checked,
        "violations": violations[:25],
        "violation_count": len(violations),
        "labels_measured_at_or_before_cutoff": future_labels,
        "features_as_of_after_cutoff": leaked_features,
        "passes": (not violations and future_labels == 0 and leaked_features == 0),
    }


def verify_source_unchanged(source_db: str, expected: Dict[str, str]) -> Dict[str, object]:
    """Recompute the old corpus and snapshot hashes and compare."""
    with _ro(source_db) as conn:
        corpus = manifest_hash(corpus_manifest(conn))
        snapshot = manifest_hash(snapshot_manifest(conn))
    return {
        "corpus_hash_now": corpus,
        "corpus_hash_expected": expected.get("corpus"),
        "corpus_unchanged": corpus == expected.get("corpus"),
        "snapshot_hash_now": snapshot,
        "snapshot_hash_expected": expected.get("snapshot"),
        "snapshot_unchanged": snapshot == expected.get("snapshot"),
    }


# ======================================================================
# Provenance
# ======================================================================

def write_provenance(target_db: str, *, recovery_run_id: str,
                     corpus_version: str, recovered_at: datetime) -> Dict[str, int]:
    """
    Mark every record the replay produced, with information time and
    processing time stored separately from the recovery instant.
    """
    written: Dict[str, int] = {}
    conn = sqlite3.connect(target_db)
    try:
        initialize_recovery_schema(conn)
        for kind, sql in (
            ("event_report",
             "SELECT event_id, publication_time, detection_time FROM events"),
            ("canonical_event",
             "SELECT canonical_event_id, first_reported_at, last_updated_at "
             "FROM canonical_events"),
            ("research_observation",
             "SELECT observation_id, information_cutoff, observation_created_at "
             "FROM research_observations"),
        ):
            rows = list(conn.execute(sql))
            written[kind] = record_provenance(
                conn, record_type=kind, rows=rows,
                recovery_run_id=recovery_run_id, corpus_version=corpus_version,
                recovered_at=recovered_at)
        conn.commit()
    finally:
        conn.close()
    return written


# ======================================================================
# Horizon readiness on the new snapshot
# ======================================================================

def horizon_readiness(target_db: str, *,
                      exclude_protected: bool = True) -> List[Dict[str, object]]:
    """
    Rows, clusters, sessions and information span per horizon in the new
    snapshot. Counts rows only; never reads a label value.

    The protected horizon is excluded rather than reported, because
    reporting its readiness would still be reporting something about it.
    """
    out: List[Dict[str, object]] = []
    with _ro(target_db) as conn:
        horizons = sorted({
            str(row[0]).split(".")[0]
            for row in conn.execute("SELECT DISTINCT name FROM research_labels")
            if row[0] and "." in str(row[0])})
        for horizon in horizons:
            if exclude_protected and horizon == PROTECTED_HORIZON:
                out.append({"horizon": horizon, "status": "PROTECTED"})
                continue
            row = conn.execute("""
                SELECT COUNT(DISTINCT o.observation_id),
                       COUNT(DISTINCT o.instrument_id),
                       COUNT(DISTINCT o.event_cluster_id),
                       COUNT(DISTINCT substr(o.information_cutoff,1,10)),
                       MIN(o.information_cutoff), MAX(o.information_cutoff)
                FROM research_observations o
                JOIN research_labels l ON l.observation_id = o.observation_id
                WHERE l.name = ? AND o.quality_level != 'invalid'
            """, (f"{horizon}.abnormal_return",)).fetchone()
            early, late = _parse(row[4]), _parse(row[5])
            out.append({
                "horizon": horizon,
                "status": "MEASURED",
                "observations": row[0] or 0,
                "instruments": row[1] or 0,
                "clusters": row[2] or 0,
                "sessions": row[3] or 0,
                "earliest_information_time": early.isoformat() if early else None,
                "latest_information_time": late.isoformat() if late else None,
                "information_span_days": (late - early).days if early and late else None,
            })
    return out


# ======================================================================
# The run
# ======================================================================

def recover(source_db: str, target_db: str, *,
            slice_end: Optional[datetime] = None,
            verbose: bool = True) -> Dict[str, object]:
    """
    Build a recovered corpus in `target_db`.

    `slice_end` restricts the replay to articles published before an
    instant, for the validation slice. A sliced run is never marked
    COMPLETE: it did not cover its range, and `RecoveryRun` refuses to
    pretend otherwise.
    """
    started_at = datetime.now(UTC)
    wall = time.time()
    commit = current_commit(ROOT)

    with _ro(source_db) as conn:
        entity_cut = funnel.entity_cutoff(conn)
        earliest_article = _parse(conn.execute(
            "SELECT MIN(published_at) FROM articles").fetchone()[0])
        latest_article = _parse(conn.execute(
            "SELECT MAX(published_at) FROM articles").fetchone()[0])
        v1_hashes = {"corpus": manifest_hash(corpus_manifest(conn)),
                     "snapshot": manifest_hash(snapshot_manifest(conn))}
        v1_corpus = measure_corpus(conn, version_id=CORPUS_V1, code_commit=commit,
                                   storage_location=os.path.relpath(source_db, ROOT),
                                   note="pre-F.7 corpus, measured read-only")
        v1_snapshot = measure_snapshot(
            conn, snapshot_id=SNAPSHOT_V1, corpus_version_id=CORPUS_V1,
            code_commit=commit,
            storage_location=os.path.relpath(source_db, ROOT),
            note="pre-F.7 snapshot, measured read-only")

    if earliest_article is None or latest_article is None:
        raise RecoveryError("the source corpus holds no articles")

    run = RecoveryRun(
        recovery_run_id=f"rec-{uuid.uuid4().hex[:16]}",
        pipeline_version=PIPELINE_VERSION,
        code_commit=commit,
        source_snapshot_id=SNAPSHOT_V1,
        range_start=earliest_article,
        range_end=slice_end or latest_article,
        started_at=started_at,
        status=RecoveryStatus.RUNNING,
        note="sliced validation run" if slice_end else "")

    if verbose:
        print(f"RECOVERY RUN {run.recovery_run_id}")
        print(f"  commit         : {commit[:12]}")
        print(f"  source         : {os.path.relpath(source_db, ROOT)} (read-only)")
        print(f"  target         : {os.path.relpath(target_db, ROOT)}")
        print(f"  range          : {run.range_start.date()} -> {run.range_end.date()}"
              f"  ({run.range_days} days)")
        print(f"  entity cutoff  : {entity_cut.isoformat() if entity_cut else 'none'}")
        if slice_end:
            print(f"  SLICE          : articles published before "
                  f"{slice_end.isoformat()} only")
        print()

    prepared = prepare_target(source_db, target_db)
    if verbose:
        print(f"  prepared: {prepared['total_rows_cleared']:,} derived rows cleared, "
              f"{prepared['copied_mb']} MB -> {prepared['after_vacuum_mb']} MB "
              f"in {prepared['seconds']}s")

    # For a slice, physically remove later articles so the stage scripts
    # cannot see them. A filter they do not have cannot be trusted; an
    # absent row can.
    truncated = 0
    if slice_end is not None:
        conn = sqlite3.connect(target_db)
        try:
            truncated = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE published_at >= ?",
                (slice_end.isoformat(),)).fetchone()[0]
            conn.execute("DELETE FROM articles WHERE published_at >= ?",
                         (slice_end.isoformat(),))
            conn.execute("DELETE FROM news_articles WHERE published_at >= ?",
                         (slice_end.isoformat(),))
            conn.commit()
        finally:
            conn.close()
        if verbose:
            print(f"  slice: {truncated:,} later articles physically removed")

    stage_results: List[Dict[str, object]] = []
    errors: List[str] = []
    try:
        for name, argv in STAGES:
            stage_results.append(run_stage(name, argv, target_db, verbose=verbose))
    except RecoveryError as exc:
        errors.append(str(exc))

    recovered_at = datetime.now(UTC)
    provenance = write_provenance(target_db, recovery_run_id=run.recovery_run_id,
                                  corpus_version=CORPUS_V2,
                                  recovered_at=recovered_at)

    with _ro(target_db) as conn:
        measured_funnel = funnel.measure_funnel(conn, period_label="corpus v2")
        processed_through = _parse(conn.execute(
            "SELECT MAX(published_at) FROM articles").fetchone()[0])
        counts = {
            "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "entity_links": conn.execute(
                "SELECT COUNT(*) FROM article_entities").fetchone()[0],
            "event_reports": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "canonical_events": conn.execute(
                "SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            "research_observations": conn.execute(
                "SELECT COUNT(*) FROM research_observations").fetchone()[0],
            "research_features": conn.execute(
                "SELECT COUNT(*) FROM research_features").fetchone()[0],
            "research_labels": conn.execute(
                "SELECT COUNT(*) FROM research_labels").fetchone()[0],
        }

    integrity = verify_temporal_integrity(target_db, recovered_at=recovered_at)
    if not integrity["passes"]:
        errors.append(
            f"temporal integrity failed: {integrity['violation_count']} violations, "
            f"{integrity['labels_measured_at_or_before_cutoff']} labels at or before "
            f"cutoff, {integrity['features_as_of_after_cutoff']} features after cutoff")

    source_check = verify_source_unchanged(source_db, v1_hashes)
    if not (source_check["corpus_unchanged"] and source_check["snapshot_unchanged"]):
        errors.append("the source database changed during the recovery")

    completed_at = datetime.now(UTC)
    if errors:
        run = run.mark_failed(completed_at=completed_at, errors=errors)
    elif slice_end is not None:
        run = run.mark_partial(
            completed_at=completed_at, processed_through=processed_through,
            reason="validation slice; the full range was deliberately not covered",
            counts=counts)
    else:
        run = run.mark_complete(completed_at=completed_at,
                                processed_through=run.range_end, counts=counts)

    # Register versions. v1 is recorded as what it is: already frozen,
    # measured from the untouched original.
    conn = sqlite3.connect(target_db)
    try:
        initialize_recovery_schema(conn)
        record_recovery_run(conn, run)
        for version, digest in ((v1_corpus, v1_hashes["corpus"]),):
            frozen = (version.transition_to(ArtifactState.VALIDATED, at=started_at)
                      .transition_to(ArtifactState.FROZEN, at=started_at,
                                     content_hash=digest)
                      .transition_to(ArtifactState.ELIGIBLE_FOR_RESEARCH,
                                     at=started_at))
            record_corpus_version(conn, frozen)
        frozen_v1_snapshot = (
            v1_snapshot.transition_to(ArtifactState.VALIDATED, at=started_at)
            .transition_to(ArtifactState.FROZEN, at=started_at,
                           content_hash=v1_hashes["snapshot"])
            .transition_to(ArtifactState.ELIGIBLE_FOR_RESEARCH, at=started_at))
        record_snapshot(conn, frozen_v1_snapshot)
        conn.commit()
    finally:
        conn.close()

    corpus_v2 = None
    snapshot_v2 = None
    if run.status is RecoveryStatus.COMPLETE:
        conn = sqlite3.connect(target_db)
        try:
            corpus_v2 = measure_corpus(
                conn, version_id=CORPUS_V2, code_commit=commit,
                recovery_run_id=run.recovery_run_id,
                storage_location=os.path.relpath(target_db, ROOT),
                note="recovered corpus; entity resolution replayed without a "
                     "publication floor")
            corpus_v2 = corpus_v2.transition_to(ArtifactState.VALIDATED,
                                                at=completed_at)
            record_corpus_version(conn, corpus_v2)
            corpus_v2 = freeze_corpus(conn, corpus_v2, at=completed_at)

            snapshot_v2 = measure_snapshot(
                conn, snapshot_id=SNAPSHOT_V2, corpus_version_id=CORPUS_V2,
                code_commit=commit,
                storage_location=os.path.relpath(target_db, ROOT),
                note="research snapshot built from the recovered corpus")
            snapshot_v2 = snapshot_v2.transition_to(ArtifactState.VALIDATED,
                                                    at=completed_at)
            record_snapshot(conn, snapshot_v2)
            snapshot_v2 = freeze_snapshot(conn, snapshot_v2, at=completed_at)
            conn.commit()
        finally:
            conn.close()

    # Cross-version lineage, measured not assumed.
    cross: Dict[str, int] = {}
    if run.status is RecoveryStatus.COMPLETE:
        with _ro(source_db) as old_conn:
            conn = sqlite3.connect(target_db)
            try:
                mappings, cross = compare_corpora(
                    old_conn, conn, old_version=CORPUS_V1, new_version=CORPUS_V2)
                initialize_recovery_schema(conn)
                conn.execute("DELETE FROM cross_version_event_map "
                             "WHERE old_corpus_version = ? AND new_corpus_version = ?",
                             (CORPUS_V1, CORPUS_V2))
                for mapping in mappings:
                    record_cross_version_mapping(conn, mapping)
                conn.commit()
            finally:
                conn.close()

    return {
        "recovery_run": run.describe(),
        "prepared": prepared,
        "slice_end": slice_end.isoformat() if slice_end else None,
        "articles_truncated_for_slice": truncated,
        "stages": stage_results,
        "counts": counts,
        "provenance_rows": provenance,
        "funnel": measured_funnel.describe(),
        "temporal_integrity": integrity,
        "source_unchanged": source_check,
        "v1_corpus": v1_corpus.describe(),
        "v1_snapshot": v1_snapshot.describe(),
        "v1_hashes": v1_hashes,
        "corpus_v2": corpus_v2.describe() if corpus_v2 else None,
        "snapshot_v2": snapshot_v2.describe() if snapshot_v2 else None,
        "cross_version": cross,
        "horizons": horizon_readiness(target_db),
        "target_size_mb": _mb(target_db),
        "elapsed_seconds": round(time.time() - wall, 2),
    }


def validation_slice_end(source_db: str, *, days: int = 3) -> datetime:
    """
    The end of the deterministic validation slice: the first `days`
    calendar days of the never-scanned region.

    Chosen by position in the gap -- the earliest articles the pipeline
    never saw -- and never by market outcome. A slice picked for its
    returns would make the validation a selection of evidence.
    """
    with _ro(source_db) as conn:
        earliest = _parse(conn.execute(
            "SELECT MIN(published_at) FROM articles").fetchone()[0])
    if earliest is None:
        raise RecoveryError("no articles to slice")
    start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=days)


# ======================================================================
# CLI
# ======================================================================

def _print_plan(plan: Dict[str, object]) -> None:
    print("PHASE 25.9I-F.7 RECOVERY PLAN (nothing written)")
    print(f"  source            : {plan['source_db']} ({plan['source_size_mb']} MB)")
    print(f"  target            : {plan['target_db']}")
    print(f"  root-cause boundary: {plan['root_cause_boundary']}")
    rng = plan["recovery_range"]
    print(f"  never-scanned range: {rng['start']} -> {rng['end']} ({rng['days']} days)")
    print()
    aff = plan["records_affected"]
    print("  RECORDS AFFECTED")
    print(f"    articles never scanned            {aff['articles_never_scanned']:>8,}")
    print(f"    with resolvable mention data      {aff['of_those_with_resolvable_mentions']:>8,}")
    print(f"    without any mention data          {aff['of_those_without_mentions']:>8,}")
    print()
    pro = plan["projection"]
    print("  PROJECTION (from the processed region's own yield, not a promise)")
    print(f"    projected evidencing articles     {pro['projected_evidencing_articles']:>8,}")
    print(f"    projected new canonical events    {pro['projected_new_canonical_events']:>8,}")
    print(f"    projected new observations        {pro['projected_new_observations']:>8,}")
    print()
    print("  FUNNEL (distinct articles reaching each stage)")
    for stage in plan["funnel"]["stages"]:
        print(f"    {stage['stage']:24s} {stage['exiting']:>8,}  "
              f"drop {stage['drop']:>7,}  {str(stage['drop_pct']):>6s}%  "
              f"from {str(stage['earliest'])[:10]}")
    print(f"    reconciles: {plan['funnel']['reconciles']}   "
          f"first material break: {plan['funnel']['first_material_break']}")
    print()
    print("  LOSS ACCOUNTING")
    for reason, count in plan["loss_accounting"]["by_reason"].items():
        print(f"    {reason:30s} {count:>8,}")
    print(f"    reconciles: {plan['loss_accounting']['reconciles']}  "
          f"(unclassified {plan['loss_accounting']['unclassified']})")
    print()
    prices = plan["price_coverage"]
    print(f"  PRICE COVERAGE      {prices['earliest'][:10]} -> {prices['latest'][:10]}, "
          f"{prices['rows']:,} rows, {prices['instruments']} instruments")
    cost = plan["external_cost"]
    print(f"  EXTERNAL COST       paid={cost['paid_api_calls']} llm={cost['llm_calls']} "
          f"network={cost['network_calls']}")
    print(f"  ROLLBACK            {plan['rollback']['strategy']}")
    print()
    print("  STAGES TO RERUN     " + " -> ".join(plan["stages_to_rerun"]))


def _print_result(result: Dict[str, object]) -> None:
    run = result["recovery_run"]
    print()
    print("RESULT")
    print(f"  run            : {run['recovery_run_id']}  status={run['status'].upper()}")
    print(f"  covers range   : {run['covers_full_range']}")
    print(f"  elapsed        : {result['elapsed_seconds']}s")
    print(f"  target size    : {result['target_size_mb']} MB")
    if run["errors"]:
        for err in run["errors"]:
            print(f"  ERROR          : {err}")
    print()
    print("  COUNTS")
    for key, value in result["counts"].items():
        print(f"    {key:24s} {value:>8,}")
    print()
    print("  FUNNEL (v2)")
    for stage in result["funnel"]["stages"]:
        print(f"    {stage['stage']:24s} {stage['exiting']:>8,}  "
              f"from {str(stage['earliest'])[:10]}")
    integrity = result["temporal_integrity"]
    print()
    print(f"  TEMPORAL INTEGRITY  checked={integrity['records_checked']:,} "
          f"violations={integrity['violation_count']} "
          f"passes={integrity['passes']}")
    src = result["source_unchanged"]
    print(f"  SOURCE UNCHANGED    corpus={src['corpus_unchanged']} "
          f"snapshot={src['snapshot_unchanged']}")
    if result["corpus_v2"]:
        print(f"  CORPUS v2           {result['corpus_v2']['state']} "
              f"hash={str(result['corpus_v2']['content_hash'])[:16]}... "
              f"span={result['corpus_v2']['information_span_days']}d")
    if result["snapshot_v2"]:
        snap = result["snapshot_v2"]
        print(f"  SNAPSHOT v2         {snap['state']} "
              f"hash={str(snap['content_hash'])[:16]}... "
              f"obs={snap['observation_count']:,} clusters={snap['cluster_count']:,}")
    if result["cross_version"]:
        print("  CROSS-VERSION       " + "  ".join(
            f"{k}={v}" for k, v in result["cross_version"].items() if v))
    print()
    print("  HORIZONS (new snapshot)")
    for row in result["horizons"]:
        if row.get("status") == "PROTECTED":
            print(f"    {row['horizon']:16s} PROTECTED")
            continue
        print(f"    {row['horizon']:16s} obs={row['observations']:>6,} "
              f"clusters={row['clusters']:>5,} sessions={row['sessions']:>4,} "
              f"span={row['information_span_days']}d")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=SOURCE_DB)
    parser.add_argument("--target-db", default=TARGET_DB)
    parser.add_argument("--plan", action="store_true",
                        help="Measure and report; write nothing. The default.")
    parser.add_argument("--validate-slice", action="store_true",
                        help="Replay a deterministic 3-day historical slice into a "
                             "throwaway file and verify lineage and timestamps.")
    parser.add_argument("--slice-days", type=int, default=3)
    parser.add_argument("--run", action="store_true",
                        help="Build the full recovered corpus and the new snapshot.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.source_db):
        print(f"ERROR: source database not found: {args.source_db}")
        return 1

    if args.validate_slice:
        end = validation_slice_end(args.source_db, days=args.slice_days)
        target = args.target_db.replace(".db", f"_slice.db")
        result = recover(args.source_db, target, slice_end=end,
                         verbose=not args.json)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_result(result)
        status = result["recovery_run"]["status"]
        return 0 if status in ("partial", "complete") else 3

    if args.run:
        result = recover(args.source_db, args.target_db, verbose=not args.json)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_result(result)
        return 0 if result["recovery_run"]["status"] == "complete" else 3

    plan = build_plan(args.source_db)
    if args.json:
        print(json.dumps(plan, indent=2, default=str))
    else:
        _print_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
