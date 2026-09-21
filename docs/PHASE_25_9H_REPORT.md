# Phase 25.9H — Real IBKR Capture Validation & First-Session Data Quality Audit

**Status of this document: STAGE A (pre-session), written 2026-09-21 08:20 UTC.** The first real session opens today at 13:30 UTC. Every Stage B section below is marked **PENDING**; none of it is filled from assumption.

Evidence labels:

- **VERIFIED REAL IBKR**: an IBKR endpoint answered.
- **REAL HOST**: the real process or store on this PC, with no IBKR data involved.
- **MOCK VERIFIED**: production code against the mock venue.
- **CODE ONLY**: implemented, not exercised.
- **NOT TESTED**.

---

## Post-session runbook (Stage B re-entry)

After today's close (20:00 UTC = 23:00 local; the session is finalized about 10 minutes later), run in PowerShell from the repository:

```
python scripts\capture_status.py
python scripts\capture_report.py
python scripts\capture_report.py --acceptance
```

`--acceptance` exits 0 when every check passes, 1 when a check fails and 2 when there is no real session. Then start Claude with exactly:

> **Resume Phase 25.9H Stage B using the existing capture store. Do not rerun or rewrite Stage A unless evidence requires it.**

Stage B reads only persisted evidence:

- `data/capture/intraday_capture.db` (opened read-only)
- `data/capture/logs/`
- `data/capture/supervisor.json`
- Task Scheduler state

Nothing is rebuilt.

---

## A. Executive Summary

Stage A is complete.

- **Real IBKR contact.** The real Client Portal Gateway answered read-only calls: authenticated, connected, not competing, keepalive accepted (VERIFIED REAL IBKR, auth and heartbeat only).
- **Capture is running under Task Scheduler.** State IDLE, keeping your morning login alive; zero broker writes.
- **Automatic start after sleep.** On Monday morning the host woke from a 42-hour sleep and Task Scheduler started capture again without any human action (REAL HOST).

The audit found three things that threatened today's evidence. They were fixed and deployed at a market-closed boundary (08:05 UTC), with one justified restart:

1. **HIGH:** the host puts itself to sleep, as it did on Saturday at 16:22 local. A sleep during 16:30–23:00 local loses the session. Capture now asks Windows not to *idle*-sleep from the pre-open to the post-close only; the request is withdrawn outside that window. It cannot stop a lid close or a Sleep you choose.
2. **MEDIUM:** "real" had no structural definition. Every process now records its transport, and only sessions ticked entirely by `ClientPortalTransport` count as REAL, in the audit and in research maturity.
3. **MEDIUM:** today's session would not have left the evidence §17–§21 require. Per tick it now records realtime/delayed counts, venue-timestamp spread and lag, and requests in the last minute, plus a bounded sample of normalized real quotes.

The Stage B audit is built into the canonical `capture_report.py` as `--acceptance`. It is read-only, and 21 negative-control tests prove it catches each failure in §76.

## B. Evidence Classification

See §91. At Stage A only auth and heartbeat are VERIFIED REAL IBKR. **Authentication is not market data** (§8): contracts, quotes, bars, archive and features remain unproven until Stage B.

## C. Repository Baseline

| Item | Value |
|---|---|
| Branch / HEAD at start | `ibkr-paper-validation-fixes` / `447d482`, clean tree |
| Python | 3.12.10 |
| Capture store | `data/capture/intraday_capture.db`, 0.62 MB + 0.24 MB WAL, `quick_check` ok, WAL mode |
| Universe | `v1`, sha256 `66b2c3098f3c…`, 31 members, registered 2026-09-19 |
| Mappings / sessions / ticks / bars / features | 0 / 0 / 0 / 0 / 0 (nothing captured yet: expected before the first pre-open) |
| Broker write attempts | 0 (all instances) |
| Free disk | 14.7 GB |
| Market | closed; opens 2026-09-21 13:30 UTC, closes 20:00 UTC |

## D. Capture Runtime State

- Supervisor RUNNING; child `run_capture.py` running; lease held and renewing.
- Runner IDLE, `auth connected`, heartbeat about every 60 s (idle keepalive, commit `447d482`).
- Instance history, all REAL HOST:

| Instance | Transport | Fate |
|---|---|---|
| Sat 19 Sep 12:48 UTC | (pre-25.9H) | last heartbeat 13:18 UTC, then **vanished during host sleep with no log line** |
| Mon 07:17 UTC | (pre-25.9H) | started automatically after wake; recorded LEASE_TAKEOVER of the vanished instance; stopped by me at 07:35 to deploy the keepalive |
| Mon 07:35 UTC | (pre-25.9H) | stopped by me at 08:05 to deploy 25.9H evidence fields |
| Mon 08:05 UTC | `ClientPortalTransport` | **current** |

## E. Task Scheduler

- Registered, enabled, State Running. Triggers: log-on and daily 08:00. Interactive, no stored password. Next run 2026-09-22 08:00.
- **Automatic start: OBSERVED.** The host resumed from sleep at 10:15:48 local; a supervisor started at 10:17:10 with no human or Claude action (supervisor and capture logs, and the LEASE_TAKEOVER event).
- Which trigger fired cannot be proven. The Task Scheduler history log is disabled on this PC, and it is consistent with the missed 08:00 daily trigger catching up (`StartWhenAvailable`) or with log-on. So: **TRIGGER VERIFIED (automatic start observed; trigger type unidentified)**.
- Open item: why the Saturday processes disappeared. There is no Python crash in the Application log; only GPU-driver LiveKernelEvents 141/15f on resume. Recovery was automatic.

## F. Real IBKR Gateway

The gateway is running on this host (`localhost:5000`) and was started by you.

| Call | Result |
|---|---|
| `POST /iserver/auth/status` | answered, 4.4 s |
| `POST /tickle` | answered, 0.1 s |

The probe went through `ReadOnlyTransport`, so no write method was reachable. It cost 2 requests while the runner was idle, so it did not compete with capture.

## G. Authentication

- 07:29 UTC probe: authenticated=True, connected=True, competing=False.
- 08:19 UTC probe: the same.
- Since 07:35 the runner's own keepalive has kept `auth_state=connected`.
- **VERIFIED REAL IBKR: auth, heartbeat.**
- Not yet known: session stability across market hours (Stage B, §45).

## H. Capture Safety

- Unchanged: gateway and transport both refuse writes. The runner refuses an unguarded gateway, and `ordering_enabled` is forced off.
- The probe used the same read-only transport wrapper.
- **BROKER WRITES: BLOCKED**, attempts 0.

## I. Universe Configuration

`config/capture_universe_v1.json`, immutable, sha-registered. 31 members: SPY plus 2 per sector across 15 sectors. Unchanged by this phase.

## J. Real Contract Mapping — **PENDING (Stage B)**

Mapping happens in the canonical preflight at 13:10 UTC. It was deliberately not run early (§9–§10) to avoid duplicating the preflight's work. The audit reports, per member: status, conid, symbol, security type, currency and exchange, and FAILs on identity mismatches (§14).

## K. Request Budget

Now recorded per tick (`requests_last_minute`), checked against the 50/min limit by the audit. Real figures: PENDING.

## L–Q, R–AA, AG — **PENDING (Stage B)**

Real quote shape, delayed/realtime status, timestamps, synchronization, bars, gaps, archive, provenance, features, cross-sectional coverage, dispersion, session coverage and quality, process and auth stability, performance, SQLite growth, EOD finalization: none exists yet.

The instruments to measure them are in place:

| Evidence | Where it comes from |
|---|---|
| snapshot shape, realtime/delayed, clocks | `capture_quote_samples`, plus per-tick `realtime`/`delayed`/`unknown_availability`/`venue_spread_seconds`/`venue_lag_seconds` |
| gaps, attributed (AUTH / HOST SLEEP / PROCESS RESTART / QUOTE FAILURE / NOT STARTED / UNKNOWN) | audit gap scan |
| archive: duplicates (timestamp-normalized), gap/incomplete archived, complete-but-unarchived, OHLC mismatch, missing provenance, foreign source | audit archive check |
| features: eligible 5-min windows, computed, failed, recomputed, non-finite | audit feature check |
| denominators kept apart: session minutes, expected / resolved / observed instrument-minutes | acceptance record |

## AB. SQLite / Storage (Stage A part)

`quick_check` ok (read-only), WAL mode, 0.87 MB total, 14.7 GB free. Growth: PENDING.

## AC. Production Write Boundary

Unchanged allowlist, plus the new capture-owned table `capture_quote_samples`. The audit FAILs on any non-allowlisted table holding rows, or on any execution, model, signal or risk row. Real-store audit now: PASS (no unexpected rows; `execution_orders`, `execution_fills` and `execution_events` are empty).

## AD. Broker-Write Audit

Stage A: 0 attempts in every instance row and 0 BROKER_WRITE_REFUSED events. Re-checked in Stage B.

## AE. Mock / Real Isolation

- **Structural.** The real store is only ever written by `run_capture.py`, which has no mock mode. Every instance now records its transport class.
- **In the audit and in maturity:** REAL means every ticking process used `ClientPortalTransport`. A mock class name gives MOCK; a missing value (the pre-25.9H instances) gives UNKNOWN. Neither counts.
- The audit FAILs if the store holds any MOCK session.
- Tests prove it: a relabelled-mock session is "NO REAL SESSION CAPTURED", a legacy one is UNKNOWN, a mixed store FAILs, and maturity counts 0 MOCK sessions.

## AF. Research Data Maturity

INSUFFICIENT, with 0 REAL sessions. One session cannot move it past INSUFFICIENT: the 25.9F calendar ceiling is unchanged.

## AH. D20 Isolation

No D20 code was run except the status-only `check_d20_readiness.py`. The ledger is `NOT_CONSUMED` (readiness NOT READY: the earliest test date has not arrived). D20 HYPOTHESIS FROZEN, ANCHOR-V2 UNCHANGED, RESULT UNSEEN, TEST NOT EXECUTED.

## AI. Tests

- New: `tests/capture/test_audit_25_9h.py` (21), plus 7 evidence-field and 5 idle-keepalive tests.
- Capture suite: 110.
- Full suite (`PYTHONPATH=src python -m unittest discover -s tests -t . -b`): **4,329 tests OK (1 skipped)**. No existing test weakened.

## AJ–AL. Research Integrity / Trading Readiness / Live Safety

| Audit | Result |
|---|---|
| research integrity | CLEAN, exit 0: ledger chain intact, D20 NOT_CONSUMED |
| trading readiness | exit 0: order submission impossible from the command. Stop point unchanged, because the operational layer has not run against the *production* database; capture uses its own store by design |
| live safety | exit 0: IBKR is the only broker; real-money execution blocked by default |

## AM. Findings / Remediations (Stage A)

| ID | Severity | Finding | Evidence | Fixed | Tests |
|---|---|---|---|---|---|
| H-01 | HIGH | Host idle-sleeps (Sat 16:22 → Mon 10:15); a sleep in market hours loses the session | System log events 42/107; last Saturday heartbeat 16:18 local | yes: keep-awake request pre-open → post-close only; deployed 08:05 UTC | window on/off, weekend never |
| H-02 | MEDIUM | No structural real/mock distinction in the store | schema review | yes: `capture_instances.transport`; REAL-only maturity and audit | 5 |
| H-03 | MEDIUM | §17–§21 evidence (realtime/delayed, venue clocks, spread, request rate, quote shape) would not be persisted | schema review | yes: per-tick columns and bounded `capture_quote_samples` (in-place migration) | 6 |
| H-04 | LOW | urllib3 warned on every localhost request (≈ 250 KB/day to `child-stderr.log`) | log inspection | yes: once per process | — |
| H-05 | LOW | Features are delayed up to 5 min after a late login (an empty cutoff consumes the cadence slot) | fixture | documented; the audit accounts for it; runner unchanged (no restart for a LOW) | audit test |
| H-06 | LOW (open) | Saturday processes vanished during sleep with no log line | logs; no Python crash event | not reproducible; recovery automatic | — |
| H-07 | INFO | `auth/status` took 4.4 s | probe | — | — |

The audit tool's own defects (feature-window alignment, gap auth-state attribution) were found by its negative controls and fixed before use.

## AN. Remaining Risks

- **Everything in Stage B:** the real contract mapping for 31 symbols (AAPL may be AMBIGUOUS), whether the data is realtime or delayed for this paper account, the real cold-contract behaviour, and real timing.
- **The host:** lid close or a manual Sleep still stops capture; keep-awake prevents only idle sleep. The gateway window must stay open.
- **Auth:** an IBKR-side session reset during the day forces a re-login; capture waits and records the outage.

## AO. Phase 25.9G Closure Status

Remains **INCOMPLETE** until Stage B evidence exists. It is decided in Stage B.

## AP. Next Evidence-Based Step

Let the persistent capture process collect today's session. After the close, run the runbook above and resume Stage B.

---

PHASE 25.9H ENGINEERING / AUDIT PREPARATION:
COMPLETE

REAL IBKR GATEWAY:
VERIFIED

REAL IBKR AUTH:
VERIFIED

CAPTURE PROCESS:
RUNNING

CAPTURE-ONLY SAFETY:
PASS

BROKER WRITES:
BLOCKED

AUDIT TOOLING:
READY

REAL SESSION:
PENDING

PHASE 25.9H OVERALL:
AWAITING REAL SESSION

D20 RESULT:
UNSEEN

D20 TEST:
NOT EXECUTED

REAL IBKR ORDER:
NOT ATTEMPTED

LIVE:
DISABLED

NEXT ACTION:
Let the existing persistent capture process collect the real market session.
After the session, resume Phase 25.9H Stage B against the existing capture
store. Do not rebuild Stage A unless evidence requires it.
