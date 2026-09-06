"""
tests/experiments/test_pipeline_position.py
-----------------------------------------------------
Where the experiment stage sits, and what it is allowed to do there.

Phases 19, 20 and 21 each added a test like this one, for the same
reason: a stage that runs before its inputs exist does not fail, it
succeeds on an empty table and reports nothing wrong. Ordering is a
correctness property here, not a preference.

THE ORDER
-------------
    ... -> measure outcomes -> attribute errors -> build memory
        -> PROPOSE EXPERIMENTS -> rebuild dashboard

Proposals are mined from memory patterns and recurring error
attributions, so the stage must follow both. Running it earlier would
propose nothing and say so quietly.

THE OTHER HALF
------------------
The pipeline PROPOSES and does not RUN. An automated loop that both
generates hypotheses and evaluates them, on a schedule, with no human
between, is the autonomous learning §80 forbids. The test below reads
the workflow and fails if a `--run` ever appears in it.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "pipeline.yml")


def workflow_text():
    with open(WORKFLOW, encoding="utf-8") as handle:
        return handle.read()


class TestPipelinePosition(unittest.TestCase):

    def setUp(self):
        self.body = workflow_text()

    def test_the_experiment_stage_is_in_the_pipeline(self):
        self.assertIn("run_experiment.py", self.body)

    def test_it_runs_after_memory_is_built(self):
        """It mines memory patterns, so memory must already exist."""
        self.assertLess(self.body.index("build_memory.py"),
                        self.body.index("run_experiment.py"))

    def test_it_runs_after_errors_are_attributed(self):
        """It also mines recurring error attributions."""
        self.assertLess(self.body.index("attribute_errors.py"),
                        self.body.index("run_experiment.py"))

    def test_it_runs_before_the_dashboard_is_rebuilt(self):
        """Otherwise the Lab renders yesterday's proposals."""
        self.assertLess(self.body.index("run_experiment.py"),
                        self.body.index("build_dashboard.py"))

    def test_the_pipeline_proposes_and_does_not_run(self):
        """
        §80: no autonomous learning. A scheduled job that proposes a
        hypothesis and then decides whether it succeeded, with nobody
        in between, is a learning loop however it is labelled.
        """
        for line in self.body.splitlines():
            if "run_experiment.py" not in line:
                continue
            self.assertIn("--propose", line)
            self.assertNotIn("--run", line)
            self.assertNotIn("--sweep", line)
            self.assertNotIn("--ablate", line)

    def test_the_stage_numbering_is_consistent(self):
        """
        A stage labelled 14/14 sitting above a 15/15 is how a reader
        concludes the run finished when it did not.
        """
        labels = re.findall(r"'(\d+)/(\d+) ", self.body)
        self.assertTrue(labels)
        totals = {total for _index, total in labels}
        self.assertEqual(len(totals), 1, "mixed stage totals: %s" % totals)
        total = int(totals.pop())
        self.assertEqual(sorted(int(i) for i, _ in labels),
                         list(range(1, total + 1)))

    def test_every_stage_outcome_is_reported(self):
        """
        A partial run must not read as a clean one -- so each stage id
        has to appear in the summary that reports what actually ran.
        """
        ids = set(re.findall(r"^        id: (s\d+)$", self.body, re.M))
        self.assertTrue(ids)
        for step_id in sorted(ids):
            self.assertIn("steps.%s.outcome" % step_id, self.body,
                          "stage %s runs but is never reported" % step_id)

    def test_no_stage_enables_live_trading(self):
        forbidden = ("--live", "enable_live", "place_order", "metatrader")
        for word in forbidden:
            self.assertNotIn(word, self.body.lower(), "workflow mentions %r" % word)


if __name__ == "__main__":
    unittest.main()
