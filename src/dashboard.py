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

        total_articles = _scalar(conn, "SELECT COUNT(*) FROM articles", default=0) if _table_exists(conn, "articles") else 0
        linked_articles = _scalar(
            conn, "SELECT COUNT(DISTINCT article_id) FROM article_entities", default=0
        ) if _table_exists(conn, "article_entities") else 0
        sources = _scalar(conn, "SELECT COUNT(DISTINCT source) FROM articles WHERE source IS NOT NULL", default=0) \
            if _table_exists(conn, "articles") else 0
        latest_article = _scalar(conn, "SELECT MAX(published_at) FROM articles") if _table_exists(conn, "articles") else None
        return {
            "size_bytes": size_bytes,
            "total_articles": total_articles,
            "linked_articles": linked_articles,
            "entity_coverage": (linked_articles / total_articles) if total_articles else None,
            "sources": sources,
            "latest_article": latest_article,
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
        if not _table_exists(conn, "trained_models"):
            return {"available": False}
        models = _rows(conn, """
            SELECT m.model_qualified_id, m.label_name, m.train_sample_size, m.train_cluster_count,
                   e.small_sample, e.beats_all_baselines, e.metrics_json
            FROM trained_models m LEFT JOIN model_evaluations e ON e.trained_model_id = m.trained_model_id
            ORDER BY m.trained_at DESC LIMIT 20
        """)
        predictions = _scalar(conn, "SELECT COUNT(*) FROM predictions", default=0) if _table_exists(conn, "predictions") else 0
        return {"available": True, "models": models, "predictions": predictions}

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
            SELECT signal_id, instrument_id, direction, status, strength, confidence,
                   expected_return, source_information_cutoff
            FROM signals ORDER BY source_information_cutoff DESC LIMIT 40
        """)
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

    def _collect_rec_index(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        """entity -> latest recommendation, across the FULL history table (every
        entity that has ever received one) — used to power the Markets 'call'
        column and the Company page's last-call panel."""
        if not _table_exists(conn, "recommendations"):
            return {}
        latest = _rows(conn, """
            SELECT entity, ticker, recommendation, confidence_score, time_horizon, generated_at
            FROM recommendations r
            WHERE generated_at = (
                SELECT MAX(generated_at) FROM recommendations r2 WHERE r2.entity = r.entity
            )
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
        legacy = self._collect_legacy(conn, watchlist)
        rec_index = self._collect_rec_index(conn)

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
            "legacy": legacy,
            "rec_index": rec_index,
            "current_recs": current_recs_by_entity,
            "universe": universe,
            "sector_summary": sector_summary,
            "unmapped": unmapped,
            "lexicon": lexicon,
            "market_data": market_data or None,
            "risk_data": risk_data or None,
            "price_history": price_history_map or None,
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
  // navigation state
  // ---------------------------------------------------------------
  var STUB_META = {
    watchlist: { kicker: "Sectiune", title: "Watchlist", stat: [String(D.meta.watchlist_count), "companii urmarite"],
      body: D.meta.watchlist_count
        ? "Companiile urmarite (" + D.meta.watchlist_count + ") sunt pinuite pe pagina Rezumat de mai sus fiecare data cand pipeline-ul zilnic ruleaza cu un fisier watchlist.txt prezent. O vedere dedicata (alerte, comparatie side-by-side) este un punct de extindere viitor, nu o functie neimplementata azi."
        : "Nicio companie urmarita momentan. Adauga nume de companii (unul pe linie) in watchlist.txt la radacina repo-ului, iar urmatoarea rulare zilnica le va pinui pe pagina Rezumat." },
    portfolio: { kicker: "Sectiune", title: "Portofoliu", stat: [D.portfolio_result ? String(D.portfolio_result.trades_simulated) : "0", "tranzactii simulate"],
      body: D.portfolio_result
        ? "Simularea de portofoliu curenta are " + D.portfolio_result.trades_simulated + " tranzactii, pe baza recomandarilor deja verificate prin Backtest Engine. O pagina dedicata (alocare, expunere pe sector, P&L per pozitie) este urmatorul pas — datele brute exista deja in portfolio_snapshots."
        : "Nicio simulare de portofoliu disponibila inca — necesita recomandari deja verificate prin Backtest Engine. Portofoliul real (holdings, alocare, expunere, P&L) este planificat pentru o faza ulterioara (MT5 / Interactive Brokers) si nu exista inca in pipeline." },
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
      { id: "signals", label: "Semnale", tag: D.signals.available ? fmtNum(D.signals.total) : "0" }
    ]},
    { label: "Performanta", items: [
      { id: "outcomes", label: "Rezultate", tag: D.legacy.available ? fmtNum(D.legacy.checked) : "0" },
      { id: "models", label: "Modele", tag: D.models.available ? String(D.models.models.length) : "0" },
      { id: "research", label: "Cercetare", tag: D.research.available ? fmtNum(D.research.total) : "0", stub: true },
      { id: "portfolio", label: "Portofoliu", tag: D.portfolio_result ? String(D.portfolio_result.trades_simulated) : "0", stub: true }
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
        return '<tr><td style="font-weight:700;">' + esc(s[1]) + '</td><td><span class="pill outline-up">' + esc(s[2]) + '</span></td><td class="r">' + fmtNum(s[4], 2) + '</td><td class="r" style="color:var(--accent-dark);font-weight:700;">' + fmtNum(s[5], 2) + '</td><td class="r">' + fmtSignedPct(s[6]) + '</td></tr>';
      }).join("");
      var activeCount = 0; D.signals.by_status.forEach(function (p) { if (p[0] === "active") activeCount = p[1]; });
      html += '<section class="blk"><div class="grid32">' +
        '<div><div class="blk-head"><h2>Semnale recente</h2><span class="blk-note warn">' + activeCount + ' / ' + D.signals.total + ' active</span></div>' +
        '<table class="data"><thead><tr><th>Instrument</th><th>Directie</th><th class="r">Forta</th><th class="r">Incredere</th><th class="r">R. asteptat</th></tr></thead><tbody>' + (sigRows || '<tr><td colspan="5" class="empty">Niciun semnal</td></tr>') + '</tbody></table></div>' +
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
      priceHtml = '<div style="font-size:28px;font-weight:800;">' + fmtNum(mkt.current_price, 2) + '</div>' +
        '<div style="font-size:13px;font-weight:700;color:' + (up ? "#00795a" : "#ae1800") + ';">' + fmtSignedPct(chg / 100, 2) + ' azi</div>' +
        (hist ? sparkline(hist, 400, 60) : "");
    } else {
      priceHtml = '<div class="hatched" style="text-align:center;"><span class="mono" style="font-size:11px;color:var(--muted);">price_candle_cache — indisponibil</span></div>' +
        '<p class="copy" style="margin-top:12px;">Pretul live nu este persistat in baza de date pentru acest instrument in aceasta rulare.</p>';
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
      return String(s[1]).replace("crypto-", "").toUpperCase() === sel.t.toUpperCase();
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
      return pageHead("Inteligenta · semnale", "Semnale", null) + blk("Fara date", null, '<div class="empty">Faza 10 nu a rulat inca pe aceasta baza de date.</div>');
    }
    var activeCount = 0; D.signals.by_status.forEach(function (p) { if (p[0] === "active") activeCount = p[1]; });
    var html = pageHead("Centrul de semnale", "Semnale", [["Active", activeCount + " / " + D.signals.total]]);

    var sig = D.signals.recent[state.sigIdx] || D.signals.recent[0];
    var rows = D.signals.recent.map(function (s, idx) {
      var active = idx === state.sigIdx;
      return '<tr class="rowlink' + (active ? " sel" : "") + '" onclick="MLSetSigIdx(' + idx + ')"><td style="font-weight:700;">' + esc(s[1]) + '</td><td><span class="pill outline-up">' + esc(s[2]) + '</span></td><td class="r">' + fmtNum(s[4], 2) + '</td><td class="r" style="color:var(--accent-dark);font-weight:700;">' + fmtNum(s[5], 2) + '</td><td class="r">' + fmtSignedPct(s[6]) + '</td></tr>';
    }).join("");

    var detail = "";
    if (sig) {
      detail = '<div style="font-size:20px;font-weight:800;letter-spacing:-0.02em;margin-bottom:2px;">' + esc(sig[1]) + '</div>' +
        '<div style="font-size:11px;color:var(--muted);" class="mono">cutoff ' + esc(sig[7] || "—") + '</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 0;margin-top:12px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);">' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtNum(sig[4], 2) + '</div><div style="font-size:10px;color:var(--muted);">forta</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;color:var(--accent-dark);">' + fmtNum(sig[5], 2) + '</div><div style="font-size:10px;color:var(--muted);">incredere</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtSignedPct(sig[6]) + '</div><div style="font-size:10px;color:var(--muted);">r. asteptat</div></div></div>' +
        '<div style="padding:14px 0;"><div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px;">stare</div>' +
        '<span class="pill" style="border:1px solid var(--line-strong);">' + esc(sig[3]) + '</span></div>';
    }

    html += '<section class="blk"><div class="grid32">' +
      '<div><table class="data"><thead><tr><th>Instrument</th><th>Directie</th><th class="r">Forta</th><th class="r">Incredere</th><th class="r">R. asteptat</th></tr></thead><tbody>' + (rows || '<tr><td colspan="5" class="empty">Niciun semnal</td></tr>') + '</tbody></table></div>' +
      '<div><div class="blk-head"><h2>Detaliu semnal</h2></div><div class="blk-body">' + detail + '</div></div>' +
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
    var html = pageHead("Performanta · track record", "Rezultate", [["Emise", fmtNum(D.legacy.total_recs)]]);

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

  function viewModels() {
    if (!D.models.available) {
      return pageHead("Performanta · modele", "Modele", null) + blk("Fara date", null, '<div class="empty">Faza 9 nu a rulat inca pe aceasta baza de date.</div>');
    }
    var html = pageHead("Performanta · inteligenta modelelor", "Modele", [["Antrenate", String(D.models.models.length)], ["Predictii", fmtNum(D.models.predictions)]]);

    var cards = D.models.models.map(function (m) {
      var qid = m[0], label = m[1], n = m[2], clusters = m[3], small = m[4], beats = m[5], metricsJson = m[6];
      var metrics = {}; try { metrics = metricsJson ? JSON.parse(metricsJson) : {}; } catch (e) {}
      var verdict = beats ? '<span class="pill" style="border:1px solid #00795a;color:#00795a;">bate baseline</span>' : (beats === 0 ? '<span class="pill" style="border:1px solid #ae1800;color:#ae1800;">nu bate baseline</span>' : "");
      return '<div class="regcard" style="margin-bottom:14px;"><div class="rc-top"><span class="rc-key">' + esc(qid) + '</span>' + (small ? '<span class="pill" style="border:1px solid var(--accent);color:var(--accent-dark);">esantion mic</span>' : '') + '</div>' +
        '<div style="font-size:11px;color:var(--muted);" class="mono">' + esc(label) + '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);">' +
        '<div><div style="font-size:17px;font-weight:800;">' + fmtNum(n) + '</div><div style="font-size:10px;color:var(--muted);">N antrenare</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;">' + (clusters === null ? "—" : fmtNum(clusters)) + '</div><div style="font-size:10px;color:var(--muted);">clustere</div></div>' +
        '<div><div style="font-size:17px;font-weight:800;">' + (metrics.mae !== undefined ? Number(metrics.mae).toFixed(4) : "—") + '</div><div style="font-size:10px;color:var(--muted);">MAE</div></div></div>' +
        (verdict ? '<div style="margin-top:12px;">' + verdict + '</div>' : "") + '</div>';
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

  var STUB_IDS = ["watchlist", "portfolio", "research", "features"];

  function render() {
    renderNav();
    document.getElementById("ml-lastrun").innerHTML = "<b>Ultima actualizare</b><br>" + esc(D.meta.generated_at);
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
