"""
tests/autoresearch/test_pipeline_position.py
------------------------------------------------------
Phase 23 §26, §52 — where the research stage sits, and what it is not
allowed to do there.

THE ORDER
-------------
    ... attribute errors -> build memory -> propose experiments
        -> OBSERVE AND TRIAGE -> rebuild dashboard

It mines memory patterns, error attributions and experiment results,
so it must follow all three.

THE LIMIT THAT MATTERS MORE THAN THE ORDER
----------------------------------------------
The pipeline observes and triages. It does NOT run experiments, and
the test below fails if `--cycle` ever appears in the workflow.

The reason is specific to this database rather than general caution.
Every experiment here evaluates on the same held-out window, because
the record is short enough that there is only one. A scheduled job
testing hypotheses against it twice a day drives the data-snooping
count up on every run, and by the third pass the reuse warning
correctly blocks every result from being called promising. Automated
testing would spend the one test set the project has and return
nothing usable — the automation would degrade the research rather than
scale it.

Running an experiment therefore stays a deliberate act.
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

    def test_the_research_stage_is_in_the_pipeline(self):
        self.assertIn("run_research.py", self.body)

    def test_it_runs_after_memory_is_built(self):
        self.assertLess(self.body.index("build_memory.py"),
                        self.body.index("run_research.py"))

    def test_it_runs_after_errors_are_attributed(self):
        self.assertLess(self.body.index("attribute_errors.py"),
                        self.body.index("run_research.py"))

    def test_it_runs_after_experiments_are_proposed(self):
        """It observes Phase 22's results, so they must exist first."""
        self.assertLess(self.body.index("run_experiment.py"),
                        self.body.index("run_research.py"))

    def test_it_runs_before_the_dashboard_is_rebuilt(self):
        self.assertLess(self.body.index("run_research.py"),
                        self.body.index("build_dashboard.py"))

    def test_the_pipeline_observes_and_does_not_experiment(self):
        """
        §26, §52: no autonomous testing on a schedule. See the module
        docstring for why this is a correctness limit here and not
        general caution.
        """
        for line in self.body.splitlines():
            if "run_research.py" not in line or line.strip().startswith("#"):
                continue
            self.assertIn("--questions", line)
            for forbidden in ("--cycle", "--protect"):
                self.assertNotIn(forbidden, line)

    def test_the_stage_numbering_is_consistent(self):
        labels = re.findall(r"'(\d+)/(\d+) ", self.body)
        self.assertTrue(labels)
        totals = {total for _index, total in labels}
        self.assertEqual(len(totals), 1, "mixed stage totals: %s" % totals)
        total = int(totals.pop())
        self.assertEqual(sorted(int(i) for i, _ in labels),
                         list(range(1, total + 1)))

    def test_every_stage_outcome_is_reported(self):
        ids = set(re.findall(r"^        id: (s\d+)$", self.body, re.M))
        self.assertTrue(ids)
        for step_id in sorted(ids):
            self.assertIn("steps.%s.outcome" % step_id, self.body,
                          "stage %s runs but is never reported" % step_id)

    def test_no_stage_enables_live_trading(self):
        for word in ("--live", "enable_live", "place_order", "metatrader"):
            self.assertNotIn(word, self.body.lower())


if __name__ == "__main__":
    unittest.main()
