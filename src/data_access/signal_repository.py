"""
src/data_access/signal_repository.py
-----------------------------------------
Read/write access for Phase 10 signals.

SIGNAL IDENTITY IS THE CORE IDEA HERE (spec §22)
----------------------------------------------------
`signal_identity_hash` is derived from (strategy_id, strategy_version,
configuration_version, instrument_id, source_information_cutoff). Two
generation runs over the SAME information state produce the same hash.

That is what makes generation idempotent without overwriting: calling
the generator twice does not create a second signal, because the
second one is recognized as the same claim about the same information.
It also means a genuinely NEW information state produces a genuinely
new signal, which is correct — the system did learn something new.

Note what is deliberately NOT in the hash: strength, confidence,
direction. If the same strategy on the same information produced a
different direction, that is a bug worth surfacing, not a new signal
worth storing. Including the output in the identity would hide it.

SUPERSEDING, NOT UPDATING (spec §15, §45)
---------------------------------------------
When a newer signal covers the same instrument and strategy, the older
one is marked SUPERSEDED and points at its replacement. Its row is not
modified otherwise. Reconstructing what the system believed at any past
moment stays possible, which is the whole point of an audit trail.
"""

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.domain.signal_models import (
    Signal, SignalCandidate, SignalContext, SignalDirection, SignalExplanation,
    SignalProvenance, SignalStatus, SignalStrategyDefinition, SignalType,
    AgreementState, ModelContribution, SuppressionReason,
)


def compute_identity_hash(strategy_id: str, strategy_version: str,
                          configuration_version: str, instrument_id: str,
                          source_information_cutoff: Optional[datetime]) -> str:
    """
    Deterministic identity for a signal claim. See module docstring for
    what is included and, more importantly, what is not.
    """
    cutoff = source_information_cutoff.isoformat() if source_information_cutoff else "none"
    raw = f"{strategy_id}|{strategy_version}|{configuration_version}|{instrument_id}|{cutoff}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class SignalRepository:
    """Persists and retrieves signals, strategies, and their contributions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- strategies ----------------

    def save_strategy(self, definition: SignalStrategyDefinition) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO signal_strategies
            (strategy_id, version, name, signal_type, description, is_active,
             configuration_version, parameters_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (definition.strategy_id, definition.version, definition.name,
              definition.signal_type.value, definition.description,
              int(definition.is_active), definition.configuration_version,
              json.dumps(definition.parameters), _iso(definition.created_at)))
        self.conn.commit()

    def get_strategy(self, strategy_id: str, version: str) -> Optional[SignalStrategyDefinition]:
        row = self.conn.execute("""
            SELECT strategy_id, version, name, signal_type, description, is_active,
                   configuration_version, parameters_json, created_at
            FROM signal_strategies WHERE strategy_id = ? AND version = ?
        """, (strategy_id, version)).fetchone()
        if row is None:
            return None
        return SignalStrategyDefinition(
            strategy_id=row[0], version=row[1], name=row[2],
            signal_type=SignalType(row[3]), description=row[4],
            is_active=bool(row[5]), configuration_version=row[6],
            parameters=json.loads(row[7]), created_at=_parse(row[8]),
        )

    def active_strategies(self) -> List[SignalStrategyDefinition]:
        rows = self.conn.execute(
            "SELECT strategy_id, version FROM signal_strategies WHERE is_active = 1").fetchall()
        found = [self.get_strategy(sid, ver) for sid, ver in rows]
        return [s for s in found if s is not None]

    # ---------------- signals ----------------

    def find_by_identity(self, identity_hash: str) -> Optional[Signal]:
        """The idempotency check: has this exact claim already been recorded?"""
        row = self.conn.execute(
            "SELECT signal_id FROM signals WHERE signal_identity_hash = ? LIMIT 1",
            (identity_hash,)).fetchone()
        return self.get(row[0]) if row else None

    def save(self, signal: Signal) -> str:
        """
        Write a signal and its children. Returns the signal_id.

        Uses INSERT OR REPLACE on signal_id — a caller re-saving the
        SAME signal object (e.g. after a status transition) updates it,
        while a genuinely different claim gets a different id. Duplicate
        prevention happens earlier, via find_by_identity.
        """
        p = signal.provenance
        c = signal.context
        identity = compute_identity_hash(
            p.strategy_id or "", p.strategy_version or "",
            p.configuration_version or "v1", signal.instrument_id,
            p.source_information_cutoff)

        self.conn.execute("""
            INSERT OR REPLACE INTO signals (
                signal_id, signal_identity_hash, instrument_id, security_id, company_id,
                signal_type, direction, status, strength, confidence, expected_return,
                expected_return_horizon_days, probability_up, agreement_state,
                observation_id, event_id, strategy_id, strategy_version,
                configuration_version, feature_set_version, dataset_version,
                source_information_cutoff, provenance_inputs_json,
                market_regime, volatility_percentile, relative_volume, liquidity_note,
                event_type, event_corroboration_state, independent_source_count,
                data_quality_level, explanation_summary, explanation_factors_json,
                explanation_caveats_json, suppression_note, created_at, valid_from,
                valid_until, superseded_by, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal.signal_id, identity, signal.instrument_id, signal.security_id,
            signal.company_id, signal.signal_type.value, signal.direction.value,
            signal.status.value, signal.strength, signal.confidence,
            signal.expected_return, signal.expected_return_horizon_days,
            signal.probability_up, signal.agreement_state.value,
            p.observation_id, p.event_id, p.strategy_id, p.strategy_version,
            p.configuration_version, p.feature_set_version, p.dataset_version,
            _iso(p.source_information_cutoff), json.dumps(p.inputs, default=str),
            c.market_regime, c.volatility_percentile, c.relative_volume, c.liquidity_note,
            c.event_type, c.event_corroboration_state, c.independent_source_count,
            c.data_quality_level, signal.explanation.summary,
            json.dumps(signal.explanation.factors), json.dumps(signal.explanation.caveats),
            signal.suppression_note, _iso(signal.created_at), _iso(signal.valid_from),
            _iso(signal.valid_until), signal.superseded_by,
            json.dumps(signal.metadata, default=str),
        ))

        self.conn.execute("DELETE FROM signal_contributions WHERE signal_id = ?", (signal.signal_id,))
        for contribution in signal.contributions:
            self.conn.execute("""
                INSERT OR REPLACE INTO signal_contributions
                (signal_id, prediction_id, trained_model_id, model_qualified_id,
                 predicted_value, probability_up, confidence, weight, reliability,
                 is_abstention, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (signal.signal_id, contribution.prediction_id, contribution.trained_model_id,
                  contribution.model_qualified_id, contribution.predicted_value,
                  contribution.probability_up, contribution.confidence, contribution.weight,
                  contribution.reliability, int(contribution.is_abstention), contribution.note))

        self.conn.execute("DELETE FROM signal_suppressions WHERE signal_id = ?", (signal.signal_id,))
        for reason in signal.suppression_reasons:
            self.conn.execute(
                "INSERT OR REPLACE INTO signal_suppressions (signal_id, reason) VALUES (?,?)",
                (signal.signal_id, reason.value))

        self.conn.commit()
        return signal.signal_id

    def get(self, signal_id: str) -> Optional[Signal]:
        row = self.conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
        if row is None:
            return None
        columns = [d[0] for d in self.conn.execute("SELECT * FROM signals LIMIT 0").description]
        data = dict(zip(columns, row))

        signal = Signal(
            signal_id=data["signal_id"], instrument_id=data["instrument_id"],
            signal_type=SignalType(data["signal_type"]),
            direction=SignalDirection(data["direction"]),
            status=SignalStatus(data["status"]),
            strength=data["strength"], confidence=data["confidence"],
            expected_return=data["expected_return"],
            expected_return_horizon_days=data["expected_return_horizon_days"],
            probability_up=data["probability_up"],
            security_id=data["security_id"], company_id=data["company_id"],
            agreement_state=AgreementState(data["agreement_state"]),
            provenance=SignalProvenance(
                observation_id=data["observation_id"], event_id=data["event_id"],
                instrument_id=data["instrument_id"],
                feature_set_version=data["feature_set_version"],
                dataset_version=data["dataset_version"],
                strategy_id=data["strategy_id"], strategy_version=data["strategy_version"],
                configuration_version=data["configuration_version"],
                source_information_cutoff=_parse(data["source_information_cutoff"]),
                inputs=json.loads(data["provenance_inputs_json"]),
            ),
            context=SignalContext(
                market_regime=data["market_regime"],
                volatility_percentile=data["volatility_percentile"],
                relative_volume=data["relative_volume"],
                liquidity_note=data["liquidity_note"],
                event_type=data["event_type"],
                event_corroboration_state=data["event_corroboration_state"],
                independent_source_count=data["independent_source_count"],
                data_quality_level=data["data_quality_level"],
            ),
            explanation=SignalExplanation(
                summary=data["explanation_summary"],
                factors=json.loads(data["explanation_factors_json"]),
                caveats=json.loads(data["explanation_caveats_json"]),
            ),
            suppression_note=data["suppression_note"],
            created_at=_parse(data["created_at"]),
            valid_from=_parse(data["valid_from"]),
            valid_until=_parse(data["valid_until"]),
            superseded_by=data["superseded_by"],
            metadata=json.loads(data["metadata_json"]),
        )

        for r in self.conn.execute("""
            SELECT prediction_id, trained_model_id, model_qualified_id, predicted_value,
                   probability_up, confidence, weight, reliability, is_abstention, note
            FROM signal_contributions WHERE signal_id = ?
        """, (signal_id,)):
            signal.contributions.append(ModelContribution(
                prediction_id=r[0], trained_model_id=r[1], model_qualified_id=r[2],
                predicted_value=r[3], probability_up=r[4], confidence=r[5],
                weight=r[6], reliability=r[7], is_abstention=bool(r[8]), note=r[9]))

        for (reason,) in self.conn.execute(
                "SELECT reason FROM signal_suppressions WHERE signal_id = ?", (signal_id,)):
            signal.suppression_reasons.append(SuppressionReason(reason))

        return signal

    def supersede(self, old_signal_id: str, new_signal_id: str) -> None:
        """
        Mark an older signal as replaced. Its claim is left intact —
        only the lifecycle pointer changes (spec §15, §45).
        """
        self.conn.execute(
            "UPDATE signals SET status = ?, superseded_by = ? WHERE signal_id = ?",
            (SignalStatus.SUPERSEDED.value, new_signal_id, old_signal_id))
        self.conn.commit()

    def expire_before(self, moment: datetime) -> int:
        """
        Move ACTIVE signals whose validity has passed into EXPIRED.

        Returns the number changed. Only ACTIVE rows are touched:
        expiring a SUPPRESSED or REJECTED signal would erase why it was
        withheld.
        """
        cursor = self.conn.execute("""
            UPDATE signals SET status = ?
            WHERE status = ? AND valid_until IS NOT NULL AND valid_until < ?
        """, (SignalStatus.EXPIRED.value, SignalStatus.ACTIVE.value, moment.isoformat()))
        self.conn.commit()
        return cursor.rowcount

    def active_signals(self, instrument_id: Optional[str] = None) -> List[Signal]:
        sql = "SELECT signal_id FROM signals WHERE status = ?"
        params: List[Any] = [SignalStatus.ACTIVE.value]
        if instrument_id:
            sql += " AND instrument_id = ?"
            params.append(instrument_id)
        sql += " ORDER BY created_at DESC"
        found = [self.get(row[0]) for row in self.conn.execute(sql, params)]
        return [s for s in found if s is not None]

    def signals_as_of(self, moment: datetime, instrument_id: Optional[str] = None) -> List[Signal]:
        """
        What the system believed at a past moment (spec §28, historical
        replay).

        Filters on source_information_cutoff, not created_at: the
        question is what INFORMATION the signal was built from, not
        when the row happened to be written. A backfill run today over
        last month's data must not appear as if it were known today.
        """
        sql = ("SELECT signal_id FROM signals "
               "WHERE source_information_cutoff IS NOT NULL AND source_information_cutoff <= ?")
        params: List[Any] = [moment.isoformat()]
        if instrument_id:
            sql += " AND instrument_id = ?"
            params.append(instrument_id)
        sql += " ORDER BY source_information_cutoff DESC"
        found = [self.get(row[0]) for row in self.conn.execute(sql, params)]
        return [s for s in found if s is not None]
