"""
src/adaptive/corpus_versioning.py
--------------------------------------------------
Phase 25.9I-F.7: corpus and snapshot identity, manifests, recovery-run
metadata and cross-version lineage.

WHERE THE REGISTRY LIVES, AND WHY NOT IN THE OLD DATABASE
---------------------------------------------------------
The whole point of this phase is that the old corpus and the old
research snapshot must remain exactly as they are. So this module never
writes to the database holding them. The registry tables are created in
the NEW corpus database, and the old version is recorded there as a
*measurement of* the old database -- its manifest hash computed
read-only.

That is a stronger guarantee than an entry inside the old file would be:
anyone can recompute the hash against the untouched original and compare
it with the recorded value. Nothing had to be written to the protected
artifact in order to prove it was not written to.

MANIFESTS
---------
A manifest hash is taken over sorted identities plus their
information-time fields, canonicalised as JSON. Two corpora match only
if they contain the same events with the same timing. Row order,
insertion order and processing timestamps do not enter it, because those
differ between a build and a replay of the same build and would make the
hash useless for the one thing it is for.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.domain.corpus_recovery_models import (
    ArtifactState, CorpusVersion, CrossVersionMapping, CrossVersionRelation,
    FrozenArtifactError, RecoveryRun, RecoveryStatus, ResearchSnapshot,
    manifest_hash,
)
from src.domain.research_models import DatasetVersion

UTC = timezone.utc

SCHEMA_VERSION = "f7.v1"

#: The identity the pre-F.7 corpus and snapshot are recorded under.
#: These name what already exists; they do not create it.
CORPUS_V1 = "canonical_corpus_v1"
SNAPSHOT_V1 = "research_snapshot_v1"


# ======================================================================
# Schema
# ======================================================================

def initialize_recovery_schema(conn: sqlite3.Connection) -> None:
    """
    Create the registry tables if absent. Additive only: no existing
    table is altered and no existing row is read-modified-written.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS corpus_versions (
            version_id                TEXT PRIMARY KEY,
            state                     TEXT NOT NULL,
            created_at                TEXT NOT NULL,
            code_commit               TEXT NOT NULL,
            schema_version            TEXT NOT NULL,
            recovery_run_id           TEXT,
            earliest_information_time TEXT,
            latest_information_time   TEXT,
            counts_json               TEXT NOT NULL DEFAULT '{}',
            content_hash              TEXT,
            frozen_at                 TEXT,
            storage_location          TEXT,
            note                      TEXT
        );

        CREATE TABLE IF NOT EXISTS research_snapshots (
            snapshot_id               TEXT PRIMARY KEY,
            corpus_version_id         TEXT NOT NULL,
            dataset_version           TEXT NOT NULL,
            universe_version          TEXT NOT NULL,
            event_version             TEXT NOT NULL,
            state                     TEXT NOT NULL,
            created_at                TEXT NOT NULL,
            code_commit               TEXT NOT NULL,
            as_of_cutoff              TEXT NOT NULL,
            observation_count         INTEGER NOT NULL DEFAULT 0,
            instrument_count          INTEGER NOT NULL DEFAULT 0,
            cluster_count             INTEGER NOT NULL DEFAULT 0,
            earliest_information_time TEXT,
            latest_information_time   TEXT,
            horizons_json             TEXT NOT NULL DEFAULT '[]',
            content_hash              TEXT,
            frozen_at                 TEXT,
            storage_location          TEXT,
            note                      TEXT
        );

        CREATE TABLE IF NOT EXISTS recovery_runs (
            recovery_run_id    TEXT PRIMARY KEY,
            pipeline_version   TEXT NOT NULL,
            code_commit        TEXT NOT NULL,
            source_snapshot_id TEXT,
            range_start        TEXT NOT NULL,
            range_end          TEXT NOT NULL,
            started_at         TEXT NOT NULL,
            completed_at       TEXT,
            status             TEXT NOT NULL,
            processed_through  TEXT,
            counts_json        TEXT NOT NULL DEFAULT '{}',
            errors_json        TEXT NOT NULL DEFAULT '[]',
            note               TEXT
        );

        CREATE TABLE IF NOT EXISTS recovery_provenance (
            record_type      TEXT NOT NULL,
            record_id        TEXT NOT NULL,
            recovery_run_id  TEXT NOT NULL,
            corpus_version   TEXT NOT NULL,
            information_time TEXT,
            processing_time  TEXT,
            recovered_at     TEXT NOT NULL,
            PRIMARY KEY (record_type, record_id, recovery_run_id)
        );

        CREATE TABLE IF NOT EXISTS cross_version_event_map (
            mapping_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            relation            TEXT NOT NULL,
            old_corpus_version  TEXT NOT NULL,
            new_corpus_version  TEXT NOT NULL,
            old_event_ids_json  TEXT NOT NULL DEFAULT '[]',
            new_event_ids_json  TEXT NOT NULL DEFAULT '[]',
            fingerprint         TEXT,
            note                TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_recovery_provenance_run
            ON recovery_provenance(recovery_run_id);
        CREATE INDEX IF NOT EXISTS idx_cross_version_relation
            ON cross_version_event_map(relation);
    """)


# ======================================================================
# Code identity
# ======================================================================

def current_commit(repo_root: Optional[str] = None) -> str:
    """
    The commit a recovery ran at. Falls back to a marker rather than an
    empty string, because `RecoveryRun` refuses to be constructed
    without one and a silent blank would be worse than an honest
    "unknown".
    """
    root = repo_root or os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


# ======================================================================
# Manifests
# ======================================================================

def corpus_manifest(conn: sqlite3.Connection) -> Dict[str, object]:
    """
    Canonical content description of the event corpus in `conn`.

    Includes each canonical event's id, fingerprint and
    `first_reported_at` -- identity plus information timing. Excludes
    `last_updated_at` and every processing timestamp, so replaying the
    same build produces the same manifest.
    """
    events = [
        {"id": row[0], "fingerprint": row[1],
         "first_reported_at": row[2], "event_type": row[3]}
        for row in conn.execute(
            "SELECT canonical_event_id, fingerprint, first_reported_at, event_type "
            "FROM canonical_events ORDER BY canonical_event_id")
    ]
    reports = conn.execute(
        "SELECT COUNT(*) FROM canonical_event_reports").fetchone()[0]
    return {
        "kind": "canonical_corpus",
        "schema_version": SCHEMA_VERSION,
        "canonical_event_count": len(events),
        "report_link_count": reports,
        "events": events,
    }


def snapshot_manifest(conn: sqlite3.Connection, *,
                      dataset_version: Optional[str] = None) -> Dict[str, object]:
    """
    Canonical content description of a research snapshot.

    Observation identity, instrument, cluster and `information_cutoff`.
    Never a label value: a manifest must be computable without reading
    an outcome, protected or otherwise.
    """
    where, params = "", []
    if dataset_version:
        where = " WHERE dataset_version = ?"
        params = [dataset_version]

    observations = [
        {"id": row[0], "instrument": row[1], "cluster": row[2],
         "information_cutoff": row[3], "quality": row[4]}
        for row in conn.execute(
            "SELECT observation_id, instrument_id, event_cluster_id, "
            "information_cutoff, quality_level FROM research_observations"
            + where + " ORDER BY observation_id", params)
    ]
    label_names = [row[0] for row in conn.execute(
        "SELECT DISTINCT name FROM research_labels ORDER BY name")]
    feature_names = [row[0] for row in conn.execute(
        "SELECT DISTINCT qualified_name FROM research_features ORDER BY 1")]
    return {
        "kind": "research_snapshot",
        "schema_version": SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "observation_count": len(observations),
        "label_names": label_names,
        "feature_names": feature_names,
        "observations": observations,
    }


def file_digest(path: str, *, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file, for pinning a pre-registration document."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ======================================================================
# Measuring a corpus / snapshot
# ======================================================================

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


def measure_corpus(conn: sqlite3.Connection, *, version_id: str,
                   code_commit: str, storage_location: str,
                   created_at: Optional[datetime] = None,
                   recovery_run_id: Optional[str] = None,
                   note: str = "") -> CorpusVersion:
    """Build a `CorpusVersion` from what is actually in `conn`."""
    early, late = conn.execute(
        "SELECT MIN(first_reported_at), MAX(first_reported_at) "
        "FROM canonical_events").fetchone()
    counts = {
        "articles": conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        "entity_links": conn.execute(
            "SELECT COUNT(*) FROM article_entities").fetchone()[0],
        "event_reports": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "canonical_events": conn.execute(
            "SELECT COUNT(*) FROM canonical_events").fetchone()[0],
        "research_observations": conn.execute(
            "SELECT COUNT(*) FROM research_observations").fetchone()[0],
    }
    return CorpusVersion(
        version_id=version_id,
        created_at=created_at or datetime.now(UTC),
        code_commit=code_commit,
        schema_version=SCHEMA_VERSION,
        state=ArtifactState.BUILDING,
        recovery_run_id=recovery_run_id,
        earliest_information_time=_parse(early),
        latest_information_time=_parse(late),
        counts=counts,
        storage_location=storage_location,
        note=note)


def measure_snapshot(conn: sqlite3.Connection, *, snapshot_id: str,
                     corpus_version_id: str, code_commit: str,
                     dataset_version: Optional[DatasetVersion] = None,
                     as_of_cutoff: Optional[datetime] = None,
                     storage_location: str = "",
                     created_at: Optional[datetime] = None,
                     note: str = "") -> ResearchSnapshot:
    """
    Build a `ResearchSnapshot` from what is actually in `conn`.

    `as_of_cutoff` defaults to the latest information time present --
    the snapshot knows nothing after that, so claiming a later cutoff
    would be a claim about data it does not hold.
    """
    early, late, observations, instruments, clusters = conn.execute("""
        SELECT MIN(information_cutoff), MAX(information_cutoff), COUNT(*),
               COUNT(DISTINCT instrument_id), COUNT(DISTINCT event_cluster_id)
        FROM research_observations
    """).fetchone()
    horizons = tuple(sorted({
        str(row[0]).split(".")[0]
        for row in conn.execute("SELECT DISTINCT name FROM research_labels")
        if row[0] and "." in str(row[0])}))
    stored = conn.execute(
        "SELECT dataset_version FROM research_observations LIMIT 1").fetchone()

    version = dataset_version
    if version is None:
        version = DatasetVersion(version=(stored[0] if stored and stored[0]
                                          else "unknown"),
                                 created_at=created_at or datetime.now(UTC))
    return ResearchSnapshot(
        snapshot_id=snapshot_id,
        corpus_version_id=corpus_version_id,
        dataset_version=version,
        created_at=created_at or datetime.now(UTC),
        code_commit=code_commit,
        as_of_cutoff=as_of_cutoff or _parse(late) or datetime.now(UTC),
        state=ArtifactState.BUILDING,
        observation_count=int(observations or 0),
        instrument_count=int(instruments or 0),
        cluster_count=int(clusters or 0),
        earliest_information_time=_parse(early),
        latest_information_time=_parse(late),
        horizons=horizons,
        storage_location=storage_location,
        note=note)


# ======================================================================
# Persistence
# ======================================================================

def _guard_immutable(label: str, existing: Optional[Tuple[str, Optional[str]]],
                     state: ArtifactState,
                     content_hash: Optional[str]) -> bool:
    """
    Decide whether an immutable row may be written again.

    Exactly one change is legal once an artifact is frozen: FROZEN ->
    ELIGIBLE_FOR_RESEARCH, with the content hash unchanged. Eligibility
    is a publishing decision layered on top of frozen content, not a
    change to it. Anything else -- a different hash, a state that moves
    backwards -- is refused.

    Returns True when the write should be skipped as a no-op.
    """
    if not existing:
        return False
    was = ArtifactState(existing[0])
    if not was.is_immutable:
        return False
    if existing[1] != content_hash:
        raise FrozenArtifactError(
            f"{label} is {was.value} and its content hash would change -- "
            f"a frozen artifact cannot be rewritten")
    if state is was:
        return True
    if was is ArtifactState.FROZEN and state is ArtifactState.ELIGIBLE_FOR_RESEARCH:
        return False
    raise FrozenArtifactError(
        f"{label} is {was.value}; {was.value} -> {state.value} is not a legal "
        f"transition for a frozen artifact")


def record_corpus_version(conn: sqlite3.Connection, version: CorpusVersion) -> None:
    """
    Insert or update a corpus version.

    A frozen version's content cannot be rewritten; only its eligibility
    may advance. See `_guard_immutable`.
    """
    existing = conn.execute(
        "SELECT state, content_hash FROM corpus_versions WHERE version_id = ?",
        (version.version_id,)).fetchone()
    if _guard_immutable(version.version_id, existing, version.state,
                        version.content_hash):
        return
    conn.execute("""
        INSERT OR REPLACE INTO corpus_versions
            (version_id, state, created_at, code_commit, schema_version,
             recovery_run_id, earliest_information_time, latest_information_time,
             counts_json, content_hash, frozen_at, storage_location, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        version.version_id, version.state.value, version.created_at.isoformat(),
        version.code_commit, version.schema_version, version.recovery_run_id,
        version.earliest_information_time.isoformat()
        if version.earliest_information_time else None,
        version.latest_information_time.isoformat()
        if version.latest_information_time else None,
        json.dumps(dict(version.counts), sort_keys=True), version.content_hash,
        version.frozen_at.isoformat() if version.frozen_at else None,
        version.storage_location, version.note))


def record_snapshot(conn: sqlite3.Connection, snapshot: ResearchSnapshot) -> None:
    existing = conn.execute(
        "SELECT state, content_hash FROM research_snapshots WHERE snapshot_id = ?",
        (snapshot.snapshot_id,)).fetchone()
    if _guard_immutable(snapshot.snapshot_id, existing, snapshot.state,
                        snapshot.content_hash):
        return
    conn.execute("""
        INSERT OR REPLACE INTO research_snapshots
            (snapshot_id, corpus_version_id, dataset_version, universe_version,
             event_version, state, created_at, code_commit, as_of_cutoff,
             observation_count, instrument_count, cluster_count,
             earliest_information_time, latest_information_time, horizons_json,
             content_hash, frozen_at, storage_location, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snapshot.snapshot_id, snapshot.corpus_version_id,
        snapshot.dataset_version.fingerprint(), snapshot.universe_version,
        snapshot.event_version, snapshot.state.value,
        snapshot.created_at.isoformat(), snapshot.code_commit,
        snapshot.as_of_cutoff.isoformat(), snapshot.observation_count,
        snapshot.instrument_count, snapshot.cluster_count,
        snapshot.earliest_information_time.isoformat()
        if snapshot.earliest_information_time else None,
        snapshot.latest_information_time.isoformat()
        if snapshot.latest_information_time else None,
        json.dumps(list(snapshot.horizons)), snapshot.content_hash,
        snapshot.frozen_at.isoformat() if snapshot.frozen_at else None,
        snapshot.storage_location, snapshot.note))


def record_recovery_run(conn: sqlite3.Connection, run: RecoveryRun) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO recovery_runs
            (recovery_run_id, pipeline_version, code_commit, source_snapshot_id,
             range_start, range_end, started_at, completed_at, status,
             processed_through, counts_json, errors_json, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run.recovery_run_id, run.pipeline_version, run.code_commit,
        run.source_snapshot_id, run.range_start.isoformat(),
        run.range_end.isoformat(), run.started_at.isoformat(),
        run.completed_at.isoformat() if run.completed_at else None,
        run.status.value,
        run.processed_through.isoformat() if run.processed_through else None,
        json.dumps(dict(run.counts), sort_keys=True),
        json.dumps(list(run.errors)), run.note))


def record_provenance(conn: sqlite3.Connection, *, record_type: str,
                      rows: Iterable[Tuple[str, Optional[str], Optional[str]]],
                      recovery_run_id: str, corpus_version: str,
                      recovered_at: datetime) -> int:
    """
    Mark records as produced by a recovery run.

    `rows` are `(record_id, information_time, processing_time)`. The
    recovery instant is stored separately and never substituted for
    either -- that substitution is the failure this phase exists to
    prevent.
    """
    payload = [
        (record_type, record_id, recovery_run_id, corpus_version,
         information_time, processing_time, recovered_at.isoformat())
        for record_id, information_time, processing_time in rows
    ]
    conn.executemany("""
        INSERT OR REPLACE INTO recovery_provenance
            (record_type, record_id, recovery_run_id, corpus_version,
             information_time, processing_time, recovered_at)
        VALUES (?,?,?,?,?,?,?)
    """, payload)
    return len(payload)


def record_cross_version_mapping(conn: sqlite3.Connection,
                                 mapping: CrossVersionMapping) -> None:
    conn.execute("""
        INSERT INTO cross_version_event_map
            (relation, old_corpus_version, new_corpus_version,
             old_event_ids_json, new_event_ids_json, fingerprint, note)
        VALUES (?,?,?,?,?,?,?)
    """, (mapping.relation.value, mapping.old_corpus_version,
          mapping.new_corpus_version, json.dumps(list(mapping.old_event_ids)),
          json.dumps(list(mapping.new_event_ids)), mapping.fingerprint,
          mapping.note))


# ======================================================================
# Reading back
# ======================================================================

def load_corpus_version(conn: sqlite3.Connection,
                        version_id: str) -> Optional[CorpusVersion]:
    row = conn.execute("""
        SELECT version_id, state, created_at, code_commit, schema_version,
               recovery_run_id, earliest_information_time, latest_information_time,
               counts_json, content_hash, frozen_at, storage_location, note
        FROM corpus_versions WHERE version_id = ?""", (version_id,)).fetchone()
    if not row:
        return None
    return CorpusVersion(
        version_id=row[0], state=ArtifactState(row[1]),
        created_at=_parse(row[2]), code_commit=row[3], schema_version=row[4],
        recovery_run_id=row[5], earliest_information_time=_parse(row[6]),
        latest_information_time=_parse(row[7]),
        counts=json.loads(row[8] or "{}"), content_hash=row[9],
        frozen_at=_parse(row[10]), storage_location=row[11] or "",
        note=row[12] or "")


def load_snapshot(conn: sqlite3.Connection,
                  snapshot_id: str) -> Optional[ResearchSnapshot]:
    row = conn.execute("""
        SELECT snapshot_id, corpus_version_id, dataset_version, universe_version,
               event_version, state, created_at, code_commit, as_of_cutoff,
               observation_count, instrument_count, cluster_count,
               earliest_information_time, latest_information_time, horizons_json,
               content_hash, frozen_at, storage_location, note
        FROM research_snapshots WHERE snapshot_id = ?""",
        (snapshot_id,)).fetchone()
    if not row:
        return None
    return ResearchSnapshot(
        snapshot_id=row[0], corpus_version_id=row[1],
        dataset_version=DatasetVersion(version=row[2].split("|")[0],
                                       created_at=_parse(row[6])),
        universe_version=row[3], event_version=row[4],
        state=ArtifactState(row[5]), created_at=_parse(row[6]),
        code_commit=row[7], as_of_cutoff=_parse(row[8]),
        observation_count=row[9], instrument_count=row[10],
        cluster_count=row[11], earliest_information_time=_parse(row[12]),
        latest_information_time=_parse(row[13]),
        horizons=tuple(json.loads(row[14] or "[]")), content_hash=row[15],
        frozen_at=_parse(row[16]), storage_location=row[17] or "",
        note=row[18] or "")


def load_recovery_run(conn: sqlite3.Connection,
                      recovery_run_id: str) -> Optional[RecoveryRun]:
    row = conn.execute("""
        SELECT recovery_run_id, pipeline_version, code_commit, source_snapshot_id,
               range_start, range_end, started_at, completed_at, status,
               processed_through, counts_json, errors_json, note
        FROM recovery_runs WHERE recovery_run_id = ?""",
        (recovery_run_id,)).fetchone()
    if not row:
        return None
    return RecoveryRun(
        recovery_run_id=row[0], pipeline_version=row[1], code_commit=row[2],
        source_snapshot_id=row[3], range_start=_parse(row[4]),
        range_end=_parse(row[5]), started_at=_parse(row[6]),
        completed_at=_parse(row[7]), status=RecoveryStatus(row[8]),
        processed_through=_parse(row[9]), counts=json.loads(row[10] or "{}"),
        errors=tuple(json.loads(row[11] or "[]")), note=row[12] or "")


def list_corpus_versions(conn: sqlite3.Connection) -> List[CorpusVersion]:
    ids = [row[0] for row in conn.execute(
        "SELECT version_id FROM corpus_versions ORDER BY created_at, version_id")]
    return [v for v in (load_corpus_version(conn, i) for i in ids) if v]


def list_snapshots(conn: sqlite3.Connection) -> List[ResearchSnapshot]:
    ids = [row[0] for row in conn.execute(
        "SELECT snapshot_id FROM research_snapshots ORDER BY created_at, snapshot_id")]
    return [s for s in (load_snapshot(conn, i) for i in ids) if s]


# ======================================================================
# Freezing
# ======================================================================

def freeze_corpus(conn: sqlite3.Connection, version: CorpusVersion, *,
                  at: Optional[datetime] = None) -> CorpusVersion:
    """
    VALIDATED -> FROZEN, with the content hash computed from the data
    at the moment of freezing. The hash is what makes the freeze
    checkable later.
    """
    at = at or datetime.now(UTC)
    digest = manifest_hash(corpus_manifest(conn))
    frozen = version.transition_to(ArtifactState.FROZEN, at=at,
                                   content_hash=digest)
    record_corpus_version(conn, frozen)
    return frozen


def freeze_snapshot(conn: sqlite3.Connection, snapshot: ResearchSnapshot, *,
                    at: Optional[datetime] = None,
                    dataset_version: Optional[str] = None) -> ResearchSnapshot:
    at = at or datetime.now(UTC)
    digest = manifest_hash(snapshot_manifest(conn, dataset_version=dataset_version))
    frozen = snapshot.transition_to(ArtifactState.FROZEN, at=at,
                                    content_hash=digest)
    record_snapshot(conn, frozen)
    return frozen


def verify_corpus_hash(conn: sqlite3.Connection,
                       version: CorpusVersion) -> bool:
    """
    Recompute and compare. False means the corpus changed after it was
    frozen, which is a finding, not a warning to be suppressed.
    """
    if not version.content_hash:
        return False
    return manifest_hash(corpus_manifest(conn)) == version.content_hash


def verify_snapshot_hash(conn: sqlite3.Connection, snapshot: ResearchSnapshot, *,
                         dataset_version: Optional[str] = None) -> bool:
    if not snapshot.content_hash:
        return False
    return manifest_hash(snapshot_manifest(
        conn, dataset_version=dataset_version)) == snapshot.content_hash


# ======================================================================
# Cross-version comparison
# ======================================================================

def compare_corpora(old_conn: sqlite3.Connection,
                    new_conn: sqlite3.Connection, *,
                    old_version: str = CORPUS_V1,
                    new_version: str = "canonical_corpus_v2"
                    ) -> Tuple[List[CrossVersionMapping], Dict[str, int]]:
    """
    Match canonical events across two corpora by fingerprint.

    Fingerprint equality means the same underlying event was identified,
    whatever id each build assigned it. Where fingerprints do not match,
    the event is reported as NEW or REMOVED rather than forced into a
    correspondence -- an improved pipeline is allowed to cluster
    differently, and pretending otherwise would hide exactly the
    difference a later phase needs to explain.
    """
    def by_fingerprint(conn: sqlite3.Connection) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for event_id, fingerprint in conn.execute(
                "SELECT canonical_event_id, fingerprint FROM canonical_events"):
            out.setdefault(fingerprint or f"__nofp__{event_id}", []).append(event_id)
        return out

    old = by_fingerprint(old_conn)
    new = by_fingerprint(new_conn)
    mappings: List[CrossVersionMapping] = []
    tally: Dict[str, int] = {r.value: 0 for r in CrossVersionRelation}

    for fingerprint in sorted(set(old) | set(new)):
        old_ids = sorted(old.get(fingerprint, ()))
        new_ids = sorted(new.get(fingerprint, ()))
        if old_ids and new_ids:
            if len(old_ids) == 1 and len(new_ids) == 1:
                relation = CrossVersionRelation.SAME
            elif len(old_ids) > 1 and len(new_ids) == 1:
                relation = CrossVersionRelation.MERGED
            elif len(old_ids) == 1 and len(new_ids) > 1:
                relation = CrossVersionRelation.SPLIT
            else:
                relation = CrossVersionRelation.CHANGED
        elif new_ids:
            relation = CrossVersionRelation.NEW
        else:
            relation = CrossVersionRelation.REMOVED

        mappings.append(CrossVersionMapping(
            relation=relation, old_corpus_version=old_version,
            new_corpus_version=new_version,
            old_event_ids=tuple(old_ids), new_event_ids=tuple(new_ids),
            fingerprint=fingerprint if not fingerprint.startswith("__nofp__") else None))
        tally[relation.value] += 1

    return mappings, tally
