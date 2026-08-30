"""
Scores stored signals against realized outcomes, persists the results,
and renders a minimal inspection page (Phase 10, spec §29, §30, §47).

WHY SCORING AND THE PAGE LIVE IN ONE SCRIPT
-----------------------------------------------
The page shows exactly what was just computed. Splitting them would
create a window where the page displays yesterday's evaluation next to
today's signals, which is the kind of quiet inconsistency that erodes
trust in a dashboard faster than a missing feature does.

THE PAGE IS FOR INSPECTION, NOT FOR ACTING (spec §47)
---------------------------------------------------------
It shows what the system claimed, why, and how those claims turned
out. It has no buttons, no order entry, no position sizing — nothing
that could be mistaken for a trading interface. Suppressed signals are
shown alongside active ones WITH their reasons, because a page that
displayed only the signals that passed would misrepresent how
selective the system actually is.

OUTCOMES COME FROM PHASE 7 LABELS, NOT FROM A FRESH PRICE PULL
------------------------------------------------------------------
The realized return is the label Phase 7 already recorded and
timestamped. Re-deriving it from prices here would be a second
implementation of "what happened", and the two would eventually
disagree. The label's measured_at is what proves it resolved after the
signal's cutoff.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- Signals and labels are read-only inputs; outcomes are written once
  per (signal, horizon) and never rewritten with a different result.
- --dry-run computes and reports without writing or rendering.
"""

from __future__ import annotations

import argparse
import html
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

from src.data_access.signal_outcome_schema import initialize_signal_outcome_schema
from src.data_access.signal_repository import SignalRepository
from src.data_access.signal_schema import initialize_signal_schema
from src.domain.signal_models import SignalStatus
from src.signals.evaluation import (
    MIN_SAMPLE, OutcomeScoringError, SignalOutcome, evaluate_by, evaluate_cohort,
    score_signal,
)

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")
DEFAULT_PAGE = os.path.join(REPO_ROOT, "docs", "signals.html")

#: Horizons to score, mapped to the Phase 7 label that resolves them.
HORIZONS = {"d1": "d1.abnormal_return", "d5": "d5.abnormal_return",
            "d20": "d20.abnormal_return"}


def load_labels(conn: sqlite3.Connection, observation_ids: List[str],
                label_name: str) -> Dict[str, tuple]:
    """Realized labels keyed by observation, as (value, measured_at)."""
    if not observation_ids:
        return {}
    placeholders = ",".join("?" * len(observation_ids))
    rows = conn.execute(f"""
        SELECT observation_id, value_json, measured_at FROM research_labels
        WHERE observation_id IN ({placeholders}) AND name = ?
    """, observation_ids + [label_name]).fetchall()

    labels = {}
    for observation_id, value_json, measured_at in rows:
        try:
            value = json.loads(value_json) if value_json else None
        except (ValueError, TypeError):
            value = None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        labels[observation_id] = (
            float(value),
            datetime.fromisoformat(measured_at) if measured_at else None)
    return labels


def persist_outcome(conn: sqlite3.Connection, outcome: SignalOutcome) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO signal_outcomes (
            signal_id, horizon, realized_return, realized_direction, signal_direction,
            expected_return, strength, confidence, direction_correct, error,
            absolute_error, strategy_id, strategy_version, market_regime, event_type,
            confidence_bucket, label_name, measured_at, scored_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        outcome.signal_id, outcome.horizon, outcome.realized_return,
        outcome.realized_direction, outcome.signal_direction, outcome.expected_return,
        outcome.strength, outcome.confidence,
        None if outcome.direction_correct is None else int(outcome.direction_correct),
        outcome.error, outcome.absolute_error, outcome.strategy_id,
        outcome.strategy_version, outcome.market_regime, outcome.event_type,
        outcome.confidence_bucket, outcome.label_name,
        outcome.measured_at.isoformat() if outcome.measured_at else None,
        outcome.scored_at.isoformat() if outcome.scored_at else None,
    ))


def persist_evaluation(conn: sqlite3.Connection, evaluation) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO signal_evaluations (
            evaluation_id, cohort_kind, cohort_value, horizon, sample_size,
            instrument_count, hit_rate, mean_return, median_return,
            mean_absolute_error, mean_expected_return, return_stdev,
            baseline_hit_rate, beats_baseline, small_sample, notes_json, evaluated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        evaluation.evaluation_id, evaluation.cohort_kind, evaluation.cohort_value,
        evaluation.horizon, evaluation.sample_size, evaluation.instrument_count,
        evaluation.hit_rate, evaluation.mean_return, evaluation.median_return,
        evaluation.mean_absolute_error, evaluation.mean_expected_return,
        evaluation.return_stdev, evaluation.baseline_hit_rate,
        None if evaluation.beats_baseline is None else int(evaluation.beats_baseline),
        int(evaluation.small_sample), json.dumps(evaluation.notes),
        evaluation.evaluated_at.isoformat() if evaluation.evaluated_at else None,
    ))


def _fmt(value, digits=4, suffix=""):
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def render_page(conn: sqlite3.Connection, path: str) -> None:
    """Render the inspection page. Read-only; shows suppressed signals too."""
    signals = conn.execute("""
        SELECT signal_id, instrument_id, direction, status, strength, confidence,
               expected_return, agreement_state, strategy_id, strategy_version,
               source_information_cutoff, explanation_summary, suppression_note
        FROM signals ORDER BY source_information_cutoff DESC LIMIT 200
    """).fetchall()

    suppression_counts = conn.execute("""
        SELECT reason, COUNT(*) FROM signal_suppressions GROUP BY reason ORDER BY 2 DESC
    """).fetchall()

    evaluations = conn.execute("""
        SELECT cohort_kind, cohort_value, horizon, sample_size, hit_rate,
               baseline_hit_rate, beats_baseline, mean_return, small_sample
        FROM signal_evaluations ORDER BY cohort_kind, cohort_value
    """).fetchall()

    status_counts = conn.execute(
        "SELECT status, COUNT(*) FROM signals GROUP BY status ORDER BY 2 DESC").fetchall()

    def rows_signals():
        out = []
        for (sid, instrument, direction, status, strength, confidence, expected,
             agreement, strategy, version, cutoff, summary, note) in signals:
            badge = "ok" if status == "active" else "warn"
            reason = html.escape(note or "")
            out.append(f"""<tr>
<td class="mono">{html.escape(instrument)}</td>
<td><span class="dir {html.escape(direction)}">{html.escape(direction)}</span></td>
<td><span class="badge {badge}">{html.escape(status)}</span></td>
<td class="num">{_fmt(strength, 3)}</td>
<td class="num">{_fmt(confidence, 3)}</td>
<td class="num">{_fmt(expected, 4)}</td>
<td>{html.escape(agreement or '')}</td>
<td class="mono small">{html.escape((cutoff or '')[:16])}</td>
<td class="small">{html.escape(summary or '')}{f'<div class="reason">{reason}</div>' if reason else ''}</td>
</tr>""")
        return "\n".join(out) or '<tr><td colspan="9">Niciun semnal in baza.</td></tr>'

    def rows_suppressions():
        out = [f'<tr><td>{html.escape(r)}</td><td class="num">{c}</td></tr>'
               for r, c in suppression_counts]
        return "\n".join(out) or '<tr><td colspan="2">Nicio suprimare.</td></tr>'

    def rows_evaluations():
        out = []
        for (kind, value, horizon, n, hit, baseline, beats, mean_ret, small) in evaluations:
            verdict = ("—" if beats is None else
                       '<span class="badge ok">da</span>' if beats
                       else '<span class="badge warn">nu</span>')
            flag = ' <span class="badge warn">esantion mic</span>' if small else ""
            out.append(f"""<tr>
<td>{html.escape(kind)}</td><td>{html.escape(value)}</td><td>{html.escape(horizon)}</td>
<td class="num">{n}{flag}</td>
<td class="num">{_fmt(hit, 3)}</td>
<td class="num">{_fmt(baseline, 3)}</td>
<td>{verdict}</td>
<td class="num">{_fmt(mean_ret, 4)}</td>
</tr>""")
        return "\n".join(out) or '<tr><td colspan="8">Nicio evaluare inca.</td></tr>'

    status_line = " · ".join(f"{html.escape(s)}: {c}" for s, c in status_counts) or "niciun semnal"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    page = f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MarketLens — Semnale (Faza 10)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Sans+3:wght@400;500;600&display=swap" rel="stylesheet">
<style>
* {{ box-sizing:border-box; }}
body {{ font-family:'Source Sans 3',Georgia,serif; background:#0d0c0a; color:#eae6da; margin:0; }}
.masthead {{ text-align:center; border-bottom:4px double #eae6da; padding:26px 20px 14px; }}
.masthead h1 {{ font-family:'Playfair Display',serif; font-size:34px; font-weight:900; margin:0; color:#f5f1e6; }}
.masthead .sub {{ font-size:11px; letter-spacing:3px; text-transform:uppercase; color:#8c8470; margin-top:6px; }}
.wrap {{ max-width:1200px; margin:0 auto; padding:22px 18px 60px; }}
.notice {{ border:1px solid #5a4a2a; background:#1a1710; padding:14px 16px; margin:18px 0; font-size:13px; line-height:1.6; color:#cdc5ad; }}
.notice strong {{ color:#d4915a; }}
h2 {{ font-family:'Playfair Display',serif; font-size:20px; margin:30px 0 10px; color:#f5f1e6; border-bottom:1px solid #33301f; padding-bottom:6px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; font-size:10px; letter-spacing:1.4px; text-transform:uppercase; color:#8c8470; border-bottom:1px solid #33301f; padding:8px 6px; }}
td {{ padding:8px 6px; border-bottom:1px solid #1e1c14; vertical-align:top; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.mono {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; }}
.small {{ font-size:12px; color:#b6ae97; }}
.reason {{ color:#c07a4a; font-size:11px; margin-top:3px; }}
.badge {{ display:inline-block; padding:1px 7px; border-radius:2px; font-size:10px; letter-spacing:.8px; text-transform:uppercase; }}
.badge.ok {{ background:#22331f; color:#8fbf7a; }}
.badge.warn {{ background:#33291f; color:#d4915a; }}
.dir {{ font-weight:600; }}
.dir.long {{ color:#8fbf7a; }} .dir.short {{ color:#cf7a6a; }}
.dir.neutral, .dir.no_signal {{ color:#8c8470; }}
.foot {{ margin-top:34px; font-size:11px; color:#6e6857; text-align:center; }}
</style>
</head>
<body>
<div class="masthead">
  <h1>Semnale</h1>
  <div class="sub">MarketLens · Faza 10 · pagina de inspectie</div>
</div>
<div class="wrap">

<div class="notice">
<strong>Aceasta pagina este doar pentru inspectie.</strong> Nu contine ordine, marimi de
pozitie sau actiuni de tranzactionare. Un semnal este o afirmatie despre un instrument,
nu o recomandare de a tranzactiona; alocarea si riscul apartin unei faze ulterioare.
<br><br>
Semnalele <em>suprimate</em> sunt afisate deliberat alaturi de cele active, cu motivele lor.
O pagina care ar arata doar semnalele trecute de validare ar ascunde cat de selectiv
este de fapt sistemul. Evaluarile marcate <span class="badge warn">esantion mic</span>
(sub {MIN_SAMPLE} observatii) sunt descriptive, nu concluzive.
</div>

<p class="small">Stare curenta: {status_line}</p>

<h2>Evaluari</h2>
<table>
<thead><tr><th>Cohorta</th><th>Valoare</th><th>Orizont</th><th>N</th>
<th>Rata de succes</th><th>Baseline</th><th>Bate baseline</th><th>Randament mediu</th></tr></thead>
<tbody>{rows_evaluations()}</tbody>
</table>

<h2>Motive de suprimare</h2>
<table>
<thead><tr><th>Motiv</th><th>Numar</th></tr></thead>
<tbody>{rows_suppressions()}</tbody>
</table>

<h2>Semnale recente</h2>
<table>
<thead><tr><th>Instrument</th><th>Directie</th><th>Stare</th><th>Forta</th><th>Incredere</th>
<th>Randament asteptat</th><th>Acord</th><th>Cutoff informational</th><th>Explicatie</th></tr></thead>
<tbody>{rows_signals()}</tbody>
</table>

<div class="foot">Generat {generated} · MarketLens Faza 10</div>
</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(page)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--page", default=DEFAULT_PAGE)
    parser.add_argument("--apply", action="store_true",
                        help="Write outcomes, evaluations and the page.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_signal_schema(conn)
    initialize_signal_outcome_schema(conn)
    repository = SignalRepository(conn)

    signal_rows = conn.execute(
        "SELECT signal_id, observation_id FROM signals WHERE observation_id IS NOT NULL"
    ).fetchall()
    print(f"Semnale in baza: {len(signal_rows):,}")
    if not signal_rows:
        print("Niciun semnal de evaluat. Ruleaza intai 'Generate Signals (Phase 10)'.")
        if args.apply:
            render_page(conn, args.page)
            print(f"Pagina generata (goala): {args.page}")
        conn.close()
        return 0

    observation_ids = [row[1] for row in signal_rows]
    all_outcomes: Dict[str, List[SignalOutcome]] = defaultdict(list)
    skipped_lookahead = 0
    skipped_unresolved = 0

    for horizon, label_name in HORIZONS.items():
        labels = load_labels(conn, observation_ids, label_name)
        if not labels:
            continue
        for signal_id, observation_id in signal_rows:
            if observation_id not in labels:
                skipped_unresolved += 1
                continue
            signal = repository.get(signal_id)
            if signal is None:
                continue
            realized, measured_at = labels[observation_id]
            try:
                outcome = score_signal(signal, realized, horizon, label_name, measured_at)
            except OutcomeScoringError:
                skipped_lookahead += 1
                continue
            all_outcomes[horizon].append(outcome)

    total = sum(len(v) for v in all_outcomes.values())
    print(f"Rezultate calculate: {total:,}")
    print(f"  sarite (eticheta nerezolvata): {skipped_unresolved:,}")
    print(f"  sarite (ar fi fost look-ahead): {skipped_lookahead:,}")

    evaluations = []
    for horizon, outcomes in all_outcomes.items():
        active = [o for o in outcomes
                  if o.signal_direction in ("long", "short")]
        evaluations.append(evaluate_cohort(outcomes, "overall", "all", horizon))
        if active:
            evaluations.append(evaluate_cohort(active, "overall", "directional_only", horizon))
        evaluations.extend(evaluate_by(outcomes, "confidence_bucket", "confidence_bucket", horizon))
        evaluations.extend(evaluate_by(outcomes, "strategy_id", "strategy", horizon))
        evaluations.extend(evaluate_by(outcomes, "event_type", "event_type", horizon))

    print(f"Evaluari produse: {len(evaluations):,}")
    for evaluation in evaluations:
        if evaluation.cohort_kind == "overall":
            print(f"  {evaluation.horizon} {evaluation.cohort_value}: n={evaluation.sample_size}, "
                  f"hit={evaluation.hit_rate}, baseline={evaluation.baseline_hit_rate}, "
                  f"bate={evaluation.beats_baseline}, mic={evaluation.small_sample}")

    if not args.apply:
        print("\nDRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    for outcomes in all_outcomes.values():
        for outcome in outcomes:
            persist_outcome(conn, outcome)
    for evaluation in evaluations:
        persist_evaluation(conn, evaluation)
    conn.commit()

    render_page(conn, args.page)
    print(f"\nSCRIS: {total:,} rezultate, {len(evaluations):,} evaluari")
    print(f"Pagina generata: {args.page}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
