"""
tests/scripts/test_run_ibkr_risk_verdict.py
-----------------------------------------------------------
Tests for `risk_verdict_for` in scripts/run_ibkr.py -- the CLI's risk
gate.

WHY THIS FILE EXISTS

Phase 25.5 deleted `--assume-risk-approved`, a flag that set
`risk_approved=True` with no RiskDecision behind it. The audit noted
its defining feature: it had no test at all. `risk_verdict_for`
replaced it and also had none, and it carried the same hole reached a
different way.

`if covered and instrument_id not in covered` skipped the coverage
check whenever the decision approved no changes, and fell through to
approval. An APPROVED decision that changed nothing therefore
authorised an order for any instrument it had never mentioned.

That is the ordinary verdict on this project's data, not a contrived
one: with every signal below Phase 11's 0.40 confidence floor the
engine proposes nothing and records APPROVED with the summary "no
changes proposed; current state is within all limits".
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.run_ibkr import risk_verdict_for
from src.domain.portfolio_models import (
    AllocationChange, RiskDecision, RiskDecisionState,
)

ANCHOR = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class _Repository:
    """Stands in for PortfolioRepository.get_decision."""

    def __init__(self, decision):
        self._decision = decision

    def get_decision(self, decision_id):
        if self._decision is None:
            return None
        return self._decision if decision_id == self._decision.decision_id else None


class _Connection:
    pass


def _decision(state, changes=(), summary=""):
    return RiskDecision(
        decision_id="risk-test-1",
        portfolio_id="venue-mechanics",
        state=state,
        as_of=ANCHOR,
        summary=summary,
        approved_changes=list(changes),
    )


def _change(instrument_id):
    return AllocationChange(instrument_id=instrument_id)


def _verdict(monkey_decision, instrument_id):
    """Call risk_verdict_for with the repository patched to our double."""
    import scripts.run_ibkr as module
    import src.data_access.portfolio_repository as repository_module

    original = repository_module.PortfolioRepository
    repository_module.PortfolioRepository = lambda conn: _Repository(monkey_decision)
    try:
        args = SimpleNamespace(decision_id="risk-test-1")
        return module.risk_verdict_for(_Connection(), args, instrument_id)
    finally:
        repository_module.PortfolioRepository = original


class TestADecisionAboutNothingAuthorisesNothing(unittest.TestCase):

    def test_approved_with_no_changes_is_refused(self):
        decision = _decision(
            RiskDecisionState.APPROVED,
            changes=(),
            summary="no changes proposed; current state is within all limits")
        approved, detail = _verdict(decision, "i-aapl")
        self.assertIsNone(approved)
        self.assertIn("authorises no instrument", detail)

    def test_the_refusal_names_the_decision_and_its_summary(self):
        decision = _decision(
            RiskDecisionState.APPROVED, changes=(),
            summary="no changes proposed; current state is within all limits")
        _approved, detail = _verdict(decision, "i-aapl")
        self.assertIn("risk-test-1", detail)
        self.assertIn("no changes proposed", detail)

    def test_a_covered_instrument_is_still_approved(self):
        """The fix must not refuse a decision that genuinely approves."""
        decision = _decision(RiskDecisionState.APPROVED,
                             changes=(_change("i-aapl"),))
        approved, detail = _verdict(decision, "i-aapl")
        self.assertIs(approved, True)
        self.assertIn("risk-test-1", detail)

    def test_an_uncovered_instrument_is_refused_by_name(self):
        decision = _decision(RiskDecisionState.APPROVED,
                             changes=(_change("i-msft"),))
        approved, detail = _verdict(decision, "i-aapl")
        self.assertIsNone(approved)
        self.assertIn("i-msft", detail)
        self.assertIn("i-aapl", detail)

    def test_a_reduced_decision_covering_the_instrument_still_approves(self):
        """REDUCED permits exposure to change, at a smaller size."""
        decision = _decision(RiskDecisionState.REDUCED,
                             changes=(_change("i-aapl"),))
        approved, _detail = _verdict(decision, "i-aapl")
        self.assertIs(approved, True)

    def test_a_rejected_decision_is_refused_before_coverage_matters(self):
        decision = _decision(RiskDecisionState.REJECTED,
                             changes=(_change("i-aapl"),))
        approved, detail = _verdict(decision, "i-aapl")
        self.assertIsNone(approved)
        self.assertIn("REJECTED", detail)

    def test_no_decision_supplied_is_not_approval(self):
        args = SimpleNamespace(decision_id=None)
        import scripts.run_ibkr as module
        approved, detail = module.risk_verdict_for(_Connection(), args, "i-aapl")
        self.assertIsNone(approved)
        self.assertIn("no risk decision", detail)

    def test_a_decision_id_that_does_not_exist_is_not_approval(self):
        approved, detail = _verdict(None, "i-aapl")
        self.assertIsNone(approved)
        self.assertIn("no risk decision", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
