"""
Generates Phase 10 signals from the predictions, features and context
already stored by Phases 7, 8 and 9.

WHERE EVERY INPUT COMES FROM
--------------------------------
  predictions          -> Phase 9 `predictions` (test-set only; see
                          train_models.py for why in-sample
                          predictions must never become signals)
  information_cutoff   -> Phase 7 `research_observations`
  data quality         -> Phase 7 observation quality level
  volatility / volume  -> Phase 8 `research_features`
  event context        -> Phase 5 `canonical_events`
  small-sample flag    -> Phase 9 `model_evaluations`

Nothing is recomputed here. The script's job is to assemble what
already exists into a GenerationContext and hand it to the engine.

THE CUTOFF IS INHERITED, NOT INVENTED
-----------------------------------------
Every signal's source_information_cutoff comes from the observation
that produced its prediction. That is the same cutoff Phase 6 computed
and Phase 7 recorded, so the whole chain agrees on when each piece of
information became knowable. Deriving a fresh cutoff here would be a
second opinion, and two opinions eventually disagree.

THE SMALL-SAMPLE FLAG IS PROPAGATED, NOT DROPPED
----------------------------------------------------
Phase 9 marks evaluations whose effective sample is below the minimum.
On the current dataset that is essentially all of them. That flag
travels into the confidence calculation and, by default, into a
suppression reason — so signals built on thin evidence are visibly
withheld rather than quietly issued. This is the honest behaviour on a
dataset this small, and it will relax on its own as history
accumulates.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- All source tables are read-only inputs.
- --dry-run generates and validates without writing.
- Re-running is idempotent by signal identity: the same information
  state does not produce a second signal.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.model_schema import initialize_model_schema
from src.data_access.signal_schema import initialize_signal_schema
from src.data_access.signal_repository import SignalRepository
from src.domain.model_models import Prediction
from src.domain.signal_models import SignalContext, SignalStrategyDefinition, SignalType
from src.signals.engine import SignalEngine
from src.signals.strategy import DEFAULT_STRENGTH_SCALE, GenerationContext, MLDirectionalStrategy
from src.signals.validator import SignalValidator, ValidationConfig

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

STRATEGY = SignalStrategyDefinition(
    strategy_id="ml_directional",
    name="ML Directional (ridge on abnormal return)",
    version="v1",
    signal_type=SignalType.DIRECTIONAL,
    description=("Turns Phase 9 ridge-regression predictions of 5-day abnormal "
                 "return into directional candidates."),
    configuration_version="v1",
    parameters={"strength_scale": DEFAULT_STRENGTH_SCALE, "horizon_days": 5},
)


def load_small_sample_models(conn: sqlite3.Connection) -> set:
    """Trained models whose evaluation was flagged small-sample."""
    rows = conn.execute(
        "SELECT DISTINCT trained_model_id FROM model_evaluations WHERE small_sample = 1"
    ).fetchall()
    return {row[0] for row in rows}


def load_contexts(conn: sqlite3.Connection, limit: Optional[int]) -> List[GenerationContext]:
    """
    Assemble one GenerationContext per observation that has at least
    one prediction.

    Observations without predictions are skipped rather than given an
    empty context: a strategy with nothing to work from produces no
    candidate, and manufacturing an empty context would only create
    noise in the report.
    """
    small_sample_models = load_small_sample_models(conn)

    prediction_rows = conn.execute("""
        SELECT p.prediction_id, p.trained_model_id, p.model_qualified_id, p.observation_id,
               p.predicted_value, p.predicted_class, p.class_probabilities_json,
               p.confidence, p.prediction_interval_low, p.prediction_interval_high,
               p.uncertainty_basis, p.information_cutoff, p.feature_set_version,
               p.predicted_at, p.is_abstention, p.abstention_reason
        FROM predictions p
        ORDER BY p.information_cutoff DESC
    """).fetchall()

    by_observation: Dict[str, List[Prediction]] = defaultdict(list)
    flagged: Dict[str, bool] = defaultdict(bool)
    for row in prediction_rows:
        prediction = Prediction(
            prediction_id=row[0], trained_model_id=row[1], model_qualified_id=row[2],
            observation_id=row[3], predicted_value=row[4], predicted_class=row[5],
            class_probabilities=json.loads(row[6]) if row[6] else None,
            confidence=row[7], prediction_interval_low=row[8],
            prediction_interval_high=row[9], uncertainty_basis=row[10] or "",
            information_cutoff=datetime.fromisoformat(row[11]) if row[11] else None,
            feature_set_version=row[12],
            predicted_at=datetime.fromisoformat(row[13]) if row[13] else None,
            is_abstention=bool(row[14]), abstention_reason=row[15],
        )
        by_observation[row[3]].append(prediction)
        if row[1] in small_sample_models:
            flagged[row[3]] = True

    if not by_observation:
        return []

    observation_ids = list(by_observation.keys())
    if limit:
        observation_ids = observation_ids[:limit]
    placeholders = ",".join("?" * len(observation_ids))

    observation_rows = conn.execute(f"""
        SELECT observation_id, instrument_id, information_cutoff, quality_level,
               event_id, dataset_version, feature_version
        FROM research_observations WHERE observation_id IN ({placeholders})
    """, observation_ids).fetchall()

    feature_rows = conn.execute(f"""
        SELECT observation_id, qualified_name, value_json
        FROM research_features WHERE observation_id IN ({placeholders})
    """, observation_ids).fetchall()
    features_by_observation: Dict[str, Dict[str, Optional[float]]] = defaultdict(dict)
    for observation_id, name, value_json in feature_rows:
        try:
            value = json.loads(value_json) if value_json else None
        except (ValueError, TypeError):
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            features_by_observation[observation_id][name] = float(value)

    event_ids = [row[4] for row in observation_rows if row[4]]
    events: Dict[str, tuple] = {}
    if event_ids:
        event_placeholders = ",".join("?" * len(event_ids))
        for row in conn.execute(f"""
            SELECT canonical_event_id, event_type, corroboration_state,
                   independent_source_count
            FROM canonical_events WHERE canonical_event_id IN ({event_placeholders})
        """, event_ids):
            events[row[0]] = (row[1], row[2], row[3])

    contexts = []
    for (observation_id, instrument_id, cutoff_str, quality_level,
         event_id, dataset_version, feature_version) in observation_rows:
        if not cutoff_str:
            continue
        features = features_by_observation.get(observation_id, {})
        event_type, corroboration, source_count = events.get(event_id, (None, None, None))

        contexts.append(GenerationContext(
            instrument_id=instrument_id,
            information_cutoff=datetime.fromisoformat(cutoff_str),
            predictions=by_observation[observation_id],
            features=features,
            observation_id=observation_id,
            event_id=event_id,
            feature_set_version=feature_version,
            dataset_version=dataset_version,
            small_sample_evidence=flagged.get(observation_id, False),
            context=SignalContext(
                volatility_percentile=features.get("volatility.percentile_20d"),
                relative_volume=features.get("liquidity.relative_volume_20d"),
                event_type=event_type,
                event_corroboration_state=corroboration,
                independent_source_count=source_count,
                data_quality_level=quality_level,
            ),
        ))
    return contexts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max observations to process, newest first.")
    parser.add_argument("--min-confidence", type=float, default=0.25)
    parser.add_argument("--min-strength", type=float, default=0.10)
    parser.add_argument("--allow-small-sample", action="store_true",
                        help="Do NOT suppress signals whose evidence was flagged "
                             "small-sample. Off by default; see module docstring.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_signal_schema(conn)
    # Phase 9's schema is created here too, defensively: this script
    # READS `predictions`, and a raw "no such table" from SQLite is a
    # much worse error message than the explicit check below. Creating
    # it is harmless (IF NOT EXISTS) and makes the real problem —
    # "Phase 9 has not run yet" — say so plainly.
    initialize_model_schema(conn)
    repository = SignalRepository(conn)
    repository.save_strategy(STRATEGY)

    prediction_count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    if prediction_count == 0:
        print("Nicio predictie in baza.")
        print("Ruleaza intai 'Train Models (Phase 9)' — acesta populeaza tabelul")
        print("`predictions`, din care Faza 10 isi construieste semnalele.")
        conn.close()
        return 2

    contexts = load_contexts(conn, args.limit)
    print(f"Predictii in baza: {prediction_count:,}")
    print(f"Contexte de generare (observatii cu predictii): {len(contexts):,}")
    if not contexts:
        print("Predictiile existente nu au observatii corespunzatoare cu information_cutoff.")
        conn.close()
        return 2

    flagged = sum(1 for c in contexts if c.small_sample_evidence)
    print(f"  dintre care cu dovezi de esantion mic: {flagged:,}")

    config = ValidationConfig(
        version="v1",
        min_confidence=args.min_confidence,
        min_strength=args.min_strength,
    )
    validator = SignalValidator(config)
    engine = SignalEngine(repository, validator)
    strategies = [MLDirectionalStrategy(STRATEGY)]

    if args.allow_small_sample:
        # Removing the caveat is what disables the corresponding
        # suppression — the flag is not deleted from the record, only
        # from the text the validator inspects.
        for context in contexts:
            context.small_sample_evidence = False
        print("  ATENTIE: suprimarea pentru esantion mic e DEZACTIVATA (--allow-small-sample)")

    report = engine.run(strategies, contexts, apply=args.apply)

    print()
    print(f"Candidati generati    : {report.candidates_generated:,}")
    print(f"Semnale create        : {report.signals_created:,}")
    print(f"Semnale suprimate     : {report.signals_suppressed:,}")
    print(f"Duplicate sarite      : {report.duplicates_skipped:,}")
    print(f"Semnale inlocuite     : {report.signals_superseded:,}")
    print(f"Contabilitate corecta : {report.is_balanced}")

    if report.suppression_reasons:
        print()
        print("Motive de suprimare:")
        for reason, count in sorted(report.suppression_reasons.items(),
                                    key=lambda kv: -kv[1]):
            print(f"  {reason:30s} {count:>6,}")

    for note in report.notes:
        print(f"  NOTA: {note}")

    if not args.apply:
        print("\nDRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    expired = engine.expire_stale()
    if expired:
        print(f"\nSemnale expirate (validitate depasita): {expired:,}")

    active = len(repository.active_signals())
    print(f"\nSCRIS. Semnale active in baza: {active:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
