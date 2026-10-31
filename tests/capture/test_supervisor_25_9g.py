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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import capture_status, capture_supervisor
from scripts.run_capture import build_parser, supervisor_gone
from src.capture.schema import initialize_capture_schema
from src.data_access.trading_loop_schema import initialize_trading_loop_schema


def run_capture_parse(argv):
    return build_parser().parse_args(argv)


#: A child spawned by a supervisor, and one started by hand, as each
#: records itself in capture_instances.lease_owner.
SUPERVISED_OWNER = capture_status.SUPERVISOR_OWNER_PREFIX + "host-abcd1234"
MANUAL_OWNER = "capture-manual-0123456789ab"

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

    def test_the_child_is_told_which_supervisor_owns_it(self):
        # The orphan check compares the owner in the state file with the
        # owner the child was given, so the two must be the same string
        # and the state file must actually record it.
        sup = capture_supervisor.Supervisor(self.args)
        argv = sup.command()
        self.assertIn("--owner", argv)
        self.assertEqual(argv[argv.index("--owner") + 1], sup.owner)
        self.assertIn("--supervisor-state", argv)
        sup.save()
        self.assertEqual(self.state()["owner"], sup.owner)
        # and the child's parser accepts it under that name
        parsed = run_capture_parse(argv[2:])
        self.assertEqual(parsed.owner, sup.owner)
        self.assertEqual(parsed.supervisor_state, self.args.state_file)

    def test_a_supervisors_owner_is_recognisable_as_one(self):
        # capture_status spells this prefix itself rather than importing
        # it, for the reason SUPERVISOR_STALE_SECONDS is duplicated. This
        # is the test that keeps the two spellings honest: status decides
        # whether to expect a supervisor by matching it.
        sup = capture_supervisor.Supervisor(self.args)
        self.assertTrue(
            sup.owner.startswith(capture_status.SUPERVISOR_OWNER_PREFIX),
            "capture_status.SUPERVISOR_OWNER_PREFIX no longer matches a real "
            "supervisor owner (%s): status would treat every supervised child "
            "as a manual one and stop checking its supervisor" % sup.owner)


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

    def test_one_failed_read_is_not_a_dead_supervisor(self):
        # 2026-09-21: the child stopped itself twice mid-session with the
        # supervisor alive; a read racing os.replace fails on Windows.
        capture_supervisor.write_state({"state": "RUNNING"}, self.args.state_file)
        real_open, calls = open, {"n": 0}

        def flaky(path, *args, **kwargs):
            if path == self.args.state_file and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError(13, "sharing violation")
            return real_open(path, *args, **kwargs)
        with mock.patch("builtins.open", flaky):
            self.assertFalse(supervisor_gone(self.args.state_file, retry_seconds=0))
        self.assertEqual(calls["n"], 1)

    def test_a_missing_state_file_is_still_a_dead_supervisor(self):
        missing = self.args.state_file + ".absent"
        self.assertTrue(supervisor_gone(missing, retry_seconds=0))

    def test_a_fresh_file_owned_by_another_supervisor_means_orphaned(self):
        """
        2026-10-23 and 2026-10-27, the outage this test exists to stop.

        The supervisor dies; its child lives on. A new supervisor takes
        the OS lock and refreshes the SAME state file, so the orphan
        sees a timestamp seconds old and keeps running -- holding the
        capture lease under the dead supervisor's identity. Every child
        the new supervisor spawns exits 3, and after ten minutes of that
        it latches MANUAL_ATTENTION and capture stops until a human
        intervenes. Freshness cannot distinguish the two cases; the
        owner can.
        """
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": "supervisor-B"}, self.args.state_file)
        # Fresh file, and it is genuinely fresh -- B is alive and well.
        self.assertFalse(supervisor_gone(self.args.state_file, owner="supervisor-B"))
        # Same fresh file, read by a child A spawned. A is gone.
        self.assertTrue(supervisor_gone(self.args.state_file, owner="supervisor-A"))

    def test_an_ownerless_state_file_is_still_judged_on_freshness(self):
        # Back-compatibility: a file written before owners were recorded,
        # and a child started by hand with no --owner, must behave as
        # they always did rather than exit immediately.
        capture_supervisor.write_state({"state": "RUNNING"}, self.args.state_file)
        self.assertFalse(supervisor_gone(self.args.state_file, owner="supervisor-A"))
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": "supervisor-B"}, self.args.state_file)
        self.assertFalse(supervisor_gone(self.args.state_file, owner=None))

    def test_a_real_orphan_process_exits_instead_of_holding_the_lease(self):
        """
        The 2026-10-23 / 2026-10-27 outage, as a process.

        A REAL child process, started under supervisor A, polling the
        same predicate the production child polls, while the state file
        is owned and actively refreshed by supervisor B. Before the
        owner check it would have run forever -- holding the capture
        lease, refusing every child B spawned. It must now exit.
        """
        probe = os.path.join(self.dir, "orphan_probe.py")
        verdict = os.path.join(self.dir, "verdict")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {os.getcwd()!r})
                from scripts.run_capture import build_parser, supervisor_gone
                args = build_parser().parse_args(sys.argv[1:])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if supervisor_gone(args.supervisor_state, owner=args.owner):
                        open({verdict!r}, "w").write("orphaned")
                        sys.exit(0)
                    time.sleep(0.1)
                open({verdict!r}, "w").write("still running")
                sys.exit(1)
            """))

        # Supervisor B owns the file and keeps it genuinely fresh.
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": "supervisor-B"}, self.args.state_file)

        child = subprocess.Popen(
            [sys.executable, probe,
             "--owner", "supervisor-A",
             "--supervisor-state", self.args.state_file,
             "--db", self.args.db,
             "--stop-file", self.args.stop_file,
             "--log-dir", self.args.log_dir])
        try:
            # B refreshes while A's orphan decides, which is the whole
            # point: freshness alone would say "my supervisor is alive".
            for _ in range(20):
                if child.poll() is not None:
                    break
                capture_supervisor.write_state(
                    {"state": "RUNNING", "owner": "supervisor-B"},
                    self.args.state_file)
                time.sleep(0.1)
            self.assertEqual(child.wait(timeout=30), 0,
                             "the orphan did not exit; it would still hold "
                             "the capture lease")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        with open(verdict, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "orphaned")

    def test_the_owner_check_does_not_mask_staleness(self):
        # Right owner, dead supervisor: still gone.
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": "supervisor-A"}, self.args.state_file)
        stale = json.load(open(self.args.state_file))
        stale["updated_at"] = (datetime.now(timezone.utc)
                               - timedelta(minutes=5)).isoformat()
        with open(self.args.state_file, "w") as handle:
            json.dump(stale, handle)
        self.assertTrue(supervisor_gone(self.args.state_file, owner="supervisor-A"))


class TestStatusVerdicts(unittest.TestCase):

    def report(self, **overrides):
        # `age_seconds` is what collect() always records for the state
        # file; a live supervisor rewrites it every 5s. The fixture used
        # to omit it, which is why no test noticed that verdict() never
        # read it.
        # `lease_owner` is what the child itself recorded when it took
        # the lease, and it is how verdict() knows whether a supervisor
        # is expected at all. The baseline child is supervisor-managed.
        report = {"reasons": [], "stop_file": False,
                  "supervisor": {"state": "RUNNING", "age_seconds": 5.0},
                  "instance": {"state": "ACTIVE_SESSION", "heartbeat_age_seconds": 30,
                               "broker_write_attempts": 0,
                               "lease_owner": SUPERVISED_OWNER},
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
        on_battery = self.report(power_alert={"kind": "ON_BATTERY", "battery_percent": 60})
        self.assertEqual(v(on_battery), capture_status.ATTENTION)
        self.assertIn("BATTERY", on_battery["reasons"][0])
        skewed = self.report(clock_alert={"venue_ahead_seconds": 674.7})
        self.assertEqual(v(skewed), capture_status.ATTENTION)
        self.assertIn("674.7", skewed["reasons"][0])
        self.assertEqual(v(self.report(sessions=[{"session_id": "s", "status":
                                                  "finalized", "quality": "FAILED"}])),
                         capture_status.ATTENTION)

    def test_a_live_child_under_a_dead_supervisor_is_not_ok(self):
        # The orphan state: the child heartbeats normally, so every
        # instance-level check passes, but the state file has not been
        # rewritten in minutes. Nothing will restart this child, and
        # nothing holds the lock that keeps a second supervisor out.
        orphaned = self.report(supervisor={"state": "RUNNING",
                                           "age_seconds": 3600.0})
        self.assertEqual(capture_status.verdict(orphaned),
                         capture_status.ATTENTION)
        self.assertTrue(any("supervisor is not" in r
                            for r in orphaned["reasons"]),
                        orphaned["reasons"])

    def test_a_state_file_with_no_timestamp_is_not_assumed_healthy(self):
        unknown = self.report(supervisor={"state": "RUNNING"})
        self.assertEqual(capture_status.verdict(unknown),
                         capture_status.ATTENTION)

    def test_a_fresh_supervisor_is_still_ok(self):
        self.assertEqual(capture_status.verdict(self.report()),
                         capture_status.OK)

    # -- D1: a supervisor that cannot be verified is not a healthy one --
    #
    # collect() sets report["supervisor"] = None for every unreadable
    # state file -- absent, locked mid-os.replace, truncated JSON, or a
    # timestamp fromisoformat refuses -- so all three cases below arrive
    # at verdict() as the same empty dict. Each one leaves a live child
    # with nothing verifiably supervising it, which is the condition
    # that went unreported on 2026-10-23 and 2026-10-27.

    def test_a_supervised_child_with_no_state_file_is_not_ok(self):
        missing = self.report(supervisor=None)
        self.assertEqual(capture_status.verdict(missing),
                         capture_status.ATTENTION)
        self.assertTrue(any("cannot be verified" in r for r in missing["reasons"]),
                        missing["reasons"])

    def test_a_supervised_child_with_unreadable_state_is_not_ok(self):
        # collect() reports None, which verdict() normalises to {}.
        # Pinning the empty dict too keeps both shapes covered if that
        # normalisation ever moves.
        unreadable = self.report(supervisor={})
        self.assertEqual(capture_status.verdict(unreadable),
                         capture_status.ATTENTION)

    def test_a_supervised_child_with_an_unparseable_timestamp_is_not_ok(self):
        # _age() raises ValueError inside collect()'s try, so a state
        # file whose updated_at is not a timestamp lands here as None.
        bad_clock = self.report(supervisor=None)
        self.assertEqual(capture_status.verdict(bad_clock),
                         capture_status.ATTENTION)

    def test_no_reason_reads_none_seconds_ago(self):
        # The operator reads these at 09:00; "None s ago" is not a time.
        for supervisor in (None, {}, {"state": "RUNNING"}):
            with self.subTest(supervisor=supervisor):
                report = self.report(supervisor=supervisor)
                capture_status.verdict(report)
                self.assertNotIn("None", " ".join(report["reasons"]))

    def test_a_manual_child_without_a_supervisor_is_not_flagged(self):
        # A child started by hand has no supervisor by design. Flagging
        # it would make the check cry wolf on every manual session.
        manual = self.report(
            supervisor=None,
            instance={"state": "ACTIVE_SESSION", "heartbeat_age_seconds": 30,
                      "broker_write_attempts": 0, "lease_owner": MANUAL_OWNER})
        self.assertEqual(capture_status.verdict(manual), capture_status.OK)
        self.assertEqual(manual["reasons"], [])

    def test_a_manual_child_is_judged_on_its_own_lease_owner(self):
        # Not on whether a state file happens to be lying around: a
        # stale supervisor.json from an earlier supervised session must
        # not make an unrelated manual child look orphaned.
        manual = self.report(
            supervisor={"state": "RUNNING", "age_seconds": 3600.0},
            instance={"state": "ACTIVE_SESSION", "heartbeat_age_seconds": 30,
                      "broker_write_attempts": 0, "lease_owner": MANUAL_OWNER})
        self.assertEqual(capture_status.verdict(manual), capture_status.OK)

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


class TestUnreadableSupervisorStateThroughCollect(unittest.TestCase):
    """
    D1 through the production reader, not through a hand-built report.

    The verdict tests above hand `verdict()` a dict, so they never touch
    the code that parses the file -- and `{"state": ...}` is valid JSON,
    which is exactly the case `json.load` handles happily. These drive
    `collect()` against a real store and a real state file, so the JSON
    failure and the read failure happen where they happen in production.

    A live, supervised child whose supervisor cannot be verified must
    not read as healthy, however the reading failed.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = os.path.join(self.dir, "capture.db")
        self.state = os.path.join(self.dir, "supervisor.json")
        self.write_instance(SUPERVISED_OWNER)
        # collect() reads the STOP file from the module, so an operator
        # stopping capture for real must not change this test's answer.
        patcher = mock.patch.object(capture_status, "STOP_FILE",
                                    os.path.join(self.dir, "STOP"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_instance(self, lease_owner):
        """One live child, heartbeating now, owned by `lease_owner`."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db)
        try:
            initialize_capture_schema(conn)
            # collect() also reads session_runner_leases, which the
            # trading-loop schema owns; a real store carries both.
            initialize_trading_loop_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO capture_instances "
                "(instance_id, lease_owner, pid, host, started_at, state, "
                " heartbeat_at, broker_write_attempts) "
                "VALUES ('i-1', ?, 4321, 'testhost', ?, 'ACTIVE_SESSION', ?, 0)",
                (lease_owner, now, now))
            conn.commit()
        finally:
            conn.close()

    def verdict(self):
        report = capture_status.collect(self.db, self.state,
                                        datetime.now(timezone.utc))
        return capture_status.verdict(report), report

    def test_a_healthy_supervisor_reads_as_ok(self):
        # The control. Without it the two tests below could pass because
        # the fixture is broken rather than because the check works.
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": SUPERVISED_OWNER}, self.state)
        code, report = self.verdict()
        self.assertEqual(code, capture_status.OK, report["reasons"])

    def test_malformed_json_is_not_reported_as_healthy(self):
        # Truncated mid-key: what a half-written or torn file looks like,
        # and what `{}` never was -- `{}` parses cleanly.
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write('{"updated_at":')
        code, report = self.verdict()
        self.assertIsNone(report["supervisor"],
                          "collect() must survive malformed JSON")
        self.assertEqual(code, capture_status.ATTENTION, report["reasons"])
        self.assertTrue(
            any("cannot be verified" in r for r in report["reasons"]),
            report["reasons"])

    def test_an_unparseable_timestamp_is_not_reported_as_healthy(self):
        # Valid JSON, but _age() raises ValueError inside collect()'s try.
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": SUPERVISED_OWNER}, self.state)
        with open(self.state, encoding="utf-8") as handle:
            state = json.load(handle)
        state["updated_at"] = "not a timestamp"
        with open(self.state, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        code, report = self.verdict()
        self.assertIsNone(report["supervisor"])
        self.assertEqual(code, capture_status.ATTENTION, report["reasons"])

    def test_a_read_error_is_not_reported_as_healthy(self):
        # The Windows case run_capture retries for: the file is there and
        # well-formed, but the open fails. Mocked rather than produced
        # with permissions, which behave differently per platform.
        capture_supervisor.write_state(
            {"state": "RUNNING", "owner": SUPERVISED_OWNER}, self.state)
        real_open = open

        def refuse_the_state_file(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(self.state):
                raise OSError(13, "used by another process")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=refuse_the_state_file):
            code, report = self.verdict()
        self.assertIsNone(report["supervisor"])
        self.assertEqual(code, capture_status.ATTENTION, report["reasons"])
        self.assertTrue(
            any("cannot be verified" in r for r in report["reasons"]),
            report["reasons"])

    def test_a_manual_child_is_unaffected_by_an_unreadable_file(self):
        # Same broken file, a child that never had a supervisor: silence.
        self.write_instance(MANUAL_OWNER)
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write('{"updated_at":')
        code, report = self.verdict()
        self.assertEqual(code, capture_status.OK, report["reasons"])


if __name__ == "__main__":
    unittest.main()
