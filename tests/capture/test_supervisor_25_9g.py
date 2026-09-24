"""
tests/capture/test_supervisor_25_9g.py
--------------------------------------------
Phase 25.9G -- the supervisor's restart policy and the status verdicts.

The supervisor under test is the production class spawning REAL child
processes. Only the child is a stand-in: a script that exits with the
codes a real capture process would, in order. Timing constants are
shrunk so a crash loop takes a second rather than ten minutes.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import capture_status, capture_supervisor
from scripts.run_capture import supervisor_gone

FAST = dict(BACKOFF_START=0.05, BACKOFF_CAP=0.1, TICK_SECONDS=0.05,
            LEASE_RETRY_SECONDS=0.05, STOP_GRACE_SECONDS=5.0)


class _Supervised(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = Namespace(
            db=os.path.join(self.dir, "c.db"),
            stop_file=os.path.join(self.dir, "STOP"),
            state_file=os.path.join(self.dir, "supervisor.json"),
            lock_file=os.path.join(self.dir, "supervisor.lock"),
            log_dir=self.dir)
        self.patches = [mock.patch.object(capture_supervisor, k, v)
                        for k, v in FAST.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def child(self, codes, touch_stop_after=None):
        """A stand-in child exiting with `codes` in turn, one per launch."""
        counter = os.path.join(self.dir, "launches")
        script = os.path.join(self.dir, "child.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(f"""
                import os, sys
                path = {counter!r}
                n = int(open(path).read()) if os.path.exists(path) else 0
                open(path, "w").write(str(n + 1))
                codes = {list(codes)!r}
                stop_after = {touch_stop_after!r}
                if stop_after is not None and n + 1 >= stop_after:
                    open({self.args.stop_file!r}, "w").write("stop")
                sys.exit(codes[min(n, len(codes) - 1)])
            """))
        return script

    def launches(self):
        with open(os.path.join(self.dir, "launches")) as handle:
            return int(handle.read())

    def state(self):
        with open(self.args.state_file, encoding="utf-8") as handle:
            return json.load(handle)


class TestRestartPolicy(_Supervised):

    def test_crash_loop_ends_in_manual_attention(self):
        sup = capture_supervisor.Supervisor(self.args, child_script=self.child([1]))
        self.assertEqual(sup.run(), 1)
        self.assertEqual(self.launches(), capture_supervisor.CRASH_LIMIT)
        self.assertEqual(self.state()["state"], "MANUAL_ATTENTION")
        self.assertIn("crash loop", self.state()["reason"])

    def test_crashes_then_recovery_are_restarted(self):
        script = self.child([1, 1, 0], touch_stop_after=3)
        sup = capture_supervisor.Supervisor(self.args, child_script=script)
        self.assertEqual(sup.run(), 0)
        self.assertEqual(self.launches(), 3)
        self.assertEqual(self.state()["state"], "STOPPED")
        self.assertEqual(self.state()["restarts"], 2)

    def test_configuration_error_is_not_restarted(self):
        sup = capture_supervisor.Supervisor(self.args, child_script=self.child([2]))
        self.assertEqual(sup.run(), 2)
        self.assertEqual(self.launches(), 1)
        self.assertEqual(self.state()["state"], "MANUAL_ATTENTION")

    def test_safety_exit_is_not_restarted(self):
        sup = capture_supervisor.Supervisor(self.args, child_script=self.child([4]))
        self.assertEqual(sup.run(), 4)
        self.assertEqual(self.launches(), 1)
        self.assertIn("SAFETY", self.state()["reason"])

    def test_busy_lease_is_retried_then_escalated(self):
        with mock.patch.object(capture_supervisor, "LEASE_RETRY_LIMIT", 3):
            sup = capture_supervisor.Supervisor(self.args, child_script=self.child([3]))
            self.assertEqual(sup.run(), 3)
        self.assertEqual(self.launches(), 4)

    def test_busy_lease_that_clears_resumes(self):
        script = self.child([3, 3, 0], touch_stop_after=3)
        sup = capture_supervisor.Supervisor(self.args, child_script=script)
        self.assertEqual(sup.run(), 0)
        self.assertEqual(self.state()["state"], "STOPPED")

    def test_command_carries_no_secret(self):
        sup = capture_supervisor.Supervisor(self.args)
        joined = " ".join(sup.command()).lower()
        for word in ("password", "token", "cookie", "secret", "--account"):
            self.assertNotIn(word, joined)


class TestLaunchGuards(_Supervised):

    def run_main(self, *extra):
        # A guard test must never launch a real capture process: if a
        # guard fails open, this returns 99 instead of spawning one.
        patch = mock.patch.object(capture_supervisor.Supervisor, "run",
                                  lambda sup: 99)
        patch.start()
        self.addCleanup(patch.stop)
        argv = ["--db", self.args.db, "--stop-file", self.args.stop_file,
                "--state-file", self.args.state_file, "--lock-file",
                self.args.lock_file, "--log-dir", self.args.log_dir, *extra]
        return capture_supervisor.main(argv)

    def test_stop_file_prevents_a_scheduled_launch(self):
        open(self.args.stop_file, "w").close()
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(os.path.exists(self.args.state_file))

    def test_manual_attention_survives_relaunch_until_cleared(self):
        capture_supervisor.write_state(
            {"state": "MANUAL_ATTENTION", "reason": "x", "attention_code": 2},
            self.args.state_file)
        self.assertEqual(self.run_main(), 2)
        self.assertEqual(self.state()["state"], "MANUAL_ATTENTION")
        self.assertEqual(self.run_main("--clear-attention"), 0)
        self.assertEqual(self.state()["state"], "CLEARED")

    def test_second_supervisor_is_refused(self):
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from scripts.capture_supervisor import SingleInstance
            lock = SingleInstance({self.args.lock_file!r})
            assert lock.acquire()
            print("held", flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(self.run_main(), 3)
        finally:
            holder.kill()
            holder.wait()
        # the kernel released it with the process
        released = capture_supervisor.SingleInstance(self.args.lock_file)
        self.assertTrue(released.acquire())

    def test_orphaned_child_notices_a_dead_supervisor(self):
        capture_supervisor.write_state({"state": "RUNNING"}, self.args.state_file)
        self.assertFalse(supervisor_gone(self.args.state_file))
        stale = json.load(open(self.args.state_file))
        stale["updated_at"] = (datetime.now(timezone.utc)
                               - timedelta(minutes=5)).isoformat()
        with open(self.args.state_file, "w") as handle:
            json.dump(stale, handle)
        self.assertTrue(supervisor_gone(self.args.state_file))
        self.assertFalse(supervisor_gone(""))       # unsupervised: never orphaned


class TestStatusVerdicts(unittest.TestCase):

    def report(self, **overrides):
        report = {"reasons": [], "stop_file": False,
                  "supervisor": {"state": "RUNNING"},
                  "instance": {"state": "ACTIVE_SESSION", "heartbeat_age_seconds": 30,
                               "broker_write_attempts": 0},
                  "sessions": [], "mappings": {"RESOLVED": 31}}
        report.update(overrides)
        return report

    def test_codes(self):
        v = capture_status.verdict
        self.assertEqual(v(self.report()), capture_status.OK)
        self.assertEqual(v(self.report(instance={"state": "WAITING_FOR_AUTH",
                                                 "heartbeat_age_seconds": 10})),
                         capture_status.ATTENTION)
        self.assertEqual(v(self.report(instance={"state": "IDLE",
                                                 "heartbeat_age_seconds": 5000})),
                         capture_status.NOT_RUNNING)
        self.assertEqual(v(self.report(instance=None)), capture_status.NOT_RUNNING)
        self.assertEqual(v(self.report(supervisor={"state": "MANUAL_ATTENTION",
                                                   "reason": "crash loop"})),
                         capture_status.MANUAL)
        self.assertEqual(v(self.report(instance={"state": "ACTIVE_SESSION",
                                                 "heartbeat_age_seconds": 1,
                                                 "broker_write_attempts": 1})),
                         capture_status.SAFETY)
        self.assertEqual(v(self.report(mappings={"RESOLVED": 29, "AMBIGUOUS": 2})),
                         capture_status.ATTENTION)
        self.assertEqual(v(self.report(sessions=[{"session_id": "s", "status":
                                                  "finalized", "quality": "FAILED"}])),
                         capture_status.ATTENTION)

    def test_waiting_for_auth_tells_the_human_what_to_do(self):
        report = self.report(instance={"state": "WAITING_FOR_AUTH",
                                       "heartbeat_age_seconds": 10})
        capture_status.verdict(report)
        self.assertTrue(any("browser" in r for r in report["reasons"]))

    def test_status_is_read_only_and_works_without_a_store(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "none.db")
            report = capture_status.collect(db, os.path.join(d, "s.json"),
                                            datetime.now(timezone.utc))
            self.assertFalse(os.path.exists(db), "status must never create the store")
            self.assertEqual(capture_status.verdict(report), capture_status.NOT_RUNNING)


if __name__ == "__main__":
    unittest.main()
