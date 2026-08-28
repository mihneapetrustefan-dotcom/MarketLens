"""
Assembles Phase 7 research observations from Phase 6 event studies —
splitting each into an InformationSnapshot (pre-event features) and an
OutcomeSet (post-event labels), with the leakage gate applied.

WHY THIS SCRIPT NEVER TOUCHES POLYGON OR EVEN price_candle_cache
DIRECTLY FOR PRICES
----------------------------------------------------------------------
Everything numeric here already exists in event_studies /
event_study_returns / event_study_volume / event_study_volatility
(Phase 6's output). This script's only job is to REFRAME that output
through the feature/label split Phase 7's domain model requires — not
to recompute anything. The one exception is price_candle_cache, read
ONLY to recover each window's trading-day boundary (for label
timestamps), using the exact same EventStudyEngine.window_bounds()
method Phase 6 itself used — not a re-derivation, a re-use.

THE CUTOFF IS market_visibility_latest, ALREADY COMPUTED BY PHASE 6
----------------------------------------------------------------------
event_studies.market_visibility_latest is the conservative "latest
plausible moment this became public" figure the point-in-time engine
already produced. Reusing it here — rather than recomputing from
scratch — is what keeps Phase 6 and Phase 7 from silently disagreeing
about when an event became knowable.

WHAT BECOMES A FEATURE VS A LABEL
--------------------------------------
FEATURES (InformationSnapshot), all with as_of <= information_cutoff:
  - event.event_type, event.category — contemporaneous event
    attributes (spec §27's stated exception: known exactly at the
    event, not a leak).
  - event.independent_source_count, event.quality_confidence — Phase
    5 fusion's own assessment of the report set, contemporaneous.
  - entity.asset_class, entity.exchange_id, entity.ticker — static
    structural facts about the instrument, contemporaneous.
  - liquidity.baseline_mean_volume, liquidity.baseline_std_volume,
    volatility.pre_volatility — computed by Phase 6 EXCLUSIVELY from
    the pre-event baseline window, so legitimately known at the
    cutoff; as_of is set to the cutoff itself.

LABELS (OutcomeSet), all with measured_at > information_cutoff:
  - {window}.raw_return, {window}.abnormal_return
  - {window}.relative_volume, {window}.volume_zscore
  - {window}.volatility_change_pct
  measured_at is each window's END boundary, recomputed via
  EventStudyEngine.window_bounds() (minute windows: wall-clock;
  trading-day windows: walked over the real session calendar from
  price_candle_cache) — never the event time itself.

QUALITY IS NEVER USED TO DROP A ROW HERE
--------------------------------------------
An UNUSABLE Phase 6 study still gets a research_observations row here,
marked INVALID with the reason recorded (spec §20: bad observations
are marked and kept, never silently deleted). Filtering to only usable
observations is DatasetBuilder's job at read time, not this script's
job at write time.

EVENT_CLUSTER_ID: SAME INSTRUMENT, STATED PLAINLY AS A LIMITATION
----------------------------------------------------------------------
Observations are clustered by instrument_id — multiple events on the
same company are correlated, and clustering standard errors by
instrument is the minimum defensible grouping. This does NOT cluster
same-day macro events across different companies (e.g. a rate
decision affecting many instruments at once); that would need a
cross-instrument event-clustering step this project has not built.
Stated here rather than silently under-clustering.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- canonical_events, event_studies, and price_candle_cache are
  read-only inputs.
- --dry-run builds every observation in memory and reports the
  outcome without writing.
- Re-running is safe: observation ids are derived deterministically
  from (event_id, instrument_id), and features/labels for an
  observation are fully replaced (delete-then-insert) each run rather
  than accumulated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.research_schema import initialize_research_schema
from src.domain.impact_models import DEFAULT_WINDOWS
from src.domain.research_models import (
    DatasetVersion, FeatureNamespace, FeatureValue, LabelValue,
    InformationSnapshot, OutcomeSet, ResearchObservation, ResearchQuality,
    SampleQuality, ExclusionReason,
)
from src.impact.engine import EventStudyEngine

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")
DATASET_VERSION = DatasetVersion(version="v1")

_WINDOW_BY_NAME = {w.name: w for w in DEFAULT_WINDOWS}
_QUALITY_MAP = {"high": ResearchQuality.HIGH, "medium": ResearchQuality.MEDIUM,
                "low": ResearchQuality.LOW, "unusable": ResearchQuality.INVALID}


def observation_id_for(event_id: str, instrument_id: str) -> str:
    """Stable observation id, so re-running rewrites rather than duplicates."""
    digest = hashlib.sha1(f"{event_id}|{instrument_id}".encode("utf-8")).hexdigest()
    return f"obs-{digest[:16]}"


def load_daily_timestamps(conn: sqlite3.Connection, instrument_id: str) -> List[datetime]:
    rows = conn.execute(
        "SELECT timestamp FROM price_candle_cache WHERE instrument_id = ? AND interval = '1d'",
        (instrument_id,)).fetchall()
    return [datetime.fromisoformat(r[0]) for r in rows]


def window_end(engine: EventStudyEngine, anchor: datetime, window_name: str,
               daily_timestamps: List[datetime]) -> Optional[datetime]:
    window = _WINDOW_BY_NAME.get(window_name)
    if window is None:
        return None
    _, end = engine.window_bounds(anchor, window, session_timestamps=daily_timestamps)
    return end


def build_observation(conn: sqlite3.Connection, engine: EventStudyEngine,
                      study_row, daily_ts_cache: Dict[str, List[datetime]]) -> ResearchObservation:
    (event_id, instrument_id, benchmark_id, event_time, publication_time,
     visibility_latest, quality_level, exclusions_json) = study_row

    cutoff = datetime.fromisoformat(visibility_latest) if visibility_latest else \
        (datetime.fromisoformat(publication_time) if publication_time else None)

    event_row = conn.execute(
        "SELECT event_type, category, independent_source_count, quality_confidence "
        "FROM canonical_events WHERE canonical_event_id = ?", (event_id,)).fetchone()
    e_type, e_category, source_count, fusion_conf = event_row if event_row else (None, None, None, None)

    inst_row = conn.execute(
        "SELECT ticker, asset_class, exchange_id FROM instruments WHERE instrument_id = ?",
        (instrument_id,)).fetchone()
    ticker, asset_class, exchange_id = inst_row if inst_row else (None, None, None)

    snapshot = InformationSnapshot(information_cutoff=cutoff, cutoff_basis="market_visibility_latest")
    outcomes = OutcomeSet(information_cutoff=cutoff)
    quality = SampleQuality(level=_QUALITY_MAP.get(quality_level, ResearchQuality.INVALID))
    if quality_level == "unusable":
        for reason in json.loads(exclusions_json or "[]"):
            quality.notes.append(f"phase6: {reason}")
        quality.exclude(ExclusionReason.INSUFFICIENT_PRICE_DATA, "Phase 6 study quality was UNUSABLE")

    def add_feature(name, namespace, value, contemporaneous=False, as_of=None):
        if value is None:
            return
        snapshot.add(FeatureValue(
            name=name, namespace=namespace, value=value,
            as_of=(cutoff if as_of is None else as_of),
            source="phase6_event_study", is_contemporaneous_event_attribute=contemporaneous,
        ))

    add_feature("event_type", FeatureNamespace.EVENT, e_type, contemporaneous=True)
    add_feature("category", FeatureNamespace.EVENT, e_category, contemporaneous=True)
    add_feature("independent_source_count", FeatureNamespace.EVENT, source_count, contemporaneous=True)
    add_feature("quality_confidence", FeatureNamespace.EVENT, fusion_conf, contemporaneous=True)
    add_feature("ticker", FeatureNamespace.ENTITY, ticker, contemporaneous=True)
    add_feature("asset_class", FeatureNamespace.ENTITY, asset_class, contemporaneous=True)
    add_feature("exchange_id", FeatureNamespace.ENTITY, exchange_id, contemporaneous=True)

    if instrument_id not in daily_ts_cache:
        daily_ts_cache[instrument_id] = load_daily_timestamps(conn, instrument_id)
    daily_timestamps = daily_ts_cache[instrument_id]

    for window_name, base_vol, base_vol_std, pre_vol in conn.execute(
        "SELECT window_name, baseline_mean_volume, baseline_std_volume, NULL FROM event_study_volume "
        "WHERE study_id = (SELECT study_id FROM event_studies WHERE event_id=? AND instrument_id=?) "
        "ORDER BY window_name LIMIT 1", (event_id, instrument_id)).fetchall():
        add_feature("baseline_mean_volume", FeatureNamespace.LIQUIDITY, base_vol)
        add_feature("baseline_std_volume", FeatureNamespace.LIQUIDITY, base_vol_std)

    for (pre_vol,) in conn.execute(
        "SELECT pre_volatility FROM event_study_volatility "
        "WHERE study_id = (SELECT study_id FROM event_studies WHERE event_id=? AND instrument_id=?) "
        "ORDER BY window_name LIMIT 1", (event_id, instrument_id)).fetchall():
        add_feature("pre_volatility", FeatureNamespace.VOLATILITY, pre_vol)

    study_id_row = conn.execute(
        "SELECT study_id FROM event_studies WHERE event_id=? AND instrument_id=?",
        (event_id, instrument_id)).fetchone()
    study_id = study_id_row[0] if study_id_row else None

    if study_id and cutoff:
        for window_name, raw_ret, abnormal_ret in conn.execute(
            "SELECT window_name, raw_return, abnormal_return FROM event_study_returns WHERE study_id = ?",
            (study_id,)).fetchall():
            measured_at = window_end(engine, cutoff, window_name, daily_timestamps)
            if measured_at is None or measured_at <= cutoff:
                continue
            if raw_ret is not None:
                outcomes.add(LabelValue(name=f"{window_name}.raw_return", value=raw_ret,
                                        measured_at=measured_at, window_name=window_name))
            if abnormal_ret is not None:
                outcomes.add(LabelValue(name=f"{window_name}.abnormal_return", value=abnormal_ret,
                                        measured_at=measured_at, window_name=window_name))

        for window_name, rel_vol, zscore in conn.execute(
            "SELECT window_name, relative_volume, volume_zscore FROM event_study_volume WHERE study_id = ?",
            (study_id,)).fetchall():
            measured_at = window_end(engine, cutoff, window_name, daily_timestamps)
            if measured_at is None or measured_at <= cutoff:
                continue
            if rel_vol is not None:
                outcomes.add(LabelValue(name=f"{window_name}.relative_volume", value=rel_vol,
                                        measured_at=measured_at, window_name=window_name))
            if zscore is not None:
                outcomes.add(LabelValue(name=f"{window_name}.volume_zscore", value=zscore,
                                        measured_at=measured_at, window_name=window_name))

        for window_name, vol_change in conn.execute(
            "SELECT window_name, volatility_change_pct FROM event_study_volatility WHERE study_id = ?",
            (study_id,)).fetchall():
            measured_at = window_end(engine, cutoff, window_name, daily_timestamps)
            if measured_at is None or measured_at <= cutoff or vol_change is None:
                continue
            outcomes.add(LabelValue(name=f"{window_name}.volatility_change_pct", value=vol_change,
                                    measured_at=measured_at, window_name=window_name))

    violations = snapshot.validate() + outcomes.validate()
    if violations:
        quality.exclude(ExclusionReason.LEAKAGE_DETECTED, "; ".join(violations[:3]))

    return ResearchObservation(
        observation_id=observation_id_for(event_id, instrument_id),
        event_id=event_id, instrument_id=instrument_id, benchmark_id=benchmark_id,
        event_type=e_type, event_time=None, information_time=cutoff,
        observation_created_at=datetime.now(timezone.utc),
        information=snapshot, outcomes=outcomes, quality=quality,
        dataset_version=DATASET_VERSION.fingerprint(),
        event_cluster_id=instrument_id,
    )


def persist(conn: sqlite3.Connection, obs: ResearchObservation) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO research_observations (
            observation_id, event_id, instrument_id, benchmark_id, event_type,
            event_time, information_time, observation_created_at, sector_id, geography,
            market_regime, information_cutoff, event_cluster_id, quality_level,
            exclusions_json, notes_json, dataset_version, label_version, feature_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        obs.observation_id, obs.event_id, obs.instrument_id, obs.benchmark_id, obs.event_type,
        _iso(obs.event_time), _iso(obs.information_time), _iso(obs.observation_created_at),
        obs.sector_id, obs.geography, obs.market_regime, _iso(obs.information_cutoff),
        obs.event_cluster_id, obs.quality.level.value,
        json.dumps([e.value for e in obs.quality.exclusions]), json.dumps(obs.quality.notes),
        obs.dataset_version, obs.label_version, obs.feature_version,
    ))

    conn.execute("DELETE FROM research_features WHERE observation_id = ?", (obs.observation_id,))
    for feature in (obs.information.features.values() if obs.information else []):
        conn.execute("""
            INSERT INTO research_features
            (observation_id, qualified_name, namespace, value_json, as_of, source,
             calculation, feature_version, is_contemporaneous)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (obs.observation_id, feature.qualified_name, feature.namespace.value,
              json.dumps(feature.value), _iso(feature.as_of), feature.source,
              feature.calculation, feature.feature_version, int(feature.is_contemporaneous_event_attribute)))

    conn.execute("DELETE FROM research_labels WHERE observation_id = ?", (obs.observation_id,))
    for label in (obs.outcomes.labels.values() if obs.outcomes else []):
        conn.execute("""
            INSERT INTO research_labels
            (observation_id, name, value_json, measured_at, window_name, label_version, calculation)
            VALUES (?,?,?,?,?,?,?)
        """, (obs.observation_id, label.name, json.dumps(label.value), _iso(label.measured_at),
              label.window_name, label.label_version, label.calculation))


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max studies to convert, newest first.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_research_schema(conn)

    sql = """
        SELECT event_id, instrument_id, benchmark_id, event_time, publication_time,
               market_visibility_latest, quality_level, quality_issues_json
        FROM event_studies ORDER BY publication_time DESC
    """
    rows = conn.execute(sql).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"Studii de convertit: {len(rows):,}")

    engine = EventStudyEngine()
    daily_ts_cache: Dict[str, List[datetime]] = {}
    observations = [build_observation(conn, engine, row, daily_ts_cache) for row in rows]

    quality_counts = Counter(o.quality.level.value for o in observations)
    label_counts = Counter()
    for o in observations:
        label_counts["with_labels"] += 1 if (o.outcomes and o.outcomes.labels) else 0
    total_labels = sum(len(o.outcomes.labels) for o in observations if o.outcomes)
    total_features = sum(len(o.information.features) for o in observations if o.information)

    print("Calitate observatii:")
    for level, n in quality_counts.most_common():
        print(f"  {level:16s} {n:>6,}")
    print(f"Observatii cu cel putin o eticheta: {label_counts['with_labels']:,} / {len(observations):,}")
    print(f"Total caracteristici (features): {total_features:,}")
    print(f"Total etichete (labels)       : {total_labels:,}")

    if not args.apply:
        print("DRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    for obs in observations:
        persist(conn, obs)
    conn.commit()
    conn.close()
    print(f"SCRIS: {len(observations):,} observatii de cercetare")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
