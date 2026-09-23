# Phase 25.9H — Real IBKR Capture Validation & First-Session Data Quality Audit

**Status of this document: STAGE A (pre-session), written 2026-09-21 08:20 UTC, left as written. STAGE B (post-session), written 2026-09-21 ~20:45 UTC, is appended at the end ([Stage B](#stage-b--first-real-session-2026-09-21)); it answers every PENDING below from persisted evidence.** Stage A was not rerun: nothing in the session evidence contradicted it.

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

---
---

# Stage B — First Real Session, 2026-09-21

Written after the close, from persisted evidence only. The store was opened read-only (`mode=ro`) and nothing was rebuilt. Sources:

- `capture_status.py`, `capture_report.py` and `capture_report.py --acceptance`
- `capture_events`, `capture_ticks`, `capture_quote_samples` and `market_data_bars`
- `data/capture/logs/` and `supervisor.json`
- the Windows System log and `powercfg /batteryreport`

## B1. Verdict

**REAL DATA CAPTURED BUT SESSION INVALID. Quality DEGRADED. Not qualifying.**

- **What was captured is real and clean.** From 13:30 to 19:23 UTC, all 31 members were real IBKR quotes, archived with no duplicates, gaps or contamination, and 0 broker writes.
- **The capture ended 36 minutes early.** The laptop ran on battery from 16:26 UTC and **lost power at ~19:24 UTC**. It rebooted uncleanly and came back with its **system clock 674 s slow**, which is what made the remaining evidence invalid.
- **It would not count even if GOOD.** The data was **delayed for the entire session** (11,222 delayed quotes, 0 realtime).

## B2. Timeline

All times are UTC, each taken from the named source. Times in quotes are the host's slow clock, which ran 674 s behind real time until 20:11:48.

| Time | Event | Source |
|---|---|---|
| 11:48 | Laptop taken off AC before the session; back on AC at 14:55 with 20% left | battery report, Kernel-Power 105 |
| 13:10 | Preflight: 30 of 31 AMBIGUOUS (IBKR lists foreign venues and returns no currency) | `MAPPED` event |
| 13:22 | Fix `3f63152` deployed; restart; 31/31 RESOLVED | events, commit |
| 13:28 | WARMED_UP (31) | event |
| 13:30:00.2 | First tick; 31 requested | ticks |
| 13:55:42 | Child stopped itself ("stop requested"), with no STOP file and the supervisor alive; back at 13:55:54 | log, events |
| 16:26 | **Laptop taken off AC again, 94%** | Kernel-Power 105, battery report |
| 16:38:48 | Same self-stop as 13:55; back at 16:39:05 | log, events |
| 17:07:17 | AUTH_LOST (keepalive refused); re-authenticated 17:08:38 with no human action | events |
| 19:23 | Battery at 3% | battery report |
| 19:24:02 | Last tick of the session | ticks |
| ~19:24 | **Unclean power loss** (Kernel-Power 41; EventLog 6008: "previous system shutdown … was unexpected") | System log |
| boot, +1–5 min | TrustedInstaller (Windows Update) forces **three more reboots** (User32 1074 ×3) | System log |
| boot | System time initialized from the RTC **674 s slow** | Kernel-General 1 at the correction |
| "19:31" | New supervisor started by Task Scheduler with no human action | supervisor log |
| "19:31–19:39" | New child refused 12 times: the dead instance's lease had not yet expired (exit 3) | capture log |
| "19:39" | LEASE_TAKEOVER; gateway **not authenticated** after the reboot | events |
| "19:52:25" = **20:03:39 real** | You logged in; capture resumed **after the close**, on the slow clock | events, lag −674 s |
| "19:52–19:59" = 20:03–20:10 real | 8 ticks labelled in-session; bars placed by venue time at 20:03–20:10; **none archived, no features** | ticks, bars |
| "20:00:03" = 20:11:17 real | Session finalized, DEGRADED | event |
| 20:11:48 | Windows resyncs the clock (**Time Delta 674,488 ms**). The runner logged this as a 406 s "HOST_SUSPEND_GAP". | Kernel-General 1, event |

## B3. Acceptance Record (`capture_report.py --acceptance`)

**Scope and quality**

| Field | Value |
|---|---|
| Evidence | **REAL** (all 4 ticking instances used `ClientPortalTransport`) |
| Status / quality | finalized / DEGRADED |
| Instances / takeovers | 4 / 1 |
| Auth outages / waits / re-auths | 1 / 2 / 5 |
| Resolved | 31/31 |

**Coverage**

| Field | Value |
|---|---|
| Captured minutes | 354 of 390 (0.908), all before the crash |
| Instrument-minutes, observed / resolved | 9,820 / 12,090 (0.812). Before the crash: 9,820 of 10,974 (0.895) |
| Cross-sectional ≥80% minutes | 328 |
| Minutes with ≥1 / ≥3 / ≥5 instruments | 354 / 351 / 351 |
| Median simultaneous | 28 of 31 |
| 1-minute dispersion coverage | 0.975 |
| Largest gap | 36 min, 19:24–20:00, all 31 instruments. Attributed "PROCESS RESTART"; **actually a host power loss.** |

**Archive, features and requests**

| Field | Value |
|---|---|
| Archive | 9,820 bars; 0 duplicates, 0 gap/incomplete archived, 0 unarchived complete, 0 OHLC mismatch, single source |
| Features | 72 cutoffs computed, 0 failed, 0 non-finite, 37,715 values, v1 |
| Requests | max 5 per minute (budget 50); 0 tick overruns; average tick 0.76 s, max 9.3 s |
| Broker writes | **0** |

**Checks (after this stage's audit corrections, §B6)**

| Check | Result | Why |
|---|---|---|
| no mock session, write scope, broker writes, REAL, finalized | PASS | |
| archive integrity | PASS | |
| contract identity, universe mapped | PASS | |
| request budget, tick overrun | PASS | |
| features at every due cutoff | **FAIL** (19:50) | The window comes from the tick stamped "19:52:23", really taken at 20:03:37. That tick returned no quote (all unavailable just after re-login), so it carries no venue time and cannot be flagged as skewed by itself. No real due cutoff was missed. |
| no venue timestamp in the future | **FAIL** (124 beyond 5 s) | All 124 are slow-clock ticks. 56 more, before the crash, were within 4.5 s: ordinary jitter. |
| local clock agrees with venue time | **FAIL** (7 ticks, up to 674.7 s) | Host clock after the power loss |
| no large gap | WARN (36) | Power loss |
| realtime data | WARN | 0 realtime, 11,222 delayed |

## B4. Real Market Data Findings (closes the Stage A PENDING items)

| Stage A item | Evidence | Label |
|---|---|---|
| J. contract mapping | 31/31 RESOLVED to single US primary listings (e.g. AAPL 265598, SPY 756733 ARCA) after the 13:22 fix | VERIFIED REAL IBKR |
| K. request budget | max 5/min; one batched snapshot per tick | VERIFIED REAL IBKR |
| L–N. quote shape, delayed/realtime | Last, bid, ask and volume present. IBKR's marker says **delayed for every quote**: this paper account has no live subscription. The service caps delayed quotes at AGING, so `tradeable=0` and per-tick `health=failed` all day. That is a trading verdict, not a capture failure. | VERIFIED REAL IBKR |
| O. timestamps | `_updated` tracks receipt within ~0.3 s on average (−4.5 to +2.8 s). **So venue "lag" measures gateway refresh, not data delay:** the delay shows only in the marker. The spread of venue times across instruments averages 52 s (max 1,020 s), because illiquid names do not update every minute. | VERIFIED REAL IBKR |
| P. bars | 9,840 bars written before the crash. About 10% of instrument-minutes had no new quote inside the minute (a snapshot of an unchanged delayed quote). "Invalid" quotes (venue time ahead of receipt) still build bars, so no data was lost to that label. | VERIFIED REAL IBKR |
| Q. gaps | 1 (the power loss); 17 gap markers, kept out of the archive | VERIFIED |
| R. archive / provenance | Clean (B3) | VERIFIED |
| S. features | Computed at every real cadence slot | VERIFIED |
| T. auth stability | One IBKR-side reset (17:07), recovered in 81 s without a human. A reboot needs a human login. | VERIFIED REAL IBKR |
| U. process stability | Two spurious self-stops of 12–17 s each; 0 minutes lost (every minute 13:30–19:23 has bars) | REAL HOST |
| V. storage | 18.5 MB + 2.5 MB WAL after one session; `quick_check` ok; 26.1 GB free | REAL HOST |
| W. EOD finalization | Ran, but 11 min late on the slow clock | REAL HOST |
| X. keep-awake | Honoured every time; no idle sleep all session. It cannot prevent a power loss. | REAL HOST |

## B5. Findings

| ID | Severity | Finding | Evidence | Fix |
|---|---|---|---|---|
| B-01 | **HIGH** | The host ran on battery during market hours and died 36 min before the close | Kernel-Power 105/41; EventLog 6008; battery report 94% → 3% | **Operational: keep the laptop on AC from 16:10 to 23:10 local.** Code: the runner now reads the power source during a session and records `ON_BATTERY`, `BATTERY_LOW` (≤25%) and `AC_RESTORED`; `capture_status` shows ATTENTION while on battery. It cannot plug the laptop in. |
| B-02 | **HIGH** | After the power loss the system clock ran 674 s slow until Windows resynced it 48 min later. The runner trusted it: 8 post-close ticks were labelled in-session, and finalization ran 11 min late. | lag −674 on every measured tick of `cap-94c2`; Kernel-General 1 | Runner: a tick whose venue time is >60 s ahead of the local clock gets `health=CLOCK_SKEW`, with one `CLOCK_SKEW`/`CLOCK_OK` event per episode; `capture_status` shows ATTENTION. Bars are placed by venue time and archived only inside the session window, so research data was not moved and cannot be (a test proves nothing past the close is archived). |
| B-03 | MEDIUM | Windows Update (TrustedInstaller) restarted the machine three times at 22:25–22:29 local, during market hours | User32 1074 ×3 | **Operational:** set Windows Update active hours to cover 16:00–23:30 local (Settings → Windows Update → Advanced options). A system setting: yours to change, not the code's. |
| B-04 | MEDIUM | The child stopped itself twice mid-session with the supervisor alive: `supervisor_gone()` treated one failed read of `supervisor.json` as a dead supervisor. The probable cause is a Windows sharing violation while the supervisor `os.replace`s the file. | "stop requested"; no STOP file; exit 0; same supervisor | `supervisor_gone` re-reads up to 3 times, 0.5 s apart. A missing or stale file still counts as gone (tests for both). **Cause inferred, not reproduced.** |
| B-05 | MEDIUM | The acceptance audit counted ≤4.5 s of venue/host jitter as "future" timestamps, and derived feature windows from untrustworthy slow-clock ticks | 56 pre-crash samples within 4.5 s | Audit: 5 s tolerance, with the within-tolerance count still reported; new check "local clock agrees with venue time"; skewed ticks no longer create expected feature windows; negative controls added. **This session's verdict is unchanged: INVALID.** |
| B-06 | MEDIUM | Research maturity would have counted a GOOD session of **delayed** data | 0 realtime all session | `quality.maturity` counts only REALTIME sessions (≥90% of observed quotes realtime). DELAYED and UNPROVEN sessions are listed under `excluded_not_realtime_sessions`. Test added. |
| B-07 | LOW | After a crash, a new supervisor waits out the dead instance's 15-min lease (8 min here). No data was lost: the gateway needed a human login, which came after the close. | 12 × exit 3 | Documented; TTL unchanged. A takeover based on a dead PID is not worth its risk when no data was lost. |
| B-08 | LOW | The gap cause reads "PROCESS RESTART" for what was a power loss, because the store cannot see boots | gaps | Documented; B-01's events make future cases attributable |
| B-09 | INFO | Venue lag does not measure data delay (B4, row O) | samples | Documented; the marker is the evidence |
| B-10 | INFO | Saturday's vanished processes (H-06) now have a likely sibling: today's disappearance was a hard power loss, not a Python crash | — | H-06 stays open; sleep plus battery is the prime suspect |

## B6. Changes in This Stage

| File | Change |
|---|---|
| `src/capture/host.py` | `power_status()` (read-only `GetSystemPowerStatus`) |
| `src/capture/runner.py` | Power check during a session; clock-skew labelling and events; config `clock_skew_seconds=60`, `battery_low_percent=25` |
| `scripts/run_capture.py` | `supervisor_gone` retries a failed read |
| `scripts/capture_status.py` | ATTENTION on an open battery or clock-skew episode |
| `src/capture/audit.py` | 5 s venue-clock tolerance; local-clock check; skewed ticks excluded from feature windows |
| `src/capture/quality.py` | Only REALTIME sessions qualify |
| tests | 5 runner (power, skew, recovery), 3 audit, 2 supervisor-read, status codes, 1 maturity; the harness fakes power and a venue clock offset |

None of this touches the order path, the gateway guards or `ordering_enabled`.

## B7. Isolation and Integrity (re-run, all read-only)

| Audit | Result |
|---|---|
| research integrity | CLEAN, exit 0; ledger chain intact; D20 NOT_CONSUMED |
| D20 readiness (status only) | NOT READY (before 2026-09-25; 213 pending) |
| live safety | 16/16 PASS |
| trading readiness | Order submission impossible; stop point unchanged |
| capture write scope | PASS; execution tables empty |

## B8. Research Data Maturity

**INSUFFICIENT: 0 qualifying sessions.**

- Today's session is DEGRADED. Under B-06 it would be excluded as DELAYED even if it were GOOD.
- Until the account has a realtime subscription, **no session can qualify**, so progress toward the 20-session milestone has not started. For the paper account, that means IBKR market-data sharing from a funded live account.

## B9. Phase 25.9G Closure

**CLOSED: capture works against the real venue.** It mapped, polled, built bars, archived, computed features, recovered from an IBKR auth reset without a human, and finalized, with 0 broker writes. The session is invalid because of the host (power, clock, Windows Update), not the capture design.

## B10. Next Evidence-Based Steps

1. **You, before 16:10 local tomorrow:** keep the laptop on AC, set Windows Update active hours (B-03), and log in to the gateway.
2. **Deploy:** these changes go live at a closed-market boundary (stop, then start); see the commit.
3. **After tomorrow's close:** run `capture_report.py --acceptance`. Expected: without a power loss, a GOOD or DEGRADED grade on data quality alone, still not qualifying while data is delayed.
4. **Realtime data** is the gate for maturity. When the live account is funded, add the paper-account (DU…) guard before any capture or trading is pointed at it.

---

PHASE 25.9H STAGE B:
COMPLETE

REAL SESSION:
CAPTURED (DEGRADED; 354/390 minutes; host power loss)

ACCEPTANCE VERDICT:
REAL DATA CAPTURED BUT SESSION INVALID

REAL IBKR MARKET DATA:
VERIFIED (DELAYED)

BROKER WRITES:
0

PHASE 25.9G:
CLOSED

RESEARCH MATURITY:
INSUFFICIENT (0 qualifying; delayed sessions never count)

D20 RESULT:
UNSEEN

D20 TEST:
NOT EXECUTED

REAL IBKR ORDER:
NOT ATTEMPTED

LIVE:
DISABLED

---

# Stage B addendum — Second Session, 2026-09-22

Read-only, from the same store. Stage B above is unchanged; this records the first session that passed.

## C1. Verdict

**FULL SESSION VERIFIED (`--acceptance` exit 0). Quality GOOD. Still not qualifying: delayed.**

Every check passes. The only warning is the one that cannot be fixed from here: 12,028 delayed quotes, 0 realtime.

## C2. What the Stage B fixes were meant to catch, and what happened

| Stage B finding | Yesterday | Today |
|---|---|---|
| B-01 host on battery | Died at 19:24, 36 min lost | No `ON_BATTERY` event: the laptop stayed on AC all session |
| B-02 clock skew | 674 s slow; 8 post-close ticks stamped in-session | 0 skewed ticks; 0 venue timestamps beyond the 5 s tolerance (1 within it) |
| B-04 spurious self-stops | 2 restarts (13:55, 16:38) | 1 process instance, 0 restarts, 0 takeovers |
| B-05 audit attribution | 2 FAILs from the clock | No FAILs |
| B-06 delayed sessions | Would have counted if GOOD | GOOD, and correctly excluded as DELAYED |

The guards stayed silent because there was nothing to report. They are not yet proven against a real recurrence — only the tests prove the detection itself.

## C3. Record

| Field | 2026-09-22 | 2026-09-21 |
|---|---|---|
| Verdict / quality | **FULL SESSION VERIFIED** / GOOD | INVALID / DEGRADED |
| Captured minutes | 388 / 390 (0.995) | 354 (0.908) |
| Instrument-minutes | 10,841 / 12,090 (0.897) | 9,820 (0.812) |
| Cross-sectional ≥80% minutes | 354 | 328 |
| Median simultaneous | 29 of 31 | 28 |
| Largest gap | 1 min | 36 min |
| Process instances / takeovers | 1 / 0 | 4 / 1 |
| Auth outages / waits / re-auths | 2 / 1 / 1 | 1 / 2 / 5 |
| Features | 78 cutoffs, 0 failed, 41,800 values, coverage 1.0 | 72 cutoffs, 37,715 values |
| Archive | 10,841 bars; clean | 9,820 bars; clean |
| Venue lag avg | +0.60 s | −12.6 s (clock) |
| Venue spread avg / max | 44.8 s / 959.8 s | 52.4 s / 1,019.9 s |
| Requests peak | 5/min (budget 50) | 5/min |
| Broker writes | **0** | 0 |
| Store | 42.9 MB total, 21.0 GB free | 18.5 MB |

## C4. Notes

- **Both IBKR auth resets healed themselves.** 17:08:06 AUTH_LOST → 17:10:22 AUTHENTICATED (2 min, the only 2 missing minutes of the day); 19:34:17 → 19:34:24 (7 s). No human acted. Three such resets in two sessions: an IBKR-side session reset is routine, not an incident.
- **~10% of instrument-minutes still have no bar** (10,841 of 12,090), as on the first session. The cause is unchanged: a delayed snapshot of a thinly traded name does not update inside every minute. This should be re-measured on realtime data before it is treated as a property of the capture.
- **Storage:** 42.9 MB after two sessions (~21 MB each), consistent with the ~5.3 GB/year estimate.

## C5. Maturity

**INSUFFICIENT, 0 qualifying sessions**, `excluded_not_realtime_sessions = {cap-2026-09-21: DELAYED, cap-2026-09-22: DELAYED}`. The count toward 20 starts on the first REALTIME session, not before.

CAPTURE PIPELINE:
VERIFIED END TO END AGAINST THE REAL VENUE

REALTIME DATA:
STILL ABSENT (the only blocker to maturity)

---

# Stage B addendum — Third Session, 2026-09-23, and the GPU-hang finding

## D1. Verdict

**FULL SESSION VERIFIED (exit 0). Quality GOOD, 390 of 390 minutes, coverage 1.000, largest gap 0.** Still not qualifying: 12,059 delayed quotes, 0 realtime.

The best session so far, and it nearly did not happen: capture was dead three minutes before the open.

| Field | 2026-09-23 | 2026-09-22 | 2026-09-21 |
|---|---|---|---|
| Verdict / quality | **VERIFIED** / GOOD | VERIFIED / GOOD | INVALID / DEGRADED |
| Captured minutes | **390 / 390 (1.000)** | 388 (0.995) | 354 (0.908) |
| Instrument-minutes | 10,889 (0.901) | 10,841 (0.897) | 9,820 (0.812) |
| Cross-sectional ≥80% minutes | **362** | 354 | 328 |
| Largest gap | **0** | 1 min | 36 min |
| Process instances | 1 | 1 | 4 |
| Auth outages | 1 | 2 | 1 |
| Features | 78 cutoffs, 41,287 values, coverage 1.0 | 78, 41,800 | 72, 37,715 |
| Venue timestamps ahead, within 5 s tolerance | 10 | 1 | 56 (plus 124 skewed) |
| Broker writes | **0** | 0 | 0 |

## D2. Finding B-11 (HIGH): a GPU driver hang kills capture, and only a log-on restarts it

**What happened.** At about 16:26 local (13:26 UTC), four minutes before the open, the desktop app died and took its terminal with it. Both capture processes were terminated at the same moment. The child's last line is "capture stopped: stop requested" with **no STOP file and no Python error**: it noticed its supervisor was gone and exited cleanly, which is the orphan path behaving correctly. The IBKR gateway session went too, so a human had to log in again.

**Cause.** Windows recorded bursts of `LiveKernelEvent 141` (video engine timeout: the GPU stopped responding and the driver was reset) at 16:04, 16:09 and 16:29 local. This is chronic, not new: **four events per burst, ~38 bursts in the seven days to 2026-09-23**, several a day since at least 09-16. Both display drivers are years old — Intel UHD 620 `27.20.100.8681` (2020-09-05) and NVIDIA MX150 `30.0.15.1169` (2022-02-01). The desktop app renders through the GPU, so a hang kills it.

**Not proven:** that the hang is what killed the two Python processes. There is no crash record for `pythonw`; they were terminated without an exception, at the moment the app died. The timing is the evidence; the mechanism is not.

**Why it mattered more than the crash.** The task's triggers were log-on and daily 08:00, and the supervisor only restarts *its own* child. With the supervisor itself dead mid-morning, **nothing would have started capture again**: the session would have been lost entirely. It was restarted by hand at 13:29:36 UTC, 24 seconds before the open, which is why the day still scored 390/390.

This is also the likeliest explanation for **H-06** (the processes that vanished on Saturday 09-19 with no log line); bursts were recorded that afternoon. H-06 stays open: still not reproduced.

## D3. The watchdog, and what it actually recovers

`capture_task.ps1 install` now registers a third trigger: a daily trigger carrying a 24-hour repetition every `-WatchdogMinutes` (default 10). `MultipleInstances IgnoreNew` makes every firing a no-op while capture runs, so it costs nothing and only acts when capture has died. A STOP file still blocks it, so `stop` keeps working.

Deployed after the close on 2026-09-23 (remove, install, start; the market was closed and capture idle).

**Two things this stage got wrong before it worked, both recorded because the fix looked right both times:**

1. `[TimeSpan]::MaxValue` — the usual "repeat indefinitely" idiom — is **rejected by this Windows build**: `Duration:P99999999DT23H59M59S … incorrectly formatted or out of range`. The install failed with the task already removed, so capture stayed down until it was fixed. A daily trigger with a 1-day repetition gives the same continuous cover and every day renews it. (`-RepetitionInterval` is only accepted on a `-Once` trigger, so the repetition is built there and moved onto the daily trigger.)
2. An untested watchdog is worth little, so it was tested on the real host with the market closed: **both processes killed at 20:05:33 UTC**.

**Measured recovery: 12 min 41 s.**

| Step | Time | |
|---|---|---|
| Both processes killed | 20:05:33 | |
| Watchdog fired, supervisor started | 20:10:01 | 4.5 min: the 10-minute cadence |
| Child refused, exit 3, four times | 20:10:06 – 20:17:43 | the killed instance's lease ran to 20:17:49 |
| New instance IDLE, new lease | 20:18:14 | recovered |

So the watchdog closes the hole, but the **lease TTL now dominates recovery**: worst case is about 10 min of watchdog plus up to 15 min of a dead instance's lease — roughly **25 minutes of a session**, instead of all of it. B-07 judged a dead-PID takeover not worth the risk when the alternative was losing nothing; with the watchdog in place that trade has changed, and it is the obvious next improvement.

## D4. What is now proven about the host, and what is still on you

| | State |
|---|---|
| Battery (B-01) | Held: on AC all session, no `ON_BATTERY` event |
| Clock (B-02) | Held: 0 skewed ticks, 0 timestamps beyond tolerance |
| Self-stops (B-04) | Held: 1 process instance, 0 restarts |
| GPU hangs (B-11) | **Open: needs a driver update.** The watchdog limits the damage; it does not stop the crashes |
| Windows Update hours (B-03) | Unverified: no forced reboot happened today |
| Realtime data | Still absent. Three GOOD-grade sessions, 0 qualifying |

CAPTURE AFTER A HOST CRASH:
SELF-HEALING, VERIFIED ON THE REAL HOST (12 min 41 s)

GPU DRIVER HANGS:
CHRONIC, UNFIXED (driver update is a human action)
