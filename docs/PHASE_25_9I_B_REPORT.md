# Phase 25.9I-B — Completion Report

**Dynamic Universe & Opportunity Admission.** Written 2026-09-28.
Architecture: [`PHASE_25_9I_B_ARCHITECTURE.md`](PHASE_25_9I_B_ARCHITECTURE.md).

---

## 1. Phase status

**COMPLETE.**

The dynamic universe exists end to end — parent universe, as-of eligibility, decomposed
scoring, admission verdicts, capacity with a waitlist, sector limits, expiry, intraday
revision, anti-churn hooks, append-only persistence, a dry-run CLI and a shadow
comparison — and none of it controls anything. `adaptive_universe_enabled` is OFF,
capture still runs `config/capture_universe_v1.json` untouched, and no order, broker
write or subscription exists anywhere in the phase. Two real defects surfaced during
implementation (below); both were found by tests and fixed.

---

## 2. Git state

- **Branch:** `ibkr-paper-validation-fixes`
- **HEAD:** `4f0bc69`
- **Working tree:** clean
- **Commit:** `4f0bc69` "Phase 25.9I-B: a session universe that is allowed to stay small"

**Diff:** 15 files changed, **+6,218 / −4**.

| Category | Files |
|---|---|
| New artifact | `config/parent_universe_v1.json` (2,779 lines) |
| New source | `src/adaptive/{universe,eligibility,scoring,admission,selector,repository}.py`, `src/data_access/adaptive_universe_schema.py` |
| New scripts | `scripts/build_parent_universe.py`, `scripts/adaptive_universe.py` |
| New tests | `tests/adaptive/test_dynamic_universe.py`, `tests/adaptive/test_universe_persistence.py` |
| Modified | `src/domain/adaptive_models.py` (+69, additive), `tests/adaptive/test_freshness_and_legacy.py` (+13/−4) |
| Docs | `PHASE_25_9I_B_ARCHITECTURE.md`, this report |

The only two modifications are additive: three new contract types in the I-A domain
module, and the I-A deferral-guard test updated to the state this phase justified.

---

## 3. Baseline before the phase

| Item | Value |
|---|---|
| Baseline commit | `e0ab80a`, working tree clean |
| Tests | **4,453 run — OK, 0 failures, 0 errors, 1 skipped** |
| Runtime | 482 s |

---

## 4. What was implemented

| Area | Files | Behaviour | Rationale |
|---|---|---|---|
| **Parent universe** | `build_parent_universe.py`, `config/parent_universe_v1.json`, `adaptive/universe.py` | 130 members / 15 sectors, as-of bounded, sha256-verified on load | Reuses the capture universe's returns-free rule family; a hash mismatch refuses the file rather than silently changing history |
| **As-of history** | `adaptive/universe.py` | Read-only connection, `timestamp <= as_of` in every query, per-run cache | The guarantee is worth what its weakest query is worth |
| **Eligibility** | `adaptive/eligibility.py` | ELIGIBLE / INELIGIBLE / INSUFFICIENT_DATA with reasons and measurements | Unknown must never pass as good |
| **Scoring** | `adaptive/scoring.py` | 5 measured components, weights renormalised over what exists, 7 declared unavailable | A missing component is not a zero |
| **Admission** | `adaptive/admission.py` | PASS/WATCH/REJECT/INSUFFICIENT_DATA, thresholds, reasons, validity | A high score is not admission |
| **Capacity** | `adaptive/admission.py` | 0..max, waitlist as CAPACITY_REACHED, deterministic ranking | 31 is a ceiling |
| **Sector logic** | `adaptive/admission.py` | `max_per_sector` and `max_sector_fraction`, tighter wins | Limits may only subtract |
| **Expiry** | `adaptive/admission.py`, `selector.py` | TTL / session-close with reasons; expired cannot self-readmit | Otherwise expiry is decorative |
| **Intraday admission** | `adaptive/selector.py` | Free capacity only, never evicts, flags `requires_market_data_activation` | Eviction without a thesis is churn; subscriptions belong to I-C |
| **Anti-churn** | `adaptive/admission.py` | margin / residency / cooldown, all-zero defaults, fails closed | No thresholds without evidence |
| **Persistence** | `adaptive_universe_schema.py`, `adaptive/repository.py` | 5 append-only tables, content-derived ids, `INSERT OR IGNORE` | Re-runs must not duplicate; revisions must not erase |
| **CLI / shadow** | `scripts/adaptive_universe.py` | Dry-run default, `--shadow`, `--explain`, `--persist` | A diagnostic must work with the flag off |

---

## 5. What worked — with evidence

**Capacity is genuinely a ceiling, on real data.** Same session, three configurations:

| Run | pass | watch | selected | unused | waitlist |
|---|---|---|---|---|---|
| default thresholds | 127 | 3 | **31** | 0 | 96 |
| `--pass-threshold 0.75` | 5 | 125 | **5** | **26** | 0 |
| today's date, `--max-data-age-days 5` | 0 | 0 | **0** | **31** | 0 |

The second and third rows are the phase's central claim demonstrated on the real
research cache: five qualify, five are selected, twenty-six slots stay empty; nothing
stale is admitted and the session is empty rather than filled.

**Determinism.** Same inputs, `as_of` and config produce identical members, identical
`content_hash` and identical `snapshot_id`. `content_hash` excludes wall-clock time
precisely so this is testable.

**No lookahead.** A selection run against a database containing future candles is
byte-identical to one run against a database physically truncated at `as_of` (members
and content hash both). A 20:00 close cannot reach a 13:25 decision.

**Idempotent persistence.** Two `--persist` runs against a scratch copy of the real
database: 1 snapshot, 31 members, 130 decisions, second run reports `new=False`.

**Revisions preserve history.** Revision 1 names revision 0 as predecessor; revision 0's
members remain readable unchanged.

**Safety.** No adaptive module imports `src.execution`, `src.trading` or `src.portfolio`
(AST-checked). No order or subscription method is called anywhere (AST-checked). Every
write statement in the repository targets an `adaptive_*` table (source-checked).

---

## 6. What did not work

**Two real defects, both found by tests, both fixed.**

**(a) Expiry was decorative.** A revision dropped expired members from the held list and
then immediately re-admitted them from their own stale assessments, so an expired
opportunity kept its slot through a revision.
*Root cause:* carried-forward assessments retained `PASS` regardless of their validity.
*Fix:* a carried assessment that has expired is downgraded to `REJECT` with the reason
"validity expired … re-entry needs a fresh assessment"; re-entry requires a fresh
assessment at the new `as_of`. *Status:* fixed, covered by
`test_an_expired_member_does_not_keep_its_slot_through_a_revision`.

**(b) The I-A deferral guard failed, correctly.** `test_no_new_persistence_was_introduced_by_this_phase`
asserted that `src/data_access/` contained no adaptive schema module. I-B adds one
deliberately, so the test failed on the combined run.
*Root cause:* none — the test did its job.
*Fix:* rewritten to assert exactly one adaptive schema module exists and that **thesis**
persistence remains deferred to 25.9I-E, so the guard still bites for the next phase
rather than being deleted. *Status:* fixed.

**Four test failures from a fixture gap.** Synthetic markets had no benchmark series, so
`relative_strength` was unavailable, completeness fell to 0.333, and every candidate
became `INSUFFICIENT_DATA` under the 0.40 default.
*Root cause:* fixture, not code — the behaviour was correct.
*Fix:* fixtures include a benchmark by default. *But it exposed a real limitation:* see
§19.

**Assumption disproven.** I expected the default thresholds to leave capacity unused on
real data. They do not: 127 of 130 members pass at 0.55, so capacity binds and 96 names
are waitlisted. The attention score is dominated by liquidity and data completeness,
which are near-saturated for every member of a universe selected *for* liquidity and
coverage. The thresholds were **not** tuned to produce a prettier number — tuning them
against appearance is the same error as tuning against returns. They are documented as
uncalibrated.

---

## 7. What was deferred

| Deferred | Owner |
|---|---|
| Production capture switching | 25.9I-C (flag remains off) |
| Live market-data activation, streaming, throttling | 25.9I-C |
| Correlation / redundancy control | 25.9I-C or later — interface preserved, `correlation_context` declared unavailable; a redundancy rule needs a threshold and there is no evidence for one |
| Capital allocation, the qualified→funded boundary | 25.9I-D |
| Position thesis, thesis-based expiry, thesis persistence | 25.9I-E |
| Event relevance / materiality in scoring | 25.9I-F |
| Attribution of selection quality | 25.9I-G |
| Broker execution of any kind | 25.9I-H |

---

## 8. Parent universe result

| Field | Value |
|---|---|
| Artifact | `config/parent_universe_v1.json` |
| Version / as-of | `v1` / 2026-08-27 |
| Source | `price_candle_cache` (1d) + canonical instrument master |
| Rule | ≥150 daily candles at as_of, volume on ≥90%, ranked per sector by median dollar volume over 60 candles, ≤10 per sector |
| Members | **130** (target 75–150 met) |
| Sectors | **15** |
| Excluded | 186 — 91 coverage/metadata, 95 per-sector cap, all with reasons |
| sha256 | `53ad22ddbc29c753…` (verified on every load) |
| Benchmarks | `benchmark-spy`, carried separately |

**Missing metadata:** none among members — a member without sector metadata is refused
at construction. Instruments excluded for missing sector metadata are recorded in the
`excluded` list rather than silently dropped.

---

## 9. Session selection evidence

`as_of = 2026-08-27T13:25:00+00:00`, default configuration:

```
parent 130 | eligible 130 | ineligible 0 | insufficient_data 0
pass 127 | watch 3 | reject 0 | insufficient_data 0
selected 31 of 31 | unused 0 | waitlist 96
sectors: technology 6, media-entertainment 5, healthcare 4, semiconductors 4,
         automotive 3, energy 3, materials-mining 2, retail-e-commerce 2,
         consumer-goods 1, telecommunications 1
```

**Shadow vs the fixed capture universe:** fixed 30, dynamic 31, **overlap 6**, added 25,
dropped 24. Every drop reason is recorded; the sampled ones read "passed but lost the
capacity competition" — i.e. the fixed universe's members were not judged unfit, they
were out-ranked under a liquidity-weighted attention score.

**No performance claim is made or possible here.** Whether the dynamic set would have
earned more is a question about future returns, and using them would be the lookahead
this phase forbids.

---

## 10. SPY / benchmark decision

**Repository evidence:** `benchmark-spy` has 0 rows in `instruments`;
`capture/universe.py` skips `benchmark-*` by name when writing instrument rows;
`portfolio/exposure.py` states it is absent from the canonical tables;
`features/intraday.py` computes market context "on the benchmark, never on self"; no
trading-eligibility path treats it as tradeable.

**Design:** SPY is a **reference instrument**. It lives in `ParentUniverse.benchmarks`,
supplies `relative_strength`, and **never consumes capacity**. `ParentUniverse` refuses
construction if an instrument is both a candidate and a benchmark. Semantics unchanged
from the repository's existing behaviour.

---

## 11. No-lookahead / as-of evidence

- `MarketHistory` opens `mode=ro` and bounds every query with `timestamp <= as_of`.
- **Truncation test:** selection against a database containing future candles equals
  selection against one truncated at `as_of` — same members, same content hash.
- **Pre-open test:** a 20:00 bar is not visible to a 13:25 decision.
- **Benchmark test:** benchmark reads are bounded too (a plausible leak path, since the
  benchmark is read separately).
- **Replay:** deterministic — same members, `content_hash`, `snapshot_id` across runs.
- **No leakage bug was found** in the as-of layer itself; the defects in §6 were
  elsewhere.

---

## 12. Persistence / lineage

Five tables, created by `initialize_adaptive_universe_schema` with
`CREATE TABLE IF NOT EXISTS` only: `adaptive_parent_universe_versions`,
`adaptive_session_snapshots`, `adaptive_session_members`,
`adaptive_admission_decisions`, `adaptive_expiry_events`.

- **Migration:** additive only; a test proves an unrelated pre-existing table survives
  initialization with its rows, and that a fresh database receives exactly these five
  tables and nothing else.
- **Idempotency:** content-derived ids + `INSERT OR IGNORE`; proven on a real-data copy.
- **Revisions:** append-only, each naming `previous_snapshot_id`; `revisions(session)`
  returns 0 and 1 with members intact.
- **Lineage:** `DecisionLineage` from I-A carries `universe_decision_id` forward.
- **Production databases are untouched:** `data/marketlens.db` and the capture store
  contain **no** `adaptive_*` tables; the persistence demonstration ran against a scratch
  copy.

---

## 13. Feature flag state

| Flag | Value |
|---|---|
| `adaptive_universe_enabled` | **False** |
| all other adaptive flags | **False** |
| `layer_enabled` | **False** |
| `scripts/adaptive_status.py` | exit 0, `any flag enabled: False` |

The CLI reports the flag and runs regardless, by design.

---

## 14. Production behaviour

| Path | Changed? |
|---|---|
| Capture universe | **No** — `capture_universe_v1.json` byte-identical (sha still `66b2c3098f3c93a7…`) |
| Capture cadence | **No** — `src/capture` untouched by this commit |
| IBKR transport | **No** |
| Market-data subscriptions | **No** |
| Portfolio | **No** |
| Risk | **No** |
| Execution | **No** |

---

## 15. Broker safety

| Metric | Value |
|---|---|
| Broker writes attempted | **0** (`capture_instances.broker_write_attempts` sum = 0) |
| Broker writes succeeded | **0** (0 `BROKER_WRITE_REFUSED` events; execution tables empty in both stores) |
| Paper orders caused by this phase | **0** (`paper_orders` 0, `paper_fills` 0, `order_intents` 0) |
| Live orders | **0** |

---

## 16. Test results

| Suite | Result | Runtime |
|---|---|---|
| Targeted 25.9I-B (`test_dynamic_universe`) | **45 OK**, 0 fail, 0 skip | 1.3 s |
| Targeted 25.9I-B (`test_universe_persistence`) | **18 OK**, 0 fail, 0 skip | 4.0 s |
| Adaptive suite (all) | **172 OK**, 0 fail, 0 skip | 7.3 s |
| Capture / session | **126 OK**, 0 fail, 0 skip | 463 s |
| Research integrity | CLEAN, exit 0 — ledger intact, `d20_state NOT_CONSUMED` | — |
| Trading readiness | exit 0 — order submission impossible, stop point unchanged | — |
| Live safety | exit 0 — 16/16 questions pass | — |
| **Full suite** | **4,516 run — OK, 0 failures, 0 errors, 1 skipped** | 536 s |

Baseline 4,453 + 63 new = 4,516. ✓

---

## 17. D20 status

| Question | Answer |
|---|---|
| Protected test executed? | **NO** — `validate_d20_reversal.py` not run |
| Result inspected? | **NO** |
| Result inferred? | **NO** — no protected metric computed by any path |
| Result consumed? | **NO** — ledger reports `NOT_CONSUMED` |
| Frozen hypothesis modified? | **NO** |
| anchor-v2 modified? | **NO** |
| Protected window modified? | **NO** |

Enforced by test: no adaptive module mentions `anchor_v2`, `validate_d20`,
`protected_ledger` or `return_60d`, and none imports `src.research`. The parent universe
rule excludes returns, signals, model output and labels by construction.

---

## 18. Scope-creep review

| Check | Finding |
|---|---|
| Unexpected files | None — every file belongs to the phase |
| Unrelated refactors | None — 4 deleted lines total, all in the guard test rewritten for §6(b) |
| Schema changes | One new schema module, additive, justified in §12 |
| Execution changes | None |
| Config changes | One new artifact; `capture_universe_v1.json` untouched |
| Protected research changes | None |

---

## 19. Known limitations / technical debt

1. **Default thresholds admit almost everything (127/130 at 0.55).** The attention score
   is dominated by liquidity and data completeness, both near-saturated across a universe
   selected for liquidity and coverage. Capacity, not quality, is the binding constraint
   at defaults. Calibration needs either an event/model component or evidence from
   shadow sessions (25.9I-G).
2. **Score completeness is 0.417 with today's data** — 5 of 12 components — against a
   0.40 default. Losing any single component (e.g. a missing benchmark series) drops
   candidates below the bar into `INSUFFICIENT_DATA`. It is correct, and it is thin.
3. **The research cache ends 2026-08-27**, a month behind the calendar. Any realistic
   staleness bound marks the entire universe ineligible today, which is why
   `max_data_age_days` ships unset. A dynamic universe cannot drive a live session until
   the cache is refreshed daily.
4. **No model components.** `expected_return` and `model_confidence` are unavailable
   because no model is promoted. The selector ranks attention, not edge.
5. **Correlation/redundancy is deferred**, so two names from the same sub-industry can
   both be selected; only the sector cap limits that.
6. **Anti-churn is inert** (all-zero defaults) and untested against real intraday
   sequences, because none exist yet.
7. **Intraday admission never evicts.** Free capacity only; a full session cannot admit a
   better late candidate until 25.9I-E supplies a thesis to justify eviction.

---

## 20. Readiness for 25.9I-C

**BLOCKED EXTERNALLY.**

25.9I-C connects universe state to real market-data consumption, which needs the cost
facts. **IBKR support ticket #561227 remains unresolved** as of 2026-09-28: whether
`/iserver/marketdata/snapshot` calls are billed per request (~USD 2,500/month at this
project's polling rate) or covered by the ~USD 14.50/month subscriptions. The I-A cost
governor blocks any plan it cannot price, so I-C cannot be calibrated until that answer
arrives. A second, independent blocker: the paper account still receives delayed quotes.

**25.9I-D can proceed independently.** Capital allocation and risk budgeting consume
`SessionSelection` and the I-A allocation contracts; neither needs market-data cost facts
nor live subscriptions. It would run on the same dry-run selections this phase produces.

---

## 21. Final verdict

**PHASE 25.9I-B = COMPLETE.**

The session universe supports 0..31 with no forced occupancy, proven on real data
(5 selected, 26 unused at a 0.75 threshold; 0 selected, 31 unused under a staleness
bound). Selection is deterministic and as-of safe, verified against a physically
truncated database. Admission is separate from scoring, every non-pass verdict carries
reasons, and missing components are declared rather than defaulted. Persistence is
append-only and idempotent on real data. The flag is OFF, capture is untouched, and
broker writes, paper orders and live orders are all zero. Full suite 4,516 OK, 1 skipped.
