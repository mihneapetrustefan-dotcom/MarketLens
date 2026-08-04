"""
dashboard.py
--------------
Dashboard / Report Generator module for MarketLens.

RESPONSIBILITY:
Render the final pipeline output — recommendations, entity confidence,
sector context, risk, portfolio simulation, and upgrade/downgrade
history — into a single, professional, styled HTML report.

DESIGN — v2, sector-grouped layout (redesigned from v1's flat tables,
based on direct user feedback that the original layout felt like
"disorganized tables thrown together"):

1. HERO + KPI STRIP: a gradient hero number (portfolio return) and a
   row of key counts (BUY/SELL/HOLD/success rate) at a glance.
2. STICKY JUMP NAV: pill links to each sector, staying visible while
   scrolling — needed once there are dozens of sectors/entities.
3. SECTOR-GROUPED SECTIONS: every entity is grouped under its sector
   (via `entity_sector_map`), each section collapsible (native HTML
   `<details>`, no JS required for this part) with a colored header
   showing that sector's own aggregate stats (article count, dominant
   sentiment) from Sector Aggregator.
4. PER-ENTITY CARDS include, per user request:
   a. A confidence BAR (visual, not just a number).
   b. A "verified track record" badge (✓/✗) — IF a prior recommendation
      for this entity has already been checked by Backtest Engine.
   c. A collapsible "argument" (native `<details>`) — HIDDEN by
      default, revealed on click — showing the score breakdown
      (volume/diversity/consistency/impact) and the single most
      representative source article backing the call.
5. LIVE SEARCH BOX: a small vanilla-JS filter (no external library,
   works fully offline) that hides non-matching cards as the user
   types an entity name.

DESIGN DECISION — HTML-escaping everything from real data:
Titles, entity names, and explanations all originate from real,
external RSS content. Every such value is passed through
`html.escape()` before being inserted into the document.

DESIGN DECISION — no undervalued/overvalued verdict, ever:
Market data is shown as facts only (see _render_market_data_table) —
this module never computes or displays a valuation judgment.
"""

import html as html_lib
from collections import Counter
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


class DashboardGenerator:
    """
    Builds a self-contained HTML investment report, grouped by sector,
    with expandable per-entity evidence.
    """

    _RECOMMENDATION_COLORS = {"BUY": "#3ecf7e", "SELL": "#f0645f", "HOLD": "#8a8f98"}

    # Sector header background tint, chosen by that sector's own
    # dominant_sentiment (from Sector Aggregator) — mirrors the
    # heatmap-style color language used across the report.
    _SECTOR_TINTS = {
        "positive": "#0f3d24",
        "negative": "#3d1518",
        "mixed": "#3d3312",
        "neutral": "#1a1f2e",
    }

    _UNCATEGORIZED_SECTOR = "Altele"

    def _escape(self, value: Any) -> str:
        """Safely convert any value to HTML-escaped text."""
        return html_lib.escape(str(value)) if value is not None else ""

    # ------------------------------------------------------------------
    # Small, reusable visual building blocks
    # ------------------------------------------------------------------

    def _render_confidence_bar(self, score: float) -> str:
        """
        Render a small horizontal confidence bar (0.0-1.0), colored by
        how strong the score is. A VISUAL complement to the raw number
        — easier to scan across many cards than reading "0.87" vs "0.55".
        """
        pct = max(0, min(100, round((score or 0.0) * 100)))
        color = "#3ecf7e" if pct >= 66 else ("#e0a83e" if pct >= 40 else "#f0645f")
        return f"""
        <div class="conf-bar-track">
          <div class="conf-bar-fill" style="width:{pct}%; background:{color};"></div>
        </div>
        <div class="conf-bar-label">{self._escape(score)}</div>
        """

    def _render_verified_badge(self, entity: str, verified_track_record: Optional[Dict[str, Optional[bool]]]) -> str:
        """
        Render a small "✓ verified" / "✗ verified" badge if a PRIOR
        recommendation for this entity has already been checked by
        Backtest Engine. Returns an empty string if there's no track
        record yet — this is meant to build trust over time, not claim
        history that doesn't exist.
        """
        if not verified_track_record or entity not in verified_track_record:
            return ""
        was_correct = verified_track_record[entity]
        if was_correct is None:
            return ""
        if was_correct:
            return '<span class="verified-badge verified-ok">✓ ultima verificare: corectă</span>'
        return '<span class="verified-badge verified-bad">✗ ultima verificare: greșită</span>'

    def _change_badge(self, entity: str, upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]]) -> str:
        """Build a small inline badge showing an upgrade/downgrade/new/unchanged marker for one entity."""
        if not upgrade_downgrade_map or entity not in upgrade_downgrade_map:
            return ""
        change = upgrade_downgrade_map[entity].get("change")
        symbols = {
            "upgrade": ("↑ upgrade", "#3ecf7e"), "downgrade": ("↓ downgrade", "#f0645f"),
            "new": ("nou", "#4a90d9"), "unchanged": ("neschimbat", "#8a8f98"),
        }
        if change not in symbols:
            return ""
        label, color = symbols[change]
        return f'<span class="badge-pill" style="color:{color}">{self._escape(label)}</span>'

    def _render_breakdown(self, rec: Dict[str, Any]) -> str:
        """
        Render the transparent score breakdown (volume, source
        diversity, sentiment consistency, average impact) shown inside
        a card's expandable argument — the actual ingredients behind
        the confidence score, not just the final result.
        """
        rows = [
            ("Volum", rec.get("volume_score")),
            ("Diversitate surse", rec.get("source_diversity_score")),
            ("Consistență sentiment", rec.get("sentiment_consistency")),
            ("Impact mediu", rec.get("average_impact")),
        ]
        parts = []
        for label, value in rows:
            pct = round((value or 0.0) * 100)
            parts.append(f"""
            <div class="breakdown-row">
              <span class="breakdown-label">{self._escape(label)}</span>
              <div class="breakdown-track"><div class="breakdown-fill" style="width:{pct}%"></div></div>
              <span class="breakdown-val">{self._escape(value)}</span>
            </div>
            """)
        return "".join(parts)

    def _representative_article(self, rec: Dict[str, Any], entity_articles: Optional[List[Dict[str, Any]]]) -> str:
        """
        Pick the single most representative article for this entity —
        the one with the highest Impact Engine score among those
        matching the entity's dominant sentiment — and render a short
        citation line. Falls back to a generic note if no article list
        was supplied.
        """
        if not entity_articles:
            return ""
        dominant = rec.get("dominant_sentiment")
        candidates = [
            a for a in entity_articles
            if (a.get("sentiment") or {}).get("label") == dominant
        ] or entity_articles

        best = max(candidates, key=lambda a: (a.get("impact") or {}).get("score", 0.0))
        title = best.get("title", "")
        source = best.get("source", "")
        if not title:
            return ""
        return f'<span class="src">"{self._escape(title)}" — {self._escape(source)}</span>'

    def _render_recommendation_card(
        self,
        rec: Dict[str, Any],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]] = None,
        verified_track_record: Optional[Dict[str, Optional[bool]]] = None,
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> str:
        """Render one entity as a styled card, with confidence bar, badges, and a hidden argument."""
        color = self._RECOMMENDATION_COLORS.get(rec["recommendation"], "#8a8f98")
        horizon = rec.get("time_horizon")
        horizon_badge = f'<span class="badge-pill">{self._escape(horizon)}</span>' if horizon else ""
        change_badge = self._change_badge(rec["entity"], upgrade_downgrade_map)
        verified_badge = self._render_verified_badge(rec["entity"], verified_track_record)
        entity_articles = (entity_articles_map or {}).get(rec["entity"])
        representative = self._representative_article(rec, entity_articles)

        return f"""
        <div class="rec-card" style="box-shadow: inset 3px 0 0 {color};" data-search="{self._escape(rec['entity'].lower())}">
          <div class="rc-top">
            <div>
              <div class="rc-name">{self._escape(rec['entity'])}</div>
              <div class="rc-tags">{horizon_badge}{change_badge}{verified_badge}</div>
            </div>
            <span class="rc-verdict" style="background:{color}22; color:{color};">{self._escape(rec['recommendation'])}</span>
          </div>
          {self._render_confidence_bar(rec.get('confidence_score', 0.0))}
          <details class="argument">
            <summary><span class="icon">ⓘ</span> Vezi argumentul</summary>
            <div class="argument-body">
              {self._escape(rec.get('explanation', ''))}
              {self._render_breakdown(rec)}
              {representative}
            </div>
          </details>
        </div>
        """

    # ------------------------------------------------------------------
    # Sector grouping
    # ------------------------------------------------------------------

    def _group_by_sector(
        self, recommendations: List[Dict[str, Any]], entity_sector_map: Optional[Dict[str, str]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Group recommendations by sector, using entity_sector_map
        (typically sector_registry.COMPANY_SECTOR_MAP directly — a
        static entity-name -> sector lookup already used elsewhere in
        the pipeline). Entities with no known sector fall back to a
        single "Altele" (Other) group rather than being dropped.
        """
        entity_sector_map = entity_sector_map or {}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for rec in recommendations:
            sector = entity_sector_map.get(rec["entity"], self._UNCATEGORIZED_SECTOR)
            groups.setdefault(sector, []).append(rec)
        return groups

    def _render_sector_section(
        self,
        sector_name: str,
        sector_recs: List[Dict[str, Any]],
        sector_scores_by_name: Dict[str, Dict[str, Any]],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]],
        verified_track_record: Optional[Dict[str, Optional[bool]]],
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]],
    ) -> str:
        """Render one collapsible sector section: header with sector-level stats, then its entity cards."""
        stats = sector_scores_by_name.get(sector_name, {})
        article_count = stats.get("article_count", "-")
        source_count = stats.get("distinct_source_count", "-")
        dominant = stats.get("dominant_sentiment", "neutral")
        consistency = stats.get("sentiment_consistency")
        tint = self._SECTOR_TINTS.get(dominant, "#1a1f2e")
        consistency_text = f" · consistență {round(consistency * 100)}%" if consistency is not None else ""

        slug = "".join(c if c.isalnum() else "-" for c in sector_name.lower())
        cards = "".join(
            self._render_recommendation_card(r, upgrade_downgrade_map, verified_track_record, entity_articles_map)
            for r in sector_recs
        )

        return f"""
        <details class="sector-block" id="sector-{slug}" open>
          <summary class="sector-header" style="background:{tint};">
            <span class="sector-left">
              <span class="sector-name">{self._escape(sector_name)}</span>
              <span class="sector-meta">{self._escape(article_count)} articole · {self._escape(source_count)} surse</span>
            </span>
            <span class="sentiment-tag">{self._escape(dominant)}{consistency_text}</span>
          </summary>
          <div class="rec-cards">{cards}</div>
        </details>
        """

    def _render_jump_nav(self, sector_names: List[str], sector_scores_by_name: Dict[str, Dict[str, Any]]) -> str:
        """Render the sticky pill navigation linking to each sector section."""
        pills = []
        for name in sector_names:
            slug = "".join(c if c.isalnum() else "-" for c in name.lower())
            count = sector_scores_by_name.get(name, {}).get("article_count", "")
            suffix = f" ({count})" if count != "" else ""
            pills.append(f'<a class="jump-pill" href="#sector-{slug}">{self._escape(name)}{suffix}</a>')
        return "".join(pills)

    # ------------------------------------------------------------------
    # Existing standalone sections (market data, portfolio) — unchanged
    # ------------------------------------------------------------------

    def _render_market_data_table(
        self,
        market_data: Optional[Dict[str, Dict[str, Any]]],
        risk_data: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """
        Render a factual market-data table (price, daily change,
        position within the 52-week range, trailing P/E, real
        volatility-based risk) for whichever tickers were supplied.

        WHY NO VERDICT HERE: this table deliberately shows only raw
        figures — never a computed "undervalued"/"overvalued" label.
        """
        if not market_data:
            return '<p class="empty-state">Nicio dată de piață disponibilă.</p>'

        rows = []
        for ticker, snap in market_data.items():
            risk = (risk_data or {}).get(ticker, {})
            risk_cell = (
                f"{self._escape(risk.get('risk_level'))} ({self._escape(risk.get('annualized_volatility_pct'))}%)"
                if risk and not risk.get("error") else "-"
            )
            if snap.get("error"):
                rows.append(f"""
                <tr><td>{self._escape(ticker)}</td><td colspan="6" class="empty-state">{self._escape(snap["error"])}</td></tr>
                """)
                continue
            rows.append(f"""
            <tr>
              <td>{self._escape(ticker)}</td>
              <td>{self._escape(snap.get('current_price'))}</td>
              <td>{self._escape(snap.get('daily_change_pct'))}%</td>
              <td>{self._escape(snap.get('pct_from_52w_high'))}%</td>
              <td>{self._escape(snap.get('pct_from_52w_low'))}%</td>
              <td>{self._escape(snap.get('trailing_pe'))}</td>
              <td>{risk_cell}</td>
            </tr>
            """)
        return f"""
        <table>
          <thead><tr><th>Ticker</th><th>Preț curent</th><th>Variație zilnică</th><th>Față de max 52săpt</th><th>Față de min 52săpt</th><th>P/E</th><th>Risc</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    def _render_portfolio_summary(self, portfolio_result: Optional[Dict[str, Any]]) -> str:
        """Render the hypothetical portfolio simulation summary cards."""
        if not portfolio_result or not portfolio_result.get("trades_simulated"):
            return '<p class="empty-state">Nicio simulare de portofoliu disponibilă (necesită recomandări deja verificate prin Backtest).</p>'
        r = portfolio_result
        color = "#3ecf7e" if (r["total_return_pct"] or 0) >= 0 else "#f0645f"
        return f"""
        <div class="kpi-strip">
          <div class="kpi"><div class="n">${self._escape(r['total_invested'])}</div><div class="l">Investit</div></div>
          <div class="kpi"><div class="n">${self._escape(r['total_final_value'])}</div><div class="l">Valoare finală</div></div>
          <div class="kpi"><div class="n" style="color:{color}">{self._escape(r['total_return_pct'])}%</div><div class="l">Randament</div></div>
          <div class="kpi"><div class="n">{self._escape(r['trades_simulated'])}</div><div class="l">Tranzacții</div></div>
        </div>
        """

    def _render_sector_distribution(self, articles: List[Dict[str, Any]]) -> str:
        """Render a simple horizontal-bar breakdown of articles per sector (kept for standalone use/back-compat)."""
        counts: Counter = Counter()
        for article in articles or []:
            for sector_entry in (article.get("sectors") or []):
                counts[sector_entry["sector"]] += 1
        if not counts:
            return '<p class="empty-state">Nicio clasificare pe sector disponibilă.</p>'
        max_count = max(counts.values())
        bars = []
        for sector, count in counts.most_common():
            width_pct = round((count / max_count) * 100)
            bars.append(f"""
            <div class="bar-row">
              <span class="bar-label">{self._escape(sector)}</span>
              <div class="bar-track"><div class="bar-fill" style="width:{width_pct}%"></div></div>
              <span class="bar-count">{count}</span>
            </div>
            """)
        return "".join(bars)

    def _render_sector_scores_table(self, sector_scores: Optional[List[Dict[str, Any]]]) -> str:
        """Render the sector-level macro table (kept for standalone use/back-compat)."""
        if not sector_scores:
            return '<p class="empty-state">Nicio perspectivă pe sector disponibilă.</p>'
        rows = []
        for s in sector_scores:
            rows.append(f"""
            <tr>
              <td>{self._escape(s['sector'])}</td>
              <td>{self._escape(s['article_count'])}</td>
              <td>{self._escape(s['distinct_source_count'])}</td>
              <td>{self._escape(s['dominant_sentiment'])}</td>
              <td>{self._escape(s['sentiment_consistency'])}</td>
              <td>{self._escape(s['average_impact'])}</td>
            </tr>
            """)
        return f"""
        <table>
          <thead><tr><th>Sector</th><th>Articole</th><th>Surse</th><th>Sentiment</th><th>Consistență</th><th>Impact mediu</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate_report(
        self,
        recommendations: List[Dict[str, Any]],
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
    ) -> str:
        """
        Build the full HTML report as a single string.

        Args:
            recommendations: output of RecommendationEngine.recommend_all()
            articles: underlying processed articles; optional
            db_stats: output of NewsDatabase.get_stats(); optional
            market_data: output of MarketDataFetcher.get_snapshots_batch(); optional
            risk_data: output of RiskScoreCalculator.get_risk_scores_batch(); optional
            sector_scores: output of SectorAggregator.score_all_sectors(); optional
            upgrade_downgrade_map: entity -> UpgradeDowngradeTracker.compare_entity() result; optional
            portfolio_result: output of PortfolioSimulator.simulate(); optional
            daily_summary_text: output of DailySummaryGenerator.generate(); optional
            entity_sector_map: entity name -> sector name (e.g.
                sector_registry.COMPANY_SECTOR_MAP directly) — drives
                the sector-grouped layout. Entities not found fall
                back to an "Altele" (Other) group.
            verified_track_record: entity -> True/False/None, the most
                recent Backtest Engine outcome for that entity, if any.
            entity_articles_map: entity -> its articles (e.g. from
                ConfidenceEngine.aggregate_by_entity(articles)), used
                to pick the single most representative source article
                shown inside each card's argument.

        Returns:
            A complete, standalone HTML document (string).
        """
        articles = articles or []
        db_stats = db_stats or {}
        sector_scores = sector_scores or []
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for r in recommendations:
            counts[r["recommendation"]] = counts.get(r["recommendation"], 0) + 1

        sector_scores_by_name = {s["sector"]: s for s in sector_scores}
        grouped = self._group_by_sector(recommendations, entity_sector_map)
        # Sort sectors by article count (from Sector Aggregator when
        # available) descending, so the most-covered sector leads.
        sector_names = sorted(
            grouped.keys(),
            key=lambda name: sector_scores_by_name.get(name, {}).get("article_count", 0),
            reverse=True,
        )

        jump_nav = self._render_jump_nav(sector_names, sector_scores_by_name)
        sector_sections = "".join(
            self._render_sector_section(
                name, grouped[name], sector_scores_by_name,
                upgrade_downgrade_map, verified_track_record, entity_articles_map,
            )
            for name in sector_names
        )

        return f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<title>MarketLens — Raport de Investiții</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0a0b0f; color:#eef0f3; margin:0; padding:0; }}
  .hero-band {{ background: linear-gradient(135deg, #1a1d2e 0%, #0f1420 100%); padding:32px; border-bottom:1px solid #1f2333; display:flex; justify-content:space-between; align-items:flex-end; flex-wrap:wrap; gap:16px; }}
  .hero-eyebrow {{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:#6d7a99; margin-bottom:8px; }}
  .hero-sub {{ font-size:13px; color:#9aa3b8; margin-top:8px; max-width:560px; line-height:1.5; }}
  .kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; }}
  .kpi {{ background:#151a28; border-radius:10px; padding:10px 16px; text-align:center; min-width:70px; }}
  .kpi .n {{ font-size:18px; font-weight:800; }}
  .kpi .l {{ font-size:9px; color:#8a8f98; text-transform:uppercase; }}
  .content {{ padding:24px 32px; }}
  .search-box {{ width:100%; max-width:320px; padding:10px 14px; border-radius:8px; border:1px solid #1f2333; background:#151a28; color:#eef0f3; font-size:13px; margin-bottom:16px; }}
  .jump-nav {{ position:sticky; top:0; z-index:10; background:#0a0b0f; padding:12px 0; display:flex; gap:8px; flex-wrap:wrap; border-bottom:1px solid #1f2333; margin-bottom:20px; }}
  .jump-pill {{ font-size:11px; padding:6px 14px; border-radius:16px; background:#151a28; color:#9aa3b8; text-decoration:none; }}
  .jump-pill:hover {{ background:#1f2740; color:#fff; }}
  .section-title {{ font-size:12px; text-transform:uppercase; letter-spacing:1px; color:#6d7a99; font-weight:700; margin:28px 0 12px 0; }}

  details.sector-block {{ margin-bottom:20px; }}
  .sector-header {{ display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-radius:10px; cursor:pointer; list-style:none; }}
  .sector-header::-webkit-details-marker {{ display:none; }}
  .sector-left {{ display:flex; align-items:center; gap:10px; }}
  .sector-name {{ font-size:14px; font-weight:800; text-transform:uppercase; letter-spacing:0.5px; }}
  .sector-meta {{ font-size:11px; opacity:0.75; }}
  .sentiment-tag {{ font-size:10px; font-weight:700; padding:3px 10px; border-radius:12px; background:rgba(255,255,255,0.12); white-space:nowrap; }}

  .rec-cards {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:12px; margin-top:12px; }}
  .rec-card {{ background:#151a28; border-radius:12px; padding:14px 16px; }}
  .rc-top {{ display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }}
  .rc-name {{ font-size:14px; font-weight:700; }}
  .rc-tags {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; }}
  .rc-verdict {{ font-size:11px; font-weight:800; padding:3px 10px; border-radius:6px; white-space:nowrap; }}
  .badge-pill {{ font-size:9px; background:#1f2333; color:#8a8f98; padding:2px 7px; border-radius:8px; }}
  .verified-badge {{ font-size:9px; padding:2px 7px; border-radius:8px; }}
  .verified-ok {{ background:#12261a; color:#3ecf7e; }}
  .verified-bad {{ background:#2a1518; color:#f0645f; }}

  .conf-bar-track {{ height:5px; border-radius:3px; background:#1f2333; margin-top:10px; overflow:hidden; }}
  .conf-bar-fill {{ height:100%; }}
  .conf-bar-label {{ font-size:10px; color:#6d7a99; margin-top:3px; }}

  details.argument {{ margin-top:10px; }}
  details.argument summary {{ cursor:pointer; list-style:none; font-size:11px; color:#7a8bb0; display:flex; align-items:center; gap:5px; user-select:none; }}
  details.argument summary::-webkit-details-marker {{ display:none; }}
  details.argument summary .icon {{ display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; background:#1f2740; color:#9db4e8; font-size:10px; font-weight:800; }}
  details.argument[open] summary .icon {{ background:#4a90d9; color:#fff; }}
  .argument-body {{ background:#0e1220; border-radius:8px; padding:10px 12px; margin-top:8px; font-size:11.5px; line-height:1.5; color:#c2c8d6; }}
  .argument-body .src {{ color:#7a8bb0; font-style:italic; margin-top:6px; display:block; }}

  .breakdown-row {{ display:flex; align-items:center; gap:8px; margin:6px 0; font-size:10.5px; }}
  .breakdown-label {{ width:120px; color:#8a8f98; flex-shrink:0; }}
  .breakdown-track {{ flex:1; height:5px; border-radius:3px; background:#1c2029; overflow:hidden; }}
  .breakdown-fill {{ height:100%; background:#4a90d9; }}
  .breakdown-val {{ width:34px; text-align:right; color:#c2c6cc; }}

  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:#9aa0a6; font-weight:600; padding:8px 10px; border-bottom:1px solid #2a2e37; }}
  td {{ padding:8px 10px; border-bottom:1px solid #20232b; }}
  .bar-row {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; font-size:13px; }}
  .bar-label {{ width:170px; color:#c2c6cc; }}
  .bar-track {{ flex:1; background:#20232b; border-radius:6px; height:14px; overflow:hidden; }}
  .bar-fill {{ background:#4a90d9; height:100%; }}
  .bar-count {{ width:30px; text-align:right; color:#9aa0a6; }}
  .empty-state {{ color:#9aa0a6; font-size:13px; font-style:italic; }}
  .footer {{ margin-top:32px; padding:0 32px 24px 32px; font-size:11px; color:#5f6570; }}
</style>
</head>
<body>
  <div class="hero-band">
    <div>
      <div class="hero-eyebrow">MarketLens · {generated_at}</div>
      <div class="hero-sub">{f'<b>{self._escape(daily_summary_text)}</b>' if daily_summary_text else 'Raport de investiții generat automat.'}</div>
    </div>
    <div class="kpi-strip">
      <div class="kpi"><div class="n" style="color:#3ecf7e">{counts['BUY']}</div><div class="l">Buy</div></div>
      <div class="kpi"><div class="n" style="color:#f0645f">{counts['SELL']}</div><div class="l">Sell</div></div>
      <div class="kpi"><div class="n">{counts['HOLD']}</div><div class="l">Hold</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('total_articles', '-'))}</div><div class="l">Articole</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('distinct_sources', '-'))}</div><div class="l">Surse</div></div>
    </div>
  </div>

  <div class="content">
    <input class="search-box" type="text" id="marketlens-search" placeholder="Caută o companie (ex: Tesla)..." onkeyup="marketlensFilter()">

    <div class="jump-nav">{jump_nav}</div>

    {sector_sections}

    <div class="section-title">Simulare portofoliu (recomandări deja verificate prin Backtest)</div>
    {self._render_portfolio_summary(portfolio_result)}

    <div class="section-title">Date de piață (fapte reale — fără verdict de subevaluare/supraevaluare)</div>
    {self._render_market_data_table(market_data, risk_data)}
  </div>

  <div class="footer">MarketLens v1 — raport generat automat. Nu constituie sfat financiar.</div>

  <script>
    function marketlensFilter() {{
      var query = document.getElementById('marketlens-search').value.toLowerCase();
      var cards = document.querySelectorAll('.rec-card');
      cards.forEach(function(card) {{
        var match = card.getAttribute('data-search').indexOf(query) !== -1;
        card.style.display = match ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>"""

    def save_report(self, html_content: str, path: str) -> None:
        """Write a generated report string to disk as a standalone .html file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_content)
