# Phase 25.9G — Continuous Market Data Capture & Persistent Session-Runner Deployment Report

Date: 2026-09-19 (Saturday; US market closed; IBKR Client Portal Gateway not running on this host)
Branch: `ibkr-paper-validation-fixes`

Evidence labels used throughout:

- **VERIFIED REAL IBKR**: the check contacted IBKR itself.
- **REAL HOST**: the real process, on this Windows host, against the real transport code. IBKR was not contacted.
- **MOCK VERIFIED**: production code driven by the Phase 15 mock venue and a replay clock.
- **CODE ONLY**: implemented, not exercised.

Nothing in this report is VERIFIED REAL IBKR. No IBKR session was available.

---

## A. Executive Summary

Phase 25.9E left intraday capture as code with no process to run it. This phase builds that process. It is a supervised, calendar-driven, capture-only runner (`src/capture/`) with its own store, a supervisor with bounded restart and crash-loop protection, a read-only status command with exit codes, a session and maturity report, and a Windows Task Scheduler deployment script. It reuses the canonical components (`MarketDataService`, `MinuteBarBuilder`, `USEquityCalendar`, `IBKRGateway`, the 25.9E lease, `archive_operational_bars`, `IntradayDatasetBuilder`) and adds no second service, calendar or adapter.

The runner cannot write to IBKR, by construction:

- the gateway is wrapped by `PreSubmissionGateway`, so submit, cancel and modify are refused;
- the transport is wrapped by a new `ReadOnlyTransport`, so `place_order`, `cancel_order` and `reply` are refused;
- the runner refuses to start with anything else;
- any attempted write stops the process with exit 4.

What is proven:

- A full simulated session runs through the production code: 390 of 390 minutes, every minute cross-sectional, archived idempotently, features persisted, zero venue writes (MOCK VERIFIED).
- Every injected failure in §127 behaves as specified (MOCK VERIFIED unless marked REAL HOST).
- The real process chain was rehearsed on this host: supervisor, child, status, duplicate refusal and graceful STOP (REAL HOST).
- On the real transport with no gateway, the runner lands in WAITING_FOR_AUTH and tells the human what to do (REAL HOST).

What is not proven: any real IBKR contact, any real captured session. The gateway was not running and the market is closed. **REAL FULL-SESSION CAPTURE = NOT VERIFIED.** The scheduled task is also **not installed**, because registering it is a persistent host change that needs your approval (section AP).

## B. Baseline

- HEAD `993bbdf` on `ibkr-paper-validation-fixes`. Main carries the daily-pipeline hotfix `f05ce23`.
- Full suite green at the start of this phase (25.9F closed with a passing suite; the first full run in this phase, with the first capture tests included, passed 4,274 tests, 1 skipped).
- Existing pieces reused, not rebuilt:
  - `MarketDataService.run_cycle` (25.7)
  - `MinuteBarBuilder` (25.7), which writes explicit gap rows and never interpolates
  - `USEquityCalendar` (25.9E), covering holidays and early closes
  - `IBKRGateway` / `ClientPortalTransport` / `MockIBKRTransport` (15)
  - `PreSubmissionGateway` (25.9E)
  - `session_runner_leases` (25.9E)
  - `archive_operational_bars`, `load_research_bars` and the intraday feature registry v1 (25.9F)
  - `calendar_ceiling` (25.9F)

## C. Deployment Gap Re-Audit

Re-checked on 2026-09-19, not assumed:

| Question | Answer |
|---|---|
| Any persistent process in the repo? | None before this phase. Every entry point was a batch job. |
| Scheduled task for capture? | None registered (`Get-ScheduledTask`: no MarketLens task). |
| Could GitHub Actions host it? | No. The Client Portal session is a browser login on this machine, and the spec forbids moving it to Actions. Actions also has no persistent runtime. |
| Gateway on this host? | Not running (no listener on 5000/5001/4001/4002/7496/7497). |
| Market | Closed (Saturday). Next regular session: Mon 2026-09-21, 13:30 UTC. |
| `archive_operational_bars` | Existed but was never called. Audited and hardened (section R). |

## D. Chosen Persistent Runtime

**Windows Task Scheduler → `capture_supervisor.py` → `run_capture.py`**, on this host, as the logged-on user.

- **Why Task Scheduler.** It is built into Windows, restarts on log-on, needs no service wrapper and no admin rights, and stores no password when the logon type is *Interactive*.
- **Why interactive.** The IBKR gateway needs a person at a browser anyway, so a task running with nobody logged on could only ever wait for auth.
- **Why a Python supervisor inside the task.** Task Scheduler's own restart handles a failed *launch*, not a crash loop inside a long-lived process. The supervisor owns restart policy, crash-loop detection and MANUAL_ATTENTION.
- **Paths are explicit.** Repo root is derived from the script location; the store is `data/capture/intraday_capture.db`; logs are in `data/capture/logs/`; config is `config/capture_universe_v1.json`. Python is the resolved `pythonw.exe`. Nothing depends on the launching shell's working directory. `data/capture/` is gitignored.

**Separate store.** Production `marketlens.db` is a GitHub release asset that CI downloads and re-uploads with `--clobber`. A second uploader is exactly the race that failed the daily run on 2026-09-17. Capture therefore writes only to its own file and never uploads anything. `run_capture` refuses any store named `marketlens.db`.

## E. Supervisor / Process Lifecycle

`scripts/capture_supervisor.py`:

| Child exit | Meaning | Supervisor action |
|---|---|---|
| 0 | clean stop | honour STOP; otherwise respawn |
| 1 | crash | respawn after 5 s, 10 s, 20 s … capped at 300 s |
| 2 | configuration | MANUAL_ATTENTION, no restart |
| 3 | lease held elsewhere | retry every 30 s for 10 min, then MANUAL_ATTENTION |
| 4 | venue write attempted | MANUAL_ATTENTION, no restart |

- **Crash loop.** Five crashes within ten minutes puts the supervisor in MANUAL_ATTENTION and stops it. It **refuses to start again**, even when Task Scheduler relaunches it, until a human runs `--clear-attention`.
- **One supervisor.** An OS file lock (`msvcrt`/`fcntl`), which the kernel releases when the process dies.
- **Orphan safety.** A child whose supervisor stops refreshing `supervisor.json` for 90 s exits cleanly and releases its lease.
- **State file.** `data/capture/supervisor.json` records state, pid, owner, restarts, last exit and reason.

Runner lifecycle (`src/capture/runner.py`): STARTING → IDLE → PREFLIGHT → (WAITING_FOR_AUTH) → WAITING_FOR_MARKET → ACTIVE_SESSION → CLOSING → POST_SESSION → IDLE, and STOPPED on exit. The exchange calendar decides every transition.

## F. IBKR Authentication Boundary

- The runner **asks** the gateway whether a human is logged in (`IBKRGateway.connect()`). It never logs in.
- If nobody is logged in, it enters WAITING_FOR_AUTH:
  - announces this once in the log and the event table, with the instruction "log in to the IBKR Client Portal Gateway in a browser; capture resumes by itself";
  - retries every 60 s;
  - resumes on its own.
- `capture_status` exits 1 during the wait.
- If a session expires mid-day, the keepalive fails, AUTH_LOST is recorded and the runner goes back to WAITING_FOR_AUTH. The outage becomes explicit gap markers, never bars.
- REAL HOST, 2026-09-19: the real `ClientPortalTransport` with no gateway listening reached WAITING_FOR_AUTH (`auth_failed`) in 10.2 s, with no crash.
- Nothing stores, passes, types or scripts a credential. `IBKRConfig` has no credential field. The scheduled task carries no password: interactive logon, and the arguments are the script path only. A test scans every capture file and the PowerShell script for credential handling.

## G. Capture-Only Safety

- `capture_only(gateway)` wraps the transport in `ReadOnlyTransport` (`place_order`, `cancel_order` and `reply` raise `BrokerSubmissionForbidden`). It then wraps the gateway in `PreSubmissionGateway` (`submit_order`, `cancel_order` and `modify_order` raise).
- `CaptureRunner` refuses any gateway where either layer lacks `submission_forbidden`.
- `IBKRConfig` is forced to `ordering_enabled=False`.
- `broker_write_attempts()` is checked before and after every step. Any refused attempt raises `CaptureSafetyViolation`, which gives exit 4 and MANUAL_ATTENTION.
- Negative control (MOCK VERIFIED): all six write paths were called deliberately. All raised `PHASE_25_9E_BROKER_SUBMISSION_FORBIDDEN`, the venue's `place_calls` and `cancel_calls` stayed at 0, and the next step stopped the runner.
- **Independence.** A fresh process importing `run_capture` and `src.capture.runner` loads none of these modules: `src.models`, `src.signals`, `src.risk`, `src.trading.loop`, `session_runner`, `stack`, `execution.orchestrator`, `execution.service` (test-asserted). Capture needs no model, signal, risk approval or TradingMode.
- Status reports `DATA_CAPTURE_READY` separately from `ORDER_AUTHORIZED=NO`, which is constant.

## H. Market Calendar

- `USEquityCalendar` (25.9E) is the only calendar. No opening time is hard-coded.
- Tests:
  - weekend: IDLE, zero snapshots;
  - holiday (Thanksgiving 2026-11-26): no session;
  - early close (2026-11-27): 210 expected minutes and no bar at or after 18:00 UTC.
- Preflight begins 20 minutes before the open. Two minutes before the open, one **discarded** warm-up snapshot is taken: IBKR answers a conid's first snapshot with empty fields (measured 2026-09-11), and without the warm-up every session would lose its first minute. A pre-market price never becomes a bar (test-asserted).

## I. Active Universe

- `config/capture_universe_v1.json`, built by `scripts/build_capture_universe.py` from repository data.
- The rule, stated in the file: the top 2 US-listed stocks per sector by daily-candle count, ties broken by ticker, plus the SPY benchmark. That is 31 instruments across 15 sectors.
- It excludes returns, signals, model output and labels. A test asserts that no member carries an outcome-derived field.
- **Versioned and immutable.** The builder refuses to overwrite a version file. `register_version` stores the file's sha256 and refuses the same version with different content.
- **Point-in-time membership.** Each session freezes its expected members and their contract status in `capture_session_members`. The freeze is not rewritten after finalization (test-asserted).
- Budget: 1 snapshot + 1 keepalive per minute for the whole universe = 2 requests/min against the 50/min budget.

## J. Contract Mapping

Every member is classified, never dropped:

| Status | Meaning | Retry |
|---|---|---|
| RESOLVED | one contract; persisted as the Phase 14 mapping | none (a persisted mapping costs no request) |
| AMBIGUOUS | listings the discriminators cannot separate | **never automatic**; a human narrows it with `run_ibkr.py --resolve --exchange …` |
| UNSUPPORTED | venue has no contract | once a day |
| FAILED | transient (gateway, rate limit) | exponential, 5 min doubling, capped at 6 h |

- All four were MOCK VERIFIED, including a real-shaped AMBIGUOUS case: two same-currency US listings of one symbol.
- Status and report show the counts. A half-mapped universe grades DEGRADED, because half a universe is not multi-instrument readiness.
- If nothing is mapped, preflight fails and active capture does not start (CAPTURE_BLOCKED).
- **Real mapping has not been run.** The capture store has no mappings yet; the first real preflight will resolve 31 contracts (31 requests, paced by the budget).

## K. Request Budget

- `RequestBudget` is a sliding 60-second window over every request the process sends.
- The snapshot and keepalive are reserved. Mapping retries get only what is left, so a retry storm cannot starve market data.
- The limit is read from `IBKR_MAX_REQUESTS_PER_MINUTE` and never raised. A universe whose capacity does not fit is a configuration error (exit 2), not a quiet overrun.

## L. Quote Acquisition

`MarketDataService.run_cycle(now, instruments=resolved)` is unchanged. It makes one batched `/iserver/marketdata/snapshot` request per minute for all resolved members, normalises through the Phase 15 mapper, and returns UNAVAILABLE rather than an absence for any instrument the venue did not answer.

## M. Multi-Instrument Synchronization

- All instruments of a tick share one request.
- `capture_ticks` records `requested_at`, `received_at` and the target minute per tick, and that bounds the whole cross-section.
- Ticks fire two seconds after each minute boundary, so the minute just ended is sealed for every instrument together.

## N. Freshness / Quality

- Freshness is judged by the canonical `OperationalQuote.freshness`.
- Stale quotes are counted per session (`stale_observations`) and create **no new minutes**: a quote carrying a 10-minute-old venue time cannot open a bar in the past (MOCK VERIFIED).
- Invalid and unavailable counts come from `market_data_cycles`.
- Delayed observations are not recorded per cycle by `MarketDataService`, and the report says so.

## O. Disconnect / Reconnect

| Case | Behaviour (MOCK VERIFIED) |
|---|---|
| Auth lost mid-day | AUTH_LOST, WAITING_FOR_AUTH, no snapshots; the outage becomes explicit `is_gap` markers; resumes by itself; `reconnects` counted |
| Snapshot endpoint failing for 20 min | ticks recorded as `failed`, no bars, no STEP_ERROR, process continues, session still GOOD |
| Resume after either | no duplicate minute (primary key and `INSERT OR IGNORE`) |

## P. Minute Bars

- `MinuteBarBuilder` is unchanged. A bar is written only once the clock has passed its end.
- At close or stop, the minute in progress is written **incomplete** and is therefore never archived.
- Gap rows are markers, `is_gap=1`, with no price.

## Q. Research Archival

Every tick archives the session window into `price_candle_cache` (interval `1m`, source `ibkr_operational_archive`, i.e. IBKR_LIVE_CAPTURE):

- completed, non-gap minutes only;
- the vendor's record of a minute is never overwritten;
- each archived minute gets a `capture_archive_log` row with session, instance, archive version `v1` and time.

**Provenance** (section 50; `capture_report.py --trace INSTRUMENT BAR_START`) runs contract (session member, conid) → snapshot ticks (requested/received) → operational bar (observations, complete) → archive record → research bar → first feature cutoff using it. Asserted end to end on a random minute, including that the research close equals the operational close.

## R. archive_operational_bars()

Audited before its first real use. It had **never been called or tested**. It rescanned the whole operational table and committed everything as one transaction. It now:

- takes `since`/`until` (bar-start window) and `instruments`;
- commits every 500 rows, so a crash loses at most one batch, never a session;
- inserts with `INSERT OR IGNORE`;
- returns the keys actually written, for provenance.

Called without bounds it behaves exactly as before, and all 25.9F tests pass unchanged.

## S. Archive Idempotency

A second archive pass over the same window writes 0 rows and leaves counts unchanged (test). A supervised restart mid-session plus re-archive leaves 0 duplicate `(instrument, minute)` pairs (test).

## T. Restart Recovery

| Case | Result (MOCK VERIFIED) |
|---|---|
| Clean restart mid-session | SESSION_RESUMED; ≥ 385/390 minutes cross-sectional; 2 processes recorded |
| Crash (no release), same supervisor | LEASE_TAKEOVER recorded; new instance acquires at once |
| Old instance wakes after a takeover | its next step raises `LeaseRefused`: it stops acting (section 55) |
| Dead supervisor, lease expired | a different supervisor recovers; previous owner recorded |
| Session left open by a crash | sealed at the next start once past close + 10 min (RECOVERED_FINALIZE) |
| Archive failing for 30 min (injected by DB trigger) | ARCHIVE_FAILED recorded; operational bars untouched; retried every tick; after recovery all 390 × 6 minutes archived; session GOOD |

## U. Intraday Features

- Every 5 minutes, the full 25.9F registry (v1, 19 features) runs at the last closed boundary for every resolved member, through `IntradayDatasetBuilder.observation`. Values go to `intraday_feature_values` keyed by (instrument, cutoff, feature, version).
- **Bounded loading, identical values.** Bars are loaded from the previous trading day's open. Asserted equal to the full-history builder, including `market.overnight_gap`, which needs the prior session's close.
- A live value is never overwritten.
- **Failure.** Feature writes failing (injected by trigger) produce FEATURE_FAILED with the cutoff. Bars are unaffected (149 × 6 archived).
- **Recompute.** `recompute_session` rebuilt exactly the failed cutoffs, with no re-collection. Recompute also reproduces live values bit for bit.
- Cost on the 30-instrument universe: 2.4 s average and 7.9 s maximum per feature tick, 0.38 s per plain tick, against a 60 s interval.

## V. Cross-Sectional Coverage

- Measured per session at the **same research minute**, never "somewhere during the day":
  - minutes with ≥ 1, 2, 3 and 5 instruments;
  - median and maximum simultaneous instruments;
  - minutes with ≥ 80 % of resolved members (the quality measure);
  - `cross_sectional.dispersion_1m` non-null coverage.
- Mock rehearsal (6 instruments, two sessions): ≥ 3 simultaneous in 390/390 minutes; median 6, maximum 6; dispersion coverage **96.2 %** (the earliest cutoffs lack history).
- **Real: N/A** (no real session).

## W. Session Coverage

Each finalized session carries:

- expected, resolved and captured instruments;
- expected and captured minutes; gap count and largest gap;
- archived bars; feature rows;
- stale, invalid and unavailable observations;
- archive and feature failures; errors; reconnects; auth waits;
- processes; host-suspend gaps;
- order-write attempts, which must be 0.

## X. Data-Maturity Tracking

- Counted in **sessions and calendar months**, never rows, using the unchanged 25.9F `calendar_ceiling`: 40 sessions / 2 months → MARGINAL; 120 / 6 → READY.
- Bands: INSUFFICIENT < MARGINAL < IMPROVING (MARGINAL and ≥ 90 sessions) < READY, and READY comes only from the ceiling.
- A full month of perfect sessions stays INSUFFICIENT (test).
- Milestones 20/40/60/90/120 are shown as planning targets, with the earliest date reachable if every session qualifies. The next milestone (20) cannot come before 2026-10-16.

## Y. Session Quality Report

These are the rules, in code and not tuned:

| Grade | Rule |
|---|---|
| FAILED | no resolved member or no bar, or < 10 % of minutes both cross-sectional and observed |
| GOOD | ≥ 90 % of minutes cross-sectional (≥ 80 % of resolved members present) and ≥ 90 % of members resolved |
| PARTIAL | < 90 % of the session observed (late start, early stop, host sleep), but ≥ 90 % of observed minutes cross-sectional |
| DEGRADED | everything else |

- Bad sessions are never deleted. They remain in `capture_sessions` with their summary.
- Mock results: auth restored at 15:00 → PARTIAL; 30-minute auth outage → GOOD (92 % kept); host sleep of 45 min → PARTIAL; half the universe unmapped → DEGRADED; never authenticated → FAILED, with 0 bars.

## Z. Storage / Retention

Measured on the real file format (WAL): one session, 30 instruments, features every 5 min.

| | per session | month (21) | 6 months (126) | year (252) |
|---|---:|---:|---:|---:|
| research 1m bars | 11,700 rows | 246 k | 1.47 M | 2.95 M |
| feature values | 44,460 rows | 934 k | 5.6 M | 11.2 M |
| DB file | **21.0 MB** | ≈ 0.44 GB | ≈ 2.6 GB | ≈ 5.3 GB |

- **Retention.** `market_data_state` holds one row per instrument. Operational bars and cycles are bounded at 30 days, but capture uses `prune_archived`, not the 25.7 `prune`: an old operational bar is deleted only if it is a gap or incomplete marker, or if its research copy provably exists (test). The research archive and features are never pruned.
- **Disk:** 13.9–15.0 GB was free on C:. `capture_status` raises ATTENTION below 2 GB (about three months of capture).

## AA. SQLite / Performance

- WAL mode, `busy_timeout` 30 s, commits per tick and per 500 archived rows. No transaction spans more than one minute of capture.
- **Concurrency** (REAL HOST file, mock data): a separate process read research, feature and status tables every 200 ms throughout a full 30-instrument session. Result: **898 reads, 0 lock errors**, p50 59 ms, p99 174 ms, max 368 ms.
- **Lock beyond busy_timeout** (injected by `BEGIN IMMEDIATE`): the step returns a 5 s retry, no crash, and readers are not blocked. After release the session still reached ≥ 385/390 cross-sectional minutes.
- Measured cost: a real session is roughly 390 ticks of < 1 s plus 78 feature ticks of 2–8 s. SQLite is adequate; there is no evidence for replacing it.

## AB. Host Sleep / Clock Gaps

- Every step knows when it expected to wake. Waking more than 180 s late records HOST_SUSPEND_GAP with its length and marks auth for re-checking. The session quality accounts for it.
- The missing minutes stay missing: no bar is archived inside the gap (test: a 45-minute gap leaves 0 research bars between 15:02 and 15:44).
- A sleeping laptop cannot capture. The report and status say so rather than backfilling.
- No backfill source was added (section 90).
- The store and schema keep live capture distinct by `source` if a legitimate backfill is ever added.

## AC. Observability

`python scripts/capture_status.py [--json]` is read-only: `mode=ro`, and it never creates the store (test). It reports:

- process, supervisor, lease, instance and pid;
- IBKR auth state, market state and next open;
- last quote, bar, archive and feature;
- live session coverage;
- mappings; maturity; last error;
- broker writes; store size and free disk;
- the **capture condition**: HEALTHY_CAPTURE, DEGRADED_CAPTURE, PARTIAL_CAPTURE, WAITING_FOR_AUTH, MARKET_CLOSED, STOPPED_BY_OPERATOR or SYSTEM_ERROR.

Exit codes: 0 OK, 1 ATTENTION, 2 NOT RUNNING, 3 MANUAL, 4 SAFETY.

`python scripts/capture_report.py [--json] [--trace I T]` gives the sessions table, the mapping table and maturity.

Logs: `data/capture/logs/capture.log` and `supervisor.log` rotate at 5 MB × 5 files (≤ 30 MB each). They carry no quote dumps and no credentials.

Dashboard (section 103): not integrated. The dashboard is built in CI from the release database and cannot see this host's store. `capture_status --json` is the integration point; the UI was not redesigned.

## AD. Failure Injection

See the failure matrix (§127). Faults were injected at the venue (auth, endpoint, contracts, stale timestamps), in the database (a lock from another connection, triggers that fail archive or feature writes) and in the clock. The code under test was never patched.

## AE. Production Write Boundary

- Allowlist (`CAPTURE_WRITABLE`):
  - `capture_*`, `intraday_feature_values`
  - `market_data_state`, `market_data_bars`, `market_data_cycles`
  - `price_candle_cache`
  - `broker_instrument_mapping`
  - minimal `instruments`, `securities`, `exchanges`, `companies`
  - `session_runner_leases`, `sqlite_sequence`
- Test: row counts of **every** table before and after a full session. The changed tables are a subset of the allowlist.
- Execution orders, fills, intents and events stay at 0 rows.
- No model, prediction, signal or promotion table exists in the capture store to be written.
- The production research database is never opened for writing: capture refuses any store named `marketlens.db`.

## AF. Real IBKR Read-Only Validation

**Not performed.** No Client Portal Gateway was running on this host (no listener on any IBKR port), and the market is closed. No IBKR endpoint was contacted in this phase. The only real-transport evidence is the unreachable-gateway behaviour in section F (REAL HOST, not VERIFIED REAL IBKR).

## AG. Real Capture Session

**NOT ATTEMPTED.** Distinct real captured sessions: **0**.

Mock rehearsals are labelled MOCK and are not presented as market observations:

- one full session, 6 instruments;
- two sessions, 30 instruments;
- one session, 30 instruments, for sizing.

Exact remaining step: install the task (section AP) and log in to the gateway before Monday 2026-09-21 13:10 UTC. Let one session run, then run `capture_status` and `capture_report`.

## AH. Security

Scan of every new or changed file:

- no credentials, tokens, cookies or `getpass`;
- no IBKR account id (the only `DU…` value is the mock's `DU0000000`, inside existing mock code);
- no absolute user path in tracked files;
- no `shell=True`;
- subprocess arguments are the script path plus file paths.

The supervisor owner id contains the host name. It lives only in the local, gitignored store and state file, and is not reproduced in this report. Logs contain store paths and states, no secrets.

## AI. D20 Isolation

- The capture package imports nothing from D20, anchor-v2 or the protected ledger. No D20 file is modified (git diff).
- `check_d20_readiness.py` is status-only and computes no statistic. On the 2026-09-18 snapshot: ledger `NOT_CONSUMED`, verdict NOT READY; the earliest test date has not arrived.
- `audit_research_integrity`: ledger chain intact, `d20_state NOT_CONSUMED`.

## AJ. Tests

- New: `tests/capture/` has 77 tests across 3 files plus a harness: lifecycle, calendar, auth, host sleep, restart and lease, mapping, universe versioning, safety, write boundary, quality and maturity rules, features and recompute, cold contract, failure injection, independence, store safety, supervisor policy, launch guards and status verdicts.
- Full suite (`PYTHONPATH=src python -m unittest discover -s tests -t . -b`): **4,293 tests, OK (1 skipped)**, 401 s. The 77 capture tests were re-run on the final code after the last edits: OK.
- No existing test was weakened. Test failures met during development were fixture mistakes, fixed in the fixtures, or real defects, fixed in code (section AN).

## AK. Research Integrity

`scripts/audit_research_integrity.py`: **RESEARCH INTEGRITY: CLEAN** (exit 0). Research writes outside scope 0; ledger chain intact; D20 NOT_CONSUMED.

## AL. Trading Readiness

`scripts/audit_trading_readiness.py`: exit 0.

- Runner ownership FREE.
- Order submission impossible from the command.
- Stop point unchanged: the operational market-data layer has never run against the production database. This phase is what will change that, via its own store.

## AM. Live Safety

`scripts/audit_live_safety.py`: exit 0. INTERACTIVE BROKERS = ONLY BROKER; MT5 = NOT IMPLEMENTED; REAL MONEY EXECUTION = BLOCKED BY DEFAULT.

## AN. Findings / Remediations

See §128.

## AO. Remaining Risks

1. **No real evidence yet.** Contract resolution for 31 symbols, real snapshot field coverage, real cold-contract timing and a real full session are all unproven. The mock mirrors IBKR's documented shapes, not its behaviour on the day.
2. **Human auth dependency.** Every session needs a person logged in to the gateway. Capture waits correctly, but a missed login is a missed session; the next section's status exit 1 is the signal.
3. **Host availability.** Sleep, reboot or log-off stops capture: the task is interactive, by design, with no stored password. Gaps are recorded honestly but cannot be recovered.
4. **Disk.** ≈ 5.3 GB/year against 13.9–15 GB free. Status warns below 2 GB.
5. **Lock beyond 30 s.** A write lock held past `busy_timeout` during a tick loses that tick's completed minute (the service writes after the builder emits). It is bounded, recorded as a missing minute and never synthesised. It is not fixed here, because doing so would change `MarketDataService`.
6. **AAPL may be AMBIGUOUS at IBKR**, as it is in the mock. If so it waits for a human narrowing and the universe runs at 30/31.
7. **Delayed-data flag** is not tracked per cycle.

## AP. Readiness for Phase 25.9H

Engineering is ready. Three steps remain, in order, and the first two need you:

1. **Approve installing the scheduled task.** It is a persistent host change, so it was not done without you:
   ```
   powershell -ExecutionPolicy Bypass -File deploy\windows\capture_task.ps1 install
   powershell -ExecutionPolicy Bypass -File deploy\windows\capture_task.ps1 start
   ```
2. **Log in** to the IBKR Client Portal Gateway (paper) in a browser before 13:10 UTC on each trading day.
3. After the first session: `python scripts/capture_status.py` and `python scripts/capture_report.py`. That turns REAL FULL-SESSION CAPTURE from NOT VERIFIED into PARTIAL or VERIFIED on evidence.

Data maturity then accumulates without the phase staying open: 20 sessions no earlier than 2026-10-16, and 120 sessions / 6 months no earlier than March 2027.

---

## 121. Deployment Matrix

| Component | Code | Deployed | Automatically started | Restartable | Real verified | Blocker |
|---|---|---|---|---|---|---|
| persistent process | `run_capture.py`, `capture_supervisor.py`, `capture_task.ps1` | no | no (task not installed) | yes (supervisor, MOCK + REAL HOST) | REAL HOST rehearsal | install needs approval |
| runner lease | 25.9E lease + instance takeover check | no | with process | yes | REAL HOST (duplicate supervisor refused, exit 3) | — |
| gateway detection | `IBKRGateway.connect` | no | with process | yes | REAL HOST (unreachable → WAITING_FOR_AUTH) | gateway not running |
| auth detection | WAITING_FOR_AUTH / AUTH_LOST | no | with process | yes | no | gateway not running |
| market calendar | `USEquityCalendar` | no | with process | n/a | REAL HOST (Saturday → IDLE) | — |
| market-data polling | `MarketDataService.run_cycle` | no | with process | yes | no | gateway, market |
| bar builder | `MinuteBarBuilder` | no | with process | yes | no | gateway, market |
| feature computation | `capture/features.py` | no | with process | yes (recompute) | no | gateway, market |
| archive | `archive_operational_bars` | no | with process | yes (idempotent) | no | gateway, market |
| EOD finalization | `quality.finalize_session` | no | with process | yes (recovered at start) | no | gateway, market |
| status command | `capture_status.py` | yes (on demand) | n/a | n/a | REAL HOST | — |

## 122. Universe Matrix

The IBKR mapping column is "not resolved" for all 31: no gateway was available. The capture store holds no mappings yet. No account identifier appears here.

| Instrument | Canonical ID | IBKR mapping | Capture enabled | First captured | Last captured | Coverage |
|---|---|---|---|---|---|---|
| SPY | benchmark-spy | not resolved | yes (benchmark) | — | — | — |
| UAL | us_and_intl-ual | not resolved | yes | — | — | — |
| LUV | us_and_intl-luv | not resolved | yes | — | — | — |
| BWA | us_and_intl-bwa | not resolved | yes | — | — | — |
| RIVN | us_and_intl-rivn | not resolved | yes | — | — | — |
| KMB | us_and_intl-kmb | not resolved | yes | — | — | — |
| CLX | us_and_intl-clx | not resolved | yes | — | — | — |
| COP | us_and_intl-cop | not resolved | yes | — | — | — |
| VLO | us_and_intl-vlo | not resolved | yes | — | — | — |
| USB | us_and_intl-usb | not resolved | yes | — | — | — |
| SPGI | us_and_intl-spgi | not resolved | yes | — | — | — |
| MCK | us_and_intl-mck | not resolved | yes | — | — | — |
| COR | us_and_intl-cor | not resolved | yes | — | — | — |
| ITW | us_and_intl-itw | not resolved | yes | — | — | — |
| RTX | us_and_intl-rtx | not resolved | yes | — | — | — |
| ECL | us_and_intl-ecl | not resolved | yes | — | — | — |
| FCX | us_and_intl-fcx | not resolved | yes | — | — | — |
| WBD | us_and_intl-wbd | not resolved | yes | — | — | — |
| CMCSA | us_and_intl-cmcsa | not resolved | yes | — | — | — |
| CCI | us_and_intl-cci | not resolved | yes | — | — | — |
| PSA | us_and_intl-psa | not resolved | yes | — | — | — |
| COST | us_and_intl-cost | not resolved | yes | — | — | — |
| AMZN | us_and_intl-amzn | not resolved | yes | — | — | — |
| ADI | us_and_intl-adi | not resolved | yes | — | — | — |
| KLAC | us_and_intl-klac | not resolved | yes | — | — | — |
| AAPL | us_and_intl-aapl | not resolved | yes | — | — | — |
| DBX | us_and_intl-dbx | not resolved | yes | — | — | — |
| VZ | us_and_intl-vz | not resolved | yes | — | — | — |
| TMUS | us_and_intl-tmus | not resolved | yes | — | — | — |
| AEP | us_and_intl-aep | not resolved | yes | — | — | — |
| EIX | us_and_intl-eix | not resolved | yes | — | — | — |

Mapping coverage: TOTAL 31 / RESOLVED 0 / FAILED 0 / AMBIGUOUS 0 / UNSUPPORTED 0. None attempted: no gateway.

## 123. Session Coverage Report

Real sessions: **none**. The rows below are **MOCK** rehearsals of the production path and are not market observations.

| Date | Expected instruments | Captured instruments | Expected minutes | Captured minutes | Coverage | Largest gap | Archived bars | Quality |
|---|---:|---:|---:|---:|---:|---|---:|---|
| 2026-09-17 (MOCK, 30 instr.) | 30 | 30 | 390 | 390 | 100 % | 0 min | 11,700 | GOOD |
| 2026-09-18 (MOCK, 30 instr.) | 30 | 30 | 390 | 390 | 100 % | 0 min | 11,700 | GOOD |
| 2026-09-18 (MOCK, 6 instr., host sleep 45 min) | 6 | 6 | 390 | 345 | 88.5 % | 45 min | 2,070 | PARTIAL |
| 2026-11-27 (MOCK, early close) | 6 | 6 | 210 | 210 | 100 % | 0 min | 1,260 | GOOD |

## 124. Cross-Sectional Coverage

Real: **N/A**, no real session. MOCK (6 instruments, per session):

| Metric | Value |
|---|---|
| minutes ≥ 1 instrument | 390 / 390 |
| minutes ≥ 2 instruments | 390 / 390 |
| minutes ≥ 3 instruments | 390 / 390 |
| minutes ≥ 5 instruments | 390 / 390 |
| median simultaneous instruments | 6 |
| maximum simultaneous instruments | 6 |
| `cross_sectional.dispersion_1m` coverage | 96.2 % (MOCK) |

## 125. Data Maturity

FIRST SESSION:
none (no real session)

LAST SESSION:
none

DISTINCT MARKET SESSIONS:
0

FULL SESSIONS:
0

PARTIAL SESSIONS:
0

CALENDAR SPAN:
0 days

RESEARCH MATURITY:
INSUFFICIENT

Not derived from bar count: the band comes only from sessions and calendar months through the 25.9F ceiling.

## 126. Real-IBKR Matrix

| Capability | Classification |
|---|---|
| gateway reachability | CODE ONLY (the unreachable case is REAL HOST: handled as WAITING_FOR_AUTH) |
| auth status | MOCK VERIFIED |
| heartbeat | MOCK VERIFIED |
| contract resolution | MOCK VERIFIED |
| quotes | MOCK VERIFIED |
| multi-instrument quotes | MOCK VERIFIED |
| freshness | MOCK VERIFIED |
| reconnect | MOCK VERIFIED |
| account read | NOT TESTED (capture does not read the account) |
| positions read | NOT TESTED (capture does not read positions) |
| open-order read | NOT TESTED (capture does not read orders) |
| order submission | NOT ATTEMPTED / STRUCTURALLY BLOCKED |
| cancel | NOT ATTEMPTED / STRUCTURALLY BLOCKED |
| modify | NOT ATTEMPTED / STRUCTURALLY BLOCKED |

## 127. Failure Matrix

| Failure | Expected behavior | Verified |
|---|---|---|
| gateway offline | WAITING_FOR_AUTH, retry 60 s, no crash, human instruction logged once | REAL HOST (real transport, no gateway) |
| auth expired | AUTH_LOST → WAITING_FOR_AUTH → auto-resume; outage as gap markers; reconnect counted | MOCK |
| market closed | IDLE, zero IBKR requests, heartbeat ≤ 5 min | MOCK + REAL HOST (Saturday) |
| holiday | no session opened, zero snapshots | MOCK |
| early close | 210 expected minutes, nothing after 18:00 UTC, GOOD | MOCK |
| contract mapping failure | FAILED with exponential retry; AMBIGUOUS not retried; UNSUPPORTED daily | MOCK |
| partial universe mapping | mapped half captured fully; session DEGRADED; status ATTENTION | MOCK |
| stale quote | counted stale; no new minute created | MOCK |
| quote endpoint failure | ticks `failed`, no bars, process continues | MOCK |
| DB lock | step returns 5 s retry; readers unblocked; catches up | MOCK (real SQLite file lock) |
| archive failure | ARCHIVE_FAILED; operational bars kept; retried each tick; fully recovered | MOCK (DB trigger) |
| feature failure | FEATURE_FAILED; bars intact; `recompute_session` repairs the failed cutoffs | MOCK (DB trigger) |
| process crash | supervisor restarts with backoff; 5 in 10 min → MANUAL_ATTENTION | MOCK (real processes, stand-in child) |
| second runner | second supervisor: file lock, exit 3; second owner: `LeaseRefused` | REAL HOST + MOCK |
| stale lease | expired lease recovered by new owner; LEASE_TAKEOVER recorded; superseded instance stops | MOCK |
| host clock gap | HOST_SUSPEND_GAP with length; no bar in the gap; PARTIAL | MOCK |
| forbidden broker write | `BrokerSubmissionForbidden` at both layers; `place_calls` 0; exit 4 → MANUAL_ATTENTION | MOCK |
| invalid / corrupt / production DB path | refused, exit 2, nothing created | test (real files) |

## 128. Findings Table

| ID | Severity | Finding | Evidence | Reproduced | Fixed | Tests | Remaining Risk |
|---|---|---|---|---|---|---|---|
| G-01 | High | No persistent runtime existed for capture; 25.9E code could never run continuously | re-audit, section C | yes | yes (supervisor, runner, task script) | 77 capture tests | task not yet installed |
| G-02 | High | The transport kept `place_order`/`cancel_order`/`reply` reachable behind the gateway guard | code read; `market_data_snapshot` reads `gateway.transport` directly | yes | yes (`ReadOnlyTransport`, `capture_only`) | write-path negative control | none known |
| G-03 | High | `archive_operational_bars` had never run: full-table rescan, one transaction | code audit | yes | yes (window, batches, provenance keys) | idempotency, restart, 25.9F suite | none known |
| G-04 | High | 25.7 `prune` deletes operational bars after 30 days whether archived or not, which would destroy data after an archive outage | code read | yes | yes for capture (`prune_archived`); 25.7 `prune` unchanged for its callers | prune test | other callers of `prune` |
| G-05 | Medium | IBKR's empty first snapshot per conid would cost every session its first minute | 2026-09-11 live measurement (25.7) | n/a | yes (discarded warm-up at T−2 min) | pre-open test | unverified on real IBKR |
| G-06 | Low | WAITING_FOR_AUTH announced on every retry (20× per hour) | test | yes | yes | auth test | — |
| G-07 | Low | Status called a deliberate STOP a SYSTEM_ERROR | REAL HOST rehearsal | yes | yes (STOPPED_BY_OPERATOR) | status test | — |
| G-08 | Medium | Disk headroom ~14 GB vs ≈ 5.3 GB/year | measurement | n/a | mitigated (status ATTENTION < 2 GB) | — | operator must free space over time |
| G-09 | Low | A lock held past `busy_timeout` loses that tick's minute | lock injection | yes | bounded, not fixed | lock test | recorded as missing, never synthesised |
| G-10 | Info | A test's lock holder released its lock at once (garbage-collected handle), letting a real child start in a temp dir | test hang | yes | yes (holder keeps the reference; guard tests stub `Supervisor.run`) | supervisor tests | — (the stray child idled and made no IBKR request) |

---

PHASE 25.9G STATUS:
INCOMPLETE

PERSISTENT RUNTIME:
PARTIAL

AUTOMATIC START:
NOT CONFIGURED

SUPERVISOR / RESTART:
PASS

RUNNER LEASE:
PASS

IBKR GATEWAY:
UNVERIFIED

IBKR AUTH:
UNVERIFIED

CAPTURE-ONLY SAFETY:
PASS

BROKER WRITES:
BLOCKED

ACTIVE UNIVERSE:
PARTIAL

MULTI-INSTRUMENT CAPTURE:
PASS

CONTRACT MAPPING:
PARTIAL

REQUEST BUDGET:
PASS

QUOTE CAPTURE:
PASS

PRICE FRESHNESS:
PASS

1-MINUTE BARS:
PASS

ARCHIVE_OPERATIONAL_BARS:
PASS

ARCHIVE IDEMPOTENCY:
PASS

ARCHIVE RESTART RECOVERY:
PASS

INTRADAY FEATURES:
PASS

CROSS-SECTIONAL DISPERSION COVERAGE:
N/A

DISTINCT REAL CAPTURED SESSIONS:
0

REAL FULL-SESSION CAPTURE:
NOT VERIFIED

DATA COLLECTION:
READY TO RUN

RESEARCH DATA MATURITY:
INSUFFICIENT

DEPLOYABLE MODEL:
NO

MODEL GOVERNANCE:
UNCHANGED

CONFIDENCE FLOOR:
UNCHANGED

REAL IBKR ORDER:
NOT ATTEMPTED

PAPER ORDER:
NOT ATTEMPTED

LIVE:
DISABLED

D20 HYPOTHESIS:
FROZEN

ANCHOR-V2:
UNCHANGED

D20 RESULT:
UNSEEN

D20 TEST:
NOT EXECUTED

D20 CONSUMPTION:
UNCONSUMED

FULL TEST SUITE:
PASS

RESEARCH INTEGRITY:
PASS

TRADING READINESS:
PASS

LIVE SAFETY:
PASS
