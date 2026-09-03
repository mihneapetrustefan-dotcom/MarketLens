# Test coverage audit — Phase 17

Spec §46, §47. No coverage tool is installed, so this measures test
**counts and distribution** against risk, and says so rather than
reporting a percentage it did not compute.

Full suite: **2,815 tests, all passing** (`PYTHONPATH=src`, 132s).

---

## 1. Distribution

| Area | Tests | Phase | Risk if wrong | Verdict |
|---|---|---|---|---|
| execution | **449** | 14–17 | capital | well covered |
| paper | 314 | 13 | simulated capital | well covered |
| backtest | 300 | 12 | research validity | well covered |
| portfolio | 273 | 11 | capital | well covered |
| scripts | 117 | 4–10 | data integrity | adequate |
| signals | 112 | 10 | trade decisions | adequate |
| news | 107 | 1–4 | data integrity | adequate |
| entities | 84 | 5 | identity | adequate |
| events | 81 | 6 | interpretation | adequate |
| impact | 79 | 8 | research validity | adequate |
| features | 61 | 7 | model inputs | **thin** |
| modeling | 54 | 9 | predictions | **thin** |
| research | 52 | 7 | reproducibility | **thin** |
| fusion | 47 | 6 | corroboration | **thin** |
| pointintime | 34 | 0 | **research validity** | **thin for its importance** |
| data_access | 28 | all | persistence | thin, mitigated |
| domain | 26 | all | correctness | thin, mitigated |
| providers | 23 | 2 | ingestion | adequate |

Coverage tracks phase recency almost exactly. Phases 11–17 carry 1,336
of 2,815 tests; Phases 0–10 share the rest across far more surface.

## 2. What the critical-test list requires (spec §47)

| Required | Covered | Where |
|---|---|---|
| data leakage | ✅ | `tests/pointintime` — `LookAheadViolation` raised, not filtered |
| order duplication | ✅ | restart, retry, duplicate-intent tests |
| fill duplication | ✅ | dedupe on broker execution id; overfill refused |
| reconciliation | ✅ | severity, blocking, human-only resolution |
| restart | ✅ | idempotency index restored before any submit |
| **risk bypass** | ✅ **new** | `test_phase17_intake.py` — 20 tests |
| live safety | ✅ | 57 tests + a 16-question executable audit |
| stale data | ✅ | four freshness budgets, delayed-quote detection |
| authorization | ✅ | `Caller` permissions, read-only default |
| IBKR errors | ✅ | 12 categories, 20 adversarial cases |

**All ten covered.** The risk-bypass row was the gap this phase found
and closed.

## 3. Genuine gaps

### G-1 — Point-in-time has 34 tests for the most important guarantee

`PointInTimeView` is the barrier that makes every research result
trustworthy. 34 tests is thin for something whose failure invalidates
the entire quant stack silently.

**Recommended:** adversarial leakage tests per consumer — a test that
tries to leak through *each* engine reading historical data, not only
through the view itself.

### G-2 — No end-to-end test crossing System A and System B

Every subsystem is tested. Nothing tests article → entity → event →
feature → model → signal → risk → order as one chain, because no
scheduled job runs that chain either. The join is untested because it
is unbuilt.

### G-3 — `src/dashboard.py` is 3,853 lines with 15 tests

It generates the only user-visible artefact. Most of it is HTML string
building where a fault is visible rather than silent, which is why this
is a gap and not a hole — but the collectors that read the database
deserve more than they have.

### G-4 — Feature and modeling suites are thin for leakage risk

Spec §17 lists nine model-leakage classes (normalization, imputation,
feature selection, hyperparameter, threshold, calibration…). The
modeling suite has 54 tests. It is not evident that each class is
tested. **Not confirmed as broken — confirmed as unverified.**

## 4. Test quality notes

Two observations worth recording, both from work done this session:

- The suite **fails closed on Windows** for reasons unrelated to the
  code: a leaked SQLite handle in three test seed helpers made 18 tests
  error in `tearDown` after their assertions had passed. Fixed. The
  lesson is that a red suite trains people to ignore red suites.
- One test in this phase (`test_there_is_no_override_parameter`)
  initially produced a false positive by substring-matching `force`
  inside `time_in_force`. Tightened to whole-token matching. An
  assertion that cries wolf gets weakened until it means nothing.

## 5. CI

`tests.yml` runs the full suite on every push and pull request to
`main`, on Python 3.11, plus a registry-collision check.

**Finding (LOW):** CI is on 3.11, local development on 3.12 (TD-13).

**Finding (LOW):** `PYTHONPATH=src` is required. Running with
`PYTHONPATH=.` produces 13 import errors that look exactly like
failures. Documented in CI and now in the README.
