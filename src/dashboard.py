"""
dashboard.py
---------------
Dashboard v2 module for MarketLens.

RESPONSIBILITY:
Build a complete, self-contained HTML investment report from the
pipeline's final outputs — recommendations, sector stats, market
data, risk, portfolio simulation, upgrade/downgrade history, price
charts — with NOTHING removed or trimmed versus earlier versions.

STRUCTURE (v2 — restructured from a single long scrolling page):
A sticky left sidebar with anchor links to each section — Rezumat,
Watchlist (if any), Sectoare, Portofoliu, Date de piață, Schimbări —
so the whole report is still ONE continuous page (nothing hidden,
nothing lost), but with constant orientation instead of blind
scrolling. Uses plain HTML anchor links (`<a href="#id">`), so jumping
between sections works even with JavaScript disabled.

WHAT'S NEW IN v2 vs v1:
1. Watchlist is now a DEDICATED, PINNED SECTION at the top — not a
   filter that hides everything else. Watchlist entities are pulled
   out of the sector-grouped body (so they're never shown twice) and
   shown first, in full card detail, regardless of which sector
   they're in.
2. Each card's hidden "argument" now includes a REAL, CLICKABLE LINK
   to the representative source article — not just its title as
   plain text — so the person can go verify the actual news behind a
   recommendation in one click.

SAFETY: every piece of user-facing text is HTML-escaped before
insertion, and every value embedded inside an inline <script> block
(for Chart.js) is JSON-serialized with "</" escaped, so a stray
article title can never break the page structure or inject a script.
"""

import html as html_lib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


class DashboardGenerator:
    """
    Builds a self-contained HTML investment report: sidebar navigation,
    a pinned watchlist section, sector-grouped recommendation cards
    (each with a confidence bar, badges, a price sparkline, and a
    hidden, source-linked argument), a portfolio chart, and a market
    data table.
    """

    _RECOMMENDATION_COLORS = {"BUY": "#3ecf7e", "SELL": "#f0645f", "HOLD": "#8a8f98"}

    _SECTOR_TINTS = {
        "positive": "#0f3d24",
        "negative": "#3d1518",
        "mixed": "#3d3312",
        "neutral": "#1a1f2e",
    }

    _UNCATEGORIZED_SECTOR = "Altele"

    def _escape(self, value: Any) -> str:
        """HTML-escape any value before inserting it into the page — the single point every piece of text passes through."""
        if value is None:
            return ""
        return html_lib.escape(str(value))

    def _json_for_script(self, data: Any) -> str:
        """Serialize data for safe embedding inside an inline <script> block, escaping "</" as defense in depth."""
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    def _anchor_id(self, text: str) -> str:
        """Build a safe HTML id/anchor slug from arbitrary text (sector names, entity names, etc.)."""
        return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")

    def _render_confidence_bar(self, confidence_score: Optional[float]) -> str:
        """Render a small horizontal confidence bar, color-coded green/amber/red by score."""
        if confidence_score is None:
            return ""
        pct = max(0, min(100, round(confidence_score * 100)))
        if confidence_score >= 0.7:
            color = "#3ecf7e"
        elif confidence_score >= 0.5:
            color = "#e8c547"
        else:
            color = "#8a8f98"
        return f"""
        <div class="conf-track"><div class="conf-fill" style="width:{pct}%; background:{color};"></div></div>
        <div class="conf-label">{self._escape(confidence_score)}</div>
        """

    def _render_verified_badge(
        self, entity: str, verified_track_record: Optional[Dict[str, Optional[bool]]]
    ) -> str:
        """Render a checkmark/cross badge if this entity's most recent recommendation has already been checked by Backtest Engine."""
        if not verified_track_record or entity not in verified_track_record:
            return ""
        was_correct = verified_track_record[entity]
        if was_correct is None:
            return ""
        if was_correct:
            return '<span class="badge-pill verified-ok">correct verificat</span>'
        return '<span class="badge-pill verified-bad">gresit verificat</span>'

    def _change_badge(self, entity: str, upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]]) -> str:
        """Render an upgrade/downgrade badge if this entity's recommendation changed since it was last logged."""
        if not upgrade_downgrade_map or entity not in upgrade_downgrade_map:
            return ""
        change = upgrade_downgrade_map[entity].get("change")
        if change == "upgrade":
            return '<span class="badge-pill change-up">upgrade</span>'
        if change == "downgrade":
            return '<span class="badge-pill change-down">downgrade</span>'
        return ""

    def _render_breakdown(self, rec: Dict[str, Any]) -> str:
        """Render the Volume/Diversity/Consistency/Impact chips backing a recommendation's confidence score."""
        chips = []
        if rec.get("volume_score") is not None:
            chips.append(f'<span class="chip">Volum {self._escape(rec["volume_score"])}/1</span>')
        if rec.get("source_diversity_score") is not None:
            chips.append(f'<span class="chip">Diversitate surse {self._escape(rec["source_diversity_score"])}/1</span>')
        if rec.get("sentiment_consistency") is not None:
            chips.append(f'<span class="chip">Consistență {self._escape(rec["sentiment_consistency"])}/1</span>')
        if rec.get("average_impact") is not None:
            chips.append(f'<span class="chip">Impact {self._escape(rec["average_impact"])}/1</span>')
        if not chips:
            return ""
        return f'<div class="breakdown">{"".join(chips)}</div>'

    def _representative_article(
        self, rec: Dict[str, Any], entity_articles: Optional[List[Dict[str, Any]]]
    ) -> str:
        """
        Pick the single highest-impact article backing this entity's
        recommendation and render it as a clickable link to the real
        source — so the person can verify the actual news in one click.
        Returns an empty string if no articles are available.
        """
        if not entity_articles:
            return ""

        def impact_of(article: Dict[str, Any]) -> float:
            return (article.get("impact") or {}).get("score", 0.0) or 0.0

        best = max(entity_articles, key=impact_of, default=None)
        if not best or not best.get("title"):
            return ""

        title = self._escape(best["title"])
        source = self._escape(best.get("source", ""))
        url = best.get("url")

        label = f'"{title}" — {source}' if source else f'"{title}"'
        if url:
            safe_url = self._escape(url)
            return f'<a class="source-link" href="{safe_url}" target="_blank" rel="noopener noreferrer">Vezi articolul sursa: {label}</a>'
        return f'<span class="source-link-inactive">{label}</span>'

    def _render_price_sparkline(self, entity: str, price_history: Optional[List[Dict[str, Any]]]) -> str:
        """Render one small sparkline chart (<canvas> + Chart.js config) for a single entity's recent closing prices."""
        if not price_history:
            return ""

        canvas_id = "spark-" + self._anchor_id(entity)
        labels = [self._escape(p.get("date", "")) for p in price_history]
        values = [p.get("close") for p in price_history]
        is_up = len(values) >= 2 and values[-1] is not None and values[0] is not None and values[-1] >= values[0]
        line_color = "#3ecf7e" if is_up else "#f0645f"

        return f"""
        <canvas id="{canvas_id}" height="26" class="sparkline"></canvas>
        <script>
          (function() {{
            var el = document.getElementById('{canvas_id}');
            if (el && window.Chart) {{
              new Chart(el, {{
                type: 'line',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    data: {self._json_for_script(values)},
                    borderColor: '{line_color}',
                    borderWidth: 1.5,
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                  }}]
                }},
                options: {{
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: false }} }},
                  scales: {{ x: {{ display: false }}, y: {{ display: false }} }},
                  elements: {{ point: {{ radius: 0 }} }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_recommendation_card(
        self,
        rec: Dict[str, Any],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]] = None,
        verified_track_record: Optional[Dict[str, Optional[bool]]] = None,
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        pinned: bool = False,
    ) -> str:
        """Render one entity as a styled card: confidence bar, badges, a price sparkline, and a hidden, source-linked argument."""
        color = self._RECOMMENDATION_COLORS.get(rec["recommendation"], "#8a8f98")
        horizon = rec.get("time_horizon")
        horizon_badge = f'<span class="badge-pill">{self._escape(horizon)}</span>' if horizon else ""
        change_badge = self._change_badge(rec["entity"], upgrade_downgrade_map)
        verified_badge = self._render_verified_badge(rec["entity"], verified_track_record)
        entity_articles = (entity_articles_map or {}).get(rec["entity"])
        representative = self._representative_article(rec, entity_articles)
        sparkline = self._render_price_sparkline(rec["entity"], (price_history_map or {}).get(rec["entity"]))
        pin_class = " pinned" if pinned else ""
        pin_icon = '<span class="pin-icon">*</span>' if pinned else ""

        return f"""
        <div class="rec-card{pin_class}" style="box-shadow: inset 3px 0 0 {color};" data-search="{self._escape(rec['entity'].lower())}">
          <div class="rc-top">
            <div>
              <div class="rc-name">{pin_icon}{self._escape(rec['entity'])}</div>
              <div class="rc-tags">{horizon_badge}{change_badge}{verified_badge}</div>
            </div>
            <span class="rc-verdict" style="background:{color}22; color:{color};">{self._escape(rec['recommendation'])}</span>
          </div>
          {sparkline}
          {self._render_confidence_bar(rec.get('confidence_score'))}
          <details class="argument">
            <summary><span class="icon">i</span> Vezi argumentul</summary>
            <div class="argument-body">
              {self._escape(rec.get('explanation', ''))}
              {self._render_breakdown(rec)}
              {representative}
            </div>
          </details>
        </div>
        """

    def _group_by_sector(
        self, recommendations: List[Dict[str, Any]], entity_sector_map: Optional[Dict[str, str]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group recommendations by sector, using entity_sector_map. Unmapped entities fall back to an 'Altele' group."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        entity_sector_map = entity_sector_map or {}
        for rec in recommendations:
            sector = entity_sector_map.get(rec["entity"], self._UNCATEGORIZED_SECTOR)
            grouped.setdefault(sector, []).append(rec)
        return grouped

    def _render_sector_section(
        self,
        sector_name: str,
        sector_recs: List[Dict[str, Any]],
        sector_scores_by_name: Dict[str, Dict[str, Any]],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]],
        verified_track_record: Optional[Dict[str, Optional[bool]]],
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]],
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> str:
        """Render one sector section: header with sector-level stats, then its entity cards."""
        stats = sector_scores_by_name.get(sector_name, {})
        article_count = stats.get("article_count", "-")
        source_count = stats.get("distinct_source_count", "-")
        dominant = stats.get("dominant_sentiment", "neutral")
        consistency = stats.get("sentiment_consistency")
        tint = self._SECTOR_TINTS.get(dominant, "#1a1f2e")
        consistency_text = f" · consistență {round(consistency * 100)}%" if consistency is not None else ""

        slug = self._anchor_id(sector_name)
        cards = "".join(
            self._render_recommendation_card(
                r, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map
            )
            for r in sector_recs
        )

        return f"""
        <div class="sector-block" id="sector-{slug}">
          <div class="sector-header" style="background:{tint};">
            <span class="sector-name">{self._escape(sector_name)}</span>
            <span class="sector-meta">{self._escape(article_count)} articole · {self._escape(source_count)} surse</span>
            <span class="sentiment-tag">{self._escape(dominant)}{consistency_text}</span>
          </div>
          <div class="rec-cards">{cards}</div>
        </div>
        """

    def _render_watchlist_section(
        self,
        watchlist_recs: List[Dict[str, Any]],
        upgrade_downgrade_map: Optional[Dict[str, Dict[str, Any]]],
        verified_track_record: Optional[Dict[str, Optional[bool]]],
        entity_articles_map: Optional[Dict[str, List[Dict[str, Any]]]],
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]],
    ) -> str:
        """Render the pinned Watchlist section — full-detail cards for the person's own chosen companies, shown first."""
        if not watchlist_recs:
            return ""
        cards = "".join(
            self._render_recommendation_card(
                r, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map, pinned=True
            )
            for r in watchlist_recs
        )
        return f"""
        <div class="section-title" id="watchlist">
          Watchlist-ul tau
          <span class="section-hint">companiile pe care le urmaresti, mereu sus, indiferent de sector</span>
        </div>
        <div class="rec-cards">{cards}</div>
        """

    def _render_market_data_table(
        self,
        market_data: Optional[Dict[str, Dict[str, Any]]],
        risk_data: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Render a factual market-data table — never a valuation verdict."""
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
                <tr>
                  <td>{self._escape(ticker)}</td>
                  <td colspan="6" class="empty-state">{self._escape(snap["error"])}</td>
                </tr>
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
          <thead><tr><th>Ticker</th><th>Preț curent</th><th>Variație zilnică</th><th>Față de max 52săpt</th><th>Față de min 52săpt</th><th>P/E</th><th>Risc (volatilitate anualizată)</th></tr></thead>
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
        <div class="kpi-row">
          <div class="kpi"><div class="n">${self._escape(r['total_invested'])}</div><div class="l">Investit (simulat)</div></div>
          <div class="kpi"><div class="n">${self._escape(r['total_final_value'])}</div><div class="l">Valoare finală</div></div>
          <div class="kpi"><div class="n" style="color:{color}">{self._escape(r['total_return_pct'])}%</div><div class="l">Randament total</div></div>
          <div class="kpi"><div class="n">{self._escape(r['trades_simulated'])}</div><div class="l">Tranzacții simulate</div></div>
        </div>
        """

    def _render_portfolio_chart(self, portfolio_history: Optional[List[Dict[str, Any]]]) -> str:
        """Render the portfolio-return-over-time line chart (a <canvas> + Chart.js config)."""
        if not portfolio_history:
            return '<p class="empty-state">Niciun istoric de portofoliu încă — apare pe măsură ce pipeline-ul rulează automat, zi de zi.</p>'

        labels = [self._escape((s.get("recorded_at") or "")[:10]) for s in portfolio_history]
        values = [s.get("total_return_pct") for s in portfolio_history]

        return f"""
        <canvas id="portfolioChart" height="70"></canvas>
        <script>
          (function() {{
            if (window.Chart) {{
              new Chart(document.getElementById('portfolioChart'), {{
                type: 'line',
                data: {{
                  labels: {self._json_for_script(labels)},
                  datasets: [{{
                    label: 'Randament portofoliu simulat (%)',
                    data: {self._json_for_script(values)},
                    borderColor: '#4a90d9',
                    backgroundColor: 'rgba(74,144,217,0.15)',
                    fill: true,
                    tension: 0.25,
                    pointRadius: 2,
                  }}]
                }},
                options: {{
                  responsive: true,
                  plugins: {{ legend: {{ display: false }} }},
                  scales: {{
                    x: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }},
                    y: {{ ticks: {{ color: '#9aa0a6' }}, grid: {{ color: '#20232b' }} }}
                  }}
                }}
              }});
            }}
          }})();
        </script>
        """

    def _render_changes_section(self, upgrade_downgrade_results: Optional[List[Dict[str, Any]]]) -> str:
        """Render the list of entities whose recommendation changed since it was last logged."""
        if not upgrade_downgrade_results:
            return '<p class="empty-state">Nicio schimbare de recomandare de urmărit încă.</p>'
        changes = [r for r in upgrade_downgrade_results if r.get("change") in ("upgrade", "downgrade")]
        if not changes:
            return '<p class="empty-state">Nicio schimbare azi — toate recomandările au rămas neschimbate.</p>'
        lines = []
        for r in changes:
            arrow = "up" if r["change"] == "upgrade" else "down"
            color = "#3ecf7e" if r["change"] == "upgrade" else "#f0645f"
            lines.append(
                f'<div class="change-line" style="color:{color};">{arrow} <b>{self._escape(r["entity"])}</b> '
                f'{self._escape(r.get("previous"))} -&gt; {self._escape(r.get("current"))}</div>'
            )
        return "".join(lines)

    def _render_sidebar(
        self,
        sector_names: List[str],
        total_entities: int,
        watchlist_count: int,
        changes_count: int,
    ) -> str:
        """Render the sticky left sidebar with anchor links to every section of the report."""
        watchlist_item = (
            f'<a class="nav-item star" href="#watchlist">Watchlist <span class="count">{watchlist_count}</span></a>'
            if watchlist_count else ""
        )
        return f"""
        <div class="sidebar">
          <div class="brand">MarketLens</div>
          <a class="nav-item" href="#rezumat">Rezumat</a>
          {watchlist_item}
          <a class="nav-item" href="#sectoare">Sectoare <span class="count">{len(sector_names)}</span></a>
          <a class="nav-item" href="#portofoliu">Portofoliu</a>
          <a class="nav-item" href="#piata">Date de piață</a>
          <a class="nav-item" href="#schimbari">Schimbări <span class="count">{changes_count}</span></a>
          <div class="nav-footer">{total_entities} entități urmărite</div>
        </div>
        """

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
        price_history_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        portfolio_history: Optional[List[Dict[str, Any]]] = None,
        watchlist: Optional[List[str]] = None,
        upgrade_downgrade_results: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Build the full HTML report as a single string.

        Args:
            recommendations: output of RecommendationEngine.recommend_all()
            articles: underlying processed articles; optional (kept for API compatibility)
            db_stats: output of NewsDatabase.get_stats(); optional
            market_data: output of MarketDataFetcher.get_snapshots_batch(); optional
            risk_data: output of RiskScoreCalculator.get_risk_scores_batch(); optional
            sector_scores: output of SectorAggregator.score_all_sectors(); optional
            upgrade_downgrade_map: entity -> UpgradeDowngradeTracker.compare_entity() result; optional
            portfolio_result: output of PortfolioSimulator.simulate(); optional
            daily_summary_text: output of DailySummaryGenerator.generate(); optional
            entity_sector_map: entity name -> sector name; drives sector grouping.
            verified_track_record: entity -> True/False/None, most recent Backtest Engine outcome.
            entity_articles_map: entity -> its articles, used to pick the
                representative source article (now shown as a clickable link).
            price_history_map: entity name -> price history series (sparklines).
            portfolio_history: output of PortfolioHistory.load_all().
            watchlist: optional list of entity names (case-insensitive).
                UNLIKE v1, this does NOT filter out everything else — it
                PINS those entities in a dedicated "Watchlist" section at
                the top, removed from their sector group below (so
                they're never shown twice). None/empty means no pinned
                section; the rest of the report is unaffected either way.
            upgrade_downgrade_results: the full list (not just the map) —
                used to render the "Schimbări" section. Derived from
                upgrade_downgrade_map's values if omitted.

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

        watchlist_recs: List[Dict[str, Any]] = []
        remaining_recs = recommendations
        if watchlist:
            watchlist_lower = {name.lower() for name in watchlist}
            watchlist_recs = [r for r in recommendations if r["entity"].lower() in watchlist_lower]
            remaining_recs = [r for r in recommendations if r["entity"].lower() not in watchlist_lower]

        sector_scores_by_name = {s["sector"]: s for s in sector_scores}
        grouped = self._group_by_sector(remaining_recs, entity_sector_map)
        sector_names = sorted(
            grouped.keys(),
            key=lambda name: sector_scores_by_name.get(name, {}).get("article_count", 0),
            reverse=True,
        )

        sector_sections = "".join(
            self._render_sector_section(
                name, grouped[name], sector_scores_by_name,
                upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map,
            )
            for name in sector_names
        )

        watchlist_section = self._render_watchlist_section(
            watchlist_recs, upgrade_downgrade_map, verified_track_record, entity_articles_map, price_history_map,
        )

        if upgrade_downgrade_results is None:
            upgrade_downgrade_results = list((upgrade_downgrade_map or {}).values())
        changes_count = sum(1 for r in upgrade_downgrade_results if r.get("change") in ("upgrade", "downgrade"))

        sidebar = self._render_sidebar(sector_names, len(recommendations), len(watchlist_recs), changes_count)

        return f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<title>MarketLens — Raport de Investiții</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0a0b0f; color:#eef0f3; margin:0; display:flex; }}

  .sidebar {{ width:190px; flex-shrink:0; height:100vh; position:sticky; top:0; background:#0f1218; border-right:1px solid #1f2333; padding:20px 0; overflow-y:auto; }}
  .sidebar .brand {{ padding:0 16px 16px 16px; font-size:13px; font-weight:800; border-bottom:1px solid #1f2333; margin-bottom:12px; }}
  .nav-item {{ display:flex; justify-content:space-between; padding:10px 16px; font-size:12px; color:#7a8bb0; text-decoration:none; }}
  .nav-item:hover {{ background:#151a28; color:#eef0f3; }}
  .nav-item.star {{ color:#e8c547; }}
  .nav-item .count {{ color:#4a5063; font-size:10px; }}
  .nav-footer {{ padding:14px 16px; font-size:10px; color:#4a5063; border-top:1px solid #1f2333; margin-top:12px; }}

  .main {{ flex:1; padding:24px 32px; max-width:900px; }}
  .hero-eyebrow {{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:#6d7a99; margin-bottom:6px; }}
  .hero-number {{ font-size:32px; font-weight:800; background: linear-gradient(90deg, #3ecf7e, #4a90d9); -webkit-background-clip:text; background-clip:text; color:transparent; }}
  .hero-summary {{ font-size:13px; color:#c2c8d6; margin-top:10px; line-height:1.6; background:#151a28; border-radius:8px; padding:12px 16px; }}

  .kpi-row {{ display:flex; gap:10px; margin:16px 0; }}
  .kpi {{ background:#151a28; border-radius:10px; padding:14px; flex:1; text-align:center; }}
  .kpi .n {{ font-size:20px; font-weight:800; }}
  .kpi .l {{ font-size:9px; color:#8a8f98; text-transform:uppercase; margin-top:2px; }}

  .search-box {{ width:100%; background:#151a28; border:1px solid #262c3d; border-radius:8px; padding:9px 14px; color:#eef0f3; font-size:13px; margin:8px 0 20px 0; }}
  .search-box::placeholder {{ color:#5f6673; }}

  .section-title {{ font-size:13px; font-weight:800; text-transform:uppercase; letter-spacing:0.5px; color:#eef0f3; margin:32px 0 14px 0; padding-top:10px; scroll-margin-top:16px; }}
  .section-hint {{ font-size:10px; color:#5a6178; font-weight:400; text-transform:none; margin-left:8px; }}

  .rec-cards {{ display:grid; grid-template-columns: repeat(2, 1fr); gap:12px; }}
  .rec-card {{ background:#151a28; border-radius:12px; padding:14px 16px; }}
  .rec-card.pinned {{ box-shadow: 0 0 0 1px #e8c54755, inset 3px 0 0 #e8c547 !important; }}
  .rc-top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
  .rc-name {{ font-size:14px; font-weight:700; }}
  .pin-icon {{ color:#e8c547; margin-right:5px; }}
  .rc-tags {{ display:flex; gap:5px; flex-wrap:wrap; margin-top:4px; }}
  .badge-pill {{ font-size:9px; font-weight:700; background:#1c2029; color:#9aa0a6; padding:2px 7px; border-radius:8px; }}
  .badge-pill.change-up {{ background:#12261a; color:#3ecf7e; }}
  .badge-pill.change-down {{ background:#2a1518; color:#f0645f; }}
  .badge-pill.verified-ok {{ background:#12261a; color:#3ecf7e; }}
  .badge-pill.verified-bad {{ background:#2a1518; color:#f0645f; }}
  .rc-verdict {{ font-size:12px; font-weight:800; padding:3px 10px; border-radius:6px; }}
  .sparkline {{ width:100%; max-height:28px; margin-top:8px; }}

  .conf-track {{ height:5px; border-radius:3px; background:#1f2333; margin-top:10px; overflow:hidden; }}
  .conf-fill {{ height:100%; }}
  .conf-label {{ font-size:10px; color:#6d7a99; margin-top:3px; }}

  details.argument {{ margin-top:8px; }}
  details.argument summary {{ cursor:pointer; list-style:none; font-size:11px; color:#7a8bb0; display:flex; align-items:center; gap:5px; user-select:none; }}
  details.argument summary::-webkit-details-marker {{ display:none; }}
  details.argument summary .icon {{ display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; background:#1f2740; color:#9db4e8; font-size:10px; font-weight:800; }}
  details.argument[open] summary .icon {{ background:#4a90d9; color:#fff; }}
  .argument-body {{ background:#0e1220; border-radius:8px; padding:10px 12px; margin-top:8px; font-size:11.5px; line-height:1.5; color:#c2c8d6; }}
  .breakdown {{ display:flex; gap:6px; flex-wrap:wrap; margin:8px 0; }}
  .breakdown .chip {{ font-size:9.5px; background:#1a1f30; color:#9db4e8; padding:3px 8px; border-radius:6px; }}
  .source-link {{ display:block; color:#4a90d9; text-decoration:none; margin-top:6px; }}
  .source-link:hover {{ text-decoration:underline; }}
  .source-link-inactive {{ display:block; color:#6d7a99; font-style:italic; margin-top:6px; }}

  .sector-block {{ margin-bottom:24px; scroll-margin-top:16px; }}
  .sector-header {{ display:flex; align-items:center; gap:12px; padding:12px 16px; border-radius:10px; margin-bottom:12px; }}
  .sector-name {{ font-size:14px; font-weight:800; text-transform:uppercase; letter-spacing:0.5px; }}
  .sector-meta {{ font-size:11px; opacity:0.75; }}
  .sentiment-tag {{ margin-left:auto; font-size:10px; font-weight:700; padding:3px 10px; border-radius:12px; background:rgba(255,255,255,0.12); }}

  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:#7a8bb0; font-weight:600; padding:8px 10px; border-bottom:1px solid #1f2333; font-size:10px; text-transform:uppercase; }}
  td {{ padding:8px 10px; border-bottom:1px solid #161a24; }}

  .change-line {{ font-size:13px; padding:8px 0; border-bottom:1px solid #161a24; }}
  .empty-state {{ color:#6d7a99; font-size:13px; font-style:italic; }}
  .footer {{ margin-top:40px; font-size:11px; color:#4a5063; padding-bottom:24px; }}
</style>
</head>
<body>
  {sidebar}

  <div class="main">
    <div id="rezumat"></div>
    <div class="hero-eyebrow">MarketLens · {generated_at}</div>
    <div class="hero-number">{counts['BUY']} BUY · {counts['SELL']} SELL · {counts['HOLD']} HOLD</div>
    {f'<div class="hero-summary">{self._escape(daily_summary_text)}</div>' if daily_summary_text else ''}

    <div class="kpi-row">
      <div class="kpi"><div class="n" style="color:#3ecf7e">{counts['BUY']}</div><div class="l">Buy</div></div>
      <div class="kpi"><div class="n" style="color:#f0645f">{counts['SELL']}</div><div class="l">Sell</div></div>
      <div class="kpi"><div class="n">{counts['HOLD']}</div><div class="l">Hold</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('total_articles', '-'))}</div><div class="l">Articole</div></div>
      <div class="kpi"><div class="n">{self._escape(db_stats.get('distinct_sources', '-'))}</div><div class="l">Surse</div></div>
    </div>

    <input class="search-box" type="text" placeholder="Caută o companie..." oninput="marketlensFilter(this.value)">

    {watchlist_section}

    <div class="section-title" id="sectoare">Sectoare <span class="section-hint">{len(sector_names)} sectoare, toate entitățile urmărite</span></div>
    {sector_sections}

    <div class="section-title" id="portofoliu">Simulare portofoliu <span class="section-hint">(recomandări deja verificate prin Backtest)</span></div>
    {self._render_portfolio_summary(portfolio_result)}
    <div style="margin-top:16px;">{self._render_portfolio_chart(portfolio_history)}</div>

    <div class="section-title" id="piata">Date de piață <span class="section-hint">(fapte reale — fără verdict de subevaluare/supraevaluare)</span></div>
    {self._render_market_data_table(market_data, risk_data)}

    <div class="section-title" id="schimbari">Schimbări recente</div>
    {self._render_changes_section(upgrade_downgrade_results)}

    <div class="footer">MarketLens — raport generat automat. Nu constituie sfat financiar.</div>
  </div>

  <script>
    function marketlensFilter(query) {{
      var q = query.trim().toLowerCase();
      document.querySelectorAll('.rec-card').forEach(function(card) {{
        var match = card.getAttribute('data-search').indexOf(q) !== -1;
        card.style.display = match ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>"""

    def save_report(self, html: str, path: str) -> None:
        """Write the generated HTML report to a file."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
