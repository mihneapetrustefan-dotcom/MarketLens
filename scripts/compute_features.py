"""
Computes Phase 8 engineered features for every Phase 7 research
observation, and writes them into research_features alongside the
features Phase 7 already recorded.

WHY THE SAME TABLE, NOT A NEW ONE
------------------------------------
research_features already has a `source` column, unused until now.
Phase 7's rows carry source='phase6_event_study' (facts carried over
from the impact study); this script's rows carry
source='phase8_feature_engine' (values genuinely computed here).
One table means Phase 9 reads features from one place instead of
joining two, and the origin of every row stays inspectable per-row
rather than per-table.

HOW LEAKAGE IS PREVENTED — BY REUSE, NOT BY REIMPLEMENTATION
-----------------------------------------------------------------
This script does NOT filter data by cutoff itself. It hands raw
series to FeatureContext, whose accessors do the filtering through
Phase 6's PointInTimeView. That is deliberate: a second, parallel
implementation of "what was knowable when" is exactly how two parts
of a system drift into disagreeing. The cutoff itself comes from
research_observations.information_cutoff, which Phase 7 derived from
Phase 6's market_visibility_latest.

THE CANDLE ADAPTER, AND WHY IT EXISTS
----------------------------------------
FeatureContext.prices() reads `.price` off each candle. The cached
candles from Phase 6 expose `.close` / `.adjusted_close` instead —
they were built for EventStudyEngine, which uses different accessors.
_FeatureCandle is a thin read-only adapter bridging the two. The
alternative (adding `.price` to Phase 6's Candle) would mean editing a
completed, tested phase to suit a later one; adapting at the boundary
keeps both sides untouched.

Adjusted close is preferred over raw close: an unadjusted split would
otherwise register as a catastrophic one-day return.

PEER FEATURES ARE SKIPPED, DELIBERATELY AND VISIBLY
-------------------------------------------------------
peer.relative_return_5d and peer.dispersion_5d need DEFINED peer
relationships. entity_relationships exists but is empty, and the
feature library's own docstring warns against substituting "everything
in the sector" — this project's sectors hold up to 56 companies each,
far too heterogeneous for a meaningful peer median.

So peer_candles is left empty. Those two features compute to None and
are recorded as attempted-but-unavailable, NOT silently omitted: an
absent row and a null row mean different things, and the difference
matters when Phase 9 decides what to drop.

TODO(peers): build real peer relationships — most defensibly from
historical return correlation over the baseline window, using the
candles already cached — populate entity_relationships, then re-run
this script. The two features will fill in with no code change here.

ARCHIVED ARTICLES ARE READ BACK
-----------------------------------
`archive_old_articles.py --keep-days 60` moves articles older than the
retention window out of the live `articles` table into
`data/archives/articles_YYYY-MM.jsonl.gz`. Every news- and
sentiment-derived feature here read `FROM articles`, the live table
only, so an archived article was indistinguishable from one that never
existed. Measured consequence
(docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md):
sentiment.mean_7d, sentiment.dispersion_7d and news.source_diversity_7d
fell from ~100% coverage to 8.6-10.3%, while every price-derived
feature held at ~96%, and the design matrix lost every complete-case
row.

`src/data_access/article_archive.py` now supplies those rows. The
article->company links in `article_entities` are never pruned, so an
archived article's link survives and tells us which ids to ask for.
The archive is streamed ONCE, before the per-observation loop, and
only four scalars per article are retained.

This changes WHICH ARTICLES ARE VISIBLE, nothing else. Cutoff
filtering still happens inside FeatureContext, so an archived article
published after an observation's cutoff is excluded exactly as a live
one would be. A missing value stays missing: an article with no
parseable sentiment contributes no score rather than a zero.

SAFETY
------
- Only research_features rows with source='phase8_feature_engine' are
  ever deleted or rewritten; Phase 7's rows are never touched.
- The archive is opened read-only and never written, moved or deleted.
  A missing, truncated or malformed archive file is counted and
  skipped, never fatal.
- All other tables are read-only inputs.
- --dry-run computes everything and reports without writing.
- Re-running is safe: this script's own rows are replaced, not
  accumulated.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.article_archive import ArchivedArticle, ArticleArchive
from src.data_access.research_schema import initialize_research_schema
from src.features.engine import FeatureContext, FeatureEngine
from src.features.library import build_default_registry

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

#: Marks every row this script writes, distinguishing it from Phase 7's
#: carried-over rows in the same table.
FEATURE_SOURCE = "phase8_feature_engine"

#: Features skipped because peer relationships do not exist yet. Listed
#: explicitly so the skip is a stated decision, not an accident.
PEER_FEATURE_IDS = ("peer.relative_return_5d", "peer.dispersion_5d")


@dataclass(frozen=True)
class _FeatureCandle:
    """Read-only adapter: cached candle -> what FeatureContext expects."""
    timestamp: datetime
    price: Optional[float]
    volume: Optional[float]


@dataclass(frozen=True)
class _FeatureEvent:
    publication_time: datetime
    event_type: Optional[str]


@dataclass(frozen=True)
class _FeatureArticle:
    published_at: datetime
    source_name: Optional[str]
    sentiment_score: Optional[float]


def load_candles(conn: sqlite3.Connection, instrument_id: str) -> List[_FeatureCandle]:
    """Daily candles only — every Phase 8 feature is daily-frequency."""
    rows = conn.execute(
        "SELECT timestamp, close, adjusted_close, volume FROM price_candle_cache "
        "WHERE instrument_id = ? AND interval = '1d' ORDER BY timestamp",
        (instrument_id,)).fetchall()
    candles = []
    for ts, close, adjusted, volume in rows:
        price = adjusted if adjusted is not None else close
        candles.append(_FeatureCandle(datetime.fromisoformat(ts), price, volume))
    return candles


def load_entity_events(conn: sqlite3.Connection, instrument_id: str) -> List[_FeatureEvent]:
    """
    Every canonical event whose PRIMARY participant maps to this
    instrument — the entity's own event history, which recency,
    frequency and novelty features count over.
    """
    rows = conn.execute("""
        SELECT ce.first_reported_at, ce.event_type
        FROM canonical_events ce
        JOIN canonical_event_participants cep
            ON cep.canonical_event_id = ce.canonical_event_id AND cep.role = 'primary'
        JOIN companies co ON co.company_id = cep.entity_id
        JOIN securities s ON s.company_id = co.company_id
        JOIN instruments i ON i.security_id = s.security_id
        WHERE i.instrument_id = ? AND ce.first_reported_at IS NOT NULL
    """, (instrument_id,)).fetchall()
    return [_FeatureEvent(datetime.fromisoformat(ts), event_type) for ts, event_type in rows]


def load_archived_articles(conn: sqlite3.Connection,
                           archive: "ArticleArchive") -> Dict[str, ArchivedArticle]:
    """
    Every archived article that is still linked to a company, in ONE
    archive pass.

    WHY THIS IS SEPARATE FROM THE PER-INSTRUMENT LOADER. The archive
    is a set of gzip files, not an index; streaming it once per
    instrument would stream it ~389 times. So the whole wanted set is
    resolved up front and the per-instrument loader reads from the
    result.

    WHAT MAKES IT POSSIBLE. `archive_old_articles.py` removes rows from
    `articles` but never touches `article_entities`, so an archived
    article's company link survives in the database. The link tells us
    WHICH articles to ask the archive for; the archive supplies their
    timestamp, source and sentiment.

    Only ids absent from the live table are requested — a live article
    is read from the live table, as before, and is never overridden by
    an archived copy of itself.
    """
    if not archive.available:
        return {}
    linked = {r[0] for r in conn.execute("""
        SELECT ae.article_id
        FROM article_entities ae
        JOIN companies co ON co.company_id = ae.entity_id
        WHERE ae.entity_type = 'company'
    """)}
    if not linked:
        return {}
    live = {r[0] for r in conn.execute("SELECT article_id FROM articles")}
    return archive.fetch(linked - live)


def _article_sentiment_score(sentiment_json) -> Optional[float]:
    """
    The numeric score inside the stored sentiment blob, or None.

    None means "no score recorded", which is NOT zero: a stored
    `{"score": 0.0}` is a measured neutral and comes back as 0.0.
    Legacy rows predate schema validation, hence the tolerance.
    """
    if not sentiment_json:
        return None
    try:
        parsed = json.loads(sentiment_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    score = parsed.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def load_entity_articles(conn: sqlite3.Connection, instrument_id: str,
                         archived: Optional[Dict[str, ArchivedArticle]] = None
                         ) -> List[_FeatureArticle]:
    """
    Articles linked to this instrument's company via Phase 3's
    article_entities. Sentiment is stored as a JSON blob on the legacy
    articles table; the numeric score is pulled out of it here.

    ARCHIVED ARTICLES COUNT TOO. `archived` is the map built by
    `load_archived_articles`; when supplied, articles whose rows have
    been moved to `data/archives/` contribute to the news- and
    sentiment-derived features exactly as live ones do. Without it the
    behaviour is the live-table-only one this function has always had,
    which is what keeps every existing caller and test valid.

    An archived article with no parseable timestamp is skipped, the
    same as a live one: a feature window cannot place an article it
    cannot date, and inventing a date would fabricate evidence.
    """
    rows = conn.execute("""
        SELECT a.published_at, a.source, a.sentiment
        FROM articles a
        JOIN article_entities ae ON ae.article_id = a.article_id
        JOIN companies co ON co.company_id = ae.entity_id
        JOIN securities s ON s.company_id = co.company_id
        JOIN instruments i ON i.security_id = s.security_id
        WHERE i.instrument_id = ? AND a.published_at IS NOT NULL
    """, (instrument_id,)).fetchall()

    articles = []
    for published_at, source, sentiment_json in rows:
        try:
            moment = datetime.fromisoformat(published_at)
        except (ValueError, TypeError):
            continue
        articles.append(_FeatureArticle(moment, source,
                                        _article_sentiment_score(sentiment_json)))

    if archived:
        # The same join, minus `articles` -- which is the point: these
        # ids have no live row left to join to.
        archived_ids = [r[0] for r in conn.execute("""
            SELECT ae.article_id
            FROM article_entities ae
            JOIN companies co ON co.company_id = ae.entity_id
            JOIN securities s ON s.company_id = co.company_id
            JOIN instruments i ON i.security_id = s.security_id
            WHERE i.instrument_id = ? AND ae.entity_type = 'company'
        """, (instrument_id,))]
        for article_id in sorted(set(archived_ids)):
            record = archived.get(article_id)
            if record is None or not record.published_at:
                continue
            try:
                moment = datetime.fromisoformat(record.published_at)
            except (ValueError, TypeError):
                continue
            articles.append(_FeatureArticle(moment, record.source,
                                            record.sentiment_score))

    # Deterministic order regardless of where a row came from: the
    # feature engine's windows must see the same sequence on every run.
    # Naive timestamps are read as UTC for sorting only -- comparing
    # naive against aware raises, and the live table and the archive
    # are not guaranteed to agree on offsets.
    articles.sort(key=_article_sort_key)
    return articles


def _article_sort_key(article: _FeatureArticle):
    moment = article.published_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    score = article.sentiment_score
    return (moment, article.source_name or "",
            score if score is not None else float("-inf"))


def persist(conn: sqlite3.Connection, observation_id: str, values) -> int:
    """
    Replace this script's rows for one observation. Phase 7's rows in
    the same table are matched by source and left untouched.
    """
    conn.execute("DELETE FROM research_features WHERE observation_id = ? AND source = ?",
                 (observation_id, FEATURE_SOURCE))
    written = 0
    for feature_value in values.values():
        conn.execute("""
            INSERT OR REPLACE INTO research_features
            (observation_id, qualified_name, namespace, value_json, as_of, source,
             calculation, feature_version, is_contemporaneous)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            observation_id, feature_value.qualified_name, feature_value.namespace.value,
            json.dumps(feature_value.value), feature_value.as_of.isoformat() if feature_value.as_of else None,
            FEATURE_SOURCE, feature_value.calculation, feature_value.feature_version,
            int(feature_value.is_contemporaneous_event_attribute),
        ))
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max observations to process, newest first.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_research_schema(conn)

    sql = """
        SELECT ro.observation_id, ro.instrument_id, ro.information_cutoff, ro.event_type,
               ce.quality_confidence, ce.independent_source_count
        FROM research_observations ro
        LEFT JOIN canonical_events ce ON ce.canonical_event_id = ro.event_id
        WHERE ro.information_cutoff IS NOT NULL
        ORDER BY ro.information_cutoff DESC
    """
    rows = conn.execute(sql).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"Observatii de procesat: {len(rows):,}")

    registry = build_default_registry()
    engine = FeatureEngine(registry)
    all_feature_ids = sorted(registry._definitions.keys())
    print(f"Caracteristici in registru: {len(all_feature_ids)}")
    print(f"Sarite (fara relatii de peers definite): {', '.join(PEER_FEATURE_IDS)}")

    # ARCHIVED ARTICLES, ONE PASS, BEFORE THE LOOP.
    #
    # `archive_old_articles.py --keep-days 60` moves old articles out of
    # `articles`, and every news- and sentiment-derived feature read
    # that table only -- so an archived article looked identical to one
    # that never existed, and those features went null for ~90% of the
    # corpus. The rows were never lost; nothing read them back. This
    # reads them back.
    archive = ArticleArchive.for_database(args.db)
    archived_articles = load_archived_articles(conn, archive)
    if archive.files():
        print(f"Arhiva de articole: {archive.stats.summary()}")
        print(f"  articole arhivate inca legate la o companie: {len(archived_articles):,}")
        if archive.stats.files_unreadable:
            print(f"  ATENTIE fisiere necitibile: {', '.join(archive.stats.files_unreadable)}")
    else:
        print("Arhiva de articole: niciun fisier — se folosesc doar articolele live")

    candle_cache: Dict[str, List[_FeatureCandle]] = {}
    event_cache: Dict[str, List[_FeatureEvent]] = {}
    article_cache: Dict[str, List[_FeatureArticle]] = {}

    computed_counts: Counter = Counter()
    null_counts: Counter = Counter()
    per_observation: List = []

    for observation_id, instrument_id, cutoff_str, event_type, event_confidence, source_count in rows:
        cutoff = datetime.fromisoformat(cutoff_str)

        if instrument_id not in candle_cache:
            candle_cache[instrument_id] = load_candles(conn, instrument_id)
            event_cache[instrument_id] = load_entity_events(conn, instrument_id)
            article_cache[instrument_id] = load_entity_articles(
                conn, instrument_id, archived_articles)

        context = FeatureContext(
            cutoff=cutoff,
            instrument_id=instrument_id,
            candles=candle_cache[instrument_id],
            events=event_cache[instrument_id],
            articles=article_cache[instrument_id],
            peer_candles={},  # deliberately empty — see module docstring
            # event.confidence and event.independent_source_count read
            # from metadata, not from the events list — they describe
            # THIS event, carried over from Phase 5's fusion, not the
            # entity's event history.
            metadata={
                "event_type": event_type,
                "event_confidence": event_confidence,
                "independent_source_count": source_count,
            },
        )

        values = {}
        for feature_id in registry.resolution_order(all_feature_ids):
            feature_value = engine.compute_one(feature_id, context)
            if feature_value is None:
                continue
            values[feature_id] = feature_value
            context.resolved[feature_id] = feature_value.value
            if feature_value.value is None:
                null_counts[feature_id] += 1
            else:
                computed_counts[feature_id] += 1

        per_observation.append((observation_id, values))

    total_values = sum(len(v) for _, v in per_observation)
    total_non_null = sum(computed_counts.values())
    print()
    print(f"Valori de caracteristici produse: {total_values:,}")
    print(f"  cu valoare  : {total_non_null:,}")
    print(f"  nule        : {sum(null_counts.values()):,}")
    print(f"Esecuri de calcul (exceptii): {engine.computation_failures:,}")
    ratio = engine.cache_hit_ratio()
    if ratio is not None:
        print(f"Rata de cache: {ratio:.1%}")

    print()
    print("Acoperire per caracteristica (cu valoare / total observatii):")
    for feature_id in all_feature_ids:
        have = computed_counts.get(feature_id, 0)
        share = (100 * have / len(rows)) if rows else 0
        flag = "  [SARITA - fara peers]" if feature_id in PEER_FEATURE_IDS else ""
        print(f"  {feature_id:34s} {have:>6,} / {len(rows):,}  ({share:5.1f}%){flag}")

    if not args.apply:
        print("\nDRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    written = 0
    for observation_id, values in per_observation:
        written += persist(conn, observation_id, values)
    conn.commit()
    conn.close()
    print(f"\nSCRIS: {written:,} valori de caracteristici (sursa: {FEATURE_SOURCE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
