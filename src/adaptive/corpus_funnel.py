"""
src/adaptive/corpus_funnel.py
--------------------------------------------------
Phase 25.9I-F.7: measure where historical articles stopped becoming
events, in one unit, from real records.

Read-only. Every connection this module is handed may be opened
`mode=ro`; nothing here writes, and nothing here reads a label value.

WHY THE FUNNEL IS COUNTED IN ARTICLES
-------------------------------------
The stages change unit -- 48,392 articles become 846 event reports
become 581 canonical events. A funnel that switches unit mid-way cannot
reconcile, and a drop of 22,679 "records" between entity resolution and
extraction would look catastrophic when most of it is simply news that
reports no event.

So the primary funnel counts **distinct articles that reached each
stage**. That reconciles exactly, and a drop is then always a real
answer to "how many articles stopped here". Record counts per stage are
reported separately, as counts, not as a funnel.

THE CUTOFFS ARE MEASURED, NEVER ASSUMED
---------------------------------------
`entity_cutoff()` and `extraction_cutoff()` read the earliest real
record at each stage. No date in this module is written down as a
constant, because the whole point of the phase is that a date constant
was what went wrong.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.corpus_recovery_models import (
    FunnelStage, HistoricalFunnel, LossAccounting, LossReason, PipelineStage,
)

UTC = timezone.utc

#: An article row whose mention columns hold one of these has nothing
#: for the resolver to work with. Checked in SQL so the classification
#: and the recovery estimate agree.
EMPTY_JSON = ("", "[]", "null")

_MENTIONS_PRESENT = (
    "((companies_mentioned IS NOT NULL AND companies_mentioned NOT IN ('', '[]', 'null'))"
    " OR (tickers_mentioned IS NOT NULL AND tickers_mentioned NOT IN ('', '[]', 'null')))"
)


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


def _scalar(conn: sqlite3.Connection, sql: str,
            params: Sequence[object] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0] or 0) if row else 0


def _range(conn: sqlite3.Connection, sql: str,
           params: Sequence[object] = ()) -> Tuple[Optional[datetime], Optional[datetime]]:
    row = conn.execute(sql, tuple(params)).fetchone()
    if not row:
        return None, None
    return _parse(row[0]), _parse(row[1])


# ======================================================================
# Measured cutoffs
# ======================================================================

def entity_cutoff(conn: sqlite3.Connection) -> Optional[datetime]:
    """
    The publication instant of the earliest article that carries an
    entity link. Measured, not assumed: this is the boundary the
    recovery has to cross, and reading it from the data is what proves
    it is a pipeline boundary rather than a property of the news.
    """
    row = conn.execute("""
        SELECT MIN(a.published_at)
        FROM articles a
        JOIN article_entities e ON e.article_id = a.article_id
    """).fetchone()
    return _parse(row[0]) if row else None


def extraction_cutoff(conn: sqlite3.Connection) -> Optional[datetime]:
    """Publication instant of the earliest extracted event report."""
    row = conn.execute("SELECT MIN(publication_time) FROM events").fetchone()
    return _parse(row[0]) if row else None


def canonical_cutoff(conn: sqlite3.Connection) -> Optional[datetime]:
    row = conn.execute("SELECT MIN(first_reported_at) FROM canonical_events").fetchone()
    return _parse(row[0]) if row else None


def observation_cutoff(conn: sqlite3.Connection) -> Optional[datetime]:
    row = conn.execute(
        "SELECT MIN(information_cutoff) FROM research_observations").fetchone()
    return _parse(row[0]) if row else None


# ======================================================================
# The article-unit funnel
# ======================================================================

#: Each stage: (SQL counting distinct articles that reached it,
#: SQL for that stage's own temporal range over the same population).
_STAGE_SQL: Dict[PipelineStage, str] = {
    PipelineStage.RAW_ARTICLE:
        "SELECT COUNT(*) FROM articles a WHERE 1=1 {where}",
    PipelineStage.NORMALIZED_ARTICLE:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN news_articles n ON n.article_id = a.article_id WHERE 1=1 {where}",
    PipelineStage.ENTITY_RESOLVED:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN article_entities e ON e.article_id = a.article_id WHERE 1=1 {where}",
    PipelineStage.EVENT_REPORT:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN event_evidence v ON v.article_id = a.article_id WHERE 1=1 {where}",
    PipelineStage.FUSED:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN event_evidence v ON v.article_id = a.article_id "
        "JOIN canonical_event_reports r ON r.report_id = v.event_id WHERE 1=1 {where}",
    PipelineStage.CANONICAL_EVENT:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN event_evidence v ON v.article_id = a.article_id "
        "JOIN canonical_event_reports r ON r.report_id = v.event_id "
        "JOIN canonical_events c ON c.canonical_event_id = r.canonical_event_id "
        "WHERE 1=1 {where}",
    PipelineStage.RESEARCH_OBSERVATION:
        "SELECT COUNT(DISTINCT a.article_id) FROM articles a "
        "JOIN event_evidence v ON v.article_id = a.article_id "
        "JOIN canonical_event_reports r ON r.report_id = v.event_id "
        "JOIN research_observations o ON o.event_id = r.canonical_event_id "
        "WHERE 1=1 {where}",
}

_STAGE_RANGE_SQL: Dict[PipelineStage, str] = {
    stage: sql.replace("SELECT COUNT(DISTINCT a.article_id)",
                       "SELECT MIN(a.published_at), MAX(a.published_at)")
               .replace("SELECT COUNT(*)",
                        "SELECT MIN(a.published_at), MAX(a.published_at)")
    for stage, sql in _STAGE_SQL.items()
}

_STAGE_NOTE: Dict[PipelineStage, str] = {
    PipelineStage.RAW_ARTICLE: "articles table; raw_articles is empty in this corpus",
    PipelineStage.NORMALIZED_ARTICLE: "present in news_articles",
    PipelineStage.ENTITY_RESOLVED: "has at least one row in article_entities",
    PipelineStage.EVENT_REPORT: "cited as evidence by at least one event",
    PipelineStage.FUSED: "its event entered a fusion cluster",
    PipelineStage.CANONICAL_EVENT: "its cluster produced a canonical event",
    PipelineStage.RESEARCH_OBSERVATION: "its canonical event has a research observation",
}


def _window(period_start: Optional[datetime],
            period_end: Optional[datetime]) -> Tuple[str, List[object]]:
    clauses: List[str] = []
    params: List[object] = []
    if period_start is not None:
        clauses.append("AND a.published_at >= ?")
        params.append(period_start.isoformat())
    if period_end is not None:
        clauses.append("AND a.published_at < ?")
        params.append(period_end.isoformat())
    return " ".join(clauses), params


def measure_funnel(conn: sqlite3.Connection, *,
                   period_start: Optional[datetime] = None,
                   period_end: Optional[datetime] = None,
                   period_label: str = "",
                   measured_at: Optional[datetime] = None) -> HistoricalFunnel:
    """
    Count distinct articles reaching each stage, optionally restricted
    to a publication window.

    Each stage's `count_entering` is the previous stage's `count_exiting`
    by construction, so the funnel reconciles and a drop is always
    attributable to exactly one stage.
    """
    where, params = _window(period_start, period_end)
    stages: List[FunnelStage] = []
    previous: Optional[int] = None

    for stage in (PipelineStage.RAW_ARTICLE, PipelineStage.NORMALIZED_ARTICLE,
                  PipelineStage.ENTITY_RESOLVED, PipelineStage.EVENT_REPORT,
                  PipelineStage.FUSED, PipelineStage.CANONICAL_EVENT,
                  PipelineStage.RESEARCH_OBSERVATION):
        exiting = _scalar(conn, _STAGE_SQL[stage].format(where=where), params)
        earliest, latest = _range(conn, _STAGE_RANGE_SQL[stage].format(where=where),
                                  params)
        stages.append(FunnelStage(
            stage=stage,
            count_entering=exiting if previous is None else previous,
            count_exiting=exiting,
            earliest=earliest, latest=latest,
            note=_STAGE_NOTE[stage]))
        previous = exiting

    return HistoricalFunnel(stages=tuple(stages), period_label=period_label,
                            measured_at=measured_at or datetime.now(UTC))


def record_counts(conn: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
    """
    Per-stage record counts and temporal coverage, in each stage's own
    unit. Reported beside the funnel, never inside it.
    """
    out: Dict[str, Dict[str, object]] = {}

    def add(key: str, count_sql: str, range_sql: str, unit: str,
            time_field: str) -> None:
        early, late = _range(conn, range_sql)
        out[key] = {
            "count": _scalar(conn, count_sql),
            "unit": unit,
            "time_field": time_field,
            "earliest": early.isoformat() if early else None,
            "latest": late.isoformat() if late else None,
            "span_days": (late - early).days if early and late else None,
        }

    add("raw_articles", "SELECT COUNT(*) FROM raw_articles",
        "SELECT NULL, NULL", "articles", "n/a (table empty)")
    add("articles", "SELECT COUNT(*) FROM articles",
        "SELECT MIN(published_at), MAX(published_at) FROM articles",
        "articles", "published_at")
    add("news_articles", "SELECT COUNT(*) FROM news_articles",
        "SELECT MIN(published_at), MAX(published_at) FROM news_articles",
        "articles", "published_at")
    add("article_entities", "SELECT COUNT(*) FROM article_entities",
        "SELECT MIN(a.published_at), MAX(a.published_at) FROM article_entities e "
        "JOIN articles a ON a.article_id = e.article_id",
        "links", "article published_at")
    add("events", "SELECT COUNT(*) FROM events",
        "SELECT MIN(publication_time), MAX(publication_time) FROM events",
        "event reports", "publication_time")
    add("fusion_decisions", "SELECT COUNT(*) FROM fusion_decisions",
        "SELECT MIN(decided_at), MAX(decided_at) FROM fusion_decisions",
        "decisions", "decided_at (processing time)")
    add("canonical_events", "SELECT COUNT(*) FROM canonical_events",
        "SELECT MIN(first_reported_at), MAX(first_reported_at) FROM canonical_events",
        "canonical events", "first_reported_at")
    add("research_observations", "SELECT COUNT(*) FROM research_observations",
        "SELECT MIN(information_cutoff), MAX(information_cutoff) "
        "FROM research_observations",
        "observations", "information_cutoff")
    add("research_features", "SELECT COUNT(*) FROM research_features",
        "SELECT MIN(as_of), MAX(as_of) FROM research_features",
        "feature values", "as_of")
    add("research_labels", "SELECT COUNT(*) FROM research_labels",
        "SELECT MIN(measured_at), MAX(measured_at) FROM research_labels",
        "label values", "measured_at")
    return out


def processing_times(conn: sqlite3.Connection) -> Dict[str, Dict[str, object]]:
    """
    When each stage actually ran, as distinct from what it was about.
    A stage whose entire output shares one processing instant was a
    single batch rebuild, and that is worth seeing next to its
    information-time range.
    """
    out: Dict[str, Dict[str, object]] = {}
    for key, sql, distinct_sql in (
        ("articles.collected_at",
         "SELECT MIN(collected_at), MAX(collected_at) FROM articles",
         "SELECT COUNT(DISTINCT substr(collected_at,1,10)) FROM articles"),
        ("events.detection_time",
         "SELECT MIN(detection_time), MAX(detection_time) FROM events",
         "SELECT COUNT(DISTINCT substr(detection_time,1,10)) FROM events"),
        ("fusion_decisions.decided_at",
         "SELECT MIN(decided_at), MAX(decided_at) FROM fusion_decisions",
         "SELECT COUNT(DISTINCT substr(decided_at,1,10)) FROM fusion_decisions"),
        ("research_observations.observation_created_at",
         "SELECT MIN(observation_created_at), MAX(observation_created_at) "
         "FROM research_observations",
         "SELECT COUNT(DISTINCT substr(observation_created_at,1,10)) "
         "FROM research_observations"),
    ):
        early, late = _range(conn, sql)
        out[key] = {
            "earliest": early.isoformat() if early else None,
            "latest": late.isoformat() if late else None,
            "distinct_days": _scalar(conn, distinct_sql),
            "elapsed_seconds": round((late - early).total_seconds(), 3)
                               if early and late else None,
        }
    return out


# ======================================================================
# Loss accounting
# ======================================================================

def classify_losses(conn: sqlite3.Connection, *,
                    period_start: Optional[datetime] = None,
                    period_end: Optional[datetime] = None,
                    period_label: str = "") -> LossAccounting:
    """
    Classify every source article in the window by what became of it.

    Reasons are assigned only where lineage establishes them:

      * reached a canonical event            -> BECAME_CANONICAL_EVENT
      * entity-linked, extraction ran, no
        event cited it                       -> NO_EVENT_FOUND
      * entity-linked, published before the
        earliest extracted report            -> EXTRACTION_NOT_ATTEMPTED
      * no entity link, published before the
        earliest entity-linked article       -> ENTITY_NOT_ATTEMPTED
      * no entity link, published after it    -> ENTITY_UNRESOLVED

    The last two are the distinction the phase turns on. "Not
    attempted" is recoverable by replay; "unresolved" is not, because
    the resolver already looked and declined, and loosening it to
    change that answer is forbidden.
    """
    where, params = _window(period_start, period_end)
    ent_cut = entity_cutoff(conn)
    ext_cut = extraction_cutoff(conn)

    total = _scalar(conn, f"SELECT COUNT(*) FROM articles a WHERE 1=1 {where}", params)

    became = _scalar(conn, _STAGE_SQL[PipelineStage.RESEARCH_OBSERVATION]
                     .format(where=where), params)

    # Reached a canonical event but no research observation yet -- the
    # observation builder has not caught up, which is not a loss of
    # evidence.
    canonical = _scalar(conn, _STAGE_SQL[PipelineStage.CANONICAL_EVENT]
                        .format(where=where), params)
    awaiting_observation = max(canonical - became, 0)

    # Entity-linked but never cited by any event.
    entity_linked = _scalar(conn, _STAGE_SQL[PipelineStage.ENTITY_RESOLVED]
                            .format(where=where), params)
    cited = _scalar(conn, _STAGE_SQL[PipelineStage.EVENT_REPORT]
                    .format(where=where), params)
    entity_linked_not_cited = max(entity_linked - cited, 0)

    # Of those, the ones that predate extraction were never offered to it.
    extraction_skipped = 0
    if ext_cut is not None:
        extraction_skipped = _scalar(conn, f"""
            SELECT COUNT(DISTINCT a.article_id) FROM articles a
            JOIN article_entities e ON e.article_id = a.article_id
            LEFT JOIN event_evidence v ON v.article_id = a.article_id
            WHERE v.article_id IS NULL AND a.published_at < ? {where}
        """, [ext_cut.isoformat()] + params)
    no_event_found = max(entity_linked_not_cited - extraction_skipped, 0)

    # Articles with no entity link at all, split on the measured cutoff.
    not_linked = max(total - entity_linked, 0)
    entity_skipped = 0
    if ent_cut is not None:
        entity_skipped = _scalar(conn, f"""
            SELECT COUNT(*) FROM articles a
            WHERE a.published_at < ?
              AND NOT EXISTS (SELECT 1 FROM article_entities e
                              WHERE e.article_id = a.article_id) {where}
        """, [ent_cut.isoformat()] + params)
    entity_unresolved = max(not_linked - entity_skipped, 0)

    by_reason: Dict[LossReason, int] = {}

    def put(reason: LossReason, count: int) -> None:
        if count:
            by_reason[reason] = by_reason.get(reason, 0) + count

    put(LossReason.BECAME_CANONICAL_EVENT, became)
    put(LossReason.FUSION_NOT_RUN, awaiting_observation)
    put(LossReason.NO_EVENT_FOUND, no_event_found)
    put(LossReason.EXTRACTION_NOT_ATTEMPTED, extraction_skipped)
    put(LossReason.ENTITY_NOT_ATTEMPTED, entity_skipped)
    put(LossReason.ENTITY_UNRESOLVED, entity_unresolved)

    note = ""
    if ent_cut is not None:
        note = (f"entity cutoff measured at {ent_cut.isoformat()}; "
                f"extraction cutoff "
                f"{ext_cut.isoformat() if ext_cut else 'none'}")

    return LossAccounting(period_label=period_label or "all",
                          total_source_articles=total,
                          by_reason=by_reason, note=note)


def recoverable_detail(conn: sqlite3.Connection) -> Dict[str, object]:
    """
    How much of the skipped region the resolver can actually act on.

    An article with no mention JSON is not recoverable by replaying
    entity resolution, however early it was published -- there is
    nothing in the row to resolve. Separating the two keeps the recovery
    estimate honest instead of optimistic.
    """
    ent_cut = entity_cutoff(conn)
    if ent_cut is None:
        return {"entity_cutoff": None, "skipped": 0,
                "skipped_with_mentions": 0, "skipped_without_mentions": 0}

    cut = ent_cut.isoformat()
    skipped = _scalar(conn, """
        SELECT COUNT(*) FROM articles a WHERE a.published_at < ?
          AND NOT EXISTS (SELECT 1 FROM article_entities e
                          WHERE e.article_id = a.article_id)""", [cut])
    with_mentions = _scalar(conn, f"""
        SELECT COUNT(*) FROM articles a WHERE a.published_at < ?
          AND NOT EXISTS (SELECT 1 FROM article_entities e
                          WHERE e.article_id = a.article_id)
          AND {_MENTIONS_PRESENT}""", [cut])
    return {
        "entity_cutoff": cut,
        "skipped": skipped,
        "skipped_with_mentions": with_mentions,
        "skipped_without_mentions": max(skipped - with_mentions, 0),
        "recoverable_share": (round(with_mentions / skipped, 4)
                              if skipped else None),
    }


# ======================================================================
# Time distribution
# ======================================================================

def coverage_by_period(conn: sqlite3.Connection, *,
                       grain: str = "day") -> List[Dict[str, object]]:
    """
    Articles, entity-resolved articles, event reports and canonical
    events per calendar period.

    This is what distinguishes sparse historical coverage from a hard
    pipeline cutoff: sparse coverage tapers, a cutoff steps.
    """
    width = {"day": 10, "month": 7, "year": 4}.get(grain)
    if width is None:
        raise ValueError("grain must be day, month or year")

    buckets: Dict[str, Dict[str, object]] = {}

    def bucket(key: str) -> Dict[str, object]:
        return buckets.setdefault(key, {
            "period": key, "articles": 0, "entity_resolved": 0,
            "event_reports": 0, "canonical_events": 0})

    for key, n in conn.execute(
            f"SELECT substr(published_at,1,{width}), COUNT(*) "
            f"FROM articles GROUP BY 1"):
        if key:
            bucket(key)["articles"] = n
    for key, n in conn.execute(
            f"SELECT substr(a.published_at,1,{width}), "
            f"COUNT(DISTINCT a.article_id) FROM articles a "
            f"JOIN article_entities e ON e.article_id = a.article_id GROUP BY 1"):
        if key:
            bucket(key)["entity_resolved"] = n
    for key, n in conn.execute(
            f"SELECT substr(publication_time,1,{width}), COUNT(*) "
            f"FROM events GROUP BY 1"):
        if key:
            bucket(key)["event_reports"] = n
    for key, n in conn.execute(
            f"SELECT substr(first_reported_at,1,{width}), COUNT(*) "
            f"FROM canonical_events GROUP BY 1"):
        if key:
            bucket(key)["canonical_events"] = n

    rows = [buckets[k] for k in sorted(buckets)]
    for row in rows:
        articles = row["articles"] or 0
        row["entity_pct"] = (round(100.0 * row["entity_resolved"] / articles, 1)
                             if articles else None)
    return rows


def first_hard_cutoff(rows: Sequence[Mapping[str, object]], *,
                      field: str = "entity_resolved",
                      jump: float = 10.0) -> Optional[str]:
    """
    The first period where `field` goes from nothing to something.

    A gradual ramp returns None. A step returns the period it stepped
    in, which is the thing to go looking for in the code.
    """
    previous = 0
    for row in rows:
        value = int(row.get(field) or 0)
        if previous == 0 and value >= jump:
            return str(row.get("period"))
        previous = value
    return None


# ======================================================================
# Stage audits
# ======================================================================

def entity_resolution_audit(conn: sqlite3.Connection, *,
                            cutoff: Optional[datetime] = None) -> Dict[str, object]:
    """
    Pre-cutoff versus post-cutoff entity coverage, with the mention
    columns measured on both sides.

    If both sides carry the same kind of source material and the same
    mention density, then a coverage difference is not about the news.
    It is about us.
    """
    cutoff = cutoff or entity_cutoff(conn)
    if cutoff is None:
        return {"cutoff": None, "note": "no entity links exist"}
    cut = cutoff.isoformat()

    def side(op: str) -> Dict[str, object]:
        articles = _scalar(
            conn, f"SELECT COUNT(*) FROM articles WHERE published_at {op} ?", [cut])
        linked = _scalar(conn, f"""
            SELECT COUNT(DISTINCT a.article_id) FROM articles a
            JOIN article_entities e ON e.article_id = a.article_id
            WHERE a.published_at {op} ?""", [cut])
        mentions = _scalar(conn, f"""
            SELECT COUNT(*) FROM articles a
            WHERE a.published_at {op} ? AND {_MENTIONS_PRESENT}""", [cut])
        entities = _scalar(conn, f"""
            SELECT COUNT(*) FROM article_entities e
            JOIN articles a ON a.article_id = e.article_id
            WHERE a.published_at {op} ?""", [cut])
        companies = _scalar(conn, f"""
            SELECT COUNT(DISTINCT e.entity_id) FROM article_entities e
            JOIN articles a ON a.article_id = e.article_id
            WHERE a.published_at {op} ?""", [cut])
        google = _scalar(conn, f"""
            SELECT COUNT(*) FROM articles
            WHERE published_at {op} ? AND source LIKE 'Google News:%'""", [cut])
        sources = _scalar(conn, f"""
            SELECT COUNT(DISTINCT source) FROM articles
            WHERE published_at {op} ?""", [cut])
        return {
            "articles": articles,
            "with_mentions": mentions,
            "mention_pct": round(100.0 * mentions / articles, 1) if articles else None,
            "entity_linked": linked,
            "entity_pct": round(100.0 * linked / articles, 1) if articles else None,
            "entity_links": entities,
            "distinct_companies": companies,
            "company_feed_articles": google,
            "distinct_sources": sources,
        }

    pre, post = side("<"), side(">=")
    return {
        "cutoff": cut,
        "pre_cutoff": pre,
        "post_cutoff": post,
        "mention_density_comparable": (
            pre["mention_pct"] is not None and post["mention_pct"] is not None
            and abs(pre["mention_pct"] - post["mention_pct"]) < 20.0),
        "coverage_gap_pct": (
            None if pre["entity_pct"] is None or post["entity_pct"] is None
            else round(post["entity_pct"] - pre["entity_pct"], 1)),
    }


def extraction_audit(conn: sqlite3.Connection) -> Dict[str, object]:
    """
    Whether extraction ran over the eligible population, and what it
    produced. "Never run" and "ran and found nothing" are different
    problems and are counted separately.
    """
    ext_cut = extraction_cutoff(conn)
    eligible = _scalar(conn, "SELECT COUNT(DISTINCT article_id) FROM article_entities")
    attempted = eligible
    if ext_cut is not None:
        attempted = _scalar(conn, """
            SELECT COUNT(DISTINCT e.article_id) FROM article_entities e
            JOIN articles a ON a.article_id = e.article_id
            WHERE a.published_at >= ?""", [ext_cut.isoformat()])
    produced = _scalar(conn, "SELECT COUNT(DISTINCT article_id) FROM event_evidence")
    return {
        "extraction_cutoff": ext_cut.isoformat() if ext_cut else None,
        "eligible_articles": eligible,
        "attempted_articles": attempted,
        "skipped_articles": max(eligible - attempted, 0),
        "articles_producing_events": produced,
        "no_event_articles": max(attempted - produced, 0),
        "event_reports": _scalar(conn, "SELECT COUNT(*) FROM events"),
        "by_tier": dict(conn.execute(
            "SELECT extraction_tier, COUNT(*) FROM events GROUP BY 1").fetchall()),
        "by_status": dict(conn.execute(
            "SELECT status, COUNT(*) FROM events GROUP BY 1").fetchall()),
        "errors_recorded": _scalar(conn, "SELECT COUNT(*) FROM event_corrections"),
    }


def fusion_audit(conn: sqlite3.Connection) -> Dict[str, object]:
    """Reports eligible for fusion, decisions taken, canonical events made."""
    reports = _scalar(conn, "SELECT COUNT(*) FROM events")
    fused = _scalar(conn, "SELECT COUNT(DISTINCT report_id) FROM canonical_event_reports")
    return {
        "reports_eligible": reports,
        "reports_fused": fused,
        "reports_not_fused": max(reports - fused, 0),
        "canonical_events": _scalar(conn, "SELECT COUNT(*) FROM canonical_events"),
        "decisions": _scalar(conn, "SELECT COUNT(*) FROM fusion_decisions"),
        "by_decision_state": dict(conn.execute(
            "SELECT state, COUNT(*) FROM fusion_decisions GROUP BY 1").fetchall()),
        "duplicates_collapsed": max(fused - _scalar(
            conn, "SELECT COUNT(*) FROM canonical_events"), 0),
        "review_cases": _scalar(conn, "SELECT COUNT(*) FROM fusion_review_cases"),
        "contradictions": _scalar(conn, "SELECT COUNT(*) FROM fusion_contradictions"),
        "by_corroboration": dict(conn.execute(
            "SELECT corroboration_state, COUNT(*) FROM canonical_events "
            "GROUP BY 1").fetchall()),
    }


def price_coverage_audit(conn: sqlite3.Connection) -> Dict[str, object]:
    """
    Whether price history can support labels over the recovery period.

    Recovering events whose returns cannot be measured would add rows
    and no evidence, so this is checked before any recovery is planned
    -- and if prices are already complete, they are left alone.
    """
    early, late = _range(
        conn, "SELECT MIN(timestamp), MAX(timestamp) FROM price_candle_cache")
    return {
        "earliest": early.isoformat() if early else None,
        "latest": late.isoformat() if late else None,
        "rows": _scalar(conn, "SELECT COUNT(*) FROM price_candle_cache"),
        "instruments": _scalar(
            conn, "SELECT COUNT(DISTINCT instrument_id) FROM price_candle_cache"),
        "by_month": [
            {"month": m, "rows": n, "instruments": k}
            for m, n, k in conn.execute(
                "SELECT substr(timestamp,1,7), COUNT(*), "
                "COUNT(DISTINCT instrument_id) FROM price_candle_cache "
                "GROUP BY 1 ORDER BY 1")],
    }
