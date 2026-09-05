"""
dashboard.py
---------------
MarketLens Terminal — the presentation layer.

RESPONSIBILITY
--------------
Builds ONE self-contained HTML application ("MarketLens Terminal")
covering every phase of the pipeline: ingestion/health, entity
resolution, events + fusion, market impact, research/features, models,
signals, and the legacy recommendations/portfolio track record — plus
a Markets explorer, per-company drill-down, and a Sectors explorer
built from the static company/sector registries.

WHY ONE MODULE, NOT TWO
------------------------
Earlier versions of this project had two competing dashboard builders:
this module (fed only by the CURRENT run's in-memory objects — today's
recommendations, live market prices, the daily narrative) and
scripts/build_dashboard.py (fed only by reading the database directly,
covering phases the first one never saw). Neither alone had the full
picture. This module now does both: it always reads straight from a
SQLite connection for anything durable, and additionally accepts the
CURRENT run's in-memory extras (live prices, the daily summary) for
the few things that are computed fresh each run and never persisted.
Passing no connection (or no live extras) degrades gracefully section
by section — never a crash, never an invented number.

SELF-CONTAINED BY DESIGN
-------------------------
The output is a single HTML file: inline CSS, inline JS, one embedded
JSON data blob. No build step, no bundler, no separate JS/CSS assets
to keep in sync — it works the moment GitHub Pages serves the file,
exactly like every previous version of this dashboard.

NO FAKE DATA
------------
Every figure comes from a real query against the database or a value
the caller actually computed this run. Where something cannot be
computed (live prices in the DB-only path, a phase that hasn't run
yet), the UI says so explicitly instead of omitting the section or
inventing a placeholder.

SAFETY: every piece of user/article-derived text is HTML-escaped by
the client-side renderer before insertion (see esc() in the embedded
script), and the JSON payload itself is escaped against "</" so a
stray headline can never break out of its <script> block.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP, SECTOR_KEYWORDS
from event_lexicon import EVENT_LEXICON


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default=None):
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.OperationalError:
        return default


def _safe_json(raw, default):
    """Parse a stored JSON blob, or fall back rather than crash the page."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[tuple]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


class DashboardGenerator:
    """
    Builds the MarketLens Terminal: a single-page application (client
    routing, no server) rendering Overview / Markets / Company /
    Sectors / News / Events / Signals / Outcomes / Models, plus honest
    placeholders for Watchlist / Portfolio / Research / Features.
    """

    # ------------------------------------------------------------------
    # Phase-by-phase data collection — each method is self-contained and
    # tolerant of missing tables (an older export, or a phase that
    # hasn't run yet), same discipline as the rest of this project's
    # data-access layer.
    # ------------------------------------------------------------------

    _FLOW_STAGES = [
        ("Ingestie", "articles", "articole"),
        ("Entitati", "article_entities", "legaturi"),
        ("Evenimente", "events", "rapoarte"),
        ("Fuziune", "canonical_events", "evenimente"),
        ("Impact", "event_studies", "studii"),
        ("Cercetare", "research_observations", "observatii"),
        ("Caracteristici", "research_features", "valori"),
        ("Modele", "trained_models", "modele"),
        ("Semnale", "signals", "semnale"),
    ]

    def _collect_flow(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        flow = []
        for label, table, unit in self._FLOW_STAGES:
            count = _scalar(conn, f"SELECT COUNT(*) FROM {table}") if _table_exists(conn, table) else None
            flow.append({"label": label, "count": count, "unit": unit})
        return flow

    def _collect_health(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        size_bytes = None
        try:
            page_count = _scalar(conn, "PRAGMA page_count", default=0)
            page_size = _scalar(conn, "PRAGMA page_size", default=0)
            if page_count and page_size:
                size_bytes = page_count * page_size
        except sqlite3.OperationalError:
            pass

        # TD-02b. These three aggregates now describe the CANONICAL
        # corpus (`news_articles`), which is the source of truth the
        # migration established. They fall back to the legacy table on
        # a database predating the migration -- a health page that
        # reports zero articles because a table is absent is worse than
        # one reporting the older number.
        #
        # The two corpora differ on purpose: `archive_old_articles.py`
        # prunes `articles` to 60 days and nothing prunes canonical, so
        # canonical is a superset reaching further back. Measured on
        # production 2026-09-05: 48,955 canonical vs 48,906 legacy,
        # oldest 2023-12-15 vs 2026-07-07.
        news_table = ("news_articles" if _table_exists(conn, "news_articles")
                      and _scalar(conn, "SELECT COUNT(*) FROM news_articles", default=0)
                      else "articles")
        source_column = "source_name" if news_table == "news_articles" else "source"
        total_articles = _scalar(conn, f"SELECT COUNT(*) FROM {news_table}", default=0) if _table_exists(conn, news_table) else 0
        linked_articles = _scalar(
            conn, "SELECT COUNT(DISTINCT article_id) FROM article_entities", default=0
        ) if _table_exists(conn, "article_entities") else 0
        sources = _scalar(conn, f"SELECT COUNT(DISTINCT {source_column}) FROM {news_table} WHERE {source_column} IS NOT NULL", default=0) \
            if _table_exists(conn, "articles") else 0
        latest_article = _scalar(conn, f"SELECT MAX(published_at) FROM {news_table}") if _table_exists(conn, news_table) else None
        oldest_article = _scalar(conn, f"SELECT MIN(published_at) FROM {news_table}") if _table_exists(conn, news_table) else None
        return {
            "size_bytes": size_bytes,
            "total_articles": total_articles,
            "linked_articles": linked_articles,
            "entity_coverage": (linked_articles / total_articles) if total_articles else None,
            "sources": sources,
            "latest_article": latest_article,
            "oldest_article": oldest_article,
            #: Which corpus the four numbers above describe. Named so
            #: the page can say it rather than leaving a reader to
            #: assume, and so a fallback to legacy is visible.
            "news_table": news_table,
            "legacy_articles": _scalar(conn, "SELECT COUNT(*) FROM articles",
                                       default=0) if _table_exists(conn, "articles") else 0,
        }

    def _collect_events(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        if not _table_exists(conn, "canonical_events"):
            return {"available": False}
        total = _scalar(conn, "SELECT COUNT(*) FROM canonical_events", default=0)
        corroboration = _rows(conn, """
            SELECT corroboration_state, COUNT(*) FROM canonical_events
            GROUP BY corroboration_state ORDER BY 2 DESC""")
        by_type = _rows(conn, """
            SELECT event_type, COUNT(*) FROM canonical_events
            GROUP BY event_type ORDER BY 2 DESC""")
        return {"available": True, "total": total, "corroboration": corroboration, "by_type": by_type}

    def _collect_impact(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        if not _table_exists(conn, "event_studies"):
            return {"available": False}
        total = _scalar(conn, "SELECT COUNT(*) FROM event_studies", default=0)
        quality = _rows(conn, "SELECT quality_level, COUNT(*) FROM event_studies GROUP BY quality_level ORDER BY 2 DESC")
        by_window = _rows(conn, """
            SELECT window_name, COUNT(*), AVG(abnormal_return) FROM event_study_returns
            WHERE abnormal_return IS NOT NULL GROUP BY window_name
        """) if _table_exists(conn, "event_study_returns") else []
        return {"available": True, "total": total, "quality": quality, "by_window": by_window}

    def _collect_research(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        if not _table_exists(conn, "research_observations"):
            return {"available": False}
        total = _scalar(conn, "SELECT COUNT(*) FROM research_observations", default=0)
        quality = _rows(conn, "SELECT quality_level, COUNT(*) FROM research_observations GROUP BY quality_level ORDER BY 2 DESC")
        feature_coverage = _rows(conn, """
            SELECT qualified_name, COUNT(DISTINCT observation_id) FROM research_features
            WHERE source='phase8_feature_engine' AND value_json != 'null'
            GROUP BY qualified_name ORDER BY 2 DESC
        """) if _table_exists(conn, "research_features") else []
        total_features_attempted = _scalar(conn, """
            SELECT COUNT(DISTINCT qualified_name) FROM research_features WHERE source='phase8_feature_engine'
        """, default=0) if _table_exists(conn, "research_features") else 0
        return {"available": True, "total": total, "quality": quality,
                "feature_coverage": feature_coverage, "feature_count": total_features_attempted}

    def _collect_models(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Models, with the lifecycle status that decides whether they may
        score (Phase 18, NEW-01).

        Columns 7-11 are APPENDED to the existing seven. The page reads
        this tuple positionally, so a new column goes on the end or it
        silently relabels five others.
        """
        if not _table_exists(conn, "trained_models"):
            return {"available": False}
        models = _rows(conn, """
            SELECT m.model_qualified_id, m.label_name, m.train_sample_size, m.train_cluster_count,
                   e.small_sample, e.beats_all_baselines, e.metrics_json,
                   m.status, m.trained_at, e.evaluated_at,
                   m.dataset_version, m.feature_set_version
            FROM trained_models m LEFT JOIN model_evaluations e ON e.trained_model_id = m.trained_model_id
            ORDER BY m.trained_at DESC LIMIT 20
        """)
        predictions = _scalar(conn, "SELECT COUNT(*) FROM predictions", default=0) if _table_exists(conn, "predictions") else 0
        active = _scalar(conn, """
            SELECT COUNT(*) FROM trained_models WHERE status = 'active'
        """, default=0)
        # Promotion history is optional: the table only exists once
        # something has been promoted, and a dashboard must not require
        # it in order to render.
        promotions = _rows(conn, """
            SELECT trained_model_id, action, from_status, to_status,
                   approved_by, reason, promoted_at
            FROM model_promotions ORDER BY promoted_at DESC LIMIT 10
        """) if _table_exists(conn, "model_promotions") else []
        return {"available": True, "models": models, "predictions": predictions,
                "active": active, "promotions": promotions}

    def _collect_outcomes(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Outcome intelligence (Phase 19).

        Guarded end to end: this table only exists once
        `scripts/measure_outcomes.py` has run, and a dashboard must
        render on a database where it has not. Every query goes through
        `_rows`/`_scalar`, which swallow a missing table into an empty
        result — so an absent outcome layer produces an empty workspace,
        never a broken page.
        """
        if not _table_exists(conn, "outcome_measurements"):
            return {"available": False}

        by_status = _rows(conn, """
            SELECT status, COUNT(*) FROM outcome_measurements GROUP BY status
        """)
        total = sum(row[1] for row in by_status) or 0
        available = dict(by_status).get("available", 0)

        verdicts = dict(_rows(conn, """
            SELECT direction_result, COUNT(*) FROM outcome_measurements
            WHERE status='available' GROUP BY direction_result
        """))
        hits, misses = verdicts.get("hit", 0), verdicts.get("miss", 0)

        # The decay curve, ordered by TIME rather than by string. '10d'
        # sorts before '5d' as text, and a decay chart built on string
        # order is wrong in a way that looks entirely plausible.
        decay = _rows(conn, """
            SELECT a.horizon, a.sample_size, a.directional_accuracy,
                   a.mean_return, a.median_return, a.mean_mfe, a.mean_mae,
                   a.small_sample, a.ci_low, a.ci_high,
                   MIN(m.horizon_value), MIN(m.horizon_unit)
            FROM outcome_aggregates a
            JOIN outcome_measurements m ON m.horizon = a.horizon
            WHERE a.cohort_kind='overall' AND a.cohort_value='all'
              AND a.subject_kind='signal'
            GROUP BY a.horizon
        """)
        def seconds(row):
            value, unit = row[10] or 0, row[11] or "d"
            return value * {"m": 60.0, "h": 3600.0}.get(unit, 6.5 * 3600.0)
        decay = sorted(decay, key=seconds)

        cohort_for = lambda kind: _rows(conn, """
            SELECT cohort_value, horizon, sample_size, directional_accuracy,
                   mean_return, median_return, mean_mfe, mean_mae, small_sample
            FROM outcome_aggregates
            WHERE cohort_kind = ? AND subject_kind='signal' AND horizon='5d'
            ORDER BY sample_size DESC LIMIT 25
        """, (kind,))

        # Model training metrics beside the realized record (section 45).
        # Kept apart deliberately: a model that scored well on a held-out
        # split and badly forward is exactly the case worth seeing.
        quality = _rows(conn, """
            SELECT m.trained_model_id, m.model_qualified_id, m.status,
                   e.metrics_json, e.beats_all_baselines,
                   a.sample_size, a.directional_accuracy, a.mean_return,
                   a.small_sample
            FROM trained_models m
            LEFT JOIN model_evaluations e ON e.trained_model_id = m.trained_model_id
            LEFT JOIN outcome_aggregates a
                   ON a.cohort_kind='model' AND a.cohort_value = m.trained_model_id
                  AND a.horizon='5d' AND a.subject_kind='signal'
            ORDER BY m.trained_at DESC LIMIT 10
        """) if _table_exists(conn, "trained_models") else []

        recent = _rows(conn, """
            SELECT subject_id, instrument_id, horizon, expected_direction,
                   status, simple_return, mfe, mae, direction_result,
                   confidence, strength, model_status, reference_price,
                   end_price, bars_observed, information_cutoff
            FROM outcome_measurements
            WHERE subject_kind='signal'
            ORDER BY information_cutoff DESC, horizon LIMIT 60
        """)

        return {
            "available": True,
            "total": total,
            "by_status": by_status,
            "coverage": (available / total) if total else None,
            "hits": hits, "misses": misses,
            "neutrals": verdicts.get("neutral", 0),
            # None, not 0.0, when nothing was decided: "no signal was
            # right" and "nothing was measured" are different facts.
            "accuracy": (hits / (hits + misses)) if (hits + misses) else None,
            "decay": decay,
            "by_instrument": cohort_for("instrument"),
            "by_regime": cohort_for("regime"),
            "by_direction": cohort_for("direction"),
            "by_confidence": cohort_for("confidence_bucket"),
            "by_strength": cohort_for("strength_bucket"),
            "by_model_status": cohort_for("model_status"),
            "quality": quality,
            "recent": recent,
            "cohorts": _scalar(conn, "SELECT COUNT(*) FROM outcome_aggregates",
                               default=0),
            "method_version": _scalar(
                conn, "SELECT method_version FROM outcome_measurements LIMIT 1",
                default=""),
            "measured_at": _scalar(
                conn, "SELECT MAX(computed_at) FROM outcome_measurements"),
            "data_as_of": _scalar(
                conn, "SELECT MAX(data_as_of) FROM outcome_measurements"),
        }

    def _collect_signals(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        if not _table_exists(conn, "signals"):
            return {"available": False}
        total = _scalar(conn, "SELECT COUNT(*) FROM signals", default=0)
        by_status = _rows(conn, "SELECT status, COUNT(*) FROM signals GROUP BY status ORDER BY 2 DESC")
        by_direction = _rows(conn, """
            SELECT direction, COUNT(*) FROM signals WHERE status='active' GROUP BY direction ORDER BY 2 DESC
        """)
        suppression = _rows(conn, """
            SELECT reason, COUNT(*) FROM signal_suppressions GROUP BY reason ORDER BY 2 DESC
        """) if _table_exists(conn, "signal_suppressions") else []
        recent = _rows(conn, """
            SELECT signal_id, instrument_id, direction, status, strength,
                   confidence, expected_return, source_information_cutoff
            FROM signals ORDER BY source_information_cutoff DESC LIMIT 40
        """)

        # Company name and ticker, APPENDED at 8 and 9 -- never
        # inserted, because the page indexes this tuple positionally and
        # shifting a column would mislabel five fields silently instead
        # of failing.
        #
        # Looked up SEPARATELY rather than joined into the query above.
        # Joining made instruments/securities/companies a hard
        # dependency, and `_rows` swallows a missing table into [] — so
        # a database with signals but no registry lost every signal
        # rather than merely losing the labels. A label must never be
        # able to delete the thing it labels.
        names = {}
        for instrument_id, ticker, name in _rows(conn, """
            SELECT i.instrument_id, i.ticker, co.canonical_name
            FROM instruments i
            JOIN securities se ON se.security_id = i.security_id
            JOIN companies co ON co.company_id = se.company_id
        """):
            names[instrument_id] = (name, ticker)

        # Index 10: the status of the model that produced this signal
        # (Phase 18, NEW-01/§11/§12).
        #
        # DERIVED at read time from `trained_models.status` rather than
        # stored on the signal. Storing a snapshot would let a signal go
        # on claiming validated provenance after its model was demoted,
        # and "was this produced by a model that is approved NOW" is the
        # question a reader is actually asking before acting on it.
        #
        # Guarded separately for the same reason the name lookup is:
        # `_rows` swallows a missing table into [], so joining this into
        # the signals query would delete every signal on a database
        # without a model registry.
        model_status = {}
        for signal_id, status in _rows(conn, """
            SELECT c.signal_id, m.status
            FROM signal_contributions c
            JOIN trained_models m ON m.trained_model_id = c.trained_model_id
        """):
            # A signal with several contributions is only as validated
            # as its weakest input: one unpromoted model in the mix
            # makes the whole signal experimental.
            if signal_id not in model_status or status != "active":
                model_status[signal_id] = status
        recent = [tuple(row) + names.get(row[1], (None, None))
                  + (model_status.get(row[0]),)
                  for row in recent]
        evaluations = _rows(conn, """
            SELECT cohort_kind, cohort_value, horizon, sample_size, hit_rate,
                   baseline_hit_rate, beats_baseline, small_sample
            FROM signal_evaluations WHERE cohort_kind='overall' ORDER BY horizon
        """) if _table_exists(conn, "signal_evaluations") else []
        return {"available": True, "total": total, "by_status": by_status,
                "by_direction": by_direction, "suppression": suppression,
                "recent": recent, "evaluations": evaluations}

    def _sector_breakdown(self, conn: sqlite3.Connection) -> List[Tuple[str, int]]:
        """Companies with >=1 recommendation, grouped by sector — using the SAME
        in-process registry the rest of this module already imports (no fragile
        sys.path import, unlike this figure's earlier incarnation)."""
        if not _table_exists(conn, "recommendations"):
            return []
        from collections import Counter
        active_entities = {r[0] for r in _rows(conn, "SELECT DISTINCT entity FROM recommendations")}
        counts = Counter(sector for company, sector in COMPANY_SECTOR_MAP.items() if company in active_entities)
        return counts.most_common(20)

    def _collect_legacy(self, conn: sqlite3.Connection, watchlist: Optional[List[str]]) -> Dict[str, Any]:
        if not _table_exists(conn, "recommendations"):
            return {"available": False}

        total_recs = _scalar(conn, "SELECT COUNT(*) FROM recommendations", default=0)
        by_rec = _rows(conn, "SELECT recommendation, COUNT(*) FROM recommendations GROUP BY recommendation ORDER BY 2 DESC")
        checked = _scalar(conn, "SELECT COUNT(*) FROM recommendations WHERE was_correct IS NOT NULL", default=0)
        correct = _scalar(conn, "SELECT COUNT(*) FROM recommendations WHERE was_correct = 1", default=0)

        recent = _rows(conn, """
            SELECT entity, ticker, recommendation, confidence_score, time_horizon,
                   generated_at, was_correct
            FROM recommendations ORDER BY generated_at DESC LIMIT 40
        """)

        verified = _rows(conn, """
            SELECT entity, was_correct FROM recommendations r
            WHERE was_correct IS NOT NULL
              AND generated_at = (
                  SELECT MAX(generated_at) FROM recommendations r2
                  WHERE r2.entity = r.entity AND r2.was_correct IS NOT NULL
              )
            ORDER BY entity
        """)
        verified_correct = sum(1 for _, w in verified if w)

        checked_rows = _rows(conn, """
            SELECT checked_at, was_correct FROM recommendations
            WHERE was_correct IS NOT NULL AND checked_at IS NOT NULL
            ORDER BY checked_at ASC
        """)
        daily_accuracy: Dict[str, Tuple[int, int]] = {}
        running_correct, running_total = 0, 0
        for checked_at, was_correct in checked_rows:
            running_total += 1
            running_correct += int(bool(was_correct))
            day = str(checked_at)[:10]
            daily_accuracy[day] = (running_correct, running_total)
        accuracy_trend = [(day, c / t) for day, (c, t) in sorted(daily_accuracy.items())][-30:]

        calibration_rows = _rows(conn, """
            SELECT confidence_score, was_correct FROM recommendations
            WHERE was_correct IS NOT NULL AND confidence_score IS NOT NULL AND confidence_score >= 0.5
        """)
        buckets: Dict[float, List[int]] = {}
        for confidence, was_correct in calibration_rows:
            bucket = round((confidence // 0.1) * 0.1, 2)
            buckets.setdefault(bucket, []).append(int(bool(was_correct)))
        calibration = [(f"{b:.1f}-{b+0.1:.1f}", len(v), sum(v) / len(v))
                       for b, v in sorted(buckets.items())]

        portfolio_history = _rows(conn, """
            SELECT recorded_at, total_invested, total_final_value, total_return_pct, trades_simulated
            FROM portfolio_snapshots ORDER BY recorded_at DESC LIMIT 20
        """) if _table_exists(conn, "portfolio_snapshots") else []

        return {
            "available": True, "total_recs": total_recs, "by_rec": by_rec,
            "checked": checked, "correct": correct,
            "accuracy": (correct / checked) if checked else None,
            "recent": recent, "verified_count": len(verified), "verified_correct": verified_correct,
            "accuracy_trend": accuracy_trend, "calibration": calibration,
            "portfolio_history": portfolio_history,
            "sector_breakdown": self._sector_breakdown(conn),
            "watchlist": watchlist or [],
        }

    def _collect_portfolio(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Phase 11: declared portfolios, their state, and the latest risk
        decision for each.

        The state is computed LIVE at page-build time rather than read
        from a stored snapshot. That is deliberate: it uses only the
        cached candles (no network), so it is cheap and reproducible,
        and it means the page reflects the portfolio as it stands now
        instead of whenever someone last ran the evaluator by hand.

        Anchored at wall-clock now, which is honest rather than
        convenient: if the newest candle is four days old, the page
        says the prices are stale instead of quietly anchoring itself
        to the last date that happened to have data.
        """
        if not _table_exists(conn, "portfolios"):
            return {"available": False,
                    "reason": "Phase 11 tables are not present in this database"}

        rows = _rows(conn, """
            SELECT portfolio_id, name, base_currency, cash, kind
            FROM portfolios ORDER BY portfolio_id
        """)
        if not rows:
            return {"available": True, "portfolios": [], "detail": {}}

        try:
            from src.portfolio.service import PortfolioService
            from src.domain.portfolio_models import ExposureDimension
        except ImportError:
            # The package is unreachable (e.g. generated from a context
            # where only src/ is importable). Listing what exists is
            # still better than claiming there is nothing.
            return {"available": True, "reason": "risk engine not importable here",
                    "portfolios": [{"id": r[0], "name": r[1], "currency": r[2],
                                    "cash": r[3], "kind": r[4]} for r in rows],
                    "detail": {}}

        service = PortfolioService(conn)
        as_of = datetime.now(timezone.utc)

        portfolios: List[Dict[str, Any]] = []
        detail: Dict[str, Any] = {}

        for portfolio_id, name, currency, cash, kind in rows:
            try:
                result = service.evaluate(portfolio_id, as_of, persist=False)
            except Exception as exc:      # noqa: BLE001 — one bad book must not blank the page
                portfolios.append({"id": portfolio_id, "name": name,
                                   "currency": currency, "cash": cash, "kind": kind,
                                   "positions": None, "error": str(exc)})
                continue

            snapshot, metrics, decision = result.snapshot, result.metrics, result.decision

            exposures = {}
            for dimension, breakdown in service.exposures.all_breakdowns(snapshot).items():
                exposures[dimension.value] = {
                    "buckets": [{"key": b.key, "label": b.label, "exposure": b.exposure,
                                 "weight": b.weight, "count": b.position_count,
                                 "long": b.long_exposure, "short": b.short_exposure}
                                for b in breakdown.buckets],
                    "unclassified": breakdown.unclassified_exposure,
                    "unclassified_count": breakdown.unclassified_count,
                }

            positions = [{
                "instrument_id": v.position.instrument_id,
                "quantity": v.position.quantity,
                "entry": v.position.average_entry_price,
                "price": v.price,
                "market_value": v.market_value,
                "exposure": v.exposure,
                "weight": snapshot.weight_of(v.position.instrument_id),
                "unrealized": v.unrealized_pnl,
                "status": v.status.value,
                "age_days": v.price_age_days,
                "currency": v.position.currency,
            } for v in snapshot.valuations] + [{
                "instrument_id": v.position.instrument_id,
                "quantity": v.position.quantity,
                "entry": v.position.average_entry_price,
                "price": None, "market_value": None, "exposure": None,
                "weight": None, "unrealized": None,
                "status": v.status.value, "age_days": None,
                "currency": v.position.currency,
            } for v in snapshot.unvalued_positions]

            portfolios.append({
                "id": portfolio_id, "name": name, "currency": currency,
                "cash": cash, "kind": kind, "positions": len(positions),
                "equity": snapshot.equity, "state": decision.state.value,
            })

            detail[portfolio_id] = {
                "snapshot": {
                    "as_of": snapshot.as_of, "equity": snapshot.equity,
                    "cash": snapshot.cash, "gross": snapshot.gross_exposure,
                    "net": snapshot.net_exposure, "long": snapshot.long_exposure,
                    "short": snapshot.short_exposure, "leverage": snapshot.leverage,
                    "unrealized": snapshot.unrealized_pnl,
                    "realized": snapshot.realized_pnl,
                    "complete": snapshot.is_complete,
                    "stale": snapshot.has_stale_prices,
                    "multi_currency": snapshot.is_multi_currency,
                    "currency": snapshot.base_currency,
                    "unvalued": [v.position.instrument_id
                                 for v in snapshot.unvalued_positions],
                },
                "positions": positions,
                "exposures": exposures,
                "metrics": {
                    "volatility": metrics.volatility.value,
                    "volatility_obs": metrics.volatility.observations,
                    "volatility_note": metrics.volatility.note,
                    "volatility_insufficient": metrics.volatility.insufficient_data,
                    "volatility_method": metrics.volatility.method,
                    "var": metrics.value_at_risk.value,
                    "var_confidence": metrics.value_at_risk.confidence_level,
                    "es": metrics.value_at_risk.expected_shortfall,
                    "var_insufficient": metrics.value_at_risk.insufficient_data,
                    "var_note": metrics.value_at_risk.note,
                    "max_drawdown": metrics.drawdown.max_drawdown,
                    "current_drawdown": metrics.drawdown.current_drawdown,
                    "drawdown_insufficient": metrics.drawdown.insufficient_data,
                    "hhi": metrics.concentration.hhi,
                    "effective_positions": metrics.concentration.effective_positions,
                    "invested_weight": metrics.concentration.invested_weight,
                    "largest_weight": metrics.concentration.largest_weight,
                    "largest_instrument": metrics.concentration.largest_instrument_id,
                    "top_5": metrics.concentration.top_5_weight,
                    "avg_correlation": metrics.correlation.average_correlation,
                    "max_correlation": metrics.correlation.max_correlation,
                    "correlated_pairs": metrics.correlation.highly_correlated_pairs,
                    "correlation_pairs": metrics.correlation.computed_pairs,
                    "correlation_thin": metrics.correlation.insufficient_pairs,
                    "unavailable": metrics.unavailable,
                },
                "decision": {
                    "id": decision.decision_id,
                    "state": decision.state.value,
                    "summary": decision.summary,
                    "reasons": decision.reasons,
                    "evaluated": decision.evaluated_scopes,
                    "skipped": decision.skipped_scopes,
                    "engine_version": decision.provenance.risk_engine_version,
                    "constraint_version": decision.provenance.constraint_set_version,
                    "violations": [{
                        "constraint_id": v.constraint_id, "scope": v.scope.value,
                        "severity": v.severity.value, "message": v.message,
                        "observed": v.observed_value, "current": v.current_value,
                        "limit": v.limit_value, "applies_to": v.applies_to,
                        "remediated": v.remediated,
                    } for v in decision.violations],
                },
                "intents": len(result.intents),
            }

        return {"available": True, "portfolios": portfolios, "detail": detail}

    def _collect_backtests(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Phase 12: stored backtest runs and their results.

        Reads only what a run PERSISTED. Unlike the portfolio page,
        nothing is recomputed live — a backtest is an expensive,
        deliberate experiment, and silently re-running one on every
        page build would be both slow and misleading about when it
        happened.
        """
        if not _table_exists(conn, "backtest_runs"):
            return {"available": False,
                    "reason": "Phase 12 tables are not present in this database"}

        rows = _rows(conn, """
            SELECT run_id, backtest_id, status, period_start, period_end,
                   initial_capital, execution_timing, cost_model_version,
                   slippage_method, config_fingerprint, started_at,
                   observations_processed, duration_seconds, quality_json,
                   constraint_set_version, code_version
            FROM backtest_runs ORDER BY started_at DESC LIMIT 25
        """)
        if not rows:
            return {"available": True, "runs": [], "detail": {}}

        runs: List[Dict[str, Any]] = []
        detail: Dict[str, Any] = {}

        for (run_id, backtest_id, status, start, end, capital, timing,
             cost_version, slippage_method, fingerprint, started_at,
             observations, duration, quality_json, constraints,
             code_version) in rows:
            try:
                quality = json.loads(quality_json) if quality_json else {}
            except (ValueError, TypeError):
                quality = {}

            metrics: Dict[str, Any] = {}
            unavailable: Dict[str, Any] = {}
            for metric, value, reason in _rows(conn,
                    "SELECT metric, value, unavailable_reason FROM backtest_metrics "
                    "WHERE run_id = ?", (run_id,)):
                if metric.startswith("unavailable::"):
                    unavailable[metric.split("::", 1)[1]] = reason
                else:
                    metrics[metric] = value

            runs.append({
                "run_id": run_id, "backtest_id": backtest_id, "status": status,
                "period": [start, end], "capital": capital, "timing": timing,
                "cost_version": cost_version, "slippage_method": slippage_method,
                "fingerprint": fingerprint, "started_at": started_at,
                "observations": observations, "duration": duration,
                "quality_score": quality.get("score"),
                "total_return": metrics.get("total_return"),
                "sharpe": metrics.get("sharpe"),
                "max_drawdown": metrics.get("max_drawdown"),
                "trades": metrics.get("total_trades"),
            })

            detail[run_id] = {
                "metrics": metrics,
                "unavailable": unavailable,
                "quality": quality,
                "constraints": constraints,
                "code_version": code_version,
                "equity": [{"t": r[0], "e": r[1], "b": r[2], "d": r[3]}
                           for r in _rows(conn,
                               "SELECT timestamp, equity, benchmark_value, drawdown "
                               "FROM backtest_equity WHERE run_id = ? "
                               "ORDER BY timestamp ASC", (run_id,))],
                "trades": [{
                    "instrument_id": r[0], "side": r[1], "quantity": r[2],
                    "entry_price": r[3], "exit_price": r[4], "entry_at": r[5],
                    "exit_at": r[6], "net_pnl": r[7], "costs": r[8],
                    "holding_days": r[9], "exit_reason": r[10],
                } for r in _rows(conn, """
                    SELECT instrument_id, side, quantity, entry_price, exit_price,
                           entry_at, exit_at, net_pnl, costs, holding_days,
                           exit_reason
                    FROM backtest_trades WHERE run_id = ?
                    ORDER BY exit_at DESC LIMIT 60
                """, (run_id,))],
                "attribution": [{
                    "dimension": r[0], "key": r[1], "label": r[2],
                    "trades": r[3], "wins": r[4], "net_pnl": r[5],
                } for r in _rows(conn, """
                    SELECT dimension, bucket_key, label, trades, wins, net_pnl
                    FROM backtest_attribution WHERE run_id = ?
                    ORDER BY dimension, net_pnl DESC
                """, (run_id,))],
                "warnings": [{"code": r[0], "message": r[1], "detail": r[2]}
                             for r in _rows(conn,
                                 "SELECT code, message, detail FROM backtest_warnings "
                                 "WHERE run_id = ? ORDER BY code", (run_id,))],
                "orders": _scalar(conn,
                    "SELECT COUNT(*) FROM simulated_orders WHERE run_id = ?",
                    (run_id,), default=0),
                "rejected_orders": _scalar(conn,
                    "SELECT COUNT(*) FROM simulated_orders WHERE run_id = ? "
                    "AND state = 'rejected'", (run_id,), default=0),
                "risk_events": [{"kind": r[0], "at": r[1], "reason": r[2]}
                                for r in _rows(conn, """
                    SELECT kind, occurred_at, reason FROM backtest_risk_events
                    WHERE run_id = ? ORDER BY seq ASC LIMIT 40
                """, (run_id,))],
                "config": self._backtest_config(conn, run_id),
            }

        return {"available": True, "runs": runs, "detail": detail}

    def _collect_execution(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Phase 14: brokers, accounts, orders and the execution boundary.

        Every broker is listed, including the ones with no adapter, and
        each carries `implemented` plus the reason. A venue hidden
        because it is not built yet is worse than one shown as absent —
        the reader would have to guess whether it exists.

        No connection is ever displayed as live, because none is. The
        workspace states that the phase has no real-money path rather
        than leaving it to be inferred from an empty table.
        """
        if not _table_exists(conn, "execution_orders"):
            return {"available": False, "live_execution": False,
                    "reason": "Phase 14 tables are not present in this database"}

        brokers = [{
            "broker_id": r[0], "name": r[1], "environment": r[2],
            "adapter": r[3], "enabled": bool(r[4]), "implemented": bool(r[5]),
        } for r in _rows(conn, """
            SELECT broker_id, name, environment, adapter, enabled, implemented
            FROM brokers ORDER BY implemented DESC, broker_id
        """)]

        capabilities: Dict[str, Any] = {}
        for broker_id, raw, notes in _rows(conn,
                "SELECT broker_id, capability_json, notes FROM broker_capability"):
            try:
                capabilities[broker_id] = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                capabilities[broker_id] = {}
            capabilities[broker_id]["notes"] = notes or ""

        accounts = [{
            "account_id": r[0], "broker_id": r[1], "name": r[2],
            "environment": r[3], "base_currency": r[4], "enabled": bool(r[5]),
            "position_accounting": r[6],
        } for r in _rows(conn, """
            SELECT account_id, broker_id, name, environment, base_currency,
                   enabled, position_accounting
            FROM broker_accounts ORDER BY broker_id, account_id
        """)]

        orders = [{
            "order_id": r[0], "broker_id": r[1], "account_id": r[2],
            "instrument_id": r[3], "side": r[4], "quantity": r[5],
            "order_type": r[6], "state": r[7], "filled": r[8],
            "average_price": r[9], "reject_code": r[10],
            "reject_detail": r[11], "broker_order_id": r[12],
            "client_order_id": r[13], "correlation_id": r[14],
            "signal_id": r[15], "strategy_id": r[16], "policy": r[17],
            "environment": r[18], "intent_at": r[19],
            "decision_price": r[20], "commission": r[21],
        } for r in _rows(conn, """
            SELECT order_id, broker_id, account_id, instrument_id, side,
                   quantity, order_type, state, filled_quantity,
                   average_fill_price, reject_code, reject_detail,
                   broker_order_id, client_order_id, correlation_id, signal_id,
                   strategy_id, execution_policy, environment, intent_at,
                   decision_price, commission
            FROM execution_orders ORDER BY COALESCE(intent_at, '') DESC LIMIT 60
        """)]

        by_state = {r[0]: r[1] for r in _rows(conn,
            "SELECT state, COUNT(*) FROM execution_orders GROUP BY state")}
        rejections = [{"code": r[0] or "unspecified", "count": r[1]}
                      for r in _rows(conn, """
            SELECT reject_code, COUNT(*) FROM execution_orders
            WHERE state = 'rejected' GROUP BY reject_code ORDER BY COUNT(*) DESC
        """)]

        detail: Dict[str, Any] = {}
        for order in orders[:25]:
            order_id = order["order_id"]
            detail[order_id] = {
                "states": [{"seq": r[0], "from": r[1], "to": r[2], "at": r[3],
                            "reason": r[4]}
                           for r in _rows(conn, """
                    SELECT sequence, from_state, to_state, at, reason
                    FROM order_state_history WHERE order_id = ?
                    ORDER BY sequence ASC
                """, (order_id,))],
                "fills": [{"fill_id": r[0], "quantity": r[1], "price": r[2],
                           "reference_price": r[3], "commission": r[4],
                           "fees": r[5], "at": r[6], "broker_order_id": r[7]}
                          for r in _rows(conn, """
                    SELECT fill_id, quantity, price, reference_price, commission,
                           fees, filled_at, broker_order_id
                    FROM execution_fills WHERE order_id = ?
                    ORDER BY COALESCE(filled_at, '') ASC
                """, (order_id,))],
            }

        reconciliations = [{
            "reconciliation_id": r[0], "broker_id": r[1], "account_id": r[2],
            "at": r[3], "checks": r[4], "clean": bool(r[5]),
            "mismatches": _safe_json(r[6], []),
        } for r in _rows(conn, """
            SELECT reconciliation_id, broker_id, account_id, at,
                   checks_performed, is_clean, mismatches_json
            FROM reconciliation_records ORDER BY at DESC LIMIT 20
        """)]

        errors = [{"error_id": r[0], "at": r[1], "code": r[2], "message": r[3],
                   "broker_id": r[4], "order_id": r[5]}
                  for r in _rows(conn, """
            SELECT error_id, at, code, message, broker_id, order_id
            FROM execution_errors ORDER BY COALESCE(at, '') DESC LIMIT 30
        """)]

        events = [{"event_id": r[0], "type": r[1], "at": r[2],
                   "broker_id": r[3], "order_id": r[4], "instrument_id": r[5]}
                  for r in _rows(conn, """
            SELECT event_id, event_type, at, broker_id, order_id, instrument_id
            FROM execution_events ORDER BY COALESCE(at, '') DESC LIMIT 40
        """)]

        audit = [{"at": r[0], "action": r[1], "actor": r[2],
                  "subject_id": r[3], "detail": r[4]}
                 for r in _rows(conn, """
            SELECT at, action, actor, subject_id, detail
            FROM execution_audit ORDER BY COALESCE(at, '') DESC LIMIT 30
        """)]

        mappings = [{"instrument_id": r[0], "broker_id": r[1], "symbol": r[2],
                     "asset_class": r[3], "currency": r[4],
                     "increment": r[5], "minimum": r[6], "tradable": bool(r[7])}
                    for r in _rows(conn, """
            SELECT canonical_instrument_id, broker_id, broker_symbol,
                   asset_class, currency, quantity_increment, minimum_quantity,
                   tradable
            FROM broker_instrument_mapping ORDER BY broker_id, broker_symbol
            LIMIT 60
        """)]

        return {
            "available": True,
            #: Structurally False. No adapter in this repository can
            #: place a real-money order.
            "live_execution": False,
            "brokers": brokers, "capabilities": capabilities,
            "accounts": accounts, "orders": orders, "detail": detail,
            "orders_by_state": by_state, "rejections": rejections,
            "reconciliations": reconciliations, "errors": errors,
            "events": events, "audit": audit, "mappings": mappings,
            "totals": {
                "orders": _scalar(conn, "SELECT COUNT(*) FROM execution_orders",
                                  default=0),
                "fills": _scalar(conn, "SELECT COUNT(*) FROM execution_fills",
                                 default=0),
                "brokers": len(brokers),
                "implemented_brokers": sum(1 for b in brokers if b["implemented"]),
            },
        }

    def _collect_broker_detail(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Per-broker execution detail (Phase 15).

        Reads the SAME Phase 14 tables as everything else — there is no
        IBKR-specific table, because the canonical records already hold
        what a venue-specific panel needs. What differs per broker is
        the mapping payload (IBKR keeps its conid there) and the
        capability row, and both are already canonical.

        Nothing here is invented. A broker with no orders reports no
        orders rather than a plausible-looking zero state.
        """
        if not _table_exists(conn, "execution_orders"):
            return {"available": False}

        out: Dict[str, Any] = {}
        for (broker_id, name, environment, adapter, enabled,
             implemented) in _rows(conn, """
                SELECT broker_id, name, environment, adapter, enabled, implemented
                FROM brokers ORDER BY broker_id
            """):
            mappings = [{
                "instrument_id": r[0], "symbol": r[1], "venue": r[2],
                "asset_class": r[3], "currency": r[4],
                "increment": r[5], "minimum": r[6],
                "payload": _safe_json(r[7], {}),
            } for r in _rows(conn, """
                SELECT canonical_instrument_id, broker_symbol, venue,
                       asset_class, currency, quantity_increment,
                       minimum_quantity, broker_payload_json
                FROM broker_instrument_mapping WHERE broker_id = ?
                ORDER BY broker_symbol LIMIT 40
            """, (broker_id,))]

            health = [{"at": r[0], "state": r[1], "latency_ms": r[2],
                       "detail": r[3]}
                      for r in _rows(conn, """
                SELECT at, state, latency_ms, detail FROM broker_health
                WHERE broker_id = ? ORDER BY at DESC LIMIT 5
            """, (broker_id,))]

            out[broker_id] = {
                "broker_id": broker_id, "name": name,
                "environment": environment, "adapter": adapter,
                "enabled": bool(enabled), "implemented": bool(implemented),
                "mappings": mappings,
                "health": health,
                "orders": _scalar(conn,
                    "SELECT COUNT(*) FROM execution_orders WHERE broker_id = ?",
                    (broker_id,), default=0),
                "fills": _scalar(conn,
                    "SELECT COUNT(*) FROM execution_fills WHERE broker_id = ?",
                    (broker_id,), default=0),
                "accounts": [{"account_id": r[0], "name": r[1],
                              "environment": r[2], "currency": r[3],
                              "enabled": bool(r[4])}
                             for r in _rows(conn, """
                    SELECT account_id, name, environment, base_currency, enabled
                    FROM broker_accounts WHERE broker_id = ? ORDER BY account_id
                """, (broker_id,))],
            }
        return {"available": True, "brokers": out}

    def _collect_price_history(self, conn: sqlite3.Connection
                               ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Price history and a last-close summary, read from the database.

        WHY THIS EXISTS
        -------------------
        `price_history_map` and `market_data` are supplied in memory by
        run_daily.py from a live fetch. A dashboard rebuilt from the
        database alone had neither, so every instrument page reported
        "price_candle_cache - indisponibil" while that very table held
        116,719 candles. The data was present and discarded.

        Returns (history_by_name, summary_by_ticker) so the page can
        fall back to stored candles when a live quote is absent. Keys
        match what the page already looks up: company canonical name
        for history, ticker for the summary.

        This is NOT a live price and does not pretend to be. The
        summary carries `as_of` and the page labels it, because a close
        from three days ago shown as today's price would be worse than
        showing nothing.
        """
        if not _table_exists(conn, "price_candle_cache"):
            return {}, {}

        names = {}
        for instrument_id, ticker, name in _rows(conn, """
            SELECT i.instrument_id, i.ticker, co.canonical_name
            FROM instruments i
            JOIN securities s ON i.security_id = s.security_id
            JOIN companies co ON s.company_id = co.company_id
        """):
            names[instrument_id] = (ticker, name)

        if not names:
            return {}, {}

        # Daily bars only. Intraday rows would swamp a sparkline and
        # are not what it shows.
        #
        # Stored as a FLAT LIST OF CLOSES, not objects. The sparkline
        # reads `p.close !== undefined ? p.close : p`, so it accepts
        # both -- and the object form cost 1,835 KB of embedded JSON on
        # a page that is otherwise 278 KB. Per-point timestamps were
        # never read; the date shown in the label comes from `as_of` in
        # the summary below.
        raw: Dict[str, list] = {}
        for instrument_id, timestamp, close in _rows(conn, """
            SELECT instrument_id, timestamp, COALESCE(adjusted_close, close)
            FROM price_candle_cache
            WHERE interval = '1d' AND close IS NOT NULL
            ORDER BY instrument_id, timestamp ASC
        """):
            if instrument_id not in names:
                continue
            raw.setdefault(instrument_id, []).append((timestamp, close))

        history: Dict[str, Any] = {}
        summary: Dict[str, Any] = {}
        for instrument_id, (ticker, name) in names.items():
            series = raw.get(instrument_id)
            if not series:
                continue
            # 90 points is more than a 400px sparkline can resolve; the
            # rest is payload for nothing.
            series = series[-90:]
            closes = [round(float(close), 4) for _, close in series]
            history[name] = closes

            last_close = closes[-1]
            previous = closes[-2] if len(closes) > 1 else None
            change = None
            if previous:
                change = (last_close - previous) / previous * 100.0
            summary[ticker] = {
                "current_price": last_close,
                "daily_change_pct": change,
                "as_of": series[-1][0],
                "points": len(closes),
                # Says plainly where this came from. A stored close is
                # not a live quote and the page must not imply it is.
                "from_cache": True,
            }
        return history, summary

    def _collect_operations(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        The operations centre (Phase 16, spec §88-§93).

        Governance, sessions, limits, alerts and trade outcomes. Every
        panel reports what the database actually holds; a table that
        does not exist yet reports `available: False` rather than an
        empty state that would read as "nothing wrong".

        The distinction matters more here than anywhere else in this
        file. An operations screen that renders a confident green on
        absent data is worse than no screen, because it converts a
        monitoring failure into a false assurance.
        """
        if not _table_exists(conn, "trading_sessions"):
            return {"available": False}

        sessions = [{
            "session_id": r[0], "state": r[1], "operator": r[2],
            "broker_id": r[3], "account_id": r[4], "environment": r[5],
            "level": r[6], "fingerprint": r[7], "model_version": r[8],
            "capital_limit": r[9], "created_at": r[10], "started_at": r[11],
            "ended_at": r[12], "termination_reason": r[13],
            "preflight": _safe_json(r[14], []),
            "summary": _safe_json(r[15], {}),
        } for r in _rows(conn, """
            SELECT session_id, state, operator, broker_id, account_id,
                   environment, level, config_fingerprint, model_version,
                   capital_limit, created_at, started_at, ended_at,
                   termination_reason, preflight_json, summary_json
            FROM trading_sessions
            ORDER BY COALESCE(started_at, created_at) DESC LIMIT 12
        """)]
        active = next((s for s in sessions
                       if s["state"] in ("active", "paused")), None)

        events = []
        if active is not None:
            events = [{"at": r[0], "action": r[1], "actor": r[2],
                       "from_state": r[3], "to_state": r[4], "reason": r[5]}
                      for r in _rows(conn, """
                SELECT at, action, actor, from_state, to_state, reason
                FROM session_events WHERE session_id = ?
                ORDER BY sequence DESC LIMIT 20
            """, (active["session_id"],))]

        promotions = [{
            "request_id": r[0], "level": r[1], "level_label": r[2],
            "state": r[3], "requested_by": r[4], "requested_at": r[5],
            "reason": r[6], "approved_by": r[7], "approved_at": r[8],
            "expires_at": r[9], "note": r[10],
        } for r in _rows(conn, """
            SELECT request_id, level, level_label, state, requested_by,
                   requested_at, reason, approved_by, approved_at,
                   expires_at, decision_note
            FROM promotion_requests ORDER BY requested_at DESC LIMIT 10
        """)] if _table_exists(conn, "promotion_requests") else []

        readiness = None
        if _table_exists(conn, "readiness_assessments"):
            row = _rows(conn, """
                SELECT at, is_ready, verdicts_json, notes_json
                FROM readiness_assessments ORDER BY at DESC LIMIT 1
            """)
            if row:
                readiness = {
                    "at": row[0][0], "is_ready": bool(row[0][1]),
                    "verdicts": _safe_json(row[0][2], {}),
                    "notes": _safe_json(row[0][3], {}),
                }

        health = []
        if _table_exists(conn, "system_health_readings"):
            latest = _scalar(conn,
                "SELECT MAX(at) FROM system_health_readings")
            if latest:
                health = [{"capability": r[0], "state": r[1], "detail": r[2],
                           "latency_ms": r[3], "age_seconds": r[4], "at": latest}
                          for r in _rows(conn, """
                    SELECT capability, state, detail, latency_ms, age_seconds
                    FROM system_health_readings WHERE at = ?
                    ORDER BY capability
                """, (latest,))]

        alerts = [{"alert_id": r[0], "at": r[1], "code": r[2], "severity": r[3],
                   "message": r[4], "detail": r[5], "order_id": r[6]}
                  for r in _rows(conn, """
            SELECT alert_id, at, code, severity, message, detail, order_id
            FROM execution_alerts WHERE acknowledged = 0
            ORDER BY at DESC LIMIT 25
        """)] if _table_exists(conn, "execution_alerts") else []

        breaches = [{"breach_id": r[0], "at": r[1], "limit_name": r[2],
                     "detail": r[3], "latched": bool(r[4]), "order_id": r[5]}
                    for r in _rows(conn, """
            SELECT breach_id, at, limit_name, detail, latched, order_id
            FROM limit_breaches WHERE cleared_at IS NULL
            ORDER BY at DESC LIMIT 25
        """)] if _table_exists(conn, "limit_breaches") else []

        outcomes: List[Dict[str, Any]] = []
        quality: Dict[str, Any] = {}
        if _table_exists(conn, "trade_outcomes"):
            outcomes = [{
                "outcome_id": r[0], "instrument_id": r[1], "side": r[2],
                "quantity": r[3], "entry_at": r[4], "exit_at": r[5],
                "entry_price": r[6], "exit_price": r[7], "net_pnl": r[8],
                "return_pct": r[9], "holding_days": r[10],
                "exit_reason": r[11], "slippage_bps": r[12],
                "environment": r[13], "is_open": bool(r[14]),
                "lineage_complete": bool(r[15]),
                "strategy_id": r[16], "model_version": r[17],
                "market_regime": r[18], "order_id": r[19],
            } for r in _rows(conn, """
                SELECT outcome_id, instrument_id, side, quantity, entry_at,
                       exit_at, entry_price, exit_price, net_pnl, return_pct,
                       holding_days, exit_reason, slippage_bps, environment,
                       is_open, lineage_complete, strategy_id, model_version,
                       market_regime, order_id
                FROM trade_outcomes
                ORDER BY COALESCE(exit_at, entry_at) DESC LIMIT 60
            """)]

            closed = [o for o in outcomes if not o["is_open"]]
            realized = [o["net_pnl"] for o in closed if o["net_pnl"] is not None]
            slips = sorted(o["slippage_bps"] for o in closed
                           if o["slippage_bps"] is not None)
            quality = {
                "closed": len(closed),
                "open": sum(1 for o in outcomes if o["is_open"]),
                # None, not zero. No trades is not a flat P&L.
                "net_pnl": sum(realized) if realized else None,
                "wins": sum(1 for p in realized if p > 0),
                "losses": sum(1 for p in realized if p <= 0),
                "median_slippage_bps": (slips[len(slips) // 2]
                                        if slips else None),
                "worst_slippage_bps": (max(slips) if slips else None),
                "lineage_complete": sum(1 for o in outcomes
                                        if o["lineage_complete"]),
            }

        missed = [{"missed_id": r[0], "at": r[1], "instrument_id": r[2],
                   "reason": r[3], "detail": r[4], "prevented": bool(r[5])}
                  for r in _rows(conn, """
            SELECT missed_id, at, instrument_id, reason, detail,
                   prevented_by_system
            FROM missed_trades ORDER BY at DESC LIMIT 30
        """)] if _table_exists(conn, "missed_trades") else []

        return {
            "available": True,
            "sessions": sessions, "active": active, "events": events,
            "promotions": promotions, "readiness": readiness,
            "health": health, "alerts": alerts, "breaches": breaches,
            "outcomes": outcomes, "quality": quality, "missed": missed,
        }

    def _collect_paper(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Phase 13: paper trading sessions.

        Every number here is simulated. The workspace that renders it
        says so permanently and in the same words, because a page
        showing an equity curve, positions and fills without that label
        would be indistinguishable at a glance from one showing a real
        account (spec 54, 56).

        Like the backtest collector, this reads only what a session
        PERSISTED. No session is ticked while a page is being built.
        """
        if not _table_exists(conn, "paper_sessions"):
            return {"available": False, "is_paper": True,
                    "reason": "Phase 13 tables are not present in this database"}

        accounts = [{
            "account_id": r[0], "name": r[1], "status": r[2],
            "initial_capital": r[3], "base_currency": r[4],
            "account_type": r[5], "generation": r[6],
        } for r in _rows(conn, """
            SELECT account_id, name, status, initial_capital, base_currency,
                   account_type, generation
            FROM paper_accounts ORDER BY account_id
        """)]

        rows = _rows(conn, """
            SELECT session_id, account_id, name, status, config_fingerprint,
                   clock_kind, started_at, ended_at, last_tick_at,
                   ticks_processed, code_version, constraint_set_version,
                   cost_model_version, slippage_model_version,
                   execution_model_version, config_json
            FROM paper_sessions
            -- Ordered by when the session ROW was written, which is the
            -- one timestamp here that is always wall clock. Ordering by
            -- last_tick_at would compare a replay session ticking through
            -- 2026-04 against a session created today, and rank the
            -- actively-running one last.
            ORDER BY COALESCE(created_at, started_at) DESC
            LIMIT 20
        """)
        if not rows:
            return {"available": True, "is_paper": True,
                    "accounts": accounts, "sessions": [], "detail": {}}

        sessions: List[Dict[str, Any]] = []
        detail: Dict[str, Any] = {}

        for (session_id, account_id, name, status, fingerprint, clock_kind,
             started_at, ended_at, last_tick_at, ticks, code_version,
             constraints, cost_version, slippage_version, execution_version,
             config_json) in rows:
            try:
                config = json.loads(config_json) if config_json else {}
            except (ValueError, TypeError):
                config = {}

            latest = _rows(conn, """
                SELECT at, equity, cash, positions_value, gross_exposure,
                       net_exposure, leverage, realized_pnl, unrealized_pnl,
                       drawdown, open_positions, unpriced_positions,
                       data_freshness, health
                FROM paper_snapshots WHERE session_id = ?
                ORDER BY at DESC LIMIT 1
            """, (session_id,))
            last = latest[0] if latest else None

            capital = _scalar(conn,
                "SELECT initial_capital FROM paper_accounts WHERE account_id = ?",
                (account_id,), default=None)
            equity = last[1] if last else None
            # Reported, never framed as a result: a paper period cannot
            # establish anything about a strategy, so this describes the
            # simulated book rather than claiming a track record.
            change = None
            if equity is not None and capital:
                change = (equity - capital) / capital

            orders_total = _scalar(conn,
                "SELECT COUNT(*) FROM paper_orders WHERE session_id = ?",
                (session_id,), default=0)
            fills_total = _scalar(conn,
                "SELECT COUNT(*) FROM paper_fills WHERE session_id = ?",
                (session_id,), default=0)

            sessions.append({
                "session_id": session_id, "account_id": account_id,
                "name": name, "status": status, "fingerprint": fingerprint,
                "clock_kind": clock_kind, "started_at": started_at,
                "ended_at": ended_at, "last_tick_at": last_tick_at,
                "ticks": ticks, "orders": orders_total, "fills": fills_total,
                "equity": equity, "capital": capital, "change": change,
                "cash": last[2] if last else None,
                "drawdown": last[9] if last else None,
                "open_positions": last[10] if last else 0,
                "unpriced": last[11] if last else 0,
                "health": last[13] if last else None,
                "freshness": last[12] if last else None,
            })

            by_state = {r[0]: r[1] for r in _rows(conn,
                "SELECT state, COUNT(*) FROM paper_orders WHERE session_id = ? "
                "GROUP BY state", (session_id,))}
            by_reject = [{"reason": r[0] or "unspecified", "count": r[1]}
                         for r in _rows(conn, """
                SELECT reject_reason, COUNT(*) FROM paper_orders
                WHERE session_id = ? AND state = 'rejected'
                GROUP BY reject_reason ORDER BY COUNT(*) DESC
            """, (session_id,))]

            recon = _rows(conn, """
                SELECT at, is_clean, checks_performed, discrepancies_json
                FROM paper_reconciliations WHERE session_id = ?
                ORDER BY at DESC LIMIT 1
            """, (session_id,))
            reconciliation = None
            if recon:
                try:
                    discrepancies = json.loads(recon[0][3] or "[]")
                except (ValueError, TypeError):
                    discrepancies = []
                reconciliation = {"at": recon[0][0], "clean": bool(recon[0][1]),
                                  "checks": recon[0][2],
                                  "discrepancies": discrepancies[:20]}

            detail[session_id] = {
                "config": config,
                "versions": {
                    "code": code_version, "constraints": constraints,
                    "cost": cost_version, "slippage": slippage_version,
                    "execution": execution_version,
                },
                "equity": [{"t": r[0], "e": r[1], "d": r[2], "f": r[3],
                            "h": r[4], "u": r[5]}
                           for r in _rows(conn, """
                    SELECT at, equity, drawdown, data_freshness, health,
                           unpriced_positions
                    FROM paper_snapshots WHERE session_id = ?
                    ORDER BY at ASC
                """, (session_id,))],
                "positions": [{"instrument_id": r[0], "quantity": r[1],
                               "average_cost": r[2], "opened_at": r[3],
                               "signal_id": r[4]}
                              for r in _rows(conn, """
                    SELECT instrument_id, quantity, average_cost, opened_at,
                           entry_signal_id
                    FROM paper_positions WHERE session_id = ? AND quantity != 0
                    ORDER BY instrument_id
                """, (session_id,))],
                "orders_by_state": by_state,
                "rejections": by_reject,
                "fills": [{"instrument_id": r[0], "side": r[1], "quantity": r[2],
                           "price": r[3], "reference_price": r[4],
                           "commission": r[5], "slippage_cost": r[6],
                           "at": r[7], "partial": bool(r[8]),
                           "ambiguous": bool(r[9]), "venue": r[10]}
                          for r in _rows(conn, """
                    SELECT instrument_id, side, quantity, price, reference_price,
                           commission, slippage_cost, filled_at, is_partial,
                           intrabar_ambiguous, venue
                    FROM paper_fills WHERE session_id = ?
                    ORDER BY filled_at DESC LIMIT 60
                """, (session_id,))],
                "health": [{"at": r[0], "component": r[1], "state": r[2],
                            "detail": r[3], "latency_ms": r[4]}
                           for r in _rows(conn, """
                    SELECT at, component, state, detail, latency_ms
                    FROM paper_health WHERE session_id = ?
                      AND at = (SELECT MAX(at) FROM paper_health WHERE session_id = ?)
                    ORDER BY component
                """, (session_id, session_id))],
                "alerts": [{"code": r[0], "severity": r[1], "message": r[2],
                            "detail": r[3], "at": r[4]}
                           for r in _rows(conn, """
                    SELECT code, severity, message, detail, at FROM paper_alerts
                    WHERE session_id = ? ORDER BY at DESC LIMIT 40
                """, (session_id,))],
                "controls": [{"action": r[0], "at": r[1], "actor": r[2],
                              "reason": r[3]}
                             for r in _rows(conn, """
                    SELECT action, at, actor, reason FROM paper_control_actions
                    WHERE session_id = ? ORDER BY at DESC LIMIT 30
                """, (session_id,))],
                "events": [{"seq": r[0], "at": r[1], "kind": r[2],
                            "instrument_id": r[3], "message": r[4]}
                           for r in _rows(conn, """
                    SELECT sequence, at, kind, instrument_id, message
                    FROM paper_events WHERE session_id = ?
                    ORDER BY sequence DESC LIMIT 60
                """, (session_id,))],
                "checkpoints": _scalar(conn,
                    "SELECT COUNT(*) FROM paper_checkpoints WHERE session_id = ?",
                    (session_id,), default=0),
                "last_checkpoint": _scalar(conn,
                    "SELECT MAX(at) FROM paper_checkpoints WHERE session_id = ?",
                    (session_id,), default=None),
                "reconciliation": reconciliation,
                "latency": [{"stage": r[0], "mean": r[1], "worst": r[2]}
                            for r in _rows(conn, """
                    SELECT stage, AVG(milliseconds), MAX(milliseconds)
                    FROM paper_latency WHERE session_id = ?
                    GROUP BY stage ORDER BY stage
                """, (session_id,))],
            }

        return {"available": True, "is_paper": True, "accounts": accounts,
                "sessions": sessions, "detail": detail}

    def _backtest_config(self, conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
        raw = _scalar(conn, "SELECT config_json FROM backtest_runs WHERE run_id = ?",
                      (run_id,), default="{}")
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {}

    def _collect_constraints(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """The active risk limits, so the Risk page can show what is being enforced."""
        if not _table_exists(conn, "risk_constraints"):
            return {"available": False}
        rows = _rows(conn, """
            SELECT constraint_set_version, constraint_id, scope, severity,
                   max_value, min_value, applies_to, description, enabled
            FROM risk_constraints ORDER BY constraint_set_version, scope, constraint_id
        """)
        if not rows:
            return {"available": False}
        state = _scalar(conn,
                        "SELECT trading_state FROM risk_constraint_sets "
                        "ORDER BY version DESC LIMIT 1", default="enabled")
        return {
            "available": True,
            "trading_state": state,
            "constraints": [{
                "version": r[0], "id": r[1], "scope": r[2], "severity": r[3],
                "max": r[4], "min": r[5], "applies_to": r[6],
                "description": r[7], "enabled": bool(r[8]),
            } for r in rows],
        }

    def _collect_rec_index(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        """entity -> latest recommendation, across the FULL history table (every
        entity that has ever received one) — used to power the Markets 'call'
        column and the Company page's last-call panel."""
        if not _table_exists(conn, "recommendations"):
            return {}
        # One aggregate pass, joined back -- NOT a correlated subquery.
        #
        # The previous form asked, for each of 22,725 rows, "what is the
        # maximum generated_at for THIS row's entity", re-scanning the
        # whole table each time because no index covers `entity`. It
        # took 505 seconds to produce 389 rows. This computes each
        # entity maximum once.
        latest = _rows(conn, """
            SELECT r.entity, r.ticker, r.recommendation, r.confidence_score,
                   r.time_horizon, r.generated_at
            FROM recommendations r
            JOIN (
                SELECT entity, MAX(generated_at) AS newest
                FROM recommendations
                GROUP BY entity
            ) m ON m.entity = r.entity AND m.newest = r.generated_at
        """)
        return {
            entity: {
                "ticker": ticker, "recommendation": rec, "confidence_score": conf,
                "time_horizon": horizon, "generated_at": generated_at,
            }
            for entity, ticker, rec, conf, horizon, generated_at in latest
        }

    # ------------------------------------------------------------------
    # Static registries — the "universe" (companies/instruments), the
    # sector directory, and the event-type lexicon. These never touch
    # the database: they are exactly what COMPANY_REGISTRY /
    # COMPANY_SECTOR_MAP / EVENT_LEXICON already define in this repo,
    # so they can never drift stale the way a pre-generated export can.
    # ------------------------------------------------------------------

    def _build_universe(self) -> List[Dict[str, Any]]:
        return [
            {
                "t": c["ticker"], "n": c["canonical_name"], "a": c["aliases"],
                "c": c["category"], "s": COMPANY_SECTOR_MAP.get(c["canonical_name"], ""),
            }
            for c in COMPANY_REGISTRY
        ]

    def _build_sector_summary(self) -> List[Dict[str, Any]]:
        from collections import Counter
        company_counts = Counter(COMPANY_SECTOR_MAP.values())
        sectors = sorted(SECTOR_KEYWORDS.keys())
        return [
            {
                "name": s, "company_count": company_counts.get(s, 0),
                "keyword_count": len(SECTOR_KEYWORDS.get(s, [])),
            }
            for s in sectors
        ]

    def _build_event_lexicon_summary(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        fired_types = set()
        if _table_exists(conn, "canonical_events"):
            fired_types = {row[0] for row in _rows(conn, "SELECT DISTINCT event_type FROM canonical_events")}
        return {
            event_type: {"phrases": phrases, "fired": event_type in fired_types}
            for event_type, phrases in EVENT_LEXICON.items()
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_report(
        self,
        conn: Optional[sqlite3.Connection] = None,
        recommendations: Optional[List[Dict[str, Any]]] = None,
        articles: Optional[List[Dict[str, Any]]] = None,
        db_stats: Optional[Dict[str, Any]] = None,
        market_data: Optional[Dict[str, Dict[str, Any]]] = None,
        risk_data: Optional[Dict[str, Dict[str, Any]]] = None,
        sector_scores: Optional[List[Dict[str, Any]]] = None,
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]] = None,
        portfolio_result: Optional[Dict[str, Any]] = None,
        daily_summary_text: Optional[str] = None,
        entity_sector_map: Optional[Dict[str, str]] = None,
        verified_track_record: Optional[Dict[str, Optional[bool]]] = None,
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        portfolio_history: Optional[List[Dict[str, Any]]] = None,
        watchlist: Optional[List[str]] = None,
        upgrade_downgrade_results: Optional[List[Dict[str, Any]]] = None,
        accuracy_history: Optional[List[Dict[str, Any]]] = None,
        calibration_report: Optional[List[Dict[str, Any]]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        macro_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
        macro_indicators: Optional[List[Dict[str, Any]]] = None,
        fomc_meetings: Optional[List[Dict[str, Any]]] = None,
        source_summary: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Build the full MarketLens Terminal HTML document (a string).

        `conn` is an optional SQLite connection used to read every
        durable phase of the pipeline (health, events, impact,
        research, models, signals, recommendation history). Without
        one, those sections render an honest "unavailable" state
        instead of crashing or inventing numbers.

        Every other argument mirrors what run_daily.py already
        computes in memory each run (live prices, the daily narrative,
        today's watchlist) — things that are never persisted to the
        database, so they can only be shown when the caller passes
        them directly. All are optional; omitting them degrades the
        corresponding section gracefully, never the whole page.
        """
        conn = conn or sqlite3.connect(":memory:")

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        flow = self._collect_flow(conn)
        health = self._collect_health(conn)
        events_data = self._collect_events(conn)
        impact = self._collect_impact(conn)
        research = self._collect_research(conn)
        models = self._collect_models(conn)
        signals = self._collect_signals(conn)
        outcomes = self._collect_outcomes(conn)
        legacy = self._collect_legacy(conn, watchlist)
        rec_index = self._collect_rec_index(conn)
        portfolio = self._collect_portfolio(conn)
        constraints = self._collect_constraints(conn)
        backtests = self._collect_backtests(conn)
        paper = self._collect_paper(conn)
        cached_history, cached_prices = self._collect_price_history(conn)
        execution = self._collect_execution(conn)
        broker_detail = self._collect_broker_detail(conn)
        operations = self._collect_operations(conn)

        universe = self._build_universe()
        sector_summary = self._build_sector_summary()
        unmapped = [{"t": c["ticker"], "n": c["canonical_name"]} for c in COMPANY_REGISTRY
                    if c["canonical_name"] not in COMPANY_SECTOR_MAP]
        lexicon = self._build_event_lexicon_summary(conn)

        # Prefer this run's freshly computed recommendations (has the full
        # explanation/breakdown a person can read); fall back to the
        # DB-derived index so the page is still complete when generated
        # DB-only (scripts/build_dashboard.py, no in-memory run).
        current_recs_by_entity: Dict[str, Dict[str, Any]] = {}
        if recommendations:
            for r in recommendations:
                current_recs_by_entity[r["entity"]] = r

        data: Dict[str, Any] = {
            "meta": {
                "generated_at": generated_at,
                "db_size_mb": round(health["size_bytes"] / (1024 * 1024), 1) if health.get("size_bytes") else None,
                "total_companies": len(universe),
                "total_sectors": len(sector_summary),
                "watchlist_count": len(watchlist or []),
                "daily_summary": daily_summary_text,
            },
            "flow": flow,
            "health": health,
            "events": events_data,
            "impact": impact,
            "research": research,
            "models": models,
            "signals": signals,
            "outcomes": outcomes,
            "legacy": legacy,
            "portfolio": portfolio,
            "constraints": constraints,
            "backtests": backtests,
            "paper": paper,
            "execution": execution,
            "broker_detail": broker_detail,
            "operations": operations,
            "rec_index": rec_index,
            "current_recs": current_recs_by_entity,
            "universe": universe,
            "sector_summary": sector_summary,
            "unmapped": unmapped,
            "lexicon": lexicon,
            # Live quotes when a run supplied them; last stored close
            # otherwise, flagged `from_cache` so the page can label it
            # rather than pass a three-day-old close off as today's.
            "market_data": market_data or cached_prices or None,
            "risk_data": risk_data or None,
            # Live history when a run supplied it; stored candles
            # otherwise. Previously this was None on any DB-only
            # rebuild and every chart vanished.
            "price_history": price_history_map or cached_history or None,
            "macro": {
                "snapshots": macro_snapshots or None,
                "indicators": macro_indicators or None,
                "fomc": fomc_meetings or None,
            },
            "source_summary": source_summary or None,
            "portfolio_result": portfolio_result if (portfolio_result and portfolio_result.get("trades_simulated")) else None,
        }

        json_blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
        return _HTML_TEMPLATE.replace("__DATA_JSON__", json_blob)

    def save_report(self, html: str, path: str) -> None:
        """Write the generated HTML report to a file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MarketLens Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800&family=Fira+Code:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --ink:#201e1d; --bg:#f3f2f2; --bg2:#f8f4f4; --panel:#eae9e9;
  --line:#d7d3d3; --line-strong:#201e1d; --muted:#605d5d; --faint:#7d7979; --border-mid:#bab6b6;
  --accent:#ec3013; --accent-dark:#ae1800; --accent-darker:#7c1405; --accent-bg:#ffe0d9;
  --up:#00795a; --down:#ae1800; --mono:'Fira Code',ui-monospace,Menlo,monospace;
}
* { box-sizing:border-box; }
html,body { margin:0; padding:0; background:var(--bg); }
body { font-family:'Archivo',system-ui,sans-serif; color:var(--ink); -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums; }
a { color:var(--accent-dark); text-decoration:none; }
a:hover { color:var(--accent); text-decoration:underline; }
button, select, input { font-family:'Archivo',sans-serif; }
table { border-collapse:collapse; width:100%; }
::selection { background:var(--accent-bg); }
.hidden { display:none !important; }

#header { display:flex; align-items:stretch; border-bottom:2px solid var(--line-strong); background:var(--bg); position:sticky; top:0; z-index:40; }
.brand { width:232px; flex:none; display:flex; align-items:center; gap:8px; padding:0 16px; border-right:2px solid var(--line-strong); height:56px; cursor:pointer; }
.brand .dot { width:12px; height:12px; background:var(--accent); flex:none; }
.brand .name { font-weight:800; font-size:15px; letter-spacing:-0.01em; }
.headbar { flex:1; display:flex; align-items:center; gap:16px; padding:0 16px; min-width:0; }
.search-wrap { position:relative; flex:1; max-width:460px; }
.search-wrap input { width:100%; height:34px; padding:0 46px 0 10px; border:2px solid var(--line-strong); background:var(--bg2); font-size:13px; color:var(--ink); border-radius:0; }
.search-wrap .kbd { position:absolute; right:8px; top:9px; font-size:10px; font-weight:700; color:var(--faint); letter-spacing:0.06em; pointer-events:none; }
.search-results { position:absolute; top:38px; left:0; right:0; background:var(--bg2); border:2px solid var(--line-strong); box-shadow:0 12px 32px rgba(45,43,43,0.22); max-height:420px; overflow:auto; z-index:50; }
.sr-group-head { display:flex; justify-content:space-between; padding:6px 10px; background:var(--panel); font-size:10px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:var(--muted); }
.sr-item { display:flex; align-items:baseline; gap:10px; padding:7px 10px; font-size:13px; cursor:pointer; border-bottom:1px solid var(--line); }
.sr-item:hover { background:var(--accent-bg); }
.sr-item .k { font-weight:700; min-width:62px; }
.sr-item .n { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sr-item .m { font-size:11px; color:var(--muted); }
.headbar-fill { flex:1; }
.headbar .lastrun { font-size:11px; line-height:1.35; color:var(--muted); text-align:right; }
.headbar .lastrun b { font-weight:700; color:var(--ink); }

#shell { display:flex; align-items:stretch; min-height:calc(100vh - 56px); }
#nav { width:232px; flex:none; border-right:2px solid var(--line-strong); padding:0 0 32px 0; background:var(--bg); }
.nav-group { border-bottom:1px solid var(--line); padding:12px 0 10px 0; }
.nav-group-label { padding:0 16px 8px 16px; font-size:10px; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; color:var(--faint); }
.nav-item { display:flex; align-items:center; gap:8px; padding:7px 16px; font-size:13px; font-weight:500; cursor:pointer; color:var(--ink); }
.nav-item:hover { background:var(--accent-bg); }
.nav-item.active { background:var(--ink); color:var(--bg); font-weight:700; }
.nav-item .lbl { flex:1; min-width:0; }
.nav-item .tag { font-size:9px; font-weight:700; letter-spacing:0.06em; padding:1px 4px; background:var(--panel); color:var(--muted); }
.nav-item.active .tag { background:var(--accent); color:var(--bg); }
.nav-item.stub { opacity:0.55; }
.nav-note { padding:12px 16px; font-size:11px; line-height:1.5; color:var(--faint); }

#main { flex:1; min-width:0; padding:0 0 64px 0; }
.page-head { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; padding:24px 24px 16px 24px; border-bottom:2px solid var(--line-strong); flex-wrap:wrap; }
.kicker { font-size:10px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:var(--faint); margin-bottom:6px; }
.page-head h1 { margin:0; font-size:30px; font-weight:800; letter-spacing:-0.02em; line-height:1.05; }
.stat-pill-row { display:flex; border:2px solid var(--line-strong); flex:none; }
.stat-pill { padding:6px 12px; }
.stat-pill + .stat-pill { border-left:2px solid var(--line-strong); }
.stat-pill .l { font-size:10px; letter-spacing:0.08em; text-transform:uppercase; color:var(--muted); }
.stat-pill .v { font-size:15px; font-weight:800; }

section.blk { border-bottom:2px solid var(--line-strong); }
.blk-head { display:flex; align-items:baseline; justify-content:space-between; padding:14px 24px 10px 24px; gap:12px; flex-wrap:wrap; }
.blk-head h2 { margin:0; font-size:13px; font-weight:800; letter-spacing:0.08em; text-transform:uppercase; }
.blk-note { font-size:11px; color:var(--muted); }
.blk-note.warn { color:var(--accent-dark); font-weight:700; }
.blk-body { border-top:1px solid var(--line); padding:16px 24px 20px 24px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; }
.grid2 > div:first-child { border-right:2px solid var(--line-strong); }
.grid32 { display:grid; grid-template-columns:3fr 2fr; }
.grid32 > div:first-child { border-right:2px solid var(--line-strong); }

.flowstrip { display:grid; grid-template-columns:repeat(9,1fr); border-top:1px solid var(--line); }
.flow-cell { padding:12px 12px 14px 12px; border-right:1px solid var(--line); }
.flow-cell .lbl { font-size:9px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:var(--faint); margin-bottom:8px; }
.flow-cell .n { font-size:22px; font-weight:800; letter-spacing:-0.02em; line-height:1; }
.flow-cell .u { font-size:10px; color:var(--muted); margin-top:3px; }
.flow-cell .bartrack { height:4px; margin-top:10px; background:var(--line); }
.flow-cell .barfill { height:4px; background:var(--ink); }

.statgrid { display:grid; border-top:1px solid var(--line); }
.statgrid .cell { padding:12px; border-right:1px solid var(--line); }
.statgrid .cell:last-child { border-right:0; }
.statgrid .cell .n { font-size:20px; font-weight:800; letter-spacing:-0.02em; }
.statgrid .cell .l { font-size:10px; line-height:1.3; color:var(--muted); margin-top:4px; }

.barrow { display:grid; grid-template-columns:150px 1fr 46px; align-items:center; gap:10px; margin-bottom:7px; }
.barrow .lbl { font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.barrow .track { height:10px; background:var(--line); }
.barrow .fill { display:block; height:10px; background:var(--ink); }
.barrow .val { font-size:12px; font-weight:700; text-align:right; }
.barrow.clickable { cursor:pointer; padding:2px 4px; margin:0 -4px 5px -4px; }
.barrow.clickable:hover { background:var(--accent-bg); }

.mono { font-family:var(--mono); }
p.copy { margin:0; font-size:12px; line-height:1.55; color:#444141; max-width:60ch; }
p.copy.wide { max-width:80ch; }

table.data { width:100%; font-size:12px; }
table.data th { padding:7px 24px; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; border-bottom:2px solid var(--line-strong); background:var(--panel); text-align:left; }
table.data th.r { text-align:right; }
table.data th:first-child, table.data td:first-child { padding-left:24px; }
table.data th:last-child, table.data td:last-child { padding-right:24px; }
table.data td { padding:8px; border-bottom:1px solid var(--line); }
table.data td.r { text-align:right; }
table.data tr.rowlink { cursor:pointer; }
table.data tr.rowlink:hover { background:#fff2ef; }
table.data tr.sel { background:var(--accent-bg); }

.pill { display:inline-block; font-size:10px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; padding:2px 5px; }
.pill.outline-up { border:1px solid var(--up); color:var(--up); }
.pill.solid { background:var(--ink); color:var(--bg); }
.pill.ghost { border:1px solid var(--border-mid); color:var(--border-mid); }
.pill.warn-outline { border:2px solid var(--accent); padding:3px 8px; }

.callout { border:2px solid var(--accent); padding:14px 16px; max-width:92ch; }
.callout .t { font-size:10px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:var(--accent-dark); margin-bottom:6px; }
.hatched { border:1px solid var(--border-mid); background:repeating-linear-gradient(135deg,var(--panel) 0 6px,var(--bg) 6px 12px); padding:24px; }

.filterbar { display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:12px 24px; border-bottom:1px solid var(--line); }
.segbtns { display:flex; border:2px solid var(--line-strong); }
.segbtns button { height:28px; padding:0 12px; border:0; background:transparent; font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; cursor:pointer; color:var(--ink); }
.segbtns button + button { border-left:2px solid var(--line-strong); }
.segbtns button.active { background:var(--ink); color:var(--bg); }
.field { height:32px; padding:0 10px; border:2px solid var(--line-strong); background:var(--bg2); font-size:12px; color:var(--ink); border-radius:0; }
.grow { flex:1; }

.regcard { border:2px solid var(--line-strong); padding:12px; }
.regcard .rc-top { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:4px; }
.regcard .rc-key { font-size:12px; font-weight:800; font-family:var(--mono); }
.sector-tile { padding:12px 14px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); cursor:pointer; }
.sector-tile:hover { background:var(--accent-bg); }
.sectorgrid { display:grid; grid-template-columns:repeat(4,1fr); border-top:1px solid var(--line); border-left:1px solid var(--line); }
@media (max-width:1100px) { .sectorgrid { grid-template-columns:repeat(2,1fr); } .grid2, .grid32 { grid-template-columns:1fr; } .grid2 > div:first-child, .grid32 > div:first-child { border-right:0; border-bottom:2px solid var(--line-strong); } .flowstrip { grid-template-columns:repeat(3,1fr); } }
@media (max-width:760px) { #nav { display:none; } .brand { width:auto; } }

.backbtn { height:28px; padding:0 12px; border:2px solid var(--line-strong); background:transparent; font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; cursor:pointer; color:var(--ink); }
.backbtn:hover { background:var(--accent-bg); }
.aliaschip { font-size:12px; font-family:var(--mono); padding:3px 8px; border:2px solid var(--line-strong); display:inline-block; margin:0 6px 6px 0; }
.phrasechip { font-size:12px; font-family:var(--mono); padding:3px 8px; border:1px solid var(--border-mid); display:inline-block; margin:0 6px 6px 0; }
.unfired-chip { font-size:11px; font-family:var(--mono); padding:3px 8px; border:1px solid var(--accent); color:var(--accent-darker); display:inline-block; margin:0 6px 6px 0; }

.footer { border-top:2px solid var(--line-strong); padding:16px 24px; font-size:11px; line-height:1.6; color:var(--muted); max-width:90ch; }
.empty { color:var(--muted); font-size:12.5px; font-style:italic; padding:6px 0; }

/* --- Phase 13: the paper-mode indicator ---------------------------
   Deliberately loud and deliberately unremovable. Every number in the
   paper workspace is simulated, and a page showing equity, positions
   and fills without saying so would be indistinguishable at a glance
   from one showing a real account. The banner is sticky so it stays on
   screen however far the reader scrolls. */
.papermode { position:sticky; top:56px; z-index:40; display:flex; align-items:center; gap:12px;
  background:var(--accent); color:#fff; padding:9px 24px; border-bottom:2px solid var(--line-strong); }
.papermode .tagname { font-size:12px; font-weight:800; letter-spacing:0.14em; text-transform:uppercase;
  border:2px solid #fff; padding:2px 8px; flex:none; }
.papermode .msg { font-size:11.5px; line-height:1.4; }
.papermode .msg b { font-weight:800; }
.headbar .papertag { font-size:10px; font-weight:800; letter-spacing:0.1em; text-transform:uppercase;
  background:var(--accent); color:#fff; padding:3px 8px; margin-right:12px; }
.headbar .papertag.hidden { display:none; }

/* --- Phase 14: the execution boundary -----------------------------
   The banner states what the phase cannot do, in the same place every
   time. A workspace showing brokers, accounts and orders reads at a
   glance like one attached to a real account without it. */
.xbanner { position:sticky; top:56px; z-index:40; display:flex; align-items:center; gap:12px;
  background:var(--ink); color:var(--bg); padding:9px 24px; border-bottom:2px solid var(--accent); }
.xbanner .tagname { font-size:12px; font-weight:800; letter-spacing:0.14em; text-transform:uppercase;
  background:var(--accent); color:#fff; padding:2px 8px; flex:none; }
.xbanner .msg { font-size:11.5px; line-height:1.4; }
.xbanner .msg b { font-weight:800; }
.envrow { display:flex; gap:8px; flex-wrap:wrap; padding:12px 24px; border-bottom:1px solid var(--line); }
.envchip { font-size:10px; font-weight:700; letter-spacing:0.07em; text-transform:uppercase;
  border:2px solid var(--line-strong); padding:4px 9px; }
.envchip.on { background:var(--ink); color:var(--bg); }
.envchip.off { opacity:0.5; }
.envchip.danger { border-color:var(--accent); color:var(--accent-dark); }
.xstate { font-size:10px; font-weight:700; letter-spacing:0.05em; text-transform:uppercase;
  padding:2px 6px; border:1px solid currentColor; white-space:nowrap; }
.pp-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); border-top:1px solid var(--line); }
.pp-hstate { display:inline-block; width:8px; height:8px; margin-right:6px; }
</style>
</head>
<body>
<div id="header">
  <div class="brand" onclick="MLGo('overview')">
    <span class="dot"></span><span class="name">MarketLens</span>
  </div>
  <div class="headbar">
    <div class="search-wrap">
      <input id="ml-search-input" type="text" placeholder="Cauta companii, instrumente, sectoare, tipuri de eveniment..." oninput="MLSearchInput(this.value)" onfocus="MLSearchOpen(true)">
      <span class="kbd">/</span>
      <div id="ml-search-results" class="search-results hidden"></div>
    </div>
    <div class="headbar-fill"></div>
    <span class="papertag hidden" id="ml-papertag">Paper mode &middot; simulat</span>
    <div class="lastrun" id="ml-lastrun"></div>
  </div>
</div>
<div id="shell">
  <nav id="nav"></nav>
  <main id="main"></main>
</div>

<script>
(function () {
  "use strict";
  var D = __DATA_JSON__;

  // ---------------------------------------------------------------
  // small helpers
  // ---------------------------------------------------------------
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtNum(n, digits) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("ro-RO", { maximumFractionDigits: digits === undefined ? 0 : digits, minimumFractionDigits: digits === undefined ? 0 : digits });
  }
  function fmtPct(x, digits) {
    if (x === null || x === undefined) return "—";
    return (x * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }
  function fmtSignedPct(x, digits) {
    if (x === null || x === undefined) return "—";
    var v = x * 100;
    return (v >= 0 ? "+" : "") + v.toFixed(digits === undefined ? 2 : digits) + "%";
  }
  function fmtDate(s) { return s ? esc(String(s).slice(0, 16)).replace("T", " ") : "—"; }
  function cap(s) { return s === null || s === undefined ? "—" : String(s); }

  function barRows(pairs, valueFmt) {
    valueFmt = valueFmt || function (v) { return fmtNum(v); };
    if (!pairs || !pairs.length) return '<div class="empty">Fara date.</div>';
    var max = Math.max.apply(null, pairs.map(function (p) { return p[1] || 0; })) || 1;
    return pairs.map(function (p) {
      var pct = (100 * (p[1] || 0) / max).toFixed(1);
      return '<div class="barrow"><div class="lbl mono">' + esc(p[0]) + '</div>' +
        '<div class="track"><span class="fill" style="width:' + pct + '%"></span></div>' +
        '<div class="val">' + valueFmt(p[1]) + '</div></div>';
    }).join("");
  }

  function statPillRow(pills) {
    return '<div class="stat-pill-row">' + pills.map(function (p) {
      return '<div class="stat-pill"><div class="l">' + esc(p[0]) + '</div><div class="v">' + esc(p[1]) + '</div></div>';
    }).join("") + '</div>';
  }

  function pageHead(kicker, title, pills) {
    return '<div class="page-head"><div><div class="kicker">' + esc(kicker) + '</div><h1>' + esc(title) + '</h1></div>' +
      (pills ? statPillRow(pills) : "") + '</div>';
  }

  function blk(title, note, bodyHtml, noteClass) {
    return '<section class="blk"><div class="blk-head"><h2>' + esc(title) + '</h2>' +
      (note ? '<span class="blk-note' + (noteClass ? " " + noteClass : "") + '">' + note + '</span>' : "") +
      '</div><div class="blk-body">' + bodyHtml + '</div></section>';
  }

  // Phase 18 / NEW-01. One definition of what a model status looks
  // like, used by the model cards, the signal rows and the detail
  // panel, so the three cannot end up disagreeing about whether a
  // model is approved.
  var MODEL_STATUS_LABEL = {
    active:    ["VALIDAT", "#00795a", "model promovat de un om dupa ce a trecut pragul de calitate"],
    evaluated: ["EXPERIMENTAL", "#ae6c00", "model masurat dar nepromovat — rezultat de cercetare, nu de productie"],
    trained:   ["NEEVALUAT", "#8a8a8a", "model antrenat dar neevaluat — nu poate fi judecat"],
    draft:     ["SCHITA", "#8a8a8a", "model incomplet"],
    degraded:  ["DEGRADAT", "#ae1800", "a fost activ; dovezile ulterioare l-au retras"],
    retired:   ["RETRAS", "#8a8a8a", "inlocuit de un model mai bun; pastrat pentru audit"]
  };

  function modelStatusPill(status, size) {
    var entry = MODEL_STATUS_LABEL[String(status || "").toLowerCase()];
    if (!entry) return "";
    return '<span class="pill" title="' + esc(entry[2]) + '" style="border:1px solid ' +
      entry[1] + ';color:' + entry[1] + ';font-size:' + (size || 10) + 'px;padding:2px 6px;">' +
      entry[0] + '</span>';
  }

  // s[10] is the status of the model that produced this signal.
  // Anything that is not 'active' means the reader must not treat the
  // number as a validated claim.
  function signalIsExperimental(s) {
    return String(s[10] || "").toLowerCase() !== "active";
  }

  function signalLabel(s) {
    // s[8] company name, s[9] ticker, s[1] instrument_id.
    // The id is the fallback, not the default: it is a storage key, and
    // "us_and_intl-brk.b" tells a reader which bucket the row lives in
    // rather than that this is Berkshire Hathaway.
    var name = s[8], ticker = s[9];
    if (name && ticker) return esc(name) + ' <span class="mono" style="color:var(--muted);font-weight:400;">' + esc(ticker) + '</span>';
    if (name) return esc(name);
    return '<span class="mono">' + esc(s[1]) + '</span>';
  }

  function sparkline(series, width, height) {
    width = width || 160; height = height || 28;
    var vals = series.map(function (p) { return p.close !== undefined ? p.close : p; }).filter(function (v) { return v !== null && v !== undefined; });
    if (vals.length < 2) return "";
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    var range = (max - min) || 1;
    var up = vals[vals.length - 1] >= vals[0];
    var pts = vals.map(function (v, i) {
      var x = (i / (vals.length - 1)) * width;
      var y = height - ((v - min) / range) * height;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    return '<svg viewBox="0 0 ' + width + ' ' + height + '" width="100%" height="' + height + '" preserveAspectRatio="none" style="display:block;margin-top:8px;">' +
      '<polyline points="' + pts + '" fill="none" stroke="' + (up ? "#00795a" : "#ae1800") + '" stroke-width="1.5"></polyline></svg>';
  }

  // ---------------------------------------------------------------
  // Phase 11 — portfolio and risk
  // ---------------------------------------------------------------
  var PF = D.portfolio || { available: false };
  var PF_LIST = PF.portfolios || [];
  var PF_COUNT = PF_LIST.length;

  function pfSelected() {
    // The route parameter selects a book; with one portfolio (the
    // common case) it is implicit.
    if (!PF_COUNT) return null;
    var id = state.param;
    if (id && PF.detail && PF.detail[id]) return { id: id, d: PF.detail[id] };
    var first = PF_LIST[0].id;
    return PF.detail && PF.detail[first] ? { id: first, d: PF.detail[first] } : null;
  }

  var STATE_LABEL = {
    approved: "APROBAT", reduced: "REDUS", rejected: "RESPINS",
    requires_review: "NECESITA REVIZUIRE", insufficient_data: "DATE INSUFICIENTE"
  };
  var STATE_COLOR = {
    approved: "var(--up)", reduced: "var(--up)", rejected: "var(--accent-dark)",
    requires_review: "var(--accent-dark)", insufficient_data: "var(--faint)"
  };

  var RISK_TAG = (function () {
    var sel = PF_COUNT ? (PF.detail || {})[PF_LIST[0].id] : null;
    if (!sel || !sel.decision) return "0";
    var n = (sel.decision.violations || []).filter(function (v) { return !v.remediated; }).length;
    return String(n);
  })();

  function pfMoney(v, currency) {
    if (v === null || v === undefined) return "—";
    return fmtNum(v, 2) + (currency ? " " + currency : "");
  }

  function pfEmptyState() {
    if (!PF.available) {
      return blk("Portofoliu", "Faza 11",
        '<div class="callout"><b>Tabelele Fazei 11 nu exista in aceasta baza de date.</b>' +
        '<p>Motorul de portofoliu si risc este implementat, dar schema nu a fost inca creata aici. ' +
        'Ruleaza <span class="mono">python scripts/evaluate_portfolio_risk.py --portfolio &lt;nume&gt; --create</span> ' +
        'pentru a crea tabelele si a declara un portofoliu.</p></div>');
    }
    return blk("Portofoliu", "niciun portofoliu declarat",
      '<div class="callout"><b>Niciun portofoliu nu este declarat inca.</b>' +
      '<p>Aceasta nu este o eroare si nu exista date simulate afisate in loc. ' +
      'Motorul de risc (Faza 11) este complet functional, dar opereaza doar pe pozitii ' +
      'declarate explicit — nu inventeaza un portofoliu.</p>' +
      '<p>Pentru a declara unul:</p>' +
      '<div class="mono" style="margin-top:8px;font-size:11px;background:var(--panel);padding:10px;">' +
      'python scripts/evaluate_portfolio_risk.py --portfolio meu --create --cash 100000</div>' +
      '<p style="margin-top:10px;">Odata ce exista pozitii, aceasta pagina arata expunerea reala, ' +
      'ponderile, P&amp;L nerealizat si verdictul motorului de risc.</p></div>');
  }

  function pfQualityCallouts(s) {
    var out = "";
    if (!s.complete) {
      out += '<div class="callout"><b>Instantaneu incomplet.</b> ' +
        esc((s.unvalued || []).length) + ' pozitie(i) nu au avut pret la ancora: ' +
        '<span class="mono">' + esc((s.unvalued || []).join(", ")) + '</span>. ' +
        'Capitalul propriu este subevaluat, deci fiecare pondere calculata fata de el este ' +
        'supraevaluata — motorul de risc refuza sa aprobe in aceasta stare.</div>';
    }
    if (s.stale) {
      out += '<div class="callout"><b>Preturi invechite.</b> Cel putin o pozitie a fost ' +
        'evaluata pe baza unei lumanari mai vechi decat limita de prospetime. Valoarea este ' +
        'afisata, dar marcata — o decizie de risc pe date invechite nu este aprobata.</div>';
    }
    if (s.multi_currency) {
      out += '<div class="callout"><b>Mai multe valute.</b> Nu exista date FX in acest sistem, ' +
        'deci totalurile de mai jos aduna unitati diferite. Sunt afisate exact asa cum sunt, ' +
        'fara curs inventat.</div>';
    }
    return out;
  }

  function viewPortfolio() {
    var sel = pfSelected();
    if (!sel) return pageHead("Portofoliu", "Portofoliu") + pfEmptyState();

    var d = sel.d, s = d.snapshot;
    var html = pageHead("Portofoliu - stare curenta", sel.id, [
      ["Capital propriu", pfMoney(s.equity, s.currency)],
      ["Pozitii", String((d.positions || []).length)]
    ]);

    if (PF_COUNT > 1) {
      html += '<div class="filterbar"><div class="segbtns">' + PF_LIST.map(function (p) {
        return '<button class="' + (p.id === sel.id ? "on" : "") + '" onclick="MLGo(\'portfolio\',\'' +
          esc(p.id) + '\')">' + esc(p.name) + '</button>';
      }).join("") + '</div></div>';
    }

    var q = pfQualityCallouts(s);
    if (q) html += blk("Calitatea datelor", "verificari de integritate", q, "warn");

    html += '<section class="blk"><div class="statgrid" style="grid-template-columns:repeat(6,1fr);">' +
      '<div class="cell"><div class="n">' + pfMoney(s.equity) + '</div><div class="l">capital propriu (' + esc(s.currency) + ')</div></div>' +
      '<div class="cell"><div class="n">' + pfMoney(s.cash) + '</div><div class="l">numerar</div></div>' +
      '<div class="cell"><div class="n">' + pfMoney(s.gross) + '</div><div class="l">expunere bruta</div></div>' +
      '<div class="cell"><div class="n">' + pfMoney(s.net) + '</div><div class="l">expunere neta</div></div>' +
      '<div class="cell"><div class="n">' + (s.leverage === null || s.leverage === undefined ? "—" : Number(s.leverage).toFixed(2) + "x") + '</div><div class="l">levier</div></div>' +
      '<div class="cell"><div class="n" style="color:' + ((s.unrealized || 0) >= 0 ? "var(--up)" : "var(--down)") + '">' +
        pfMoney(s.unrealized) + '</div><div class="l">P&amp;L nerealizat</div></div>' +
      '</div></section>';

    // --- positions ---
    var rows = (d.positions || []).map(function (p) {
      var statusPill = p.status === "valued"
        ? ''
        : '<span class="pill warn-outline">' + esc(p.status === "stale_price" ? "invechit" : "fara pret") + '</span>';
      return '<tr>' +
        '<td class="mono">' + esc(p.instrument_id) + '</td>' +
        '<td class="r">' + fmtNum(p.quantity, 4) + '</td>' +
        '<td class="r">' + (p.entry === null ? "—" : fmtNum(p.entry, 2)) + '</td>' +
        '<td class="r">' + (p.price === null ? "—" : fmtNum(p.price, 2)) + '</td>' +
        '<td class="r">' + pfMoney(p.market_value) + '</td>' +
        '<td class="r">' + fmtPct(p.weight, 2) + '</td>' +
        '<td class="r" style="color:' + ((p.unrealized || 0) >= 0 ? "var(--up)" : "var(--down)") + '">' +
          pfMoney(p.unrealized) + '</td>' +
        '<td>' + statusPill + '</td></tr>';
    }).join("");

    html += blk("Pozitii", String((d.positions || []).length) + " detinute",
      rows
        ? '<table class="data"><thead><tr><th>Instrument</th><th class="r">Cantitate</th>' +
          '<th class="r">Pret intrare</th><th class="r">Pret curent</th><th class="r">Valoare</th>' +
          '<th class="r">Pondere</th><th class="r">P&amp;L</th><th>Stare</th></tr></thead><tbody>' +
          rows + '</tbody></table>'
        : '<div class="empty">Portofoliu gol - doar numerar.</div>');

    // --- allocation ---
    var allocBlocks = "";
    ["sector", "asset_class", "currency"].forEach(function (dim) {
      var ex = (d.exposures || {})[dim];
      if (!ex || !ex.buckets.length) return;
      var pairs = ex.buckets.map(function (b) { return [b.label + " (" + b.count + ")", b.weight || 0]; });
      var note = ex.unclassified_count
        ? '<span class="blk-note warn">' + ex.unclassified_count + ' neclasificat(e)</span>' : "";
      allocBlocks += '<div><div class="blk-head"><h2>' +
        esc({ sector: "Alocare pe sector", asset_class: "Pe clasa de activ", currency: "Pe valuta" }[dim]) +
        '</h2>' + note + '</div><div class="blk-body">' +
        barRows(pairs, function (v) { return fmtPct(v, 1); }) + '</div></div>';
    });
    if (allocBlocks) {
      html += '<section class="blk"><div class="grid2">' + allocBlocks + '</div></section>';
    }

    html += blk("Provenienta", "de unde vine fiecare cifra",
      '<div class="copy"><p>Preturile provin exclusiv din <span class="mono">price_candle_cache</span> ' +
      '(lumanari deja stocate), niciodata dintr-un apel live — de aceea aceeasi stare poate fi ' +
      'recalculata identic mai tarziu. Sectorul vine din lantul canonic ' +
      '<span class="mono">instruments -> securities -> companies -> sectors</span>, nu din ' +
      'potrivirea pe cuvinte-cheie.</p>' +
      '<p>Ancora: <span class="mono">' + fmtDate(s.as_of) + '</span>. ' +
      'Nicio lumanare de dupa aceasta data nu este citita.</p></div>');

    return html;
  }

  function viewRisk() {
    var sel = pfSelected();
    if (!sel) {
      return pageHead("Risc", "Motorul de risc") +
        blk("Stare", "Faza 11",
          '<div class="callout"><b>Niciun portofoliu de evaluat.</b>' +
          '<p>Motorul de risc este implementat si testat, dar nu are pe ce sa opereze pana cand ' +
          'nu este declarat un portofoliu. Limitele active sunt afisate mai jos.</p></div>') +
        riskConstraintsBlock();
    }

    var d = sel.d, m = d.metrics, dec = d.decision;
    var color = STATE_COLOR[dec.state] || "var(--muted)";

    var html = pageHead("Risc - decizie", sel.id, [
      ["Verdict", STATE_LABEL[dec.state] || dec.state],
      ["Limite", dec.constraint_version]
    ]);

    html += '<section class="blk"><div class="blk-body">' +
      '<div style="border:2px solid ' + color + ';padding:14px 16px;">' +
      '<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:' + color + ';">' +
      esc(STATE_LABEL[dec.state] || dec.state) + '</div>' +
      '<div style="font-size:15px;font-weight:700;margin-top:6px;">' + esc(dec.summary) + '</div>' +
      '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:8px;">' +
      esc(dec.id) + ' - motor ' + esc(dec.engine_version) + ' / limite ' + esc(dec.constraint_version) +
      '</div></div></div></section>';

    // --- violations ---
    var vrows = (dec.violations || []).map(function (v) {
      var sev = v.remediated
        ? '<span class="pill ghost">remediat</span>'
        : (v.severity === "hard" ? '<span class="pill" style="background:var(--accent-dark);color:var(--bg);">HARD</span>'
                                 : '<span class="pill warn-outline">soft</span>');
      return '<tr><td>' + sev + '</td>' +
        '<td class="mono">' + esc(v.constraint_id) + '</td>' +
        '<td>' + esc(v.applies_to || "-") + '</td>' +
        '<td class="r">' + (v.current === null ? "—" : fmtPct(v.current, 2)) + '</td>' +
        '<td class="r"><b>' + (v.observed === null ? "—" : fmtPct(v.observed, 2)) + '</b></td>' +
        '<td class="r">' + (v.limit === null ? "—" : fmtPct(v.limit, 2)) + '</td></tr>';
    }).join("");

    html += blk("Limite incalcate", (dec.violations || []).length + " total",
      vrows
        ? '<table class="data"><thead><tr><th>Severitate</th><th>Limita</th><th>Se aplica la</th>' +
          '<th class="r">Curent</th><th class="r">Proiectat</th><th class="r">Prag</th></tr></thead>' +
          '<tbody>' + vrows + '</tbody></table>'
        : '<div class="empty">Nicio limita incalcata.</div>');

    // --- metrics ---
    function metricCell(label, value, note) {
      return '<div class="cell"><div class="n">' + value + '</div><div class="l">' + esc(label) +
        (note ? '<br><span style="color:var(--faint);">' + esc(note) + '</span>' : "") + '</div></div>';
    }
    html += '<section class="blk"><div class="blk-head"><h2>Metrici de risc</h2>' +
      '<span class="blk-note">masurate pe istoricul stocat, nu prognoze</span></div>' +
      '<div class="statgrid" style="grid-template-columns:repeat(4,1fr);">' +
      metricCell("volatilitate anualizata",
        m.volatility_insufficient ? "—" : fmtPct(m.volatility, 1),
        m.volatility_insufficient ? m.volatility_note : m.volatility_obs + " observatii") +
      metricCell("VaR " + fmtPct(m.var_confidence, 0) + " (1 zi)",
        m.var_insufficient ? "—" : fmtPct(m.var, 2),
        m.var_insufficient ? m.var_note : "istoric, din capital propriu") +
      metricCell("expected shortfall",
        m.var_insufficient ? "—" : fmtPct(m.es, 2),
        m.var_insufficient ? "" : "pierderea medie dincolo de VaR") +
      metricCell("drawdown maxim",
        m.drawdown_insufficient ? "—" : fmtPct(m.max_drawdown, 2),
        m.drawdown_insufficient ? "necesita istoric de instantanee" : "din instantanee reale") +
      '</div><div class="statgrid" style="grid-template-columns:repeat(4,1fr);">' +
      metricCell("HHI (concentrare)", m.hhi === null ? "—" : Number(m.hhi).toFixed(4),
        "fata de capitalul propriu") +
      metricCell("largime efectiva",
        m.effective_positions === null ? "—" : Number(m.effective_positions).toFixed(2),
        "pozitii echivalente, din partea investita") +
      metricCell("cea mai mare pozitie", fmtPct(m.largest_weight, 2),
        m.largest_instrument || "") +
      metricCell("corelatie medie",
        m.avg_correlation === null ? "—" : Number(m.avg_correlation).toFixed(3),
        m.correlation_pairs + " perechi, " + m.correlation_thin + " prea subtiri") +
      '</div></section>';

    if ((m.correlated_pairs || []).length) {
      html += blk("Perechi puternic corelate", "se misca impreuna",
        '<div class="copy"><p>Aceste pozitii nu diversifica atat cat sugereaza numarul lor.</p></div>' +
        '<table class="data"><thead><tr><th>Instrument A</th><th>Instrument B</th>' +
        '<th class="r">Corelatie</th></tr></thead><tbody>' +
        m.correlated_pairs.map(function (p) {
          return '<tr><td class="mono">' + esc(p[0]) + '</td><td class="mono">' + esc(p[1]) +
            '</td><td class="r"><b>' + Number(p[2]).toFixed(2) + '</b></td></tr>';
        }).join("") + '</tbody></table>', "warn");
    }

    // --- audit: what ran, what did not ---
    var skipped = dec.skipped || {};
    var skippedKeys = Object.keys(skipped);
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Verificari efectuate</h2><span class="blk-note">' +
      (dec.evaluated || []).length + '</span></div><div class="blk-body">' +
      ((dec.evaluated || []).length
        ? '<div style="display:flex;flex-wrap:wrap;gap:6px;">' + dec.evaluated.map(function (s) {
            return '<span class="phrasechip mono">' + esc(s) + '</span>'; }).join("") + '</div>'
        : '<div class="empty">Nicio verificare nu a rulat.</div>') +
      '</div></div>' +
      '<div><div class="blk-head"><h2>Verificari care nu au putut rula</h2><span class="blk-note' +
      (skippedKeys.length ? ' warn' : '') + '">' + skippedKeys.length + '</span></div><div class="blk-body">' +
      (skippedKeys.length
        ? skippedKeys.map(function (k) {
            return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:12px;">' +
              '<span class="mono"><b>' + esc(k) + '</b></span><br>' +
              '<span style="color:var(--muted);">' + esc(skipped[k]) + '</span></div>'; }).join("")
        : '<div class="empty">Toate verificarile aplicabile au rulat.</div>') +
      '</div></div></section>';

    if ((dec.reasons || []).length) {
      html += blk("Rationament", "de ce acest verdict",
        '<div class="copy">' + dec.reasons.map(function (r) {
          return '<p>- ' + esc(r) + '</p>'; }).join("") + '</div>');
    }

    html += blk("Intentii de ordin", d.intents + " generate",
      '<div class="copy"><p>O intentie de ordin este o <b>inregistrare inerta</b>: descrie ce ' +
      '<i>ar fi</i> instruit daca ar exista un nivel de executie. Nu are cont, broker, bursa sau ' +
      'identificator de ordin, si nimic din aceasta faza nu o poate transmite nicaieri. ' +
      'Integrarea Interactive Brokers apartine Fazei 15.</p></div>');

    html += riskConstraintsBlock();
    return html;
  }

  function riskConstraintsBlock() {
    var C = D.constraints || { available: false };
    if (!C.available) {
      return blk("Limite de risc", "neconfigurate",
        '<div class="empty">Setul de limite nu a fost inca scris in baza de date. ' +
        'Prima rulare a evaluatorului il creeaza din valorile implicite.</div>');
    }
    var rows = C.constraints.filter(function (c) { return c.enabled; }).map(function (c) {
      var bound = c.max !== null && c.max !== undefined
        ? "max " + Number(c.max).toFixed(2)
        : "min " + Number(c.min).toFixed(2);
      return '<tr><td>' + (c.severity === "hard"
          ? '<span class="pill solid">HARD</span>'
          : '<span class="pill ghost">soft</span>') + '</td>' +
        '<td class="mono">' + esc(c.id) + '</td>' +
        '<td class="mono">' + esc(c.scope) + '</td>' +
        '<td class="r mono">' + esc(bound) + '</td>' +
        '<td style="color:var(--muted);font-size:11px;">' + esc(c.description) + '</td></tr>';
    }).join("");

    return blk("Limite active",
      'set <span class="mono">' + esc((C.constraints[0] || {}).version || "v1") +
      '</span> - stare ' + esc(C.trading_state),
      '<table class="data"><thead><tr><th>Severitate</th><th>Identificator</th><th>Domeniu</th>' +
      '<th class="r">Prag</th><th>Ce protejeaza</th></tr></thead><tbody>' + rows + '</tbody></table>');
  }

  // ---------------------------------------------------------------
  // Phase 12 — backtesting
  // ---------------------------------------------------------------
  var PP = D.paper || { available: false, is_paper: true };
  var PP_SESSIONS = PP.sessions || [];

  var PP_HEALTH_COLOR = {
    healthy: "var(--up)", degraded: "#b07000", stale: "#b07000",
    failed: "var(--accent-dark)", paused: "var(--muted)"
  };
  var PP_FRESH_LABEL = {
    fresh: "proaspete", aging: "in imbatranire", stale: "invechite",
    invalid: "invalide", unavailable: "indisponibile"
  };

  var XE = D.execution || { available: false, live_execution: false };
  var XE_BROKERS = XE.brokers || [];
  var XE_ORDERS = XE.orders || [];

  var XSTATE_COLOR = {
    filled: "var(--up)", partially_filled: "var(--up)",
    working: "var(--ink)", acknowledged: "var(--ink)", submitted: "var(--ink)",
    approved: "var(--muted)", validating: "var(--muted)", created: "var(--muted)",
    submitting: "var(--muted)",
    rejected: "var(--accent-dark)", failed: "var(--accent-dark)",
    cancelled: "var(--muted)", expired: "var(--muted)",
    unknown: "#b07000", reconciliation_required: "#b07000"
  };

  var BT = D.backtests || { available: false };
  var BT_RUNS = BT.runs || [];

  function btSelected() {
    if (!BT_RUNS.length) return null;
    var id = state.param;
    if (id && BT.detail && BT.detail[id]) return { id: id, d: BT.detail[id] };
    var first = BT_RUNS[0].run_id;
    return BT.detail && BT.detail[first] ? { id: first, d: BT.detail[first] } : null;
  }

  var BT_STATE_COLOR = {
    completed: "var(--up)", completed_with_warnings: "var(--accent-dark)",
    failed: "var(--accent-dark)", cancelled: "var(--faint)", running: "var(--muted)"
  };

  function btQualityBand(score) {
    if (score === null || score === undefined) return "neevaluat";
    if (score >= 0.75) return "puternic";
    if (score >= 0.5) return "moderat";
    if (score >= 0.25) return "slab";
    return "foarte slab";
  }

  function btEmptyState() {
    if (!BT.available) {
      return blk("Backtesting", "Faza 12",
        '<div class="callout"><b>Tabelele Fazei 12 nu exista in aceasta baza de date.</b>' +
        '<p>Motorul de backtesting este implementat, dar schema nu a fost creata aici. ' +
        'Ruleaza <span class="mono">python scripts/run_backtest.py</span> pentru a o crea ' +
        'si a inregistra prima rulare.</p></div>');
    }
    return blk("Backtesting", "nicio rulare inregistrata",
      '<div class="callout"><b>Niciun backtest nu a fost rulat inca.</b>' +
      '<p>Aceasta nu este o eroare si nu se afiseaza rezultate simulate in loc. ' +
      'Motorul reia intregul lant de decizie istoric — semnal, risc, alocare, ' +
      'executie simulata — dar nu inventeaza nimic.</p>' +
      '<div class="mono" style="margin-top:8px;font-size:11px;background:var(--panel);padding:10px;">' +
      'python scripts/run_backtest.py --name prima-rulare</div>' +
      '<p style="margin-top:10px;"><b>Atentie:</b> toate semnalele stocate in aceasta baza ' +
      'sunt <i>suprimate</i> (incredere scazuta, predictii invechite, esantion mic), deci o ' +
      'rulare reala produce zero tranzactii si raporteaza exact asta. Foloseste ' +
      '<span class="mono">--synthetic-signals</span> pentru a testa mecanismul — dar acele ' +
      'rulari nu spun nimic despre vreo strategie.</p></div>');
  }

  function viewBacktests() {
    var sel = btSelected();
    if (!sel) return pageHead("Backtesting", "Backtesting") + btEmptyState();

    var d = sel.d, m = d.metrics || {}, un = d.unavailable || {};
    var run = null;
    for (var i = 0; i < BT_RUNS.length; i++) {
      if (BT_RUNS[i].run_id === sel.id) { run = BT_RUNS[i]; break; }
    }
    run = run || BT_RUNS[0];

    var html = pageHead("Backtesting - simulare istorica", run.backtest_id || sel.id, [
      ["Verdict", (run.status || "").toUpperCase().replace(/_/g, " ")],
      ["Tranzactii", String(m.total_trades === undefined ? 0 : Math.round(m.total_trades))]
    ]);

    if (BT_RUNS.length > 1) {
      html += '<div class="filterbar"><div class="segbtns">' + BT_RUNS.slice(0, 8).map(function (r) {
        return '<button class="' + (r.run_id === sel.id ? "on" : "") +
          '" onclick="MLGo(\'backtests\',\'' + esc(r.run_id) + '\')">' +
          esc((r.run_id || "").slice(4, 12)) + '</button>';
      }).join("") + '</div></div>';
    }

    // --- status banner ---
    var color = BT_STATE_COLOR[run.status] || "var(--muted)";
    html += '<section class="blk"><div class="blk-body">' +
      '<div style="border:2px solid ' + color + ';padding:14px 16px;">' +
      '<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:' + color + ';">' +
      esc((run.status || "").replace(/_/g, " ")) + '</div>' +
      '<div style="font-size:14px;margin-top:6px;">' +
      esc(fmtDate(run.period ? run.period[0] : "")) + ' &rarr; ' +
      esc(fmtDate(run.period ? run.period[1] : "")) + ' &middot; ' +
      esc(run.observations || 0) + ' observatii &middot; ' +
      esc(d.orders || 0) + ' ordine</div>' +
      '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:8px;">' +
      esc(sel.id) + ' &middot; amprenta ' + esc(run.fingerprint || "") + '</div>' +
      '</div></div></section>';

    // --- headline metrics ---
    function cell(label, value, note) {
      return '<div class="cell"><div class="n">' + value + '</div><div class="l">' +
        esc(label) + (note ? '<br><span style="color:var(--faint);">' + esc(note) + '</span>' : "") +
        '</div></div>';
    }
    function num(v, digits) {
      return (v === null || v === undefined) ? "—" : Number(v).toFixed(digits === undefined ? 2 : digits);
    }
    html += '<section class="blk"><div class="blk-head"><h2>Performanta</h2>' +
      '<span class="blk-note">masurata pe seria simulata, nu o prognoza</span></div>' +
      '<div class="statgrid" style="grid-template-columns:repeat(5,1fr);">' +
      cell("randament total", fmtPct(m.total_return, 2)) +
      cell("CAGR", fmtPct(m.cagr, 2)) +
      cell("volatilitate", fmtPct(m.volatility, 1), un.volatility || "") +
      cell("Sharpe", num(m.sharpe), un.sharpe || "") +
      cell("drawdown maxim", fmtPct(m.max_drawdown, 2), un.max_drawdown || "") +
      '</div><div class="statgrid" style="grid-template-columns:repeat(5,1fr);">' +
      cell("rata de castig", fmtPct(m.win_rate, 1)) +
      cell("profit factor", num(m.profit_factor), un.profit_factor || "") +
      cell("rulaj", num(m.turnover) + "x", "anualizat " + num(m.annualized_turnover) + "x") +
      cell("costuri", num(m.total_costs), "comision") +
      cell("slippage", num(m.total_slippage), "cost de executie") +
      '</div></section>';

    // --- benchmark ---
    if (m.benchmark_return !== null && m.benchmark_return !== undefined) {
      html += '<section class="blk"><div class="blk-head"><h2>Fata de benchmark</h2></div>' +
        '<div class="statgrid" style="grid-template-columns:repeat(3,1fr);">' +
        cell("strategie", fmtPct(m.total_return, 2)) +
        cell("benchmark", fmtPct(m.benchmark_return, 2)) +
        cell("excedent", fmtPct(m.excess_return, 2)) +
      '</div></section>';
    }

    // --- equity curve ---
    var eq = d.equity || [];
    if (eq.length > 1) {
      html += blk("Curba de capital", eq.length + " observatii",
        btEquityChart(eq) +
        '<div class="copy" style="margin-top:10px;"><p>Linia continua este capitalul ' +
        'simulat; cea punctata, benchmark-ul reindexat la acelasi capital initial. ' +
        'Zona de sub linia zero este drawdown-ul.</p></div>');
    }

    // --- research quality ---
    var q = d.quality || {};
    if (q.score !== null && q.score !== undefined) {
      var factors = q.factors || {};
      html += '<section class="blk"><div class="blk-head"><h2>Calitatea cercetarii</h2>' +
        '<span class="blk-note warn">NU masoara profitabilitatea</span></div>' +
        '<div class="blk-body"><div style="display:flex;align-items:baseline;gap:14px;margin-bottom:12px;">' +
        '<span style="font-size:32px;font-weight:800;letter-spacing:-0.02em;">' +
        num(q.score, 3) + '</span>' +
        '<span style="font-size:13px;font-weight:700;">' + esc(btQualityBand(q.score)) + '</span>' +
        '</div>' +
        '<div class="copy"><p><b>Ce inseamna:</b> cat de mult se poate avea incredere in ' +
        'aceste cifre — marimea esantionului, realismul executiei, integritatea ' +
        'punct-in-timp. O rulare foarte profitabila poate avea un scor mic, si invers. ' +
        'Nu este un scor de profitabilitate.</p></div>' +
        barRows(Object.keys(factors).map(function (k) { return [k, factors[k]]; }),
                function (v) { return num(v, 2); }) +
        ((q.notes || []).length
          ? '<div class="copy" style="margin-top:12px;">' + q.notes.map(function (n) {
              return '<p>- ' + esc(n) + '</p>'; }).join("") + '</div>'
          : "") +
      '</div></section>';
    }

    // --- warnings ---
    var warns = d.warnings || [];
    html += blk("Limitari declarate", warns.length + " avertisment(e)",
      warns.length
        ? warns.map(function (w) {
            return '<div style="border-bottom:1px solid var(--line);padding:8px 0;">' +
              '<span class="mono" style="font-weight:700;color:var(--accent-dark);">' +
              esc(w.code) + '</span><br>' + esc(w.message) +
              (w.detail ? '<br><span style="color:var(--muted);font-size:12px;">' +
                esc(w.detail) + '</span>' : "") + '</div>';
          }).join("")
        : '<div class="empty">Nicio limitare semnalata.</div>', "warn");

    // --- unavailable metrics ---
    var unKeys = Object.keys(un);
    if (unKeys.length) {
      html += blk("Ce nu a putut fi masurat", unKeys.length + " metrici",
        '<div class="copy"><p>Absente, nu zero — esantionul nu le sustine.</p></div>' +
        unKeys.map(function (k) {
          return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:12px;">' +
            '<span class="mono"><b>' + esc(k) + '</b></span> ' +
            '<span style="color:var(--muted);">' + esc(un[k]) + '</span></div>';
        }).join(""));
    }

    // --- risk events ---
    var events = d.risk_events || [];
    if (events.length) {
      html += blk("Alocari respinse sau reduse de motorul de risc", events.length + " eveniment(e)",
        '<div class="copy"><p>Pastrate, nu aruncate: arata cat a costat disciplina de risc.</p></div>' +
        '<table class="data"><thead><tr><th>Tip</th><th>Cand</th><th>Motiv</th>' +
        '</tr></thead><tbody>' + events.slice(0, 20).map(function (e) {
          return '<tr><td><span class="pill ' +
            (e.kind === "rejected" ? "warn-outline" : "ghost") + '">' + esc(e.kind) +
            '</span></td><td class="mono">' + esc(fmtDate(e.at)) + '</td><td>' +
            esc(e.reason) + '</td></tr>';
        }).join("") + '</tbody></table>');
    }

    // --- trades ---
    var trades = d.trades || [];
    if (trades.length) {
      html += blk("Registrul de tranzactii", trades.length + " afisate",
        '<table class="data"><thead><tr><th>Instrument</th><th>Directie</th>' +
        '<th class="r">Cantitate</th><th class="r">Intrare</th><th class="r">Iesire</th>' +
        '<th class="r">P&amp;L net</th><th class="r">Zile</th><th>Motiv iesire</th>' +
        '</tr></thead><tbody>' + trades.map(function (t) {
          return '<tr><td class="mono">' + esc(t.instrument_id) + '</td>' +
            '<td>' + esc(t.side) + '</td>' +
            '<td class="r">' + num(t.quantity, 4) + '</td>' +
            '<td class="r">' + num(t.entry_price) + '</td>' +
            '<td class="r">' + num(t.exit_price) + '</td>' +
            '<td class="r" style="color:' + ((t.net_pnl || 0) >= 0 ? "var(--up)" : "var(--down)") +
            '">' + num(t.net_pnl) + '</td>' +
            '<td class="r">' + num(t.holding_days, 1) + '</td>' +
            '<td style="font-size:11px;color:var(--muted);">' + esc(t.exit_reason) + '</td></tr>';
        }).join("") + '</tbody></table>');
    }

    // --- attribution ---
    var attr = d.attribution || [];
    if (attr.length) {
      var dims = {};
      attr.forEach(function (a) { (dims[a.dimension] = dims[a.dimension] || []).push(a); });
      var blocks = "";
      Object.keys(dims).forEach(function (dim) {
        var pairs = dims[dim].slice(0, 8).map(function (a) {
          return [a.label || a.key, a.net_pnl];
        });
        blocks += '<div><div class="blk-head"><h2>' + esc(dim) + '</h2>' +
          '<span class="blk-note">' + dims[dim].length + '</span></div>' +
          '<div class="blk-body">' + barRows(pairs, function (v) { return num(v); }) +
          '</div></div>';
      });
      html += '<section class="blk"><div class="grid2">' + blocks + '</div></section>';
      html += blk("Despre atribuire", "descriere, nu cauzalitate",
        '<div class="copy"><p>Aceste grupari arata <b>unde s-a acumulat</b> P&amp;L-ul. ' +
        'Nu demonstreaza ca acel tip de eveniment sau acel sector <i>a cauzat</i> ' +
        'rezultatul — pozitiile pot fi corelate si pot fi purtate de o miscare generala ' +
        'de piata careia eticheta doar i s-a suprapus.</p></div>');
    }

    // --- configuration and reproducibility ---
    var cfg = d.config || {};
    var exec = cfg.execution || {}, costs = cfg.costs || {}, slip = cfg.slippage || {};
    html += blk("Configuratie si reproductibilitate", "tot ce determina rezultatul",
      '<table class="data"><tbody>' +
      [["capital initial", num(cfg.initial_capital)],
       ["universe", String((cfg.universe || []).length) + " instrumente"],
       ["benchmark", esc(cfg.benchmark_instrument_id || "—")],
       ["moment executie", esc(exec.timing || "—")],
       ["latenta semnal&rarr;ordin", num(exec.signal_to_order_seconds, 0) + "s"],
       ["plafon participare", exec.max_participation === null || exec.max_participation === undefined
          ? "fara" : fmtPct(exec.max_participation, 0)],
       ["model cost", esc(costs.version || "—") + " &middot; " + num(costs.commission_bps, 2) + " bps"],
       ["model slippage", esc(slip.version || "—") + " &middot; " + esc(slip.method || "—") +
          " &middot; " + num(slip.base_bps, 2) + " bps"],
       ["set de limite risc", esc(d.constraints || "—")],
       ["reechilibrare", String(cfg.rebalance_days || "—") + " zile"],
       ["rata fara risc", num(cfg.risk_free_rate, 4) + " (" + esc(cfg.risk_free_source || "") + ")"],
       ["versiune cod", esc(d.code_version || "—")],
       ["amprenta configuratie", '<span class="mono">' + esc(run.fingerprint || "") + '</span>']
      ].map(function (row) {
        return '<tr><td style="width:34%;color:var(--muted);">' + row[0] + '</td>' +
          '<td class="mono">' + row[1] + '</td></tr>';
      }).join("") + '</tbody></table>' +
      '<div class="copy" style="margin-top:12px;"><p>Aceleasi intrari, aceleasi versiuni ' +
      'si aceeasi amprenta produc acelasi rezultat. O rulare a carei configuratie difera ' +
      'primeste un identificator diferit, deci nu poate fi confundata cu o reluare.</p></div>');

    html += blk("Ce nu este un backtest", "citire onesta",
      '<div class="copy"><p>Un backtest <b>nu este o dovada de profitabilitate viitoare</b>. ' +
      'Este o reconstructie a ceea ce ar fi produs aceste reguli pe aceste date, sub ' +
      'aceste ipoteze de executie si cost. Perioada este scurta, esantionul este mic si ' +
      'preturile sunt ajustate retroactiv.</p>' +
      '<p>Nu exista integrare cu broker si nicio capacitate de executie in aceasta faza. ' +
      'Singurul executor implementat scrie tranzactii simulate pe lumanari deja stocate.</p></div>');

    return html;
  }

  function btEquityChart(points) {
    var width = 900, height = 200, pad = 4;
    var eqs = points.map(function (p) { return p.e; }).filter(function (v) {
      return v !== null && v !== undefined; });
    if (eqs.length < 2) return '<div class="empty">Prea putine observatii.</div>';

    var base = eqs[0];
    var bench = [], bench0 = null;
    points.forEach(function (p) {
      if (p.b !== null && p.b !== undefined) {
        if (bench0 === null) bench0 = p.b;
        bench.push(base * (p.b / bench0));
      } else { bench.push(null); }
    });

    var all = eqs.concat(bench.filter(function (v) { return v !== null; }));
    var min = Math.min.apply(null, all), max = Math.max.apply(null, all);
    var range = (max - min) || 1;

    function path(series) {
      var out = [], started = false;
      for (var i = 0; i < series.length; i++) {
        var v = series[i];
        if (v === null || v === undefined) continue;
        var x = pad + (i / (series.length - 1)) * (width - 2 * pad);
        var y = height - pad - ((v - min) / range) * (height - 2 * pad);
        out.push((started ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1));
        started = true;
      }
      return out.join(" ");
    }

    return '<div style="overflow-x:auto;"><svg viewBox="0 0 ' + width + ' ' + height +
      '" width="100%" height="' + height + '" preserveAspectRatio="none" ' +
      'style="display:block;border:1px solid var(--line);background:var(--bg2);">' +
      '<path d="' + path(bench) + '" fill="none" stroke="var(--border-mid)" ' +
      'stroke-width="1.5" stroke-dasharray="4 3"></path>' +
      '<path d="' + path(eqs) + '" fill="none" stroke="var(--ink)" stroke-width="2"></path>' +
      '</svg></div>';
  }

  // ---------------------------------------------------------------
  // navigation state
  // ---------------------------------------------------------------
  var STUB_META = {
    watchlist: { kicker: "Sectiune", title: "Watchlist", stat: [String(D.meta.watchlist_count), "companii urmarite"],
      body: D.meta.watchlist_count
        ? "Companiile urmarite (" + D.meta.watchlist_count + ") sunt pinuite pe pagina Rezumat de mai sus fiecare data cand pipeline-ul zilnic ruleaza cu un fisier watchlist.txt prezent. O vedere dedicata (alerte, comparatie side-by-side) este un punct de extindere viitor, nu o functie neimplementata azi."
        : "Nicio companie urmarita momentan. Adauga nume de companii (unul pe linie) in watchlist.txt la radacina repo-ului, iar urmatoarea rulare zilnica le va pinui pe pagina Rezumat." },
    research: { kicker: "Sectiune", title: "Cercetare", stat: [fmtNum(D.research.available ? D.research.total : 0), "observatii"],
      body: D.research.available
        ? "Setul de cercetare are " + fmtNum(D.research.total) + " observatii (Faza 7) si " + fmtNum(D.research.feature_count) + " caracteristici in registru (Faza 8) — vezi sectiunea Modele pentru acoperirea lor pe nume. O pagina dedicata de explorare (filtrare pe fereastra, export, comparatie intre versiuni de dataset) este un punct de extindere viitor."
        : "Faza 7 (setul de date de cercetare) nu a rulat inca pe aceasta baza de date." },
    features: { kicker: "Sectiune", title: "Caracteristici", stat: [fmtNum(D.research.available ? D.research.feature_count : 0), "in registru"],
      body: "Acoperirea caracteristicilor per nume calificat este afisata in sectiunea Modele. O bibliotecă dedicată (definiție, formulă, distribuție per caracteristică) este un punct de extindere viitor." }
  };

  var NAV = [
    { label: "Prezentare", items: [
      { id: "overview", label: "Rezumat", tag: "" }
    ]},
    { label: "Piata", items: [
      { id: "markets", label: "Piete", tag: String(D.meta.total_companies) },
      { id: "sectors", label: "Sectoare", tag: String(D.meta.total_sectors) },
      { id: "watchlist", label: "Watchlist", tag: D.meta.watchlist_count ? String(D.meta.watchlist_count) : "0", stub: true }
    ]},
    { label: "Inteligenta", items: [
      { id: "news", label: "Stiri", tag: fmtNum(D.health.total_articles) },
      { id: "events", label: "Evenimente", tag: D.events.available ? fmtNum(D.events.total) : "0" },
      { id: "signals", label: "Semnale (model)", tag: D.signals.available ? fmtNum(D.signals.total) : "0" }
    ]},
    { label: "Portofoliu", items: [
      { id: "portfolio", label: "Portofoliu", tag: PF_COUNT ? String(PF_COUNT) : "0" },
      { id: "risk", label: "Risc", tag: RISK_TAG },
      { id: "backtests", label: "Backtesting", tag: BT_RUNS.length ? String(BT_RUNS.length) : "0" },
      { id: "paper", label: "Paper trading", tag: PP_SESSIONS.length ? String(PP_SESSIONS.length) : "0" },
      { id: "execution", label: "Executie", tag: XE_BROKERS.length ? String(XE_BROKERS.length) : "0" }
    ]},
    { label: "Performanta", items: [
      { id: "outcomes", label: "Recomandari (sentiment)", tag: D.legacy.available ? fmtNum(D.legacy.checked) : "0" },
      { id: "models", label: "Modele", tag: D.models.available ? String(D.models.models.length) : "0" },
      { id: "outcomes", label: "Rezultate reale", tag: D.outcomes.available ? fmtNum(D.outcomes.total) : "0" },
      { id: "research", label: "Cercetare", tag: D.research.available ? fmtNum(D.research.total) : "0", stub: true }
    ]}
  ];

  var state = { view: "overview", param: null, mktFilter: "all", mktSector: "", mktQuery: "", sigIdx: 0, searchOpen: false };

  function parseHash() {
    var h = location.hash.replace(/^#\/?/, "");
    var parts = h.split("/").filter(Boolean).map(decodeURIComponent);
    state.view = parts[0] || "overview";
    state.param = parts[1] || null;
  }

  window.MLGo = function (view, param) {
    location.hash = "#/" + view + (param ? "/" + encodeURIComponent(param) : "");
  };
  window.MLSetMktFilter = function (f) { state.mktFilter = f; render(); };
  window.MLSetMktSector = function (s) { state.mktSector = s; render(); };
  window.MLSetMktQuery = function (v) { state.mktQuery = v; render(); };
  window.MLSetSigIdx = function (i) { state.sigIdx = i; render(); };

  window.MLSearchOpen = function (open) {
    state.searchOpen = open;
    document.getElementById("ml-search-results").classList.toggle("hidden", !open);
  };
  window.MLSearchInput = function (q) {
    var box = document.getElementById("ml-search-results");
    q = (q || "").trim().toLowerCase();
    if (!q) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    var companies = D.universe.filter(function (i) {
      return i.n.toLowerCase().indexOf(q) !== -1 || i.t.toLowerCase().indexOf(q) !== -1;
    }).slice(0, 8);
    var sectors = D.sector_summary.filter(function (s) { return s.name.toLowerCase().indexOf(q) !== -1; }).slice(0, 5);
    var eventTypes = Object.keys(D.lexicon).filter(function (k) { return k.toLowerCase().indexOf(q) !== -1; }).slice(0, 5);
    var groups = [];
    if (companies.length) groups.push(["Companii / instrumente", companies.map(function (c) {
      return { key: c.t, name: c.n, meta: c.s || "nemapat", go: "MLGo('company','" + c.t + "')" };
    })]);
    if (sectors.length) groups.push(["Sectoare", sectors.map(function (s) {
      return { key: "—", name: s.name, meta: s.company_count + " companii", go: "MLGo('sectors')" };
    })]);
    if (eventTypes.length) groups.push(["Tipuri de eveniment", eventTypes.map(function (k) {
      return { key: "—", name: k, meta: D.lexicon[k].fired ? "are evenimente" : "fara evenimente inca", go: "MLGo('events','" + k + "')" };
    })]);
    if (!groups.length) {
      box.innerHTML = '<div class="sr-item">Niciun rezultat pentru “' + esc(q) + '”</div>';
    } else {
      box.innerHTML = groups.map(function (g) {
        return '<div class="sr-group-head"><span>' + esc(g[0]) + '</span><span>' + g[1].length + '</span></div>' +
          g[1].map(function (it) {
            return '<div class="sr-item" onclick="' + it.go + '"><span class="k mono">' + esc(it.key) + '</span><span class="n">' + esc(it.name) + '</span><span class="m">' + esc(it.meta) + '</span></div>';
          }).join("");
      }).join("");
    }
    box.classList.remove("hidden");
  };
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".search-wrap")) MLSearchOpen(false);
  });

  // ---------------------------------------------------------------
  // view renderers
  // ---------------------------------------------------------------

  function viewOverview() {
    var html = pageHead("Panou de control · pipeline zilnic", "Ce a produs sistemul", [
      ["Baza de date", D.meta.db_size_mb ? D.meta.db_size_mb + " MB" : "—"],
      ["Cel mai recent articol", fmtDate(D.health.latest_article)]
    ]);

    if (D.meta.daily_summary) {
      html += '<section class="blk"><div class="blk-body"><p class="copy wide" style="font-style:italic;">' + esc(D.meta.daily_summary) + '</p></div></section>';
    }

    var flowHtml = '<div class="flowstrip">' + D.flow.map(function (s) {
      var max = Math.max.apply(null, D.flow.map(function (x) { return x.count || 0; })) || 1;
      var w = s.count ? (100 * s.count / max).toFixed(1) : 0;
      return '<div class="flow-cell"><div class="lbl">' + esc(s.label) + '</div>' +
        '<div class="n">' + (s.count === null ? "n/a" : fmtNum(s.count)) + '</div>' +
        '<div class="u">' + esc(s.unit) + '</div>' +
        '<div class="bartrack"><div class="barfill" style="width:' + w + '%"></div></div></div>';
    }).join("") + '</div>';
    html += blk("Pipeline · fazele 1-10", "volume reale din baza de date", flowHtml);

    var healthLeft = '<div class="statgrid" style="grid-template-columns:repeat(4,1fr);">' +
      ['<div class="cell"><div class="n">' + fmtNum(D.health.total_articles) + '</div><div class="l">articole totale</div></div>',
       '<div class="cell"><div class="n">' + fmtPct(D.health.entity_coverage) + '</div><div class="l">acoperire entitati</div></div>',
       '<div class="cell"><div class="n">' + fmtNum(D.health.sources) + '</div><div class="l">surse active</div></div>',
       '<div class="cell"><div class="n">' + fmtNum(D.meta.total_companies) + '</div><div class="l">companii in registru</div></div>'].join("") + '</div>';
    var marketRight = D.market_data
      ? '<p class="copy">Preturi live disponibile pentru ' + Object.keys(D.market_data).length + ' instrumente urmarite in aceasta rulare.</p>' +
        Object.keys(D.market_data).slice(0, 6).map(function (t) {
          var s = D.market_data[t]; if (!s || s.error) return "";
          var chg = s.daily_change_pct; var up = (chg || 0) >= 0;
          return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line);font-size:12px;"><span class="mono" style="font-weight:700;">' + esc(t) + '</span><span>' + fmtNum(s.current_price, 2) + '</span><span style="color:' + (up ? "#00795a" : "#ae1800") + ';font-weight:700;">' + fmtSignedPct(chg / 100, 2) + '</span></div>';
        }).join("")
      : '<div class="hatched" style="text-align:center;"><span class="mono" style="font-size:11px;color:var(--muted);">price_candle_cache — indisponibil</span></div>' +
        '<p class="copy" style="margin-top:12px;">Preturile live nu sunt persistate in baza de date — se calculeaza doar in timpul rularii zilnice. Aceasta sectiune se populeaza cand run_daily.py transmite instantaneul de piata generatorului.</p>';
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Sanatatea datelor</h2></div><div class="blk-body">' + healthLeft + '</div></div>' +
      '<div><div class="blk-head"><h2>Date de piata</h2></div><div class="blk-body">' + marketRight + '</div></div>' +
      '</div></section>';

    if (D.events.available) {
      var singleSrc = 0, multiSrc = 0;
      D.events.corroboration.forEach(function (p) { if (p[0] === "single_source") singleSrc = p[1]; else multiSrc += p[1]; });
      var totalCorrob = singleSrc + multiSrc || 1;
      var corrobPct = (100 * singleSrc / totalCorrob).toFixed(1);
      var evLeft = barRows(D.events.by_type.slice(0, 8));
      var evRight = '<div style="display:flex;height:28px;border:2px solid var(--line-strong);">' +
        '<div style="width:' + corrobPct + '%;background:var(--ink);"></div><div style="width:' + (100 - corrobPct) + '%;background:var(--accent);"></div></div>' +
        '<div style="display:flex;justify-content:space-between;margin-top:8px;font-size:12px;"><span><b>' + fmtNum(singleSrc) + '</b> single_source</span><span><b>' + fmtNum(multiSrc) + '</b> multi_source</span></div>' +
        '<p class="copy" style="margin-top:14px;">' + (singleSrc / totalCorrob > 0.5
          ? "Majoritatea evenimentelor canonice se sprijina pe o singura sursa independenta. Pana cand numarul de surse creste, increderea in evenimente trebuie citita ca provizorie."
          : "Majoritatea evenimentelor canonice sunt confirmate de mai multe surse independente.") + '</p>';
      html += '<section class="blk"><div class="grid2">' +
        '<div><div class="blk-head"><h2>Evenimente pe tip</h2><span class="blk-note">' + fmtNum(D.events.total) + ' evenimente canonice</span></div><div class="blk-body">' + evLeft + '</div></div>' +
        '<div><div class="blk-head"><h2>Corroborare</h2><span class="blk-note warn">' + corrobPct + '% o singura sursa</span></div><div class="blk-body">' + evRight + '</div></div>' +
      '</div></section>';
    } else {
      html += blk("Evenimente si fuziune", null, '<div class="empty">Fazele 4-5 nu au rulat inca pe aceasta baza de date.</div>');
    }

    if (D.impact.available && D.impact.by_window.length) {
      var rows = D.impact.by_window.map(function (w) {
        var color = (w[2] || 0) < 0 ? "#ae1800" : "#00795a";
        return '<tr><td class="mono" style="font-weight:700;">' + esc(w[0]) + '</td><td class="r">' + fmtNum(w[1]) + '</td><td class="r" style="font-weight:700;color:' + color + ';">' + fmtSignedPct(w[2]) + '</td></tr>';
      }).join("");
      html += blk("Randament anormal mediu pe fereastra", fmtNum(D.impact.total) + " studii de impact · benchmark-ajustat",
        '<table class="data"><thead><tr><th>Fereastra</th><th class="r">N</th><th class="r">Randament</th></tr></thead><tbody>' + rows + '</tbody></table>');
    } else {
      html += blk("Impact de piata", null, '<div class="empty">Faza 6 nu a rulat inca pe aceasta baza de date.</div>');
    }

    if (D.signals.available) {
      var sigRows = D.signals.recent.slice(0, 8).map(function (s) {
        return '<tr><td style="font-weight:700;">' + signalLabel(s) + (signalIsExperimental(s) ? ' ' + modelStatusPill(s[10], 9) : '') + '</td><td><span class="pill outline-up">' + esc(s[2]) + '</span></td><td class="r">' + fmtNum(s[4], 2) + '</td><td class="r" style="color:var(--accent-dark);font-weight:700;">' + fmtNum(s[5], 2) + '</td><td class="r">' + fmtSignedPct(s[6]) + '</td></tr>';
      }).join("");
      var activeCount = 0; D.signals.by_status.forEach(function (p) { if (p[0] === "active") activeCount = p[1]; });
      html += '<section class="blk"><div class="grid32">' +
        '<div><div class="blk-head"><h2>Semnale recente</h2><span class="blk-note warn">' + activeCount + ' / ' + D.signals.total + ' active</span></div>' +
        '<table class="data"><thead><tr><th>Instrument</th><th>Directie</th><th class="r" title="Marimea miscarii asteptate raportata la scara strategiei. Nu este probabilitate.">Forta</th><th class="r" title="Scor euristic, nu probabilitate. scor = increderea modelului x calitatea datelor x acordul intre modele x esantion. Nu este calibrat fata de rezultate: 0,30 nu inseamna 30% sanse.">Scor incredere</th><th class="r">R. asteptat</th></tr></thead><tbody>' + (sigRows || '<tr><td colspan="5" class="empty">Niciun semnal</td></tr>') + '</tbody></table></div>' +
        '<div><div class="blk-head"><h2>Suprimari</h2></div><div class="blk-body">' + barRows(D.signals.suppression) + '</div></div>' +
      '</div></section>';
    } else {
      html += blk("Semnale", null, '<div class="empty">Faza 10 nu a rulat inca pe aceasta baza de date.</div>');
    }

    if (D.legacy.available) {
      var accColor = (D.legacy.accuracy || 0) >= 0.5 ? "var(--ink)" : "var(--accent-dark)";
      var legLeft = '<div class="statgrid" style="grid-template-columns:repeat(3,1fr);">' +
        '<div class="cell"><div class="n">' + fmtNum(D.legacy.total_recs) + '</div><div class="l">recomandari emise</div></div>' +
        '<div class="cell"><div class="n" style="color:' + accColor + ';">' + fmtPct(D.legacy.accuracy) + '</div><div class="l">acuratete bruta (' + fmtNum(D.legacy.checked) + ' verificate)</div></div>' +
        '<div class="cell"><div class="n">' + fmtNum(D.legacy.verified_count) + '</div><div class="l">un apel per companie</div></div></div>';
      var mixRows = D.legacy.by_rec.map(function (p) { return [p[0], p[1]]; });
      html += '<section class="blk"><div class="grid2">' +
        '<div><div class="blk-head"><h2>Istoric recomandari</h2></div><div class="blk-body">' + legLeft + '</div></div>' +
        '<div><div class="blk-head"><h2>Distributia recomandarilor</h2></div><div class="blk-body">' + barRows(mixRows) + '</div></div>' +
      '</div></section>';
    }

    html += blk("Sectoare active", D.legacy.available ? "companii cu recomandari, pe sector" : null,
      D.legacy.available ? barRows(D.legacy.sector_breakdown) : '<div class="empty">Necesita istoric de recomandari.</div>');

    return html;
  }

  function callFor(entity) {
    var r = D.current_recs[entity] || D.rec_index[entity];
    return r ? r.recommendation : null;
  }

  function viewMarkets() {
    var counts = { all: D.universe.length, stocks: 0, bvb: 0, crypto: 0, unmapped: 0 };
    D.universe.forEach(function (i) { counts[i.c] = (counts[i.c] || 0) + 1; if (!i.s) counts.unmapped++; });
    var CAT_LABEL = { stocks: "actiuni SUA", bvb: "BVB", crypto: "crypto" };

    var rows = D.universe.filter(function (i) {
      if (state.mktFilter === "unmapped" && i.s) return false;
      if (state.mktFilter !== "all" && state.mktFilter !== "unmapped" && i.c !== state.mktFilter) return false;
      if (state.mktSector && i.s !== state.mktSector) return false;
      if (state.mktQuery) {
        var q = state.mktQuery.toLowerCase();
        if (i.n.toLowerCase().indexOf(q) === -1 && i.t.toLowerCase().indexOf(q) === -1) return false;
      }
      return true;
    });

    var html = pageHead("Piata · registrul companiilor", "Piete", [
      ["Instrumente", String(D.universe.length)], ["Companii", String(D.universe.length)]
    ]);

    html += '<div class="filterbar"><div class="segbtns">' +
      ["all", "stocks", "bvb", "crypto", "unmapped"].map(function (f) {
        return '<button class="' + (state.mktFilter === f ? "active" : "") + '" onclick="MLSetMktFilter(\'' + f + '\')">' + (f === "all" ? "toate" : (f === "unmapped" ? "nemapate" : CAT_LABEL[f])) + ' · ' + counts[f] + '</button>';
      }).join("") + '</div>' +
      '<select class="field" onchange="MLSetMktSector(this.value)"><option value="">toate sectoarele</option>' +
      D.sector_summary.map(function (s) { return '<option value="' + esc(s.name) + '"' + (state.mktSector === s.name ? " selected" : "") + '>' + esc(s.name) + ' · ' + s.company_count + '</option>'; }).join("") +
      '</select><input class="field" type="text" placeholder="filtreaza dupa nume sau ticker" value="' + esc(state.mktQuery) + '" oninput="MLSetMktQuery(this.value)"><div class="grow"></div>' +
      '<span style="font-size:11px;font-weight:700;">se afiseaza ' + rows.length + ' / ' + D.universe.length + '</span></div>';

    var trs = rows.slice(0, 400).map(function (i) {
      var call = callFor(i.n);
      var callHtml = call ? '<span class="pill solid">' + esc(call) + '</span>' : '<span style="color:var(--border-mid);">—</span>';
      var mkt = D.market_data && D.market_data[i.t];
      var priceHtml = (mkt && !mkt.error) ? fmtNum(mkt.current_price, 2) : '<span class="mono" style="color:var(--faint);">n/a</span>';
      return '<tr class="rowlink" onclick="MLGo(\'company\',\'' + esc(i.t) + '\')">' +
        '<td class="mono" style="font-weight:700;">' + esc(i.t) + '</td><td>' + esc(i.n) + '</td>' +
        '<td style="font-size:11px;color:var(--muted);">' + esc(CAT_LABEL[i.c] || i.c) + '</td>' +
        '<td style="font-size:11px;color:' + (i.s ? "var(--muted)" : "var(--accent-dark)") + ';">' + esc(i.s || "nemapat") + '</td>' +
        '<td>' + callHtml + '</td><td class="r">' + priceHtml + '</td>' +
        '</tr>';
    }).join("");

    html += '<table class="data"><thead><tr><th>Instrument</th><th>Companie</th><th>Clasa</th><th>Sector</th><th>Apel</th><th class="r">Pret</th></tr></thead>' +
      '<tbody>' + (trs || '<tr><td colspan="6" class="empty">Niciun rezultat pentru filtrele curente.</td></tr>') + '</tbody></table>';

    html += '<div class="blk-body" style="border-top:2px solid var(--line-strong);"><p class="copy wide">Coloana "Apel" arata ultima recomandare emisa pentru companie, din tabela recommendations — nu doar cele mai recente ' + D.legacy.total_recs + ' randuri, ci istoricul complet per companie. "—" inseamna ca nicio recomandare nu a fost emisa inca pentru acea companie.</p></div>';
    return html;
  }

  function viewCompany(ticker) {
    var sel = D.universe.find(function (i) { return i.t === ticker; }) || D.universe[0];
    if (!sel) return pageHead("Piata", "Companie negasita", null);
    var CAT_LABEL = { stocks: "actiuni SUA", bvb: "BVB", crypto: "crypto" };
    var rec = D.current_recs[sel.n] || D.rec_index[sel.n];

    var html = '<div class="blk-body" style="border-bottom:1px solid var(--line);padding:14px 24px;"><button class="backbtn" onclick="MLGo(\'markets\')">← inapoi la Piete</button></div>';
    html += pageHead((CAT_LABEL[sel.c] || sel.c) + " · " + sel.t, sel.n, null);

    var mkt = D.market_data && D.market_data[sel.t];
    var hist = D.price_history && D.price_history[sel.n];
    var priceHtml;
    if (mkt && !mkt.error) {
      var chg = mkt.daily_change_pct; var up = (chg || 0) >= 0;
      // `from_cache` marks a last stored close rather than a live
      // quote. Rendering it as "azi" would be a lie the reader cannot
      // detect, so the label states what it is and when.
      var when = mkt.from_cache
        ? 'ultima inchidere stocata' + (mkt.as_of ? ' · ' + esc(String(mkt.as_of).slice(0, 10)) : '')
        : 'azi';
      priceHtml = '<div style="font-size:28px;font-weight:800;">' + fmtNum(mkt.current_price, 2) + '</div>' +
        '<div style="font-size:13px;font-weight:700;color:' + (up ? "#00795a" : "#ae1800") + ';">' +
        (chg === null || chg === undefined ? '—' : fmtSignedPct(chg / 100, 2)) + ' ' + when + '</div>' +
        (hist ? sparkline(hist, 400, 60) : "") +
        (mkt.from_cache
          ? '<p class="copy" style="margin-top:8px;font-size:11px;color:var(--muted);">' +
            'Din <span class="mono">price_candle_cache</span>' +
            (mkt.points ? ' · ' + fmtNum(mkt.points) + ' lumanari zilnice' : '') +
            '. Nu e un pret live.</p>'
          : "");
    } else {
      priceHtml = '<div class="hatched" style="text-align:center;"><span class="mono" style="font-size:11px;color:var(--muted);">price_candle_cache — indisponibil</span></div>' +
        '<p class="copy" style="margin-top:12px;">Nu exista lumanari zilnice stocate pentru acest instrument. ' +
        'Ruleaza <span class="mono">Cache Price Candles</span> ca sa le colectezi.</p>';
    }
    var callHtml = rec
      ? '<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:12px;"><span class="pill solid" style="font-size:11px;padding:3px 7px;">' + esc(rec.recommendation) + '</span><span style="font-size:22px;font-weight:800;">' + fmtPct(rec.confidence_score, 0) + '</span><span style="font-size:11px;color:var(--muted);">incredere</span></div>' +
        '<p class="copy">Orizont: ' + esc(rec.time_horizon || "—") + ' · emisa ' + fmtDate(rec.generated_at) + '.</p>'
      : '<p class="copy">Nicio recomandare emisa inca pentru aceasta companie.</p>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Istoric pret</h2></div><div class="blk-body">' + priceHtml + '</div></div>' +
      '<div><div class="blk-head"><h2>Ultimul apel</h2></div><div class="blk-body">' + callHtml + '</div></div>' +
      '</div></section>';

    var sigs = D.signals.available ? D.signals.recent.filter(function (s) {
      // Match on the TICKER (s[9]), not on the instrument_id with
      // "crypto-" stripped. That older rule matched crypto by accident
      // and missed every US stock: "us_and_intl-aapl" minus "crypto-"
      // is still "us_and_intl-aapl", never "AAPL". Company pages for
      // US equities therefore showed no signals even when they had
      // some. The id remains a fallback for rows that resolve to no
      // instrument row.
      var tick = s[9] ? String(s[9]) : String(s[1]).replace("crypto-", "");
      return tick.toUpperCase() === sel.t.toUpperCase();
    }) : [];
    var sigHtml = sigs.length ? sigs.map(function (s) {
      return '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:10px 24px;border-bottom:1px solid var(--line);">' +
        '<span class="pill outline-up">' + esc(s[2]) + '</span><span>forta <b>' + fmtNum(s[4], 2) + '</b></span><span>r. asteptat <b>' + fmtSignedPct(s[6]) + '</b></span></div>';
    }).join("") : '<div class="blk-body"><p class="copy">Niciun semnal pentru acest instrument inca.</p></div>';

    var path = sel.s ? "source = company" : "source = keyword";
    var pathBody = sel.s
      ? "Numele canonic exista in COMPANY_SECTOR_MAP, deci articolele care mentioneaza aceasta companie se clasifica determinist in sectorul de mai sus."
      : "Numele canonic lipseste din COMPANY_SECTOR_MAP. Articolele care o mentioneaza pot ajunge intr-un sector doar prin calea de rezerva, pe cuvinte-cheie — sau in niciunul.";

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Semnale</h2></div>' + sigHtml + '</div>' +
      '<div><div class="blk-head"><h2>Suprafata de detectie</h2><span class="blk-note">' + sel.a.length + ' alias(uri)</span></div><div class="blk-body">' +
      sel.a.map(function (a) { return '<span class="aliaschip">' + esc(a) + '</span>'; }).join("") +
      '<div style="padding-top:14px;margin-top:14px;border-top:1px solid var(--line);">' +
      '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px;">Cale de clasificare</div>' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;"><span style="width:9px;height:9px;background:' + (sel.s ? "var(--ink)" : "var(--accent)") + ';display:inline-block;"></span><span class="mono" style="font-weight:700;font-size:12px;">' + path + '</span></div>' +
      '<p class="copy">' + pathBody + '</p></div></div></div>' +
      '</div></section>';

    return html;
  }

  function viewSectors() {
    var html = pageHead("Piata · registrul sectoarelor", "Sectoare", [["Definite", String(D.sector_summary.length)]]);

    html += '<section class="blk"><div class="blk-body"><div class="callout"><div class="t">Clasificare pe doua cai</div>' +
      '<p style="margin:0 0 8px 0;font-size:13px;line-height:1.55;">Un sector se determina fie determinist (compania e in COMPANY_SECTOR_MAP), fie prin cuvinte-cheie de rezerva (SECTOR_KEYWORDS), fie deloc.</p>' +
      '<p style="margin:0;font-size:13px;line-height:1.55;color:#444141;">' + D.unmapped.length + ' din ' + D.meta.total_companies + ' companii din registru nu sunt in COMPANY_SECTOR_MAP si cad pe calea de rezerva.</p></div></div></section>';

    var tiles = D.sector_summary.map(function (s) {
      var max = Math.max.apply(null, D.sector_summary.map(function (x) { return x.company_count; })) || 1;
      var w = (100 * s.company_count / max).toFixed(0);
      return '<div class="sector-tile" onclick="MLGo(\'markets-sector\',\'' + esc(s.name) + '\')" data-sector="' + esc(s.name) + '">' +
        '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;"><span style="font-size:14px;font-weight:700;">' + esc(s.name) + '</span><span style="font-size:17px;font-weight:800;">' + s.company_count + '</span></div>' +
        '<div style="height:6px;margin-top:8px;background:var(--line);"><div style="height:6px;width:' + w + '%;background:var(--ink);"></div></div>' +
        '<div style="font-size:10px;color:var(--faint);margin-top:6px;" class="mono">' + s.keyword_count + ' cuvinte-cheie</div></div>';
    }).join("");
    html += blk("Registru sectoare", "src/sector_registry.py", '<div class="sectorgrid">' + tiles + '</div>');

    var unmappedChips = D.unmapped.slice(0, 24).map(function (u) {
      return '<span class="unfired-chip" style="cursor:pointer;" onclick="MLGo(\'company\',\'' + esc(u.t) + '\')">' + esc(u.t) + ' · ' + esc(u.n) + '</span>';
    }).join("");
    html += '<section class="blk"><div class="blk-body"><div class="callout">' +
      '<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:6px;"><span style="font-size:24px;font-weight:800;color:var(--accent-dark);">' + D.unmapped.length + '</span><span style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent-dark);">companii fara sector mapat</span></div>' +
      '<p style="margin:0 0 12px 0;font-size:12px;line-height:1.5;color:#444141;">Sunt in COMPANY_REGISTRY, dar lipsesc din COMPANY_SECTOR_MAP. Pentru ele, clasificarea cade pe calea de rezerva, cu cuvinte-cheie.</p>' +
      '<div>' + unmappedChips + '</div></div></div></section>';

    return html;
  }

  function viewNews() {
    var html = pageHead("Inteligenta · fluxul de stiri", "Stiri", [["Cel mai recent", fmtDate(D.health.latest_article)]]);

    // TD-02b. These counts describe the canonical corpus. It is a
    // superset of the legacy table, which is pruned to 60 days, so the
    // difference is by design and saying so beats letting a reader
    // wonder why two numbers disagree.
    html += '<section class="blk"><div class="blk-body" style="border-left:3px solid var(--line);font-size:11px;color:var(--muted);line-height:1.6;">' +
      '<strong>Corpus: <span class="mono">' + esc(D.health.news_table) + '</span>.</strong> ' +
      fmtNum(D.health.total_articles) + ' articole, din ' + fmtDate(D.health.oldest_article) + ' pana in ' + fmtDate(D.health.latest_article) + '. ' +
      (D.health.news_table === "news_articles" && D.health.legacy_articles
        ? 'Tabela veche <span class="mono">articles</span> are ' + fmtNum(D.health.legacy_articles) + ' randuri: este taiata la 60 de zile de arhivator, ' +
          'in timp ce corpusul canonic pastreaza tot istoricul. Diferenta este intentionata, nu o pierdere de date.'
        : 'Corpusul canonic nu este inca populat pe aceasta baza de date; numerele vin din tabela veche.') +
      '</div></section>';
    html += '<section class="blk"><div class="statgrid" style="grid-template-columns:repeat(4,1fr);">' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(D.health.total_articles) + '</div><div class="l">articole</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(D.health.sources) + '</div><div class="l">surse</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(D.health.linked_articles) + '</div><div class="l">articole cu entitate</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtPct(D.health.entity_coverage) + '</div><div class="l">acoperire entitati</div></div>' +
      '</div></section>';

    var withE = D.health.linked_articles, withoutE = Math.max(0, D.health.total_articles - D.health.linked_articles);
    var totalA = withE + withoutE || 1;
    var covPct = (100 * withE / totalA).toFixed(1);
    var covHtml = '<div style="display:flex;height:28px;border:2px solid var(--line-strong);"><div style="width:' + covPct + '%;background:var(--ink);"></div>' +
      '<div style="width:' + (100 - covPct) + '%;background:repeating-linear-gradient(135deg,var(--panel) 0 6px,var(--bg) 6px 12px);"></div></div>' +
      '<div style="display:flex;justify-content:space-between;margin-top:8px;font-size:12px;"><span><b>' + fmtNum(withE) + '</b> cu entitate</span><span style="color:var(--muted);"><b>' + fmtNum(withoutE) + '</b> fara entitate</span></div>' +
      '<p class="copy" style="margin-top:14px;">Un articol "fara entitate" nu a fost inca legat de nicio companie/instrument cunoscut de Company/Ticker Detector — fie pentru ca nu mentioneaza una, fie pentru ca alias-ul folosit nu e inca in registru.</p>';

    var taxHtml = ['company · ' + D.meta.total_companies + ' in registru', 'instrument · ' + D.meta.total_companies + ' in registru',
      'sector · ' + D.meta.total_sectors + ' in registru', 'event · ' + (D.events.available ? D.events.total : 0)].map(function (t) {
      var parts = t.split(" · ");
      return '<div style="display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:5px 0;font-size:12px;"><span class="mono">' + esc(parts[0]) + '</span><span style="color:var(--faint);">' + esc(parts[1]) + '</span></div>';
    }).join("") + '<p class="copy" style="margin-top:12px;">Taxonomia de entitati folosita de sistem — fiecare tip are propriul registru sau propria tabela.</p>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Acoperire entitati</h2></div><div class="blk-body">' + covHtml + '</div></div>' +
      '<div><div class="blk-head"><h2>Taxonomie</h2></div><div class="blk-body">' + taxHtml + '</div></div>' +
      '</div></section>';

    html += '<section class="blk"><div class="blk-body"><div class="hatched"><div style="font-size:13px;font-weight:800;margin-bottom:6px;">Flux de stiri per articol — punct de extindere</div>' +
      '<div style="font-size:12px;line-height:1.5;color:#444141;max-width:60ch;">Un tabel filtrabil, cautabil, per articol (sursa, companie, sector, tip eveniment, sentiment, impact, incredere) este planificat, dar articolele individuale nu sunt inca exportate in acest instantaneu — doar agregatele de mai sus.</div></div></div></section>';

    return html;
  }

  function viewEvents(highlightType) {
    if (!D.events.available) {
      return pageHead("Inteligenta · evenimente", "Evenimente", null) +
        blk("Fara date", null, '<div class="empty">Fazele 4-5 (detectie si fuziune) nu au rulat inca pe aceasta baza de date.</div>');
    }
    var html = pageHead("Inteligenta · evenimente canonice", "Evenimente", [["Canonice", fmtNum(D.events.total)]]);

    var typeRows = D.events.by_type.map(function (p) {
      var max = Math.max.apply(null, D.events.by_type.map(function (x) { return x[1]; })) || 1;
      var w = (100 * p[1] / max).toFixed(1);
      var active = p[0] === highlightType;
      return '<div class="barrow clickable' + (active ? '" style="background:var(--accent-bg);' : '') + '" onclick="MLGo(\'events\',\'' + esc(p[0]) + '\')"><div class="lbl mono">' + esc(p[0]) + '</div>' +
        '<div class="track"><span class="fill" style="width:' + w + '%"></span></div><div class="val">' + p[1] + '</div></div>';
    }).join("");

    var single = 0, multi = 0;
    D.events.corroboration.forEach(function (p) { if (p[0] === "single_source") single = p[1]; else multi += p[1]; });
    var fusionRight = '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">' +
      '<div style="flex:1;"><div style="height:24px;background:var(--line);"><div style="height:24px;width:100%;background:var(--ink);"></div></div><div style="font-size:11px;margin-top:4px;">' + fmtNum(D.events.total + 0) + ' rapoarte</div></div>' +
      '<span style="font-size:16px;font-weight:800;color:var(--faint);">→</span>' +
      '<div style="flex:1;"><div style="height:24px;background:var(--line);"><div style="height:24px;width:100%;background:var(--accent);"></div></div><div style="font-size:11px;margin-top:4px;">' + fmtNum(D.events.total) + ' canonice</div></div></div>' +
      '<div style="display:flex;gap:24px;padding-top:12px;border-top:1px solid var(--line);"><div><span style="font-size:20px;font-weight:800;">' + fmtNum(single) + '</span> <span style="font-size:12px;color:var(--muted);">single_source</span></div><div><span style="font-size:20px;font-weight:800;">' + fmtNum(multi) + '</span> <span style="font-size:12px;color:var(--muted);">multi_source</span></div></div>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Evenimente pe tip</h2></div><div class="blk-body">' + typeRows + '<div style="margin-top:6px;font-size:11px;color:var(--faint);">Click pe un tip pentru detaliu.</div></div></div>' +
      '<div><div class="blk-head"><h2>Rapoarte → canonice</h2></div><div class="blk-body">' + fusionRight + '</div></div>' +
      '</div></section>';

    if (D.impact.available && D.impact.by_window.length) {
      var wRows = D.impact.by_window.map(function (w) {
        var color = (w[2] || 0) < 0 ? "#ae1800" : "#00795a";
        var reliable = w[1] >= 100;
        return '<tr><td class="mono" style="font-weight:700;">' + esc(w[0]) + '</td><td class="r" style="color:var(--muted);">' + w[1] + '</td><td class="r" style="font-weight:700;color:' + color + ';">' + fmtSignedPct(w[2]) + '</td><td><span class="pill" style="border:1px solid ' + (reliable ? "#00795a" : "#ae1800") + ';color:' + (reliable ? "#00795a" : "#ae1800") + ';">' + (reliable ? "solid" : "subtire") + '</span></td></tr>';
      }).join("");
      html += blk("Studii de impact pe fereastra", fmtNum(D.impact.total) + " studii · benchmark-ajustat",
        '<table class="data"><thead><tr><th>Fereastra</th><th class="r">N</th><th class="r">Randament anormal</th><th>Fiabilitate</th></tr></thead><tbody>' + wRows + '</tbody></table>');
    }

    var firedTypes = {}; D.events.by_type.forEach(function (p) { firedTypes[p[0]] = true; });
    var unfired = Object.keys(D.lexicon).filter(function (k) { return !firedTypes[k]; });
    if (unfired.length) {
      var chips = unfired.map(function (k) { return '<span class="unfired-chip" onclick="MLGo(\'events\',\'' + esc(k) + '\')" style="cursor:pointer;">' + esc(k) + ' · ' + D.lexicon[k].phrases.length + ' fraze</span>'; }).join("");
      html += blk("Tipuri din lexicon fara evenimente inca", unfired.length + " / " + Object.keys(D.lexicon).length + " tipuri de lexicon",
        '<p class="copy wide">Aceste tipuri au fraze declanșatoare definite in event_lexicon.py, dar nicio stire nu a produs inca un eveniment canonic din aceasta categorie.</p><div style="margin-top:12px;">' + chips + '</div>');
    }

    if (highlightType && D.lexicon[highlightType]) {
      var lex = D.lexicon[highlightType];
      var count = 0; D.events.by_type.forEach(function (p) { if (p[0] === highlightType) count = p[1]; });
      html += blk("Detaliu tip: " + highlightType, count + " evenimente canonice",
        '<p class="copy" style="margin-bottom:10px;">Fraze declansatoare din lexicon:</p>' +
        (lex.phrases.length ? lex.phrases.map(function (p) { return '<span class="phrasechip">' + esc(p) + '</span>'; }).join("") : '<div class="empty">Fara fraze definite.</div>'));
    }

    return html;
  }

  function viewSignals() {
    if (!D.signals.available) {
      return pageHead("Inteligenta · semnale", "Semnale (model)", null) + blk("Fara date", null, '<div class="empty">Faza 10 nu a rulat inca pe aceasta baza de date.</div>');
    }
    var activeCount = 0; D.signals.by_status.forEach(function (p) { if (p[0] === "active") activeCount = p[1]; });
    var html = pageHead("Centrul de semnale (derivate din model)", "Semnale (model)", [["Active", activeCount + " / " + D.signals.total]]);

    // TD-03, the other half. See the note on the recommendations page.
    html += '<section class="blk"><div class="blk-body" style="border-left:3px solid var(--line);font-size:11px;color:var(--muted);line-height:1.6;">' +
      '<strong>Sursa canonica pentru ideile derivate din model.</strong> Fiecare semnal are instrument_id, ' +
      'legatura catre modelul si predictia care l-au produs, orizont, prag de informatie si ciclu de viata. ' +
      'Pagina <strong>Recomandari (sentiment)</strong> arata un obiect diferit — euristica pe stiri, cheie pe numele companiei — ' +
      'nu o versiune mai veche a acestuia. Cele doua nu se compara direct.' +
      '</div></section>';

    var sig = D.signals.recent[state.sigIdx] || D.signals.recent[0];
    var rows = D.signals.recent.map(function (s, idx) {
      var active = idx === state.sigIdx;
      return '<tr class="rowlink' + (active ? " sel" : "") + '" onclick="MLSetSigIdx(' + idx + ')"><td style="font-weight:700;">' + signalLabel(s) + (signalIsExperimental(s) ? ' ' + modelStatusPill(s[10], 9) : '') + '</td><td><span class="pill outline-up">' + esc(s[2]) + '</span></td><td class="r">' + fmtNum(s[4], 2) + '</td><td class="r" style="color:var(--accent-dark);font-weight:700;">' + fmtNum(s[5], 2) + '</td><td class="r">' + fmtSignedPct(s[6]) + '</td></tr>';
    }).join("");

    var detail = "";
    if (sig) {
      detail = (signalIsExperimental(sig)
          ? '<div style="border:1px solid #ae6c00;background:rgba(174,108,0,0.07);padding:9px 11px;margin-bottom:12px;font-size:11px;line-height:1.5;">' +
            '<strong>' + modelStatusPill(sig[10], 10) + '</strong> ' +
            'Semnal produs de un model care nu a fost promovat. Modelul nu a depasit baseline-ul propriu, ' +
            'deci acest numar este un rezultat de cercetare, nu o recomandare validata.' +
            '</div>'
          : '') +
        '<div style="font-size:20px;font-weight:800;letter-spacing:-0.02em;margin-bottom:2px;">' + signalLabel(sig) + '</div>' +
        '<div style="font-size:11px;color:var(--muted);" class="mono">' + esc(sig[1]) + ' · cutoff ' + esc(sig[7] || "—") + '</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 0;margin-top:12px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);">' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtNum(sig[4], 2) + '</div><div style="font-size:10px;color:var(--muted);">forta</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;color:var(--accent-dark);" title="Scor euristic, nu probabilitate. scor = increderea modelului x calitatea datelor x acordul intre modele x esantion. Nu este calibrat fata de rezultate: 0,30 nu inseamna 30% sanse.">' + fmtNum(sig[5], 2) + '</div><div style="font-size:10px;color:var(--muted);">scor incredere</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtSignedPct(sig[6]) + '</div><div style="font-size:10px;color:var(--muted);">r. asteptat</div></div></div>' +
        '<div style="padding:14px 0;"><div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px;">stare</div>' +
        '<span class="pill" style="border:1px solid var(--line-strong);">' + esc(sig[3]) + '</span></div>';
    }

    html += '<section class="blk"><div class="grid32">' +
      '<div><table class="data"><thead><tr><th>Instrument</th><th>Directie</th><th class="r" title="Marimea miscarii asteptate raportata la scara strategiei. Nu este probabilitate.">Forta</th><th class="r" title="Scor euristic, nu probabilitate. scor = increderea modelului x calitatea datelor x acordul intre modele x esantion. Nu este calibrat fata de rezultate: 0,30 nu inseamna 30% sanse.">Scor incredere</th><th class="r">R. asteptat</th></tr></thead><tbody>' + (rows || '<tr><td colspan="5" class="empty">Niciun semnal</td></tr>') + '</tbody></table></div>' +
      '<div><div class="blk-head"><h2>Detaliu semnal</h2></div><div class="blk-body">' + detail + '</div></div>' +
      '</div></section>';

    html += '<section class="blk"><div class="blk-body" style="font-size:11px;color:var(--muted);line-height:1.6;">' +
      '<strong>Despre cele doua numere.</strong> <em>Forta</em> este marimea miscarii asteptate raportata la scara strategiei (5% = 1,00). ' +
      '<em>Scorul de incredere</em> este o euristica multiplicativa: increderea modelului x calitatea datelor x acordul intre modele x esantion. ' +
      'Niciunul nu este o probabilitate si niciunul nu este calibrat fata de rezultate. ' +
      'Scorul este aproape constant (0,30) fiindca trei din cei patru factori sunt momentan ficsi: modelul ridge nu raporteaza incredere proprie (0,5), ' +
      'exista o singura familie de modele deci nu exista acord de verificat (0,6), iar toate observatiile trec drept calitate inalta (1,0). ' +
      'Va incepe sa varieze cand apare o a doua familie de modele, nu inainte.' +
      '</div></section>';

    if (D.signals.evaluations.length) {
      var evalRows = D.signals.evaluations.map(function (e) {
        return '<tr><td>' + esc(e[2]) + '</td><td class="r">' + e[3] + (e[7] ? ' <span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);">esantion mic</span>' : '') + '</td><td class="r">' + fmtPct(e[4]) + '</td><td class="r">' + fmtPct(e[5]) + '</td><td>' + (e[6] ? '<span class="pill" style="border:1px solid #00795a;color:#00795a;">da</span>' : '<span class="pill" style="border:1px solid #ae1800;color:#ae1800;">nu</span>') + '</td></tr>';
      }).join("");
      html += blk("Evaluare vs. baseline", null, '<table class="data"><thead><tr><th>Orizont</th><th class="r">N</th><th class="r">Rata succes</th><th class="r">Baseline</th><th>Bate</th></tr></thead><tbody>' + evalRows + '</tbody></table>');
    }
    if (D.signals.suppression.length) {
      html += blk("Motive de suprimare", null, barRows(D.signals.suppression));
    }

    return html;
  }

  function viewOutcomes() {
    if (!D.legacy.available) {
      return pageHead("Performanta · rezultate", "Rezultate", null) + blk("Fara date", null, '<div class="empty">Nicio recomandare in baza de date.</div>');
    }
    var html = pageHead("Recomandari din sentiment · track record", "Recomandari (sentiment)", [["Emise", fmtNum(D.legacy.total_recs)]]);

    // TD-03. Two tables in this database describe trade ideas and they
    // are NOT the same object. Saying so here and on the signals page
    // is the whole remediation: the ambiguity was in the presentation,
    // not in the data.
    html += '<section class="blk"><div class="blk-body" style="border-left:3px solid var(--line);font-size:11px;color:var(--muted);line-height:1.6;">' +
      '<strong>Ce este aceasta pagina.</strong> Recomandari produse de motorul de sentiment din stiri (Fazele 1-9), ' +
      'cheie pe <em>numele companiei</em>, aproximativ 1.940 pe zi, regenerate integral la fiecare rulare. ' +
      'Nu au model, nu au orizont de informatie si nu au ciclu de viata — dar au singurul istoric verificat fata de pret din sistem. ' +
      'Sursa canonica pentru ideile derivate din model este pagina <strong>Semnale (model)</strong>, care este un obiect diferit, nu o versiune mai noua a acesteia.' +
      '</div></section>';

    html += '<section class="blk"><div class="statgrid" style="grid-template-columns:repeat(4,1fr);">' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(D.legacy.checked) + '</div><div class="l">verificate</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtPct(D.legacy.accuracy) + '</div><div class="l">acuratete bruta</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtPct(D.legacy.verified_correct / (D.legacy.verified_count || 1)) + '</div><div class="l">un apel per companie (' + D.legacy.verified_count + ')</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + D.legacy.verified_count + '</div><div class="l">companii unice</div></div></div></section>';

    var calibRows = D.legacy.calibration.map(function (c) {
      return '<tr><td class="mono" style="font-weight:700;">' + esc(c[0]) + '</td><td class="r" style="color:var(--muted);">' + c[1] + '</td><td class="r" style="font-weight:700;">' + fmtPct(c[2]) + '</td></tr>';
    }).join("");
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Calibrare</h2></div><table class="data"><thead><tr><th>Interval incredere</th><th class="r">N</th><th class="r">Rata reala</th></tr></thead><tbody>' + (calibRows || '<tr><td colspan="3" class="empty">Fara date</td></tr>') + '</tbody></table></div>' +
      '<div><div class="blk-head"><h2>Distributia recomandarilor</h2></div><div class="blk-body">' + barRows(D.legacy.by_rec) + '</div></div>' +
      '</div></section>';

    if (D.legacy.accuracy_trend.length) {
      html += blk("Acuratete cumulativa in timp", "ultimele " + D.legacy.accuracy_trend.length + " zile cu verificari",
        barRows(D.legacy.accuracy_trend, function (v) { return fmtPct(v); }));
    }

    var recRows = D.legacy.recent.map(function (r) {
      var verify = r[6] === 1 ? '<span class="pill" style="border:1px solid #00795a;color:#00795a;">corect</span>' : (r[6] === 0 ? '<span class="pill" style="border:1px solid #ae1800;color:#ae1800;">gresit</span>' : '<span style="color:var(--faint);font-size:11px;">neverificat</span>');
      return '<tr><td>' + esc(r[0]) + '</td><td style="font-weight:700;">' + esc(r[1] || "") + '</td><td><span class="pill solid">' + esc(r[2]) + '</span></td><td class="r" style="font-weight:700;">' + fmtPct(r[3]) + '</td><td class="r">' + verify + '</td></tr>';
    }).join("");
    html += blk("Recomandari recente", null, '<table class="data"><thead><tr><th>Companie</th><th>Ticker</th><th>Apel</th><th class="r">Incredere</th><th class="r">Verificare</th></tr></thead><tbody>' + (recRows || '<tr><td colspan="5" class="empty">Fara recomandari</td></tr>') + '</tbody></table>');

    return html;
  }

  function viewOutcomes() {
    if (!D.outcomes.available) {
      return pageHead("Performanta · rezultate reale", "Rezultate reale", null) +
        blk("Fara date", null, '<div class="empty">Faza 19 nu a rulat inca pe aceasta baza de date. Ruleaza <span class="mono">scripts/measure_outcomes.py --apply</span>.</div>');
    }
    var O = D.outcomes;
    var pct = function (v, d) { return v === null || v === undefined ? "—" : (100 * v).toFixed(d === undefined ? 1 : d) + "%"; };
    var sgn = function (v, d) { return v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + (100 * v).toFixed(d === undefined ? 2 : d) + "%"; };

    var html = pageHead("Performanta · ce s-a intamplat dupa semnal", "Rezultate reale",
      [["Masuratori", fmtNum(O.total)], ["Acoperire", pct(O.coverage)]]);

    // Coverage FIRST, deliberately. "51% acuratete" inseamna ceva foarte
    // diferit la 66% acoperire fata de 95%, iar o pagina care ar incepe cu
    // rata l-ar invita pe cititor sa sara peste numarul care o califica.
    var statusRows = O.by_status.map(function (s) {
      var label = { available: "masurate", pending: "in asteptare (fereastra deschisa)",
                    insufficient_data: "date insuficiente", invalid: "semnalate ca invalide",
                    superseded: "inlocuite" }[s[0]] || s[0];
      return '<tr><td>' + esc(label) + '</td><td class="r" style="font-weight:700;">' + fmtNum(s[1]) + '</td><td class="r" style="color:var(--muted);">' + pct(s[1] / O.total) + '</td></tr>';
    }).join("");

    html += '<section class="blk"><div class="blk-body" style="border-left:3px solid var(--line);font-size:11px;color:var(--muted);line-height:1.6;">' +
      '<strong>Ce masoara aceasta pagina.</strong> Ce a facut piata DUPA fiecare semnal — randament, excursie favorabila (MFE), excursie adversa (MAE), directie. ' +
      'Nu masoara profit: nu exista ordine si nu exista pozitii, iar un "randament" care ar presupune tacit marimi egale de pozitie ar fi un rezultat de portofoliu deghizat in semnal. ' +
      '<em>Date insuficiente</em> nu inseamna niciodata randament zero si nu inseamna niciodata ratare — inseamna ca datele de pret nu pot raspunde la intrebare. ' +
      'Metodologie <span class="mono">' + esc(O.method_version) + '</span>, preturi pana la ' + fmtDate(O.data_as_of) + '.' +
      '</div></section>';

    html += '<section class="blk"><div class="statgrid" style="grid-template-columns:repeat(5,1fr);">' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + pct(O.coverage) + '</div><div class="l">acoperire</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + pct(O.accuracy) + '</div><div class="l">acuratete directionala</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(O.hits) + '</div><div class="l">corecte</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(O.misses) + '</div><div class="l">gresite</div></div>' +
      '<div class="cell"><div class="n" style="font-size:26px;">' + fmtNum(O.neutrals) + '</div><div class="l">neutre (excluse din rata)</div></div>' +
      '</div></section>';

    html += blk("Stare masuratori", "acoperirea inainte de rezultat",
      '<table class="data"><thead><tr><th>Stare</th><th class="r">N</th><th class="r">Pondere</th></tr></thead><tbody>' + statusRows + '</tbody></table>');

    // Decay: the question section 14 asks — does the edge fade?
    var decayRows = O.decay.map(function (d) {
      return '<tr><td class="mono" style="font-weight:700;">' + esc(d[0]) + '</td>' +
        '<td class="r">' + fmtNum(d[1]) + (d[7] ? ' <span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);font-size:9px;">esantion mic</span>' : '') + '</td>' +
        '<td class="r" style="font-weight:700;">' + pct(d[2]) + '</td>' +
        '<td class="r">' + sgn(d[3]) + '</td><td class="r">' + sgn(d[4]) + '</td>' +
        '<td class="r" style="color:#00795a;">' + sgn(d[5]) + '</td>' +
        '<td class="r" style="color:#ae1800;">' + sgn(d[6]) + '</td></tr>';
    }).join("");
    html += blk("Decaderea semnalului", "acelasi semnal, masurat pe orizonturi crescatoare",
      '<table class="data"><thead><tr><th>Orizont</th><th class="r">N</th><th class="r">Acuratete</th><th class="r">Randament mediu</th><th class="r">Median</th><th class="r">MFE mediu</th><th class="r">MAE mediu</th></tr></thead><tbody>' + decayRows + '</tbody></table>');

    // Section 45 — training metrics beside the realized record.
    var qualityRows = O.quality.map(function (m) {
      var metrics = {}; try { metrics = m[3] ? JSON.parse(m[3]) : {}; } catch (e) {}
      var r2 = metrics.r_squared;
      return '<tr><td class="mono" style="font-size:10px;">' + esc(m[0]) + '</td>' +
        '<td>' + modelStatusPill(m[2], 9) + '</td>' +
        '<td class="r" style="' + (r2 < 0 ? 'color:#ae1800;' : '') + '">' + (r2 === undefined ? "—" : Number(r2).toFixed(3)) + '</td>' +
        '<td>' + (m[4] === 1 ? '<span class="pill" style="border:1px solid #00795a;color:#00795a;">da</span>' : (m[4] === 0 ? '<span class="pill" style="border:1px solid #ae1800;color:#ae1800;">nu</span>' : '—')) + '</td>' +
        '<td class="r">' + (m[5] === null ? "—" : fmtNum(m[5])) + (m[8] ? ' <span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);font-size:9px;">mic</span>' : '') + '</td>' +
        '<td class="r" style="font-weight:700;">' + pct(m[6]) + '</td>' +
        '<td class="r">' + sgn(m[7]) + '</td></tr>';
    }).join("");
    html += blk("Evaluare la antrenare vs. rezultat realizat", "orizont 5d — cele doua coloane raspund la intrebari diferite",
      '<table class="data"><thead><tr><th>Model</th><th>Stare</th><th class="r">r&sup2; (antrenare)</th><th>Bate baseline</th><th class="r">N (realizat)</th><th class="r">Acuratete realizata</th><th class="r">Randament mediu</th></tr></thead><tbody>' + qualityRows + '</tbody></table>' +
      '<div class="blk-body" style="font-size:11px;color:var(--muted);line-height:1.6;padding-top:0;">' +
      'Coloanele din stanga sunt masurate pe o impartire chronologica retinuta la antrenare; cele din dreapta sunt ce a facut piata dupa aceea. ' +
      'Un model bun la antrenare si slab in realitate este exact cazul care merita vazut, iar amestecarea celor doua l-ar ascunde.' +
      '</div>');

    var cohortTable = function (title, note, rows) {
      var body = rows.map(function (r) {
        return '<tr><td>' + esc(r[0]) + '</td><td class="r">' + fmtNum(r[2]) +
          (r[8] ? ' <span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);font-size:9px;">mic</span>' : '') + '</td>' +
          '<td class="r" style="font-weight:700;">' + pct(r[3]) + '</td>' +
          '<td class="r">' + sgn(r[4]) + '</td><td class="r">' + sgn(r[5]) + '</td>' +
          '<td class="r" style="color:#00795a;">' + sgn(r[6]) + '</td>' +
          '<td class="r" style="color:#ae1800;">' + sgn(r[7]) + '</td></tr>';
      }).join("");
      return blk(title, note, '<table class="data"><thead><tr><th>Cohorta</th><th class="r">N</th><th class="r">Acuratete</th><th class="r">Mediu</th><th class="r">Median</th><th class="r">MFE</th><th class="r">MAE</th></tr></thead><tbody>' + (body || '<tr><td colspan="7" class="empty">Fara date</td></tr>') + '</tbody></table>');
    };

    html += cohortTable("Dupa directie", "orizont 5d", O.by_direction);
    html += cohortTable("Dupa stare model", "experimental vs. validat — nu se amesteca", O.by_model_status);
    html += cohortTable("Dupa regim de piata", "orizont 5d", O.by_regime);
    html += cohortTable("Dupa scor de incredere", "scorul e aproape constant (0,30) — vezi pagina Semnale", O.by_confidence);
    html += cohortTable("Dupa forta semnalului", "forta NU e acelasi lucru cu increderea", O.by_strength);
    html += cohortTable("Dupa instrument", "primele 25 dupa marimea esantionului", O.by_instrument);

    // Section 41 — the multiplicity caveat, on the page with the numbers.
    html += '<section class="blk"><div class="blk-body" style="border-left:3px solid #ae6c00;font-size:11px;line-height:1.6;">' +
      '<strong>Despre tabelele de mai sus.</strong> ' + fmtNum(O.cohorts) + ' cohorte au fost calculate din aceleasi masuratori. ' +
      'Feliind repetat dupa orizont, model, instrument, regim, directie si incredere se obtin subgrupuri care difera din intamplare: ' +
      'la pragul obisnuit de 5%, aproximativ una din douazeci va parea remarcabila fara sa existe vreun efect, ' +
      'iar cohorta cu valoarea cea mai extrema este cea mai probabil zgomot. ' +
      'Niciun test de semnificatie nu a fost rulat si niciun numar de aici nu este prezentat ca semnificativ.' +
      '</div></section>';

    var recentRows = O.recent.slice(0, 40).map(function (r) {
      var statusPill = r[4] === "available" ? "" :
        '<span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);font-size:9px;">' + esc(r[4]) + '</span>';
      var verdict = { hit: '<span style="color:#00795a;font-weight:700;">corect</span>',
                      miss: '<span style="color:#ae1800;font-weight:700;">gresit</span>',
                      neutral: '<span style="color:var(--muted);">neutru</span>' }[r[8]] ||
                    '<span style="color:var(--muted);">—</span>';
      return '<tr><td class="mono" style="font-size:10px;">' + esc(r[1]) + '</td>' +
        '<td class="mono">' + esc(r[2]) + '</td><td>' + esc(r[3]) + '</td>' +
        '<td>' + verdict + ' ' + statusPill + '</td>' +
        '<td class="r">' + sgn(r[5]) + '</td>' +
        '<td class="r" style="color:#00795a;">' + sgn(r[6]) + '</td>' +
        '<td class="r" style="color:#ae1800;">' + sgn(r[7]) + '</td>' +
        '<td class="r" style="color:var(--muted);">' + (r[14] || 0) + '</td></tr>';
    }).join("");
    html += blk("Masuratori recente", "un rand per semnal si orizont",
      '<table class="data"><thead><tr><th>Instrument</th><th>Orizont</th><th>Directie</th><th>Verdict</th><th class="r">Randament</th><th class="r">MFE</th><th class="r">MAE</th><th class="r">Bare</th></tr></thead><tbody>' + recentRows + '</tbody></table>');

    return html;
  }

  function viewModels() {
    if (!D.models.available) {
      return pageHead("Performanta · modele", "Modele", null) + blk("Fara date", null, '<div class="empty">Faza 9 nu a rulat inca pe aceasta baza de date.</div>');
    }
    var html = pageHead("Performanta · inteligenta modelelor", "Modele", [["Antrenate", String(D.models.models.length)], ["Validate", String(D.models.active || 0)], ["Predictii", fmtNum(D.models.predictions)]]);

    // Phase 18 / NEW-01. Inference used to take the newest model. It
    // now takes only a promoted one, and this states which of the two
    // situations the system is in rather than leaving a reader to
    // infer it from a table of numbers.
    html += (D.models.active
      ? '<section class="blk"><div class="blk-body" style="border-left:3px solid #00795a;font-size:12px;line-height:1.6;">' +
        '<strong>' + D.models.active + ' model validat.</strong> Semnalele de productie provin din modelul promovat. ' +
        'Promovarea este o decizie umana, inregistrata cu autor si motiv.</div></section>'
      : '<section class="blk"><div class="blk-body" style="border-left:3px solid #ae6c00;font-size:12px;line-height:1.6;">' +
        '<strong>Niciun model validat.</strong> Niciun model nu a depasit baseline-ul propriu pe un esantion efectiv suficient, ' +
        'deci niciunul nu a fost promovat. Inferenta refuza implicit sa scoreze in acest caz; etapa 9 a pipeline-ului ' +
        'ruleaza explicit in mod <em>experimental</em>, iar toate semnalele rezultate sunt marcate ca atare. ' +
        'Aceasta este poarta de calitate functionand, nu o eroare de configurare.</div></section>');

    if (D.models.promotions && D.models.promotions.length) {
      var promoRows = D.models.promotions.map(function (r) {
        return '<tr><td class="mono" style="font-size:10px;">' + esc(String(r[6] || "").slice(0, 19)) + '</td>' +
          '<td>' + esc(r[1]) + '</td><td class="mono" style="font-size:10px;">' + esc(r[0]) + '</td>' +
          '<td>' + esc(r[2]) + ' &rarr; ' + esc(r[3]) + '</td><td>' + esc(r[4]) + '</td><td>' + esc(r[5]) + '</td></tr>';
      }).join("");
      html += blk("Istoric promovari", "cine, cand, si de ce", '<table class="data"><thead><tr><th>Cand</th><th>Actiune</th><th>Model</th><th>Stare</th><th>Aprobat de</th><th>Motiv</th></tr></thead><tbody>' + promoRows + '</tbody></table>');
    }

    var cards = D.models.models.map(function (m) {
      var qid = m[0], label = m[1], n = m[2], clusters = m[3], small = m[4], beats = m[5], metricsJson = m[6];
      var status = m[7], trainedAt = m[8], evaluatedAt = m[9], dsv = m[10], fsv = m[11];
      var metrics = {}; try { metrics = metricsJson ? JSON.parse(metricsJson) : {}; } catch (e) {}
      var verdict = beats ? '<span class="pill" style="border:1px solid #00795a;color:#00795a;">bate baseline</span>' : (beats === 0 ? '<span class="pill" style="border:1px solid #ae1800;color:#ae1800;">nu bate baseline</span>' : '<span class="pill" style="border:1px solid #8a8a8a;color:#8a8a8a;">fara baseline — nejudecabil</span>');
      var num = function (v, d) { return (v === undefined || v === null) ? "—" : Number(v).toFixed(d); };
      return '<div class="regcard" style="margin-bottom:14px;"><div class="rc-top"><span class="rc-key">' + esc(qid) + '</span>' + modelStatusPill(status, 10) + '</div>' +
        '<div style="font-size:11px;color:var(--muted);" class="mono">' + esc(label) + '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);">' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtNum(n) + '</div><div style="font-size:10px;color:var(--muted);">N antrenare</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;' + (small ? 'color:var(--accent-dark);' : '') + '">' + (clusters === null ? "—" : fmtNum(clusters)) + '</div><div style="font-size:10px;color:var(--muted);">esantion efectiv' + (small ? ' (mic)' : '') + '</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;' + (metrics.r_squared < 0 ? 'color:#ae1800;' : '') + '">' + num(metrics.r_squared, 3) + '</div><div style="font-size:10px;color:var(--muted);">r&sup2;</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;' + (metrics.directional_accuracy < 0.5 ? 'color:#ae1800;' : '') + '">' + num(metrics.directional_accuracy, 3) + '</div><div style="font-size:10px;color:var(--muted);">acuratete directionala</div></div></div>' +
        '<div style="margin-top:12px;">' + verdict + '</div>' +
        '<div style="margin-top:10px;padding-top:9px;border-top:1px solid var(--line);font-size:10px;color:var(--muted);line-height:1.6;" class="mono">' +
        'antrenat ' + esc(String(trainedAt || "—").slice(0, 19)) +
        ' · evaluat ' + esc(String(evaluatedAt || "—").slice(0, 19)) +
        '<br>dataset ' + esc(dsv || "—") + ' · caracteristici ' + esc(fsv || "—") + '</div></div>';
    }).join("");

    var featHtml = D.research.available && D.research.feature_coverage.length
      ? D.research.feature_coverage.slice(0, 16).map(function (f) {
          var max = Math.max.apply(null, D.research.feature_coverage.map(function (x) { return x[1]; })) || 1;
          var pct = (100 * f[1] / max).toFixed(0);
          return '<div style="display:grid;grid-template-columns:1fr 80px 48px;align-items:center;gap:10px;border-bottom:1px solid var(--line);padding:5px 0;font-size:11px;">' +
            '<span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + esc(f[0]) + '</span>' +
            '<span style="height:8px;background:var(--line);"><span style="display:block;height:8px;width:' + pct + '%;background:var(--ink);"></span></span>' +
            '<span style="text-align:right;font-weight:700;">' + f[1] + '</span></div>';
        }).join("")
      : '<div class="empty">Faza 8 nu a rulat inca.</div>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Modele antrenate</h2></div><div class="blk-body">' + cards + '</div></div>' +
      '<div><div class="blk-head"><h2>Acoperire caracteristici</h2><span class="blk-note">' + (D.research.available ? D.research.feature_count : 0) + ' in registru</span></div><div class="blk-body">' + featHtml + '</div></div>' +
      '</div></section>';

    return html;
  }

  // -----------------------------------------------------------------
  // Phase 13 - paper trading
  // -----------------------------------------------------------------
  function paperBanner() {
    return '<div class="papermode"><span class="tagname">Paper mode</span>' +
      '<span class="msg"><b>Fiecare cifra de pe aceasta pagina este simulata.</b> ' +
      'Nu exista cont real, broker, credentiale sau ordine trimise undeva. ' +
      'Executia este simulata local cu aceleasi modele de cost si slippage ' +
      'folosite la backtesting.</span></div>';
  }

  function paperSelected() {
    if (!PP.available || !PP_SESSIONS.length) return null;
    var id = state.param;
    var found = null;
    for (var i = 0; i < PP_SESSIONS.length; i++) {
      if (PP_SESSIONS[i].session_id === id) { found = PP_SESSIONS[i]; break; }
    }
    found = found || PP_SESSIONS[0];
    return { s: found, d: (PP.detail || {})[found.session_id] || {} };
  }

  function paperEmptyState() {
    if (!PP.available) {
      return blk("Paper trading", "Faza 13",
        '<div class="callout"><b>Tabelele Fazei 13 nu exista in aceasta baza de date.</b>' +
        '<p>Motorul de paper trading este implementat, dar schema nu a fost creata aici. ' +
        'Ruleaza <span class="mono">python scripts/run_paper_session.py</span> pentru a o ' +
        'crea si a inregistra prima sesiune.</p></div>');
    }
    return blk("Paper trading", "nicio sesiune inregistrata",
      '<div class="callout"><b>Nicio sesiune de paper trading nu a rulat inca.</b>' +
      '<p>Aceasta nu este o eroare. Nu se afiseaza o sesiune inventata in loc.</p>' +
      '<div class="mono" style="margin-top:8px;font-size:11px;background:var(--panel);padding:10px;">' +
      'python scripts/run_paper_session.py --name prima-sesiune</div>' +
      '<p style="margin-top:10px;">Sesiunea foloseste exact acelasi lant ca productia — ' +
      'semnal, motor de risc, alocare, intentie de ordin — si difera intr-un singur punct: ' +
      'executorul, care simuleaza umplerea in loc sa trimita ordinul undeva.</p></div>');
  }

  function paperEquityChart(series) {
    var pts = (series || []).filter(function (r) { return r.e !== null && r.e !== undefined; });
    if (pts.length < 2) {
      return '<div class="empty">Sub doua observatii — nu se deseneaza o curba din care ' +
        'nu se poate citi nimic.</div>';
    }
    var vals = pts.map(function (r) { return r.e; });
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    var range = (max - min) || 1;
    var w = 1000, h = 180;
    var line = pts.map(function (r, i) {
      var x = (i / (pts.length - 1)) * w;
      var y = h - ((r.e - min) / range) * h;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h +
      '" preserveAspectRatio="none" style="display:block;border:1px solid var(--line);">' +
      '<polyline points="' + line + '" fill="none" stroke="var(--ink)" stroke-width="2" ' +
      'vector-effect="non-scaling-stroke"></polyline></svg>' +
      '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;">' +
      '<span class="mono">' + esc(fmtDate(pts[0].t)) + '</span>' +
      '<span class="mono">capital simulat ' + fmtNum(min, 0) + ' – ' + fmtNum(max, 0) + '</span>' +
      '<span class="mono">' + esc(fmtDate(pts[pts.length - 1].t)) + '</span></div>';
  }

  function viewPaper() {
    var sel = paperSelected();
    if (!sel) return pageHead("Paper trading", "Paper trading") + paperBanner() + paperEmptyState();

    var s = sel.s, d = sel.d;
    var v = d.versions || {};
    var cfg = d.config || {};

    var html = pageHead("Paper trading - simulare in timp real", s.name || s.session_id, [
      ["Stare", (s.status || "").toUpperCase()],
      ["Ticks", fmtNum(s.ticks)],
      ["Ordine", fmtNum(s.orders)]
    ]);
    html += paperBanner();

    if (PP_SESSIONS.length > 1) {
      html += '<div class="filterbar"><div class="segbtns">' + PP_SESSIONS.slice(0, 8).map(function (r) {
        return '<button class="' + (r.session_id === s.session_id ? "on" : "") +
          '" onclick="MLGo(\'paper\',\'' + esc(r.session_id) + '\')">' +
          esc((r.name || r.session_id).slice(0, 14)) + '</button>';
      }).join("") + '</div></div>';
    }

    // --- state of the session ---
    var hcolor = PP_HEALTH_COLOR[s.health] || "var(--muted)";
    html += '<section class="blk"><div class="blk-body">' +
      '<div style="border:2px solid ' + hcolor + ';padding:14px 16px;">' +
      '<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:' + hcolor + ';">' +
      esc(s.status || "") + ' &middot; sanatate ' + esc(s.health || "necunoscuta") + '</div>' +
      '<div style="font-size:14px;margin-top:6px;">ultimul tick ' + esc(fmtDate(s.last_tick_at)) +
      ' &middot; ' + fmtNum(s.ticks) + ' ticks &middot; ceas <span class="mono">' +
      esc(s.clock_kind || "system") + '</span></div>' +
      '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:8px;">' +
      esc(s.session_id) + ' &middot; amprenta ' + esc(s.fingerprint || "") + '</div>' +
      '</div></div></section>';

    // --- the book ---
    function cell(label, value, note) {
      return '<div class="cell"><div class="n">' + value + '</div><div class="l">' +
        esc(label) + (note ? '<br><span style="color:var(--faint);">' + esc(note) + '</span>' : "") +
        '</div></div>';
    }
    html += '<section class="blk"><div class="blk-head"><h2>Cont simulat</h2>' +
      '<span class="blk-note">stare la ultimul snapshot, nu un rezultat</span></div>' +
      '<div class="statgrid pp-grid">' +
      cell("capital initial", fmtNum(s.capital, 0)) +
      cell("valoare curenta", fmtNum(s.equity, 0), "simulata") +
      cell("numerar", fmtNum(s.cash, 0)) +
      cell("variatie", fmtSignedPct(s.change, 2), "nu e un rezultat masurabil") +
      cell("drawdown", fmtPct(s.drawdown, 2)) +
      cell("pozitii", fmtNum(s.open_positions) + (s.unpriced ? " (" + s.unpriced + " fara pret)" : "")) +
      '</div></section>';

    html += blk("Evolutia contului simulat",
      "o perioada scurta de paper trading nu stabileste nimic despre o strategie",
      paperEquityChart(d.equity));

    // --- data freshness: the honesty that makes the rest readable ---
    var fresh = s.freshness || "unavailable";
    html += blk("Prospetimea datelor", esc(PP_FRESH_LABEL[fresh] || fresh),
      '<div class="callout"><b>Datele folosite sunt ' +
      esc(PP_FRESH_LABEL[fresh] || fresh) + '.</b>' +
      '<p>Sesiunea masoara la fiecare tick cat de vechi sunt barele pe care decide si ' +
      'raporteaza exact asta. Nu exista flux live in acest proiect: se lucreaza pe bare ' +
      'zilnice cache-uite, iar clasificarea de mai sus este singura care spune cat de ' +
      'aproape de "acum" sunt de fapt.</p>' +
      '<p>Doar datele <i>proaspete</i> sau <i>in imbatranire</i> pot sustine un ordin nou. ' +
      'Restul opresc generarea de ordine in loc sa fie folosite oricum.</p></div>');

    // --- health components ---
    var comps = d.health || [];
    var compHtml = comps.length ? comps.map(function (c) {
      var col = PP_HEALTH_COLOR[c.state] || "var(--muted)";
      return '<div style="display:grid;grid-template-columns:1fr 110px 90px;gap:10px;align-items:center;' +
        'border-bottom:1px solid var(--line);padding:6px 0;font-size:11.5px;">' +
        '<span class="mono"><span class="pp-hstate" style="background:' + col + ';"></span>' +
        esc(c.component) + '</span>' +
        '<span style="color:' + col + ';font-weight:700;">' + esc(c.state) + '</span>' +
        '<span style="text-align:right;" class="mono">' +
        (c.latency_ms === null || c.latency_ms === undefined ? "—" : Number(c.latency_ms).toFixed(1) + " ms") +
        '</span></div>';
    }).join("") : '<div class="empty">Niciun heartbeat inregistrat.</div>';
    html += blk("Sanatatea componentelor",
      "starea generala este cea mai proasta componenta, nu media lor", compHtml);

    // --- orders ---
    var byState = d.orders_by_state || {};
    var stateRows = Object.keys(byState).sort().map(function (k) { return [k, byState[k]]; });
    var rejRows = (d.rejections || []).map(function (r) { return [r.reason, r.count]; });
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Ordine dupa stare</h2>' +
      '<span class="blk-note">' + fmtNum(s.orders) + ' in total</span></div>' +
      '<div class="blk-body">' + barRows(stateRows) + '</div></div>' +
      '<div><div class="blk-head"><h2>Motive de respingere</h2>' +
      '<span class="blk-note">enumerate, deci numarabile</span></div>' +
      '<div class="blk-body">' + (rejRows.length ? barRows(rejRows) :
        '<div class="empty">Niciun ordin respins.</div>') + '</div></div>' +
      '</section>';

    // --- positions ---
    var pos = d.positions || [];
    var posHtml = pos.length ? '<table class="data"><thead><tr><th>Instrument</th>' +
      '<th class="r">Cantitate</th><th class="r">Cost mediu</th><th>Deschisa</th>' +
      '<th>Semnal de intrare</th></tr></thead><tbody>' + pos.map(function (r) {
        return '<tr><td class="mono">' + esc(r.instrument_id) + '</td>' +
          '<td class="r">' + fmtNum(r.quantity, 4) + '</td>' +
          '<td class="r">' + fmtNum(r.average_cost, 2) + '</td>' +
          '<td>' + esc(fmtDate(r.opened_at)) + '</td>' +
          '<td class="mono" style="font-size:10px;">' + esc(r.signal_id || "—") + '</td></tr>';
      }).join("") + '</tbody></table>'
      : '<div class="empty">Nicio pozitie deschisa.</div>';
    html += blk("Pozitii simulate", "niciuna nu exista la vreun broker", posHtml);

    // --- fills ---
    var fills = d.fills || [];
    var fillHtml = fills.length ? '<table class="data"><thead><tr><th>Instrument</th><th>Sens</th>' +
      '<th class="r">Cantitate</th><th class="r">Pret referinta</th><th class="r">Pret executat</th>' +
      '<th class="r">Comision</th><th class="r">Slippage</th><th>Moment</th><th>Loc</th></tr></thead><tbody>' +
      fills.map(function (r) {
        return '<tr><td class="mono">' + esc(r.instrument_id) + '</td>' +
          '<td>' + esc(r.side) + (r.partial ? ' <span class="pill">partial</span>' : "") +
          (r.ambiguous ? ' <span class="pill" style="background:var(--accent-bg);color:var(--accent-dark);">ambiguu</span>' : "") + '</td>' +
          '<td class="r">' + fmtNum(r.quantity, 4) + '</td>' +
          '<td class="r">' + fmtNum(r.reference_price, 2) + '</td>' +
          '<td class="r">' + fmtNum(r.price, 2) + '</td>' +
          '<td class="r">' + fmtNum(r.commission, 2) + '</td>' +
          '<td class="r">' + fmtNum(r.slippage_cost, 2) + '</td>' +
          '<td>' + esc(fmtDate(r.at)) + '</td>' +
          '<td class="mono">' + esc(r.venue) + '</td></tr>';
      }).join("") + '</tbody></table>'
      : '<div class="empty">Nicio executie simulata.</div>';
    html += blk("Executii simulate",
      "pretul de referinta e pastrat langa cel executat, deci slippage-ul aplicat e verificabil",
      fillHtml);

    // --- reconciliation ---
    var rec = d.reconciliation;
    var recHtml;
    if (!rec) {
      recHtml = '<div class="empty">Nicio reconciliere inregistrata.</div>';
    } else if (rec.clean) {
      recHtml = '<div class="callout" style="border-color:var(--up);"><b>Reconciliere curata la ' +
        esc(fmtDate(rec.at)) + '.</b><p>' + fmtNum(rec.checks) + ' verificari: pozitiile, ' +
        'numerarul si cantitatile umplute derivate din executii corespund starii pastrate.</p></div>';
    } else {
      recHtml = '<div class="callout"><b>' + fmtNum((rec.discrepancies || []).length) +
        ' neconcordante la ' + esc(fmtDate(rec.at)) + '.</b><ul style="margin:8px 0 0 16px;font-size:12px;">' +
        (rec.discrepancies || []).map(function (x) {
          return '<li>' + esc(x.detail || x.kind || "") + '</li>';
        }).join("") + '</ul></div>';
    }
    html += blk("Reconciliere", "starea pastrata comparata cu ce spun executiile", recHtml);

    // --- controls and alerts ---
    var ctrl = d.controls || [];
    var ctrlHtml = ctrl.length ? ctrl.map(function (c) {
      return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:11.5px;">' +
        '<span class="mono" style="font-weight:700;">' + esc(c.action) + '</span> &middot; ' +
        esc(fmtDate(c.at)) + ' &middot; ' + esc(c.actor) +
        (c.reason ? '<div style="color:var(--muted);">' + esc(c.reason) + '</div>' : "") + '</div>';
    }).join("") : '<div class="empty">Niciun control operational aplicat.</div>';

    var alerts = d.alerts || [];
    var alertHtml = alerts.length ? alerts.map(function (a) {
      return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:11.5px;">' +
        '<span class="pill" style="background:var(--panel);">' + esc(a.severity) + '</span> ' +
        '<span class="mono">' + esc(a.code) + '</span>' +
        '<div style="color:var(--muted);">' + esc(a.message || a.detail || "") + '</div></div>';
    }).join("") : '<div class="empty">Nicio alerta.</div>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Controale operationale</h2>' +
      '<span class="blk-note">jurnal append-only</span></div>' +
      '<div class="blk-body">' + ctrlHtml + '</div></div>' +
      '<div><div class="blk-head"><h2>Alerte</h2></div>' +
      '<div class="blk-body">' + alertHtml + '</div></div></section>';

    // --- durability ---
    var lat = d.latency || [];
    var latHtml = lat.length ? lat.map(function (r) {
      return '<div style="display:grid;grid-template-columns:1fr 90px 90px;gap:10px;' +
        'border-bottom:1px solid var(--line);padding:5px 0;font-size:11.5px;">' +
        '<span class="mono">' + esc(r.stage) + '</span>' +
        '<span class="mono" style="text-align:right;">' + fmtNum(r.mean, 1) + ' ms</span>' +
        '<span class="mono" style="text-align:right;">' + fmtNum(r.worst, 1) + ' ms</span></div>';
    }).join("") : '<div class="empty">Nicio masuratoare de latenta.</div>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Durabilitate</h2>' +
      '<span class="blk-note">o sesiune de paper nu poate fi re-rulata</span></div>' +
      '<div class="blk-body"><div style="font-size:12px;line-height:1.6;">' +
      '<b>' + fmtNum(d.checkpoints) + '</b> checkpoint-uri, ultimul la <span class="mono">' +
      esc(fmtDate(d.last_checkpoint)) + '</span>.' +
      '<p style="color:var(--muted);margin-top:8px;">Timpul a trecut deja peste ticks-urile ' +
      'procesate, deci recuperarea reia de la ultimul checkpoint in loc sa porneasca ' +
      'de la zero.</p></div></div></div>' +
      '<div><div class="blk-head"><h2>Latenta pe etape</h2>' +
      '<span class="blk-note">medie / cea mai proasta</span></div>' +
      '<div class="blk-body">' + latHtml + '</div></div></section>';

    // --- event log ---
    var evs = d.events || [];
    var evHtml = evs.length ? '<table class="data"><thead><tr><th class="r">#</th><th>Moment</th>' +
      '<th>Tip</th><th>Instrument</th><th>Mesaj</th></tr></thead><tbody>' + evs.map(function (e) {
        return '<tr><td class="num mono">' + e.seq + '</td><td>' + esc(fmtDate(e.at)) + '</td>' +
          '<td class="mono">' + esc(e.kind) + '</td>' +
          '<td class="mono">' + esc(e.instrument_id || "—") + '</td>' +
          '<td>' + esc(e.message || "") + '</td></tr>';
      }).join("") + '</tbody></table>' : '<div class="empty">Niciun eveniment.</div>';
    html += blk("Jurnalul sesiunii", "append-only: nicio inregistrare nu e rescrisa", evHtml);

    // --- provenance and the boundary ---
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Versiuni</h2>' +
      '<span class="blk-note">aceleasi modele ca la backtesting</span></div>' +
      '<div class="blk-body"><div class="mono" style="font-size:11.5px;line-height:1.9;">' +
      'cod: ' + esc(v.code || "—") + '<br>' +
      'constrangeri: ' + esc(v.constraints || "—") + '<br>' +
      'cost: ' + esc(v.cost || "—") + '<br>' +
      'slippage: ' + esc(v.slippage || "—") + '<br>' +
      'executie: ' + esc(v.execution || "—") + '<br>' +
      'univers: ' + esc((cfg.universe || []).length) + ' instrumente' +
      '</div></div></div>' +
      '<div><div class="blk-head"><h2>Limita fazei</h2></div><div class="blk-body">' +
      '<div class="callout"><b>Aceasta faza nu are executie reala de niciun fel.</b>' +
      '<p>Interactive Brokers e conectat doar in mediul PAPER. Nu exista ' +
      'credentiale de broker, nu exista API de ordine si niciun cont real nu e citit ' +
      'sau modificat. Codul nu contine o cale catre asa ceva — nu e dezactivata, ' +
      'ci absenta.</p>' +
      '<p>Ce difera fata de productie este un singur obiect: executorul. Semnalele, ' +
      'motorul de risc, alocarea si contabilitatea sunt exact aceleasi.</p></div>' +
      '</div></div></section>';

    return html;
  }

  // -----------------------------------------------------------------
  // Phase 14 - broker abstraction and execution
  // -----------------------------------------------------------------
  function xBanner() {
    return '<div class="xbanner"><span class="tagname">Live execution disabled</span>' +
      '<span class="msg"><b>Nu exista executie cu bani reali.</b> ' +
      'Interactive Brokers e conectat doar in mediul PAPER; nu exista adaptor ' +
      'care sa accepte un mediu cu bani reali, nu exista credentiale in aplicatie ' +
      'si niciun cont real nu e citit sau modificat.</span></div>';
  }

  function xEnvironments() {
    var envs = [
      ["Backtest", true, "Faza 12 — bare istorice"],
      ["Paper", true, "Faza 13 — umpleri simulate"],
      ["Demo", false, "niciun adaptor implementat"],
      ["Live", false, "bani reali — refuzat structural"]
    ];
    return '<div class="envrow">' + envs.map(function (e) {
      var danger = e[0] === "Live";
      return '<span class="envchip ' + (e[1] ? "on" : "off") + (danger ? " danger" : "") +
        '" title="' + esc(e[2]) + '">' + esc(e[0]) +
        (e[1] ? "" : " · indisponibil") + '</span>';
    }).join("") + '</div>';
  }

  function xEmptyState() {
    if (!XE.available) {
      return blk("Executie", "Faza 14",
        '<div class="callout"><b>Tabelele Fazei 14 nu exista in aceasta baza de date.</b>' +
        '<p>Abstractia de broker este implementata, dar schema nu a fost creata aici. ' +
        'Ruleaza <span class="mono">python scripts/run_execution.py</span> pentru a o ' +
        'crea si a inregistra brokerii.</p></div>');
    }
    return blk("Executie", "niciun ordin inregistrat",
      '<div class="callout"><b>Niciun ordin nu a trecut inca prin orchestrator.</b>' +
      '<p>Aceasta nu este o eroare si nu se afiseaza ordine inventate in loc.</p>' +
      '<div class="mono" style="margin-top:8px;font-size:11px;background:var(--panel);padding:10px;">' +
      'python scripts/run_execution.py --dry-run-order</div></div>');
  }

  function viewExecution() {
    var html = pageHead("Executie - abstractie de broker", "Brokeri si executie", [
      ["Brokeri", String((XE.totals || {}).brokers || 0)],
      ["Implementati", String((XE.totals || {}).implemented_brokers || 0)],
      ["Ordine", String((XE.totals || {}).orders || 0)]
    ]);
    html += xBanner();
    html += xEnvironments();

    if (!XE.available || !XE_BROKERS.length) return html + xEmptyState();

    // --- brokers ---
    var caps = XE.capabilities || {};
    var brokerHtml = XE_BROKERS.map(function (b) {
      var c = caps[b.broker_id] || {};
      var types = (c.order_types || []);
      var border = b.implemented ? "var(--line-strong)" : "var(--border-mid)";
      return '<div style="border:2px solid ' + border + ';padding:12px 14px;margin-bottom:10px;' +
        (b.implemented ? "" : "opacity:0.75;") + '">' +
        '<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline;">' +
        '<span style="font-weight:800;font-size:14px;">' + esc(b.name) + '</span>' +
        '<span class="xstate" style="color:' + (b.implemented ? "var(--up)" : "var(--muted)") + ';">' +
        (b.implemented ? "adaptor implementat" : "fara adaptor") + '</span></div>' +
        '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:5px;">' +
        esc(b.broker_id) + ' · mediu ' + esc(b.environment) + ' · adaptor ' + esc(b.adapter || "—") +
        '</div>' +
        '<div style="font-size:11.5px;margin-top:8px;">tipuri de ordine: ' +
        (types.length ? '<span class="mono">' + esc(types.join(", ")) + '</span>'
                      : '<i>niciunul declarat</i>') + '</div>' +
        (c.notes ? '<div style="font-size:11.5px;color:var(--muted);margin-top:4px;">' +
                   esc(c.notes) + '</div>' : "") + '</div>';
    }).join("");
    html += blk("Brokeri inregistrati",
      "cei fara adaptor sunt listati, nu ascunsi", brokerHtml);

    // --- accounts ---
    var accounts = XE.accounts || [];
    var accHtml = accounts.length ? '<table class="data"><thead><tr><th>Cont</th>' +
      '<th>Broker</th><th>Mediu</th><th>Moneda</th><th>Contabilitate</th>' +
      '<th>Stare</th></tr></thead><tbody>' + accounts.map(function (a) {
        return '<tr><td class="mono">' + esc(a.account_id) + '</td>' +
          '<td class="mono">' + esc(a.broker_id) + '</td>' +
          '<td>' + esc(a.environment) + '</td>' +
          '<td>' + esc(a.base_currency) + '</td>' +
          '<td>' + esc(a.position_accounting) + '</td>' +
          '<td>' + (a.enabled ? "activ" : "dezactivat") + '</td></tr>';
      }).join("") + '</tbody></table>'
      : '<div class="empty">Niciun cont inregistrat.</div>';
    html += blk("Conturi", "niciunul nu e real", accHtml);

    // --- orders by state and rejections ---
    var byState = XE.orders_by_state || {};
    var stateRows = Object.keys(byState).sort().map(function (k) { return [k, byState[k]]; });
    var rejRows = (XE.rejections || []).map(function (r) { return [r.code, r.count]; });
    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Ordine dupa stare</h2>' +
      '<span class="blk-note">' + fmtNum((XE.totals || {}).orders) + ' in total</span></div>' +
      '<div class="blk-body">' + (stateRows.length ? barRows(stateRows) :
        '<div class="empty">Niciun ordin.</div>') + '</div></div>' +
      '<div><div class="blk-head"><h2>Motive de respingere</h2>' +
      '<span class="blk-note">coduri enumerate</span></div>' +
      '<div class="blk-body">' + (rejRows.length ? barRows(rejRows) :
        '<div class="empty">Niciun ordin respins.</div>') + '</div></div></section>';

    // --- orders ---
    var orderHtml = XE_ORDERS.length ? '<table class="data"><thead><tr><th>Ordin</th>' +
      '<th>Instrument</th><th>Sens</th><th class="r">Cantitate</th><th class="r">Umplut</th>' +
      '<th>Tip</th><th>Stare</th><th>Broker</th></tr></thead><tbody>' +
      XE_ORDERS.map(function (o) {
        var col = XSTATE_COLOR[o.state] || "var(--muted)";
        return '<tr class="rowlink" onclick="MLGo(\'execution\',\'' + esc(o.order_id) + '\')">' +
          '<td class="mono" style="font-size:10px;">' + esc(o.order_id) + '</td>' +
          '<td class="mono">' + esc(o.instrument_id) + '</td>' +
          '<td>' + esc(o.side) + '</td>' +
          '<td class="r">' + fmtNum(o.quantity, 4) + '</td>' +
          '<td class="r">' + fmtNum(o.filled, 4) + '</td>' +
          '<td>' + esc(o.order_type) + '</td>' +
          '<td><span class="xstate" style="color:' + col + ';">' + esc(o.state) + '</span></td>' +
          '<td class="mono">' + esc(o.broker_id) + '</td></tr>';
      }).join("") + '</tbody></table>'
      : '<div class="empty">Niciun ordin inregistrat.</div>';
    html += blk("Ordine", "apasa un rand pentru lantul complet", orderHtml);

    // --- one order in full, when selected ---
    if (state.param && (XE.detail || {})[state.param]) {
      html += xOrderDetail(state.param);
    }

    // --- per-broker detail, including IBKR ---
    html += xBrokerDetail();

    // --- the operations centre (Phase 16) ---
    html += xOperations();

    // --- instrument mappings ---
    var maps = XE.mappings || [];
    var mapHtml = maps.length ? '<table class="data"><thead><tr><th>Instrument canonic</th>' +
      '<th>Broker</th><th>Simbol la broker</th><th>Clasa</th><th class="r">Increment</th>' +
      '<th class="r">Minim</th></tr></thead><tbody>' + maps.map(function (m) {
        return '<tr><td class="mono">' + esc(m.instrument_id) + '</td>' +
          '<td class="mono">' + esc(m.broker_id) + '</td>' +
          '<td class="mono"><b>' + esc(m.symbol) + '</b></td>' +
          '<td>' + esc(m.asset_class) + '</td>' +
          '<td class="r">' + fmtNum(m.increment, 4) + '</td>' +
          '<td class="r">' + fmtNum(m.minimum, 4) + '</td></tr>';
      }).join("") + '</tbody></table>'
      : '<div class="empty">Nicio corespondenta inregistrata.</div>';
    html += blk("Corespondenta instrumentelor",
      "nucleul nu invata niciodata simbolul unui broker", mapHtml);

    // --- reconciliation ---
    var recs = XE.reconciliations || [];
    var recHtml = recs.length ? recs.map(function (r) {
      var col = r.clean ? "var(--up)" : "var(--accent-dark)";
      return '<div style="border-bottom:1px solid var(--line);padding:8px 0;font-size:11.5px;">' +
        '<span class="xstate" style="color:' + col + ';">' +
        (r.clean ? "curata" : (r.mismatches || []).length + " neconcordante") + '</span> ' +
        esc(fmtDate(r.at)) + ' · <span class="mono">' + esc(r.broker_id) + '</span> · ' +
        r.checks + ' verificari' +
        ((r.mismatches || []).length ? '<ul style="margin:6px 0 0 16px;color:var(--muted);">' +
          r.mismatches.slice(0, 6).map(function (m) {
            return '<li>[' + esc(m.kind) + '] ' + esc(m.detail) + '</li>';
          }).join("") + '</ul>' : "") + '</div>';
    }).join("") : '<div class="empty">Nicio reconciliere inregistrata.</div>';
    html += blk("Reconciliere",
      "starea noastra comparata cu a brokerului; nimic nu se rescrie in tacere", recHtml);

    // --- errors and audit ---
    var errs = XE.errors || [];
    var errHtml = errs.length ? errs.map(function (e) {
      return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:11.5px;">' +
        '<span class="mono" style="font-weight:700;">' + esc(e.code) + '</span> · ' +
        esc(fmtDate(e.at)) + '<div style="color:var(--muted);">' + esc(e.message) + '</div></div>';
    }).join("") : '<div class="empty">Nicio eroare de executie.</div>';

    var audit = XE.audit || [];
    var auditHtml = audit.length ? audit.map(function (a) {
      return '<div style="border-bottom:1px solid var(--line);padding:6px 0;font-size:11.5px;">' +
        '<span class="mono" style="font-weight:700;">' + esc(a.action) + '</span> · ' +
        esc(a.actor) + ' · ' + esc(fmtDate(a.at)) +
        (a.detail ? '<div style="color:var(--muted);">' + esc(a.detail) + '</div>' : "") +
        '</div>';
    }).join("") : '<div class="empty">Niciun eveniment de audit.</div>';

    html += '<section class="blk"><div class="grid2">' +
      '<div><div class="blk-head"><h2>Erori de executie</h2>' +
      '<span class="blk-note">structurate, nu text liber</span></div>' +
      '<div class="blk-body">' + errHtml + '</div></div>' +
      '<div><div class="blk-head"><h2>Jurnal de audit</h2>' +
      '<span class="blk-note">append-only</span></div>' +
      '<div class="blk-body">' + auditHtml + '</div></div></section>';

    // --- the boundary, restated ---
    html += blk("Limita fazei", "",
      '<div class="callout"><b>Faza 14 construieste granita, nu un broker.</b>' +
      '<p>Nucleul se opreste la <span class="mono">OrderIntent</span>. Sub el, un ' +
      'singur adaptor per loc de executie traduce catre si dinspre tipurile canonice. ' +
      'Nimic din strategie, semnale, portofoliu sau risc nu stie cu ce broker vorbeste.</p>' +
      '<p>Interactive Brokers este implementat pe aceasta interfata (Faza 15). ' +
      'Proiectul e IBKR-only: nu exista alt broker planificat. ' +
      '<span class="mono">ExecutionEnvironment.LIVE</span> ramane refuzat ' +
      'structural — nu se poate construi un cont, un broker sau un gateway pe el.' +
      '</p></div>');

    return html;
  }

  // ============================================================
  // Operations centre (Faza 16, spec 88-93)
  // ============================================================
  //
  // Regula acestui panou: nimic nu se afiseaza verde pe date
  // absente. O masuratoare care lipseste se scrie "nemasurat" si
  // blocheaza, pentru ca un ecran de operatiuni care arata increzator
  // cand instrumentatia a cazut e mai rau decat niciun ecran.

  function opsStateColor(state) {
    if (state === "healthy" || state === "active") return "var(--up)";
    if (state === "degraded" || state === "paused") return "var(--accent-dark)";
    if (state === "unknown") return "var(--muted)";
    return "var(--down)";
  }

  function xOperations() {
    var OP = D.operations || {};
    if (!OP.available) {
      return blk("Centru de operatiuni", "faza 16",
        '<div class="empty">Nicio sesiune de tranzactionare inregistrata. ' +
        'Tabelele de guvernanta se creeaza la prima rulare a ' +
        '<span class="mono">scripts/run_operations.py</span>.</div>');
    }

    var html = "";

    // --- sesiunea activa -------------------------------------
    var a = OP.active;
    if (a) {
      var pf = (a.preflight || []);
      var pfHtml = pf.length
        ? '<table class="data"><thead><tr><th>Verificare</th><th>Rezultat</th>' +
          '<th>Detaliu</th></tr></thead><tbody>' + pf.map(function (c) {
            var measured = c.measured !== false;
            var label = !measured ? "NEMASURAT" : (c.passed ? "OK" : "BLOCAT");
            var color = !measured ? "var(--muted)"
                      : (c.passed ? "var(--up)" : "var(--down)");
            return '<tr><td class="mono">' + esc(c.name || "—") + '</td>' +
              '<td style="color:' + color + ';font-weight:700;">' + label + '</td>' +
              '<td style="font-size:11.5px;">' + esc(c.detail || "—") + '</td></tr>';
          }).join("") + '</tbody></table>'
        : '<div class="empty">Niciun preflight inregistrat.</div>';

      var evHtml = (OP.events || []).length
        ? (OP.events || []).map(function (e) {
            return '<div style="font-size:11.5px;padding:3px 0;">' +
              '<span style="color:var(--muted);">' + esc(fmtDate(e.at)) + '</span> · ' +
              '<b>' + esc(e.action) + '</b> · ' + esc(e.actor) +
              (e.from_state ? ' · <span class="mono">' + esc(e.from_state) +
                ' → ' + esc(e.to_state) + '</span>' : '') +
              (e.reason ? ' · ' + esc(e.reason) : '') + '</div>';
          }).join("")
        : '<div class="empty">Niciun eveniment.</div>';

      html += blk("Sesiune activa",
        "configuratia e inghetata cat timp sesiunea ruleaza",
        '<div style="border:2px solid var(--line-strong);padding:14px 16px;">' +
        '<div style="display:flex;justify-content:space-between;gap:10px;' +
        'flex-wrap:wrap;align-items:baseline;">' +
        '<span class="mono" style="font-weight:800;font-size:14px;">' +
        esc(a.session_id) + '</span>' +
        '<span class="xstate" style="color:' + opsStateColor(a.state) + ';">' +
        esc((a.state || "").toUpperCase()) + '</span></div>' +
        '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:5px;">' +
        esc(a.operator) + ' · ' + esc(a.broker_id) + ' · ' + esc(a.account_id) +
        ' · ' + esc((a.environment || "").toUpperCase()) +
        ' · nivel ' + fmtNum(a.level) + '</div>' +
        '<div class="mono" style="font-size:10.5px;color:var(--muted);margin-top:3px;">' +
        'amprenta configuratie: ' + esc(a.fingerprint || "—") + '</div>' +
        '<div style="margin-top:12px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Preflight</div>' +
        pfHtml +
        '<div style="margin-top:12px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Istoric</div>' +
        evHtml + '</div>');
    } else {
      html += blk("Sesiune activa", "",
        '<div class="empty">Nicio sesiune activa. Ordinele nu pot fi ' +
        'trimise in afara unei sesiuni deschise explicit.</div>');
    }

    // --- sanatatea sistemului --------------------------------
    var hh = OP.health || [];
    html += blk("Sanatatea sistemului",
      "agregatul e cea mai proasta citire, niciodata o medie",
      hh.length
        ? '<table class="data"><thead><tr><th>Capabilitate</th><th>Stare</th>' +
          '<th class="r">Latenta</th><th class="r">Vechime</th><th>Detaliu</th>' +
          '</tr></thead><tbody>' + hh.map(function (h) {
            return '<tr><td class="mono">' + esc(h.capability) + '</td>' +
              '<td style="color:' + opsStateColor(h.state) +
              ';font-weight:700;">' + esc((h.state || "").toUpperCase()) + '</td>' +
              '<td class="r">' + (h.latency_ms == null ? '—' : fmtNum(h.latency_ms, 0) + ' ms') + '</td>' +
              '<td class="r">' + (h.age_seconds == null ? '—' : fmtNum(h.age_seconds, 0) + ' s') + '</td>' +
              '<td style="font-size:11.5px;">' + esc(h.detail || "—") + '</td></tr>';
          }).join("") + '</tbody></table>'
        : '<div class="empty">Nicio citire de sanatate inregistrata. ' +
          'Absenta unei masuratori nu e o stare buna — blocheaza.</div>');

    // --- limite si alerte ------------------------------------
    var br = OP.breaches || [];
    html += blk("Limite depasite",
      "cele cu zavor nu se sterg singure; cer o reactivare umana",
      br.length
        ? '<table class="data"><thead><tr><th>Limita</th><th>Zavor</th>' +
          '<th>Cand</th><th>Detaliu</th></tr></thead><tbody>' +
          br.map(function (b) {
            return '<tr><td class="mono">' + esc(b.limit_name) + '</td>' +
              '<td style="color:' + (b.latched ? 'var(--down)' : 'var(--muted)') +
              ';font-weight:700;">' + (b.latched ? 'ZAVORAT' : 'nu') + '</td>' +
              '<td class="mono" style="font-size:11px;">' + esc(fmtDate(b.at)) + '</td>' +
              '<td style="font-size:11.5px;">' + esc(b.detail || "—") + '</td></tr>';
          }).join("") + '</tbody></table>'
        : '<div class="empty">Nicio limita depasita neridicata.</div>');

    var al = OP.alerts || [];
    if (al.length) {
      html += blk("Alerte deschise", "necontirmate",
        '<table class="data"><thead><tr><th>Severitate</th><th>Cod</th>' +
        '<th>Mesaj</th><th>Cand</th></tr></thead><tbody>' +
        al.map(function (x) {
          var crit = x.severity === "critical" || x.severity === "error";
          return '<tr><td style="color:' + (crit ? 'var(--down)' : 'var(--accent-dark)') +
            ';font-weight:700;">' + esc((x.severity || "").toUpperCase()) + '</td>' +
            '<td class="mono">' + esc(x.code) + '</td>' +
            '<td style="font-size:11.5px;">' + esc(x.message || "—") + '</td>' +
            '<td class="mono" style="font-size:11px;">' + esc(fmtDate(x.at)) + '</td></tr>';
        }).join("") + '</tbody></table>');
    }

    // --- guvernanta: nivel, aprobari, pregatire --------------
    var pr = OP.promotions || [];
    var rd = OP.readiness;
    var govHtml = '<div class="callout"><b>Executia cu bani reali nu are cale in cod.</b>' +
      '<p>Nivelurile 4 si peste sunt specificate si controlate prin porti, dar ' +
      'niciunul nu e implementat: niciun adaptor nu accepta un mediu cu bani reali. ' +
      'Aprobarea unui nivel neimplementat se inregistreaza, iar nivelul efectiv ' +
      'coboara la cel mai inalt nivel <i>implementat</i>.</p></div>';

    if (pr.length) {
      govHtml += '<table class="data"><thead><tr><th>Nivel</th><th>Stare</th>' +
        '<th>Cerut de</th><th>Aprobat de</th><th>Expira</th></tr></thead><tbody>' +
        pr.map(function (p) {
          var approved = p.state === "approved";
          return '<tr><td>' + fmtNum(p.level) + ' · ' + esc(p.level_label || "—") + '</td>' +
            '<td style="color:' + (approved ? 'var(--up)' : 'var(--muted)') +
            ';font-weight:700;">' + esc((p.state || "").toUpperCase()) + '</td>' +
            '<td class="mono">' + esc(p.requested_by) + '</td>' +
            '<td class="mono">' + esc(p.approved_by || "—") + '</td>' +
            '<td class="mono" style="font-size:11px;">' +
            esc(p.expires_at ? fmtDate(p.expires_at) : "—") + '</td></tr>';
        }).join("") + '</tbody></table>';
    } else {
      govHtml += '<div class="empty">Nicio cerere de promovare. ' +
        'Nivelul implicit este PAPER.</div>';
    }

    if (rd) {
      govHtml += '<div style="margin-top:12px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">' +
        'Pregatire (' + esc(fmtDate(rd.at)) + ')</div>' +
        '<div style="font-size:11.5px;line-height:1.9;">' +
        Object.keys(rd.verdicts || {}).map(function (k) {
          var v = rd.verdicts[k];
          var color = v === "pass" ? "var(--up)"
                    : v === "unknown" ? "var(--muted)" : "var(--down)";
          return '<span class="mono">' + esc(k) + '</span>: ' +
            '<b style="color:' + color + ';">' + esc((v || "").toUpperCase()) + '</b>';
        }).join("<br>") + '</div>';
    }

    html += blk("Guvernanta executiei",
      "nivel, promovare cu patru ochi, pregatire", govHtml);

    // --- rezultate si calitatea executiei --------------------
    var q = OP.quality || {};
    var outs = OP.outcomes || [];
    function opsCell(label, value) {
      return '<div class="cell"><div class="n">' + value + '</div>' +
        '<div class="l">' + esc(label) + '</div></div>';
    }
    var qHtml = '<div class="statgrid" style="grid-template-columns:repeat(5,1fr);">' +
      opsCell("tranzactii inchise", fmtNum(q.closed || 0)) +
      opsCell("deschise", fmtNum(q.open || 0)) +
      opsCell("P&L net",
              q.net_pnl == null ? "nemasurat" : fmtNum(q.net_pnl, 2)) +
      opsCell("slippage median",
              q.median_slippage_bps == null ? "nemasurat"
              : fmtNum(q.median_slippage_bps, 1) + " bps") +
      opsCell("lineage complet", fmtNum(q.lineage_complete || 0)) +
      '</div>';

    qHtml += outs.length
      ? '<table class="data"><thead><tr><th>Instrument</th><th>Parte</th>' +
        '<th class="r">Cant.</th><th class="r">Intrare</th><th class="r">Iesire</th>' +
        '<th class="r">P&L net</th><th class="r">Slippage</th><th>Motiv iesire</th>' +
        '<th>Lineage</th></tr></thead><tbody>' +
        outs.slice(0, 30).map(function (o) {
          var pnl = o.net_pnl;
          return '<tr><td class="mono">' + esc(o.instrument_id) + '</td>' +
            '<td>' + esc(o.side) + '</td>' +
            '<td class="r">' + fmtNum(o.quantity, 2) + '</td>' +
            '<td class="r">' + fmtNum(o.entry_price, 2) + '</td>' +
            '<td class="r">' + (o.is_open ? '—' : fmtNum(o.exit_price, 2)) + '</td>' +
            '<td class="r" style="color:' +
            (pnl == null ? 'var(--muted)' : pnl > 0 ? 'var(--up)' : 'var(--down)') +
            ';font-weight:700;">' + (pnl == null ? '—' : fmtNum(pnl, 2)) + '</td>' +
            '<td class="r">' + (o.slippage_bps == null ? '—' : fmtNum(o.slippage_bps, 1)) + '</td>' +
            '<td style="font-size:11.5px;">' + esc(o.exit_reason || "—") + '</td>' +
            '<td style="color:' + (o.lineage_complete ? 'var(--up)' : 'var(--down)') +
            ';font-weight:700;">' + (o.lineage_complete ? 'complet' : 'incomplet') +
            '</td></tr>';
        }).join("") + '</tbody></table>'
      : '<div class="empty">Nicio tranzactie inregistrata.</div>';

    html += blk("Rezultate si calitatea executiei",
      "pretul deciziei, pretul trimis si pretul umplerii raman separate", qHtml);

    // --- semnale care nu au devenit tranzactii ---------------
    var ms = OP.missed || [];
    html += blk("Semnale ratate",
      "jumatatea de dovezi pe care un sistem care inregistreaza doar ce a facut o pierde",
      ms.length
        ? '<table class="data"><thead><tr><th>Instrument</th><th>Motiv</th>' +
          '<th>Oprit de sistem</th><th>Detaliu</th></tr></thead><tbody>' +
          ms.map(function (m) {
            return '<tr><td class="mono">' + esc(m.instrument_id) + '</td>' +
              '<td class="mono">' + esc(m.reason) + '</td>' +
              '<td style="font-weight:700;color:' +
              (m.prevented ? 'var(--accent-dark)' : 'var(--muted)') + ';">' +
              (m.prevented ? 'da' : 'nu — piata') + '</td>' +
              '<td style="font-size:11.5px;">' + esc(m.detail || "—") + '</td></tr>';
          }).join("") + '</tbody></table>'
        : '<div class="empty">Niciun semnal ratat inregistrat.</div>');

    // --- istoricul sesiunilor --------------------------------
    var ss = OP.sessions || [];
    if (ss.length) {
      html += blk("Istoric sesiuni", "nimic nu se sterge la inchidere",
        '<table class="data"><thead><tr><th>Sesiune</th><th>Stare</th>' +
        '<th>Operator</th><th>Deschisa</th><th>Inchisa</th><th>Motiv</th>' +
        '</tr></thead><tbody>' + ss.map(function (s) {
          return '<tr><td class="mono" style="font-size:11px;">' +
            esc(s.session_id) + '</td>' +
            '<td style="color:' + opsStateColor(s.state) + ';font-weight:700;">' +
            esc((s.state || "").toUpperCase()) + '</td>' +
            '<td class="mono">' + esc(s.operator) + '</td>' +
            '<td class="mono" style="font-size:11px;">' +
            esc(s.started_at ? fmtDate(s.started_at) : "—") + '</td>' +
            '<td class="mono" style="font-size:11px;">' +
            esc(s.ended_at ? fmtDate(s.ended_at) : "—") + '</td>' +
            '<td style="font-size:11.5px;">' + esc(s.termination_reason || "—") +
            '</td></tr>';
        }).join("") + '</tbody></table>');
    }

    return html;
  }

  function xBrokerDetail() {
    var BD = (D.broker_detail || {}).brokers || {};
    var ids = Object.keys(BD);
    if (!ids.length) return "";

    var blocks = ids.map(function (id) {
      var b = BD[id];
      var live = false;   // structurally: no adapter can trade real money
      var envLabel = (b.environment || "").toUpperCase();
      var badge = b.implemented
        ? '<span class="xstate" style="color:var(--up);">' + esc(envLabel) + ' · adaptor activ</span>'
        : '<span class="xstate" style="color:var(--muted);">fara adaptor</span>';

      var mapHtml = (b.mappings || []).length
        ? '<table class="data"><thead><tr><th>Instrument</th><th>Simbol</th>' +
          '<th>Identificator broker</th><th>Loc</th><th class="r">Increment</th>' +
          '</tr></thead><tbody>' + b.mappings.map(function (m) {
            var native = m.payload && m.payload.conid
              ? 'conid ' + esc(m.payload.conid) : '—';
            return '<tr><td class="mono">' + esc(m.instrument_id) + '</td>' +
              '<td class="mono"><b>' + esc(m.symbol) + '</b></td>' +
              '<td class="mono">' + native + '</td>' +
              '<td>' + esc(m.venue || m.payload && m.payload.primary_exchange || '—') + '</td>' +
              '<td class="r">' + fmtNum(m.increment, 4) + '</td></tr>';
          }).join("") + '</tbody></table>'
        : '<div class="empty">Niciun instrument rezolvat pentru acest broker.</div>';

      var acctHtml = (b.accounts || []).length
        ? b.accounts.map(function (a) {
            var paper = /^DU/i.test(a.account_id);
            return '<div style="font-size:11.5px;padding:3px 0;">' +
              '<span class="mono">' + esc(a.account_id) + '</span> · ' +
              esc(a.environment) + ' · ' + esc(a.currency) +
              (a.account_id && !paper && id === "ibkr"
                ? ' <span class="xstate" style="color:var(--accent-dark);">nu incepe cu DU</span>'
                : '') + '</div>';
          }).join("")
        : '<div class="empty">Niciun cont.</div>';

      var healthHtml = (b.health || []).length
        ? b.health.slice(0, 3).map(function (h) {
            return '<div style="font-size:11.5px;padding:3px 0;color:var(--muted);">' +
              esc(fmtDate(h.at)) + ' · <span class="mono">' + esc(h.state) + '</span>' +
              (h.detail ? ' · ' + esc(h.detail) : '') + '</div>';
          }).join("")
        : '<div class="empty">Nicio verificare inregistrata.</div>';

      return '<div style="border:2px solid ' +
        (b.implemented ? 'var(--line-strong)' : 'var(--border-mid)') +
        ';padding:14px 16px;margin-bottom:14px;">' +
        '<div style="display:flex;justify-content:space-between;gap:10px;' +
        'flex-wrap:wrap;align-items:baseline;">' +
        '<span style="font-weight:800;font-size:15px;">' + esc(b.name) + '</span>' +
        badge + '</div>' +
        '<div class="mono" style="font-size:11px;color:var(--muted);margin-top:5px;">' +
        esc(b.adapter || '—') + ' · ' + fmtNum(b.orders) + ' ordine · ' +
        fmtNum(b.fills) + ' executii</div>' +
        '<div style="margin-top:10px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Conturi</div>' +
        acctHtml +
        '<div style="margin-top:10px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Sanatate</div>' +
        healthHtml +
        '<div style="margin-top:10px;font-size:11px;font-weight:700;' +
        'letter-spacing:0.06em;text-transform:uppercase;color:var(--muted);">Instrumente rezolvate</div>' +
        mapHtml + '</div>';
    }).join("");

    return blk("Brokeri in detaliu",
      "identificatorii nativi (conid la IBKR) raman sub adaptor", blocks);
  }

  function xOrderDetail(orderId) {
    var d = (XE.detail || {})[orderId];
    var order = null;
    for (var i = 0; i < XE_ORDERS.length; i++) {
      if (XE_ORDERS[i].order_id === orderId) { order = XE_ORDERS[i]; break; }
    }
    if (!d || !order) return "";

    var chain = [
      ["model", order.model_version], ["semnal", order.signal_id],
      ["strategie", order.strategy_id], ["decizie", order.decision_id],
      ["intentie", order.intent_id], ["ordin", order.order_id],
      ["client order id", order.client_order_id],
      ["broker order id", order.broker_order_id],
      ["broker", order.broker_id], ["cont", order.account_id],
      ["mediu", order.environment], ["politica", order.policy],
      ["corelatie", order.correlation_id]
    ];
    var chainHtml = '<div class="mono" style="font-size:11.5px;line-height:1.9;">' +
      chain.map(function (c) {
        return esc(c[0]) + ': ' + (c[1] ? esc(c[1]) : '—');
      }).join("<br>") + '</div>';

    var stepsHtml = (d.states || []).map(function (t) {
      return '<div style="display:grid;grid-template-columns:24px 1fr 150px;gap:10px;' +
        'border-bottom:1px solid var(--line);padding:5px 0;font-size:11.5px;">' +
        '<span class="mono">' + t.seq + '</span>' +
        '<span><span class="mono">' + esc(t.from || "start") + '</span> &rarr; ' +
        '<span class="mono"><b>' + esc(t.to) + '</b></span>' +
        '<div style="color:var(--muted);">' + esc(t.reason) + '</div></span>' +
        '<span class="mono" style="text-align:right;">' + esc(fmtDate(t.at)) + '</span></div>';
    }).join("") || '<div class="empty">Niciun istoric de stare.</div>';

    var fillsHtml = (d.fills || []).length ? '<table class="data"><thead><tr>' +
      '<th class="r">Cantitate</th><th class="r">Pret</th><th class="r">Referinta</th>' +
      '<th class="r">Comision</th><th>Moment</th></tr></thead><tbody>' +
      d.fills.map(function (f) {
        return '<tr><td class="r">' + fmtNum(f.quantity, 4) + '</td>' +
          '<td class="r">' + fmtNum(f.price, 4) + '</td>' +
          '<td class="r">' + fmtNum(f.reference_price, 4) + '</td>' +
          '<td class="r">' + fmtNum(f.commission, 4) + '</td>' +
          '<td>' + esc(fmtDate(f.at)) + '</td></tr>';
      }).join("") + '</tbody></table>' : '<div class="empty">Nicio executie.</div>';

    return '<section class="blk"><div class="blk-head"><h2>Lantul ordinului</h2>' +
      '<span class="blk-note mono">' + esc(orderId) + '</span></div>' +
      '<div class="grid2">' +
      '<div class="blk-body">' + chainHtml + '</div>' +
      '<div class="blk-body">' + stepsHtml + '</div></div>' +
      '<div class="blk-body">' + fillsHtml + '</div></section>';
  }

  function viewStub(id) {
    var meta = STUB_META[id] || STUB_META.watchlist;
    var html = pageHead(meta.kicker, meta.title, [meta.stat]);
    html += '<section class="blk" style="border-bottom:0;"><div class="blk-body"><div class="hatched" style="max-width:72ch;"><div style="font-size:13px;line-height:1.55;color:#444141;">' + meta.body + '</div></div></div></section>';
    return html;
  }

  function renderNav() {
    var nav = document.getElementById("nav");
    var html = "";
    NAV.forEach(function (grp) {
      html += '<div class="nav-group"><div class="nav-group-label">' + esc(grp.label) + '</div>';
      grp.items.forEach(function (item) {
        var active = state.view === item.id || (item.id === "markets" && state.view === "company");
        html += '<div class="nav-item' + (active ? " active" : "") + (item.stub ? " stub" : "") + '" onclick="MLGo(\'' + item.id + '\')">' +
          '<span class="lbl">' + esc(item.label) + '</span><span class="tag">' + esc(item.tag) + '</span></div>';
      });
      html += '</div>';
    });
    html += '<div class="nav-note">Fiecare cifra provine dintr-o interogare reala pe baza de date curenta — niciuna nu e inventata.</div>';
    nav.innerHTML = html;
  }

  var STUB_IDS = ["watchlist", "research", "features"];

  function render() {
    renderNav();
    document.getElementById("ml-lastrun").innerHTML = "<b>Ultima actualizare</b><br>" + esc(D.meta.generated_at);
    // The paper-mode chip follows the workspace: shown only where
    // simulated numbers are on screen, so it never becomes a label
    // people learn to ignore.
    var tag = document.getElementById("ml-papertag");
    if (tag) tag.className = "papertag" + (state.view === "paper" ? "" : " hidden");
    var main = document.getElementById("main");
    var v = state.view;
    if (v === "markets-sector") { state.view = "markets"; v = "markets"; state.mktSector = state.param || ""; }
    if (v === "overview") main.innerHTML = viewOverview();
    else if (v === "markets") main.innerHTML = viewMarkets();
    else if (v === "company") main.innerHTML = viewCompany(state.param);
    else if (v === "sectors") main.innerHTML = viewSectors();
    else if (v === "news") main.innerHTML = viewNews();
    else if (v === "events") main.innerHTML = viewEvents(state.param);
    else if (v === "signals") main.innerHTML = viewSignals();
    else if (v === "outcomes") main.innerHTML = viewOutcomes();
    else if (v === "models") main.innerHTML = viewModels();
    else if (v === "outcomes") main.innerHTML = viewOutcomes();
    else if (v === "portfolio") main.innerHTML = viewPortfolio();
    else if (v === "risk") main.innerHTML = viewRisk();
    else if (v === "backtests") main.innerHTML = viewBacktests();
    else if (v === "paper") main.innerHTML = viewPaper();
    else if (v === "execution") main.innerHTML = viewExecution();
    else if (STUB_IDS.indexOf(v) !== -1) main.innerHTML = viewStub(v);
    else main.innerHTML = viewOverview();
    document.title = "MarketLens Terminal";
    window.scrollTo(0, 0);
  }

  window.addEventListener("hashchange", function () { parseHash(); render(); });
  parseHash();
  render();
})();
</script>
</body>
</html>"""
