"""
src/data_access/backtest_repository.py
-------------------------------------------
Persistence for Phase 12 runs.

WHAT IS STORED, AND WHY ALL OF IT
-------------------------------------
A run's numbers are worthless without the assumptions that produced
them, so `save_result` writes the configuration, the version identity,
the equity series, every order, every fill, every trade, the metrics
INCLUDING the ones that could not be computed, the warnings, the errors
and the allocations risk refused. Spec §55 is explicit that a result is
not a final number.

UNAVAILABLE METRICS ARE ROWS, NOT ABSENCES
----------------------------------------------
`backtest_metrics` stores a row with a null value and a reason for
every metric the sample could not support. A missing row would be
indistinguishable from a metric nobody tried to compute, and the whole
point of Phase 11's and Phase 12's treatment of missing data is that
"we could not measure this" is itself information worth keeping.

SAVING IS IDEMPOTENT BY RUN ID
----------------------------------
The run id derives from the configuration fingerprint and the code
version, so re-saving the same run replaces its own rows rather than
accumulating duplicates. A genuinely different configuration gets a
different id and a separate record — which is what makes the research
history in spec §85 possible.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.domain.backtest_models import (
    BacktestConfiguration, BacktestResult, BacktestStatus, EquityPoint,
    QualityAssessment,
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _json(value: Any) -> str:
    def convert(obj):
        if is_dataclass(obj) and not isinstance(obj, type):
            return {k: convert(v) for k, v in asdict(obj).items()}
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {str(k): convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [convert(v) for v in obj]
        if hasattr(obj, "value"):          # Enum
            return obj.value
        return obj
    return json.dumps(convert(value), default=str)


class BacktestRepository:
    """Reads and writes Phase 12 runs and their artefacts."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- experiments ----------------

    def save_backtest(self, backtest_id: str, name: str, description: str = "",
                      created_at: Optional[datetime] = None) -> str:
        self.conn.execute("""
            INSERT OR REPLACE INTO backtests
            (backtest_id, name, description, created_at, metadata_json)
            VALUES (?,?,?,?,?)
        """, (backtest_id, name, description,
              _iso(created_at or datetime.now(timezone.utc)), "{}"))
        self.conn.commit()
        return backtest_id

    # ---------------- runs ----------------

    def save_result(self, result: BacktestResult,
                    quality: Optional[QualityAssessment] = None) -> str:
        """Write a completed run and everything it produced."""
        config = result.configuration
        identity = result.identity
        run_id = result.run_id

        self.save_backtest(result.backtest_id, config.name, config.notes)
        self._clear_run(run_id)

        self.conn.execute("""
            INSERT OR REPLACE INTO backtest_runs (
                run_id, backtest_id, status, config_fingerprint,
                period_start, period_end, initial_capital, base_currency,
                benchmark_instrument_id, execution_timing, cost_model_version,
                slippage_model_version, slippage_method, constraint_set_version,
                sizing_strategy_id, risk_engine_version, execution_model_version,
                calendar_version, strategy_version, model_version,
                feature_set_version, dataset_version, code_version, random_seed,
                config_json, identity_json, quality_json,
                observations_processed, duration_seconds,
                started_at, finished_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run_id, result.backtest_id, result.status.value,
            identity.config_fingerprint,
            _iso(config.start), _iso(config.end), config.initial_capital,
            config.base_currency, config.benchmark_instrument_id,
            config.execution.timing.value, config.costs.version,
            config.slippage.version, config.slippage.method.value,
            config.constraint_set_version, config.sizing_strategy_id,
            identity.risk_engine_version, identity.execution_model_version,
            identity.calendar_version, identity.strategy_version,
            identity.model_version, identity.feature_set_version,
            identity.dataset_version, identity.code_version, config.random_seed,
            _json(config), _json(identity),
            _json(quality) if quality is not None else "{}",
            result.observations_processed, result.duration_seconds,
            _iso(result.started_at), _iso(result.finished_at),
            _iso(datetime.now(timezone.utc)),
        ))

        self._save_orders(result)
        self._save_fills(result)
        self._save_trades(result)
        self._save_equity(result)
        self._save_metrics(result)
        self._save_attribution(result)
        self._save_drawdowns(result)
        self._save_warnings(result)
        self._save_errors(result)
        self._save_risk_events(result)

        self.conn.commit()
        return run_id

    def _clear_run(self, run_id: str) -> None:
        for table in ("simulated_orders", "simulated_fills", "backtest_trades",
                      "backtest_equity", "backtest_metrics", "backtest_attribution",
                      "backtest_drawdowns", "backtest_warnings", "backtest_errors",
                      "backtest_risk_events"):
            self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    # ---------------- children ----------------

    def _save_orders(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO simulated_orders
            (order_id, run_id, instrument_id, side, quantity, filled_quantity,
             state, reject_reason, information_cutoff, decision_at, created_at,
             signal_id, decision_id, target_weight, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(o.order_id, o.run_id, o.instrument_id, o.side.value, o.quantity,
               o.filled_quantity, o.state.value,
               o.reject_reason.value if o.reject_reason else None,
               _iso(o.information_cutoff), _iso(o.decision_at), _iso(o.created_at),
               o.signal_id, o.decision_id, o.target_weight, o.note)
              for o in result.orders])

    def _save_fills(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO simulated_fills
            (fill_id, run_id, order_id, instrument_id, side, quantity,
             reference_price, price, commission, slippage_cost, participation,
             is_partial, bar_timestamp, filled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(f.fill_id, f.run_id, f.order_id, f.instrument_id, f.side.value,
               f.quantity, f.reference_price, f.price, f.commission,
               f.slippage_cost, f.participation, int(f.is_partial),
               _iso(f.bar_timestamp), _iso(f.filled_at))
              for f in result.fills])

    def _save_trades(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_trades
            (trade_id, run_id, instrument_id, side, quantity, entry_price,
             exit_price, entry_at, exit_at, gross_pnl, costs, net_pnl,
             holding_days, return_pct, mfe, mae, exit_reason, sector_id,
             strategy_id, entry_signal_id, entry_decision_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(t.trade_id, t.run_id, t.instrument_id, t.side.value, t.quantity,
               t.entry_price, t.exit_price, _iso(t.entry_at), _iso(t.exit_at),
               t.gross_pnl, t.costs, t.net_pnl, t.holding_days, t.return_pct,
               t.mfe, t.mae, t.exit_reason, t.sector_id, t.strategy_id,
               t.entry_signal_id, t.entry_decision_id)
              for t in result.trades])

    def _save_equity(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_equity
            (run_id, timestamp, equity, cash, positions_value, gross_exposure,
             net_exposure, benchmark_value, drawdown, open_positions)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [(result.run_id, _iso(p.timestamp), p.equity, p.cash,
               p.positions_value, p.gross_exposure, p.net_exposure,
               p.benchmark_value, p.drawdown, p.open_positions)
              for p in result.equity_curve])

    def _save_metrics(self, result: BacktestResult) -> None:
        metrics = result.metrics
        numeric = {
            "total_return": metrics.total_return, "cagr": metrics.cagr,
            "annualized_return": metrics.annualized_return,
            "volatility": metrics.volatility,
            "downside_volatility": metrics.downside_volatility,
            "sharpe": metrics.sharpe, "sortino": metrics.sortino,
            "calmar": metrics.calmar, "max_drawdown": metrics.max_drawdown,
            "average_drawdown": metrics.average_drawdown,
            "max_drawdown_duration_days": metrics.max_drawdown_duration_days,
            "win_rate": metrics.win_rate, "average_win": metrics.average_win,
            "average_loss": metrics.average_loss, "largest_win": metrics.largest_win,
            "largest_loss": metrics.largest_loss,
            "profit_factor": metrics.profit_factor, "expectancy": metrics.expectancy,
            "average_holding_days": metrics.average_holding_days,
            "turnover": metrics.turnover,
            "annualized_turnover": metrics.annualized_turnover,
            "average_exposure": metrics.average_exposure,
            "average_cash": metrics.average_cash,
            "total_costs": metrics.total_costs,
            "total_slippage": metrics.total_slippage,
            "benchmark_return": metrics.benchmark_return,
            "excess_return": metrics.excess_return,
            "final_capital": metrics.final_capital,
            "initial_capital": metrics.initial_capital,
            "total_trades": float(metrics.total_trades),
            "winning_trades": float(metrics.winning_trades),
            "losing_trades": float(metrics.losing_trades),
            "trading_days": float(metrics.trading_days),
        }
        rows = [(result.run_id, name, value, None)
                for name, value in numeric.items()]
        # Unavailable metrics are recorded as explicit rows.
        rows.extend((result.run_id, f"unavailable::{name}", None, reason)
                    for name, reason in metrics.unavailable.items())
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_metrics
            (run_id, metric, value, unavailable_reason) VALUES (?,?,?,?)
        """, rows)

    def _save_attribution(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_attribution
            (run_id, dimension, bucket_key, label, trades, wins, gross_pnl,
             costs, net_pnl)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [(result.run_id, b.dimension, b.key, b.label, b.trades, b.wins,
               b.gross_pnl, b.costs, b.net_pnl) for b in result.attribution])

    def _save_drawdowns(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_drawdowns
            (run_id, peak_at, peak_equity, trough_at, trough_equity, depth,
             recovered_at, duration_days, recovery_days)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, [(result.run_id, _iso(d.peak_at), d.peak_equity, _iso(d.trough_at),
               d.trough_equity, d.depth, _iso(d.recovered_at),
               d.duration_days, d.recovery_days) for d in result.drawdowns])

    def _save_warnings(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_warnings
            (run_id, code, message, detail) VALUES (?,?,?,?)
        """, [(result.run_id, w.code.value, w.message, w.detail)
              for w in result.warnings])

    def _save_errors(self, result: BacktestResult) -> None:
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_errors
            (run_id, seq, code, message, instrument_id, occurred_at, fatal)
            VALUES (?,?,?,?,?,?,?)
        """, [(result.run_id, index, e.code, e.message, e.instrument_id,
               _iso(e.at), int(e.fatal))
              for index, e in enumerate(result.errors)])

    def _save_risk_events(self, result: BacktestResult) -> None:
        rows = []
        seq = 0
        for payload in result.rejected_allocations:
            rows.append((result.run_id, seq, "rejected", payload.get("at"),
                         payload.get("proposal_id"), payload.get("reason", ""),
                         _json(payload)))
            seq += 1
        for payload in result.modified_allocations:
            rows.append((result.run_id, seq, "modified", payload.get("at"),
                         payload.get("proposal_id"), payload.get("reason", ""),
                         _json(payload)))
            seq += 1
        self.conn.executemany("""
            INSERT OR REPLACE INTO backtest_risk_events
            (run_id, seq, kind, occurred_at, proposal_id, reason, payload_json)
            VALUES (?,?,?,?,?,?,?)
        """, rows)

    # ---------------- reads ----------------

    def list_runs(self, backtest_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        sql = """
            SELECT run_id, backtest_id, status, period_start, period_end,
                   initial_capital, execution_timing, cost_model_version,
                   slippage_method, config_fingerprint, started_at,
                   observations_processed, duration_seconds
            FROM backtest_runs
        """
        params: List[Any] = []
        if backtest_id:
            sql += " WHERE backtest_id = ?"
            params.append(backtest_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        return [{
            "run_id": r[0], "backtest_id": r[1], "status": r[2],
            "period_start": r[3], "period_end": r[4], "initial_capital": r[5],
            "execution_timing": r[6], "cost_model_version": r[7],
            "slippage_method": r[8], "config_fingerprint": r[9],
            "started_at": r[10], "observations": r[11], "duration_seconds": r[12],
        } for r in self.conn.execute(sql, params)]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("""
            SELECT run_id, backtest_id, status, config_json, identity_json,
                   quality_json, started_at, finished_at, observations_processed,
                   duration_seconds, config_fingerprint
            FROM backtest_runs WHERE run_id = ?
        """, (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "run_id": row[0], "backtest_id": row[1], "status": row[2],
            "configuration": json.loads(row[3]), "identity": json.loads(row[4]),
            "quality": json.loads(row[5]), "started_at": row[6],
            "finished_at": row[7], "observations": row[8],
            "duration_seconds": row[9], "config_fingerprint": row[10],
        }

    def metrics_for(self, run_id: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"values": {}, "unavailable": {}}
        for metric, value, reason in self.conn.execute(
                "SELECT metric, value, unavailable_reason FROM backtest_metrics "
                "WHERE run_id = ?", (run_id,)):
            if metric.startswith("unavailable::"):
                out["unavailable"][metric.split("::", 1)[1]] = reason
            else:
                out["values"][metric] = value
        return out

    def equity_curve(self, run_id: str) -> List[Dict[str, Any]]:
        return [{
            "timestamp": r[0], "equity": r[1], "cash": r[2],
            "positions_value": r[3], "gross_exposure": r[4],
            "net_exposure": r[5], "benchmark_value": r[6], "drawdown": r[7],
            "open_positions": r[8],
        } for r in self.conn.execute("""
            SELECT timestamp, equity, cash, positions_value, gross_exposure,
                   net_exposure, benchmark_value, drawdown, open_positions
            FROM backtest_equity WHERE run_id = ? ORDER BY timestamp ASC
        """, (run_id,))]

    def trades_for(self, run_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return [{
            "trade_id": r[0], "instrument_id": r[1], "side": r[2],
            "quantity": r[3], "entry_price": r[4], "exit_price": r[5],
            "entry_at": r[6], "exit_at": r[7], "net_pnl": r[8],
            "costs": r[9], "holding_days": r[10], "return_pct": r[11],
            "exit_reason": r[12], "sector_id": r[13],
        } for r in self.conn.execute("""
            SELECT trade_id, instrument_id, side, quantity, entry_price,
                   exit_price, entry_at, exit_at, net_pnl, costs, holding_days,
                   return_pct, exit_reason, sector_id
            FROM backtest_trades WHERE run_id = ?
            ORDER BY exit_at DESC LIMIT ?
        """, (run_id, limit))]

    def attribution_for(self, run_id: str,
                        dimension: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT dimension, bucket_key, label, trades, wins, gross_pnl, "
               "costs, net_pnl FROM backtest_attribution WHERE run_id = ?")
        params: List[Any] = [run_id]
        if dimension:
            sql += " AND dimension = ?"
            params.append(dimension)
        sql += " ORDER BY dimension, net_pnl DESC"
        return [{
            "dimension": r[0], "key": r[1], "label": r[2], "trades": r[3],
            "wins": r[4], "gross_pnl": r[5], "costs": r[6], "net_pnl": r[7],
        } for r in self.conn.execute(sql, params)]

    def warnings_for(self, run_id: str) -> List[Dict[str, str]]:
        return [{"code": r[0], "message": r[1], "detail": r[2]}
                for r in self.conn.execute(
                    "SELECT code, message, detail FROM backtest_warnings "
                    "WHERE run_id = ? ORDER BY code", (run_id,))]

    def risk_events_for(self, run_id: str,
                        kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT kind, occurred_at, proposal_id, reason, payload_json "
               "FROM backtest_risk_events WHERE run_id = ?")
        params: List[Any] = [run_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY seq ASC"
        return [{"kind": r[0], "at": r[1], "proposal_id": r[2],
                 "reason": r[3], "payload": json.loads(r[4])}
                for r in self.conn.execute(sql, params)]

    def compare_runs(self, run_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """
        Side-by-side comparison (spec §83, §103).

        Returns each run's headline metrics plus the configuration
        fields that most often differ, so a reader can see whether two
        runs are actually comparable before comparing their returns.
        """
        out: List[Dict[str, Any]] = []
        for run_id in run_ids:
            run = self.get_run(run_id)
            if run is None:
                continue
            metrics = self.metrics_for(run_id)
            config = run.get("configuration", {})
            out.append({
                "run_id": run_id,
                "status": run["status"],
                "fingerprint": run["config_fingerprint"],
                "execution_timing": (config.get("execution") or {}).get("timing"),
                "cost_version": (config.get("costs") or {}).get("version"),
                "slippage_method": (config.get("slippage") or {}).get("method"),
                "period": [config.get("start"), config.get("end")],
                "metrics": metrics["values"],
                "unavailable": metrics["unavailable"],
            })
        return out
