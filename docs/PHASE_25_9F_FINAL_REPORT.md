# Phase 25.9F — Intraday Feature Engineering & Research Dataset Expansion Report

**Written** 2026-09-18 · **Base commit** `7525457` · **Deployable model** NO · **D20** UNSEEN / NOT EXECUTED / UNCONSUMED

---

## A. Executive Summary

**The intraday feature layer now exists and computes real features on real data. The data it computes on is not yet enough to qualify a model, and this report says so with numbers rather than adjectives.**

Three findings define the phase:

| | |
|---|---|
| **The gap was real.** | Phase 25.9E said the session runner's feature stage only counted bars. Reproduced: `_stage_features` counted instruments with ≥1 bar and computed nothing. The canonical registry held **24 features, every one of them daily-session, event or news frequency — zero intraday.** |
| **Real intraday history already existed, in the research cache, not the operational layer.** | `market_data_bars` is **ABSENT** in production (the live IBKR poller has never run). But `price_candle_cache` holds **62,559 genuine one-minute candles, 222 instruments, 2026-07-06 → 2026-09-11**, fetched around canonical events. |
| **Row count is not evidence.** | Those 62,559 minutes form **4,138 contiguous runs with a median length of 2 minutes**, across **53 sessions in 3 calendar months**. Measured serial autocorrelation of the window features is **0.93–0.99**. The corpus can support exploration; it cannot support a qualified intraday model. |

**Built:** 19 versioned intraday features in the existing Phase 8 registry, a closed-bar point-in-time contract, a time-grid label family with explicit resolution states, a dataset builder with content fingerprints, a point-in-time news join, group-aware temporal splitting, a coverage/quality audit command, and a session-runner stage that reports real computation.

**Not done, deliberately:** no model was trained, qualified or promoted; no confidence floor was touched; D20, anchor-v2 and the protected ledger are byte-identical.

---

## B. Repository / Database Baseline

| | |
|---|---|
| branch / base commit | `ibkr-paper-validation-fixes` / `7525457`, clean |
| Python | 3.12.10 |
| production snapshot | 292,921,344 bytes, `Last-Modified: Thu, 17 Sep 2026 23:36:44 GMT`, sha256 recorded and re-verified after every read |
| full test count at start | 4,153 OK, 1 skipped |
| full test count at end | **4,212 OK, 1 skipped** |
| live-safety at start | 16/16 PASS |

Distinguishing the four states §3 asks for:

| Component | Implemented | Populated | Scheduled | Actually running |
|---|---|---|---|---|
| operational market data (`market_data_state/bars/cycles`) | yes (25.7) | **no — tables ABSENT** | no | **no** |
| research 1-minute candles (`price_candle_cache`, `1m`) | yes | **yes — 62,559 rows** | yes (`cache_price_candles`, event windows) | yes |
| daily research features (`research_features`) | yes | yes — 35,590 rows, 9 namespaces | yes (pipeline 2×/week) | yes |
| **intraday features** | **no → built this phase** | no | no | no |
| event-anchored intraday labels (`intraday_5m/15m/30m/60m`) | yes (Phase 9) | yes — ~1,049 each | yes | yes |
| **time-grid intraday labels** | **no → built this phase** | computed on demand | no | no |
| models | yes | 8 trained, **0 ACTIVE** | yes | yes |
| signals | yes | 440, max confidence **0.30** vs floor 0.40 | yes | yes |

---

## C. Existing Feature Pipeline (traced, not assumed)

```
price_candle_cache (1d)  --load_candles()-->  FeatureContext(candles=daily)
                                                    |
research_observations (event-anchored) --> compute_features.py --> research_features
                                                    |
                                          FeatureEngine + FeatureRegistry (24 defs)
```

**Where the intraday path stopped, reproduced:**

- `scripts/compute_features.py::load_candles` — docstring and code both: *"Daily candles only — every Phase 8 feature is daily-frequency."*
- `SessionRunner._stage_features` — called `repository.bars_for(...)`, counted instruments with a bar, returned `"N instrument(s) have completed intraday bars"`. **No feature was computed, and the stage reported success either way.**
- `build_default_registry()` — 24 features: market 6, volatility 3, liquidity 2, technical 1, event 6, news 2, sentiment 2, peer 2. All daily/event/news. **Zero intraday.**

---

## D. Operational vs Research Data Boundary

Preserved exactly as Phase 25.7 defined it, and now bridged explicitly rather than by accident:

| | Operational | Research |
|---|---|---|
| table | `market_data_state`, `market_data_bars` | `price_candle_cache` |
| purpose | price an order, right now | reproduce a study, later |
| retention | **30 days, `prune()` deletes** | durable, append-only |
| used by this phase | read-only, via the archival bridge | **yes — the research corpus** |

**§7 answer: `market_data_bars` is NOT sufficiently durable for research.** It carries a 30-day retention ceiling and its own module docstring calls it *"operational telemetry, not the research corpus."* A dataset built on it could not be rebuilt next month.

So `archive_operational_bars()` is the explicit transformation: complete, non-gap operational bars are copied into the durable research cache with their own `source` label (`ibkr_operational_archive`), and an existing vendor candle is never overwritten. **No third copy of the same minutes was created** (§6). On production it processed 0 rows — there are no operational bars to rescue yet, which is itself the finding.

---

## E. One-Minute Research Data

| | |
|---|---|
| bars | **62,559** |
| instruments | **222** |
| span | 2026-07-06 → 2026-09-11 |
| distinct sessions | **53** |
| calendar months | **3** (Jul 20 dates, Aug 25, Sep 8) |
| contiguous runs | **4,138** |
| median run | **2 minutes** |
| mean run | 15.1 minutes |
| longest run | 990 minutes |
| instruments with any ≥31-minute window | **108 of 222** |

The shape matters more than the total: the modal instrument-day is **131 minutes** — the ~2-hour window `cache_price_candles.py` fetches around an event — not a 390-minute session. This is an **event-window corpus**, not continuous session history.

---

## F. Bar Quality

Every research bar carries additive flags (`INVALID` is the only one meaning "do not compute"):

`COMPLETE` · `PARTIAL` · `GAP_BEFORE` · `LOW_OBSERVATION_COUNT` · `OUT_OF_ORDER_INPUT` · `DELAYED_SOURCE` · `SESSION_START` · `OUTSIDE_REGULAR_HOURS` · `INVALID`

- A bar whose own extremes contradict its close (`high < low`, or close outside the range) is `INVALID` and is excluded from every run — corruption is not merely thinness.
- Gaps are flagged, never interpolated (§20). **No synthetic bar is created anywhere in this phase.**
- `OUTSIDE_REGULAR_HOURS` uses the Phase 25.9E exchange calendar, applied only to instruments whose asset class it governs.

**Documented approximation:** the database records one exchange for every listed name — `US_AND_INTL`, *"US & International (unspecified)"* — so it genuinely cannot say whether a given stock trades on NYSE or in Frankfurt. US hours are applied to that whole bucket. This affects only the informational flag and the two session-position features; **no return is computed or dropped because of it.** Crypto and BVB are not governed by it at all.

---

## G. Feature Registry

**Reused, not duplicated (§14, §45).** The Phase 8 `FeatureRegistry` already carried name, version, namespace, formula, lookback, missing policy, timestamp semantics, source, dependencies and `lineage()`. Intraday definitions were registered into it: **43 total features, 19 intraday**.

Naming keeps the two frequencies apart permanently: `market.return_5m@v1` sits beside `market.return_5d@v1`. Feeding minute bars to the daily definitions would have computed a five-minute return under a name saying five days and retroactively reinterpreted every stored value — a test asserts the two id sets are disjoint.

---

## H. Feature Families Implemented

| Feature | Version | Family | Lookback | Input | Missingness | PIT-safe | Real-data coverage |
|---|---|---|---|---|---|---|---|
| `market.return_1m` | v1 | price action | 2 bars | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.return_5m` | v1 | price action | 6 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.return_15m` | v1 | price action | 16 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.return_30m` | v1 | price action | 31 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.return_60m` | v1 | price action | 61 | 1m closes | INSUFFICIENT_HISTORY | yes | 89.3% |
| `market.momentum_5m_vs_30m` | v1 | price action | 31 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `volatility.realized_15m` | v1 | volatility | 16 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `volatility.realized_30m` | v1 | volatility | 31 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `volatility.mean_abs_return_15m` | v1 | volatility | 16 | 1m closes | INSUFFICIENT_HISTORY | yes | 100.0% |
| `volatility.range_15m` | v1 | volatility | 15 | 1m high/low | INSUFFICIENT_HISTORY | yes | 100.0% |
| `liquidity.relative_volume_30m` | v1 | volume | 31 | 1m volume | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.vwap_distance` | v1 | price action | whole run | 1m close+volume | INSUFFICIENT_HISTORY | yes | 100.0% |
| `market.overnight_gap` | v1 | session | prior session | 1m closes | INSUFFICIENT_HISTORY | yes | 99.5% |
| `regime.minutes_since_open` | v1 | session | — | exchange calendar | NOT_APPLICABLE | yes | 79.6% |
| `regime.minutes_to_close` | v1 | session | — | exchange calendar | NOT_APPLICABLE | yes | 70.1% |
| `regime.run_minutes` | v1 | honesty column | — | run length | INSUFFICIENT_HISTORY | yes | 100.0% |
| `cross_sectional.market_return_5m` | v1 | market context | 6 | **benchmark** bars | INSUFFICIENT_HISTORY | yes | 98.8% |
| `cross_sectional.market_return_30m` | v1 | market context | 31 | **benchmark** bars | INSUFFICIENT_HISTORY | yes | 97.2% |
| `cross_sectional.dispersion_1m` | v1 | cross-sectional | — | peers at same minute | INSUFFICIENT_HISTORY | yes | **0.0%** |

Coverage measured on a real 20,588-row build (NVDA, AAPL, INTC, SPY).

**Deliberately NOT implemented (§13):** no RSI, MACD, or moving-average family — the registry already carries `technical.rsi_14` for daily and a hundred indicators on a corpus whose median run is two minutes would multiply accidental correlations without adding a fact. **No spread or quote-imbalance feature:** `price_candle_cache` stores OHLCV and carries no bid or ask, so microstructure features are absent rather than approximated from a high-low range and called a spread.

**`cross_sectional.dispersion_1m` is 0% covered on real data** — it needs ≥3 instruments with a closed bar at the *same* minute, and the event-window corpus rarely aligns. Reported rather than quietly dropped: it is the feature that most needs continuous multi-instrument capture.

---

## I. Point-in-Time Semantics

Three rules, each structural rather than advisory:

1. **Closed bars only.** `IntradayBar.timestamp` returns `bar_end`, not `bar_start`. The Phase 6 PIT lens filters on whatever timestamp it is handed, so exposing `bar_start` would make the minute *in progress* visible to a decision taken inside it. Tested: at 14:30:30 the 14:30–14:31 bar is invisible; it appears at exactly 14:31:00.
2. **Contiguous minutes only.** Windows are computed inside one unbroken run. A 30-minute return with a missing minute inside the lookback **refuses** rather than silently measuring 29.
3. **A run must reach the cutoff.** A run that ended an hour ago does not describe this minute. *(This was a real bug: the docstring claimed it, the code did not enforce it. Its own test caught it.)*

Labels are measured strictly after the cutoff; the provenance trace (§AB) shows `measured_at > cutoff` explicitly.

---

## J. Feature Versioning

Every intraday feature carries `INTRADAY_FEATURE_VERSION = "v1"`, a formula string, a source (`intraday_1m_bars`) and a namespace-qualified id (`market.return_5m@v1`). `observation_id` is derived from instrument + cutoff + **feature version + label version**, so a v2 value can never inherit a v1 identity — asserted by test.

---

## K. Determinism / Idempotency

| Property | Result |
|---|---|
| same bars, same version → identical values | PASS (fixture and **real data**) |
| recomputation → one identity, not two | PASS |
| a different minute → a different observation | PASS |
| dataset fingerprint stable on unchanged data | PASS |
| **determinism on the real 22,306-row rehearsal build** | **PASS** (identical fingerprint across two builds) |

---

## L. Incremental Computation

Bars arriving one at a time produce exactly the values a full recompute produces (tested over a 60-bar series, asserting the final row equals the batch result).

**Performance, measured and then fixed twice:**

| | |
|---|---|
| naive build (profiled) | 40.3 s for 1,679 NVDA rows |
| hot spot 1 | `previous_session_close` scanned all bars per observation — O(n²) |
| hot spot 2 | the PIT lens re-filtered and re-sorted the run **once per feature**, 19× per row (31 of 40 s, 5.4 M `getattr` calls) |
| after indexing + per-observation memoisation | **6.7 s for the same 1,679 rows (6× faster), byte-identical fingerprint** |

The filter still runs — once per observation instead of nineteen times. Tests assert the indexed path returns exactly what the scanning path returns, so the batch and operational paths cannot drift.

---

## M. Restart / Recovery

**Reloading from storage reproduces the uninterrupted row exactly** (fixture and real data).

**The limit was measured, not assumed.** Reloading a fixed 200-bar tail of real AAPL data reproduced 16 of 19 features and silently changed three:

```
regime.run_minutes     201 -> 200    (the run was truncated)
market.vwap_distance   differs       (VWAP of a shorter run)
market.overnight_gap   value -> None (previous session gone)
```

So `required_history()` now states the contract: **the entire contiguous run containing the cutoff, plus the previous session's last close.** `load_research_bars` satisfies it by construction (it reads the full stored series); the requirement matters only for a caller seeding bars by hand. `regime.run_minutes` travels in every row precisely so a truncated reload is *visible* rather than merely wrong — tested.

---

## N. Missing / Late Data

- **No interpolation anywhere.** A gap is a gap.
- Too little contiguous history → `None` with `INSUFFICIENT_HISTORY`, never a shorter window substituted, never zero.
- Missing ≠ zero: `news.count_24h` is genuinely `0.0` when no article arrived (the declared `ZERO_IS_SEMANTIC` case), while `news.minutes_since_last` is `None` because "how long since the last one" has no value when there has been none.
- **Late corrections:** a revised bar changes the dataset **fingerprint** (tested: one revised close produced a different fingerprint), so a stored result computed from the old value is detectably stale rather than silently kept. Content-addressed identity is the Phase 25.9D rule applied here.

---

## O. Session Semantics

The Phase 25.9E exchange calendar supplies open/close, early closes, holidays and DST. Overnight moves are never absorbed into a one-minute return: they appear as `market.overnight_gap`, a separate feature, computed from the previous session's last close. Session-position features return `None` outside regular hours and agree with each other about when a session exists (a bug found and fixed: `minutes_to_close` answered "424" pre-market while `minutes_since_open` correctly returned `None`).

---

## P. Intraday Labels

A **separate method family**, `grid_<H>m@v1`, for horizons 5/15/30/60 minutes.

| State | Meaning |
|---|---|
| `RESOLVED` | both endpoints exist, the path between them is contiguous |
| `UNRESOLVED` | the future minute has not happened yet |
| `MISSING_FUTURE_DATA` | it happened; we have no bar |
| `INVALID` | the anchor bar is unusable |

- `training_rows(horizon)` returns **RESOLVED only** — training on an unresolved label is the leak this exists to prevent.
- A forward window may not span a gap: both endpoints existing is not enough, because a 30-minute return across an overnight break is an overnight gap wearing an intraday name.

**Why a new family (§24):** `research_labels` already holds `intraday_5m…60m`, but those are **event-anchored**, produced by `src/impact/anchoring.py` — the module the frozen D20 hypothesis depends on. Writing time-grid labels into those rows would mix two method families and touch the D20 machinery. Nothing in this phase imports `anchoring.py` or writes `research_labels` — asserted by source-parsing test.

---

## Q. Dataset Builder

`IntradayDatasetBuilder` is the one canonical path (§25): model scripts do not assemble X and y themselves. It produces X, y, cutoffs, instrument ids, feature/label versions, data cutoff, run length and quality flags, plus `matrix()`, `cross_section()`, `training_rows()`, `label_states()`, `feature_missingness()` and `temporal_split()`.

---

## R. Dataset Identity

`fingerprint()` hashes feature versions, label versions, feature ids, horizons, benchmark, data cutoff **and every row's values and label states** — the Phase 25.9D content-identity rule, reused rather than reinvented. Verified: stable across rebuilds of unchanged data; changes when a single close is revised.

---

## S. Cross-Sectional Support

`cross_section(cutoff)` groups every instrument observed at exactly one minute. Membership is decided by **what had closed**, not by who is in today's universe, so universe look-ahead is structurally impossible (§30, §37, §55): an instrument with no closed bar at that minute is simply absent, not present with nulls — tested.

`temporal_split()` cuts **between minutes, never inside one**, with an optional horizon embargo. This was found by the rehearsal: a 70% row-index split put the boundary minute on *both* sides, and the scaler's `fitted_through` equalled the first validation cutoff. On real data the group-aware split gives **0 overlapping minutes** and a scaler fitted strictly before validation.

---

## T. News / Event Alignment

Joined on **availability**, never on the event date: `available_at = max(published_at, collected_at)`, filtered `<= T` at both load and feature time. A record whose availability cannot be established is **dropped**, because an undated article cannot be proven to have been knowable.

Reuses the canonical Phase 8 chain (`article_entities → companies → securities → instruments`) rather than a second definition of what an instrument's news is.

Features: `news.count_24h`, `news.minutes_since_last`, `news.decayed_intensity_24h` (half-life 6 h, **stated, not fitted against any label**), `news.distinct_sources_24h`, `news.mean_sentiment_24h`.

**Real-data verification:** NVDA has 452 articles with establishable availability; at a 2026-09-01T13:00 cutoff, 9 were visible in the 24-hour window and **86 later articles were correctly excluded as future**.

Join coverage varies sharply by instrument and period (NVDA/GOOGL 200/200 sampled rows with news in the prior 24 h; AAPL/INTC 0/200 in their sampled windows; SPY 0 — the benchmark has no company). That variation is reported, not averaged away.

---

## U. Normalization

`fit_scaler()` takes only the rows it is given and records `fitted_rows` and `fitted_through`, so a scaler fitted past a validation boundary is **auditable** rather than merely forbidden. Cross-sectional z-scores use only observations available at that one minute. `winsorized()` returns `(clipped, was_clipped)` — the raw value is never destroyed and the count of affected rows is recoverable (§38). A constant history yields no threshold, so the value passes through unchanged rather than being clipped against a fabricated bound.

---

## V. Feature Diagnostics — EXPLORATORY ONLY

Computed on 20,588 real rows (NVDA, AAPL, INTC, SPY), non-protected data. **No edge is claimed; nothing here qualifies a model.**

| feature | n | missing | mean | stdev | lag-1 autocorr |
|---|---:|---:|---:|---:|---:|
| `market.return_1m` | 20,588 | 0.0% | −0.0000004 | 0.000496 | **−0.024** |
| `market.return_5m` | 20,588 | 0.0% | −0.000003 | 0.001098 | 0.786 |
| `market.return_15m` | 20,588 | 0.0% | −0.000018 | 0.001886 | 0.931 |
| `market.return_30m` | 20,588 | 0.0% | −0.000040 | 0.002794 | 0.965 |
| `market.return_60m` | 18,386 | 10.7% | −0.000093 | 0.004037 | 0.976 |
| `volatility.realized_30m` | 20,588 | 0.0% | 0.000313 | 0.000369 | 0.990 |
| `market.vwap_distance` | 20,588 | 0.0% | −0.000072 | 0.002925 | 0.981 |
| `liquidity.relative_volume_30m` | 20,588 | 0.0% | 1.340 | 7.841 | 0.001 |
| `market.overnight_gap` | 20,487 | 0.5% | 0.002472 | 0.016761 | 0.995 |
| `cross_sectional.dispersion_1m` | 0 | 100.0% | — | — | — |

**The single most important number in this report is that autocorrelation column.** One-minute returns are ~0 autocorrelated, as expected. Every *window* feature is 0.93–0.99 — because consecutive rows share almost all of their window. Twenty thousand rows are therefore nothing like twenty thousand independent observations, and any confidence interval that treats them as such is wrong by a large factor.

---

## W. Historical Coverage

| Instrument | First bar | Last bar | Sessions | 1m bars | Longest run | Runs | Usable @30m | Feature-ready |
|---|---|---|---:|---:|---:|---:|---:|---|
| benchmark-spy | 2026-07-06T08:00 | 2026-09-11T20:36 | 47 | 25,706 | 705 | 1,280 | 15,518 | yes |
| crypto-btc | 2026-07-22T05:55 | 2026-09-11T16:38 | 18 | 3,232 | 990 | 18 | 2,152 | yes |
| us_and_intl-nvda | 2026-08-11T11:55 | 2026-09-04T16:56 | 8 | 2,059 | 585 | 25 | 1,462 | yes |
| us_and_intl-aapl | 2026-07-10T08:00 | 2026-09-11T17:09 | 12 | 1,658 | 416 | 27 | 889 | yes |
| us_and_intl-googl | 2026-07-22T08:00 | 2026-08-21T17:41 | 4 | 1,141 | 470 | 75 | 608 | yes |
| us_and_intl-lcid | 2026-07-13T12:58 | 2026-08-20T12:50 | 5 | 876 | 423 | 79 | 508 | yes |
| us_and_intl-uber | 2026-07-16T08:00 | 2026-08-17T23:00 | 6 | 861 | 579 | 68 | 559 | yes |
| us_and_intl-intc | 2026-07-14T08:00 | 2026-08-19T12:36 | 6 | 849 | 426 | 15 | 517 | yes |
| us_and_intl-amat | 2026-08-13T08:00 | 2026-08-18T18:39 | 2 | 819 | 482 | 70 | 493 | yes |
| us_and_intl-amzn | 2026-07-20T08:00 | 2026-09-08T17:02 | 8 | 668 | 202 | 48 | 284 | yes |

Corpus-wide: 222 instruments, 108 with any ≥31-minute window, **median 2 minutes per run**. The long tail is severe — the median instrument has 75 bars across 2 days.

---

## X. Effective Sample Size

| Horizon | Labels available (resolved) | Overlapping points | Non-overlapping blocks | Independent runs | Instruments | Evidence verdict | **Capped verdict** |
|---|---:|---:|---:|---:|---:|---|---|
| 5m | 21,809 | 47,535 | 9,933 | 649 | 121 | READY | **MARGINAL** |
| 15m | 20,941 | 39,246 | 2,764 | 307 | 108 | READY | **MARGINAL** |
| 30m | 19,837 | 31,576 | 1,184 | 218 | 89 | READY | **MARGINAL** |
| 60m | 17,920 | 20,528 | 459 | 162 | 65 | MARGINAL | **MARGINAL** |

**Calendar coverage is the binding constraint and it caps every verdict.** 53 sessions across 3 calendar months is one market regime. A defensible walk-forward study wants several folds over genuinely different periods — roughly 120 sessions across 6 months on the stated rule. **More one-minute rows inside the same eight weeks cannot buy a second regime**, so horizon evidence may only *lower* a verdict, never raise it above the calendar ceiling. That rule is implemented, not just described.

---

## Y. Storage / Performance

| | |
|---|---|
| current 1m corpus | 62,559 rows ≈ **8 MB** |
| projected at 25 instruments × 390 min × 252 sessions | 2,457,000 rows/yr ≈ **0.31 GB/year** of bars alone |
| **operational feature stage** (one boundary, 10 instruments) | **48 ms** against a 300 s cadence — comfortably feasible |
| index warm-up, once per process | 4.25 s for 10 instruments |
| research batch build | 22,306 rows over 7 instruments in **131.6 s** |

No index was added: measured access is by `(instrument_id, interval, timestamp)`, which `price_candle_cache`'s existing key already serves (§57). Storage growth does not justify leaving SQLite.

---

## Z. SQLite Contention

Re-ran the Phase 25.9E concurrency probe with a **feature-reader** workload added (market-data writes, loop writes, reconciliation writes, dashboard reads, feature reads), 20 s per worker:

```
ops         feature_reader 202 | loop 38 | dashboard 254 | market_data 20 | reconciliation 20
max op (s)  feature_reader 0.373 | loop 0.832 | dashboard 0.405 | market_data 3.751 | reconciliation 2.995
lock errors 0
```

Adding the intraday read workload did **not** degrade contention (market-data worst case 3.75 s here vs 4.05 s in 25.9E, within noise). SQLite stays adequate; no evidence justifies replacing it.

---

## AA. Session Runner Integration

`_stage_features` now computes the registered intraday set at the last **closed** bar boundary for every active instrument and reports the real outcome:

```
COMPUTED   the full set resolved
PARTIAL    some features were None (short run, missing input)
NO_DATA    no closed bar reaches this boundary
BLOCKED    computation itself failed  -> stage FAILS, trading blocked on the tick
```

`feature_state` carries the result and its boundary. `features_are_fresh()` implements §52: a fresh *price* does not make a 45-minute-old *feature* current, and the signal stage now refuses to derive a signal when the feature state does not describe the current boundary. The stage can no longer report success while having computed nothing.

---

## AB. Real-Data Working-Copy Rehearsal

Working copy of the production snapshot, in the scratchpad only; production sha256 re-verified unchanged afterwards. **All data below is REAL** (vendor 1-minute candles and real articles). No synthetic bar was created.

| | |
|---|---|
| archival bridge on production | `considered: 0` — **there are no operational bars to rescue** |
| dataset built | **22,306 rows**, 7 instruments, in 131.6 s |
| fingerprint | `ids-c6ee83782dc81263941c6e7e09e7696d` |
| data cutoff | 2026-09-11T20:36:00+00:00 |
| deterministic across two builds | **True** |
| restart reproduces a sampled row | **True** |
| resolved labels | 5m: 21,809 · 15m: 20,941 · 30m: 19,837 · 60m: 17,920 |
| unresolved / missing-future at 30m | 2,469 `missing_future_data` |
| **tables changed in the working copy** | **none** |

**Provenance trace (§60), zero ambiguous links:**

```
1. raw candle   ('us_and_intl-nvda','1m','2026-08-11T13:15:00+00:00', close 221.3,
                 source 'polygon', fetched_at '2026-08-28T15:47:45Z')
2. IntradayBar  bar_start 13:15:00 -> bar_end 13:16:00, quality ['outside_regular_hours']
3. observation  iobs-06ee469e6d1c03cacfcf, cutoff 13:16:00, run_minutes 81,
                feature_version v1, label_version v1
4. feature      market.return_5m = -0.00044851
5. label        grid_15m RESOLVED 0.00119747, measured_at 13:31:00
                (measured strictly after the cutoff: True)
```

---

## AC. D20 Isolation

| | |
|---|---|
| `src/impact`, protected ledger, D20 scripts, `research/` | `git diff` vs `7525457`: **empty** |
| `research/protected_tests/ledger.jsonl` md5 | `7113fa4e54c1ce44fd418ae9da682570` (unchanged) |
| `validate_d20_reversal.py` executed | **no** |
| anchor-v2 | **unchanged** |
| intraday modules importing `impact.anchoring` / `anchor_v2` / `validate_d20` | **none** (source-parsed test) |
| intraday modules writing `research_labels` / `research_observations` | **none** (source-parsed test) |

**Overlap policy (§62):** the intraday corpus (2026-07-06 → 2026-09-11) overlaps the D20 protected window (2026-08-15 → 2026-08-27) in calendar time. That is unavoidable — they are the same weeks of market history — but they are different measurements: D20 is a daily abnormal return from an event anchor via anchor-v2; this is a time-grid intraday return computed here. **No D20 label, anchor or protected result was read, and no intraday tuning used the protected window as a pool.** Should intraday research later need its own protected window, it must declare a fresh one rather than inherit D20's.

---

## AD. Safety

| | |
|---|---|
| BROKER SUBMISSION | **BLOCKED** (unchanged; 13/13 trading-readiness negative controls pass) |
| LIVE | **DISABLED** |
| RISK GATE | **UNCHANGED** |
| CONFIDENCE FLOOR | **0.40, UNCHANGED** |
| MODEL PROMOTION | **human/governed, unchanged** — 0 ACTIVE of 8 trained |
| research integrity | 10/10 negative controls |
| live safety | 16/16 PASS |

No production signal was emitted; no model was trained or promoted; no order was submitted.

---

## AE. Tests

`tests/features/test_intraday_features_25_9f.py` — **59 tests**, covering closed-bar safety, contiguity, index-vs-scan equivalence, feature values, determinism, idempotency, incremental-vs-batch, restart (and its measured limit), versioning and name-collision, labels and their four states, dataset fingerprints, cross-sectional grouping, multi-instrument isolation, per-instrument-vs-shared-grid equivalence, temporal splitting with embargo, news PIT, normalization, outliers, and D20/operational isolation.

**Negative controls (§68), each asserting a guard actually fires:** unfinished bar · future bar · gap inside the lookback · stale run · invalid (corrupt OHLC) bar · missing future data · forward window spanning a gap · revised bar changing the fingerprint · truncated reload · future article · wrong feature version · instrument absent from a cross section.

**Two real bugs were found by these tests and fixed:** the stale-run window (documented but unenforced) and the disagreeing session-position features. A third — the row-index split putting one minute on both sides — was found by the real-data rehearsal.

**Full suite: 4,212 tests OK, 1 skipped** (4,153 at the start of this phase — the difference is exactly the 59 new tests). No existing test was weakened.

---

## AF. Remaining Limitations

1. **The operational 1-minute layer has never run.** All intraday research data is vendor candles in event windows. Continuous session capture requires the session runner to actually run (a Phase 25.9E deployment finding, unchanged).
2. **The corpus is event-window shaped**, median run 2 minutes; only 108 of 222 instruments support even a 30-minute window.
3. **Cross-sectional dispersion is 0% covered** — the corpus rarely has ≥3 instruments on the same minute.
4. **No microstructure features** — the research cache has no bid/ask.
5. **Corporate actions:** the 1m rows carry `adjusted_close`, and this layer reads `COALESCE(adjusted_close, close)`, so a split is not read as a 50% move within a series. Vendor adjustment semantics across a split boundary remain the vendor's, and the Phase 25.9C `price_cache_vintage_checks` mechanism (unchanged) is what detects a mixed vintage.
6. **International names inside `US_AND_INTL`** are judged against US hours (§F) — informational flags only.
7. **No intraday feature is persisted to a table yet**; features are computed on demand. That is deliberate — persisting values before the definitions have been used in anger would create a store to migrate.

---

## AG. Model-Quality Readiness

**Data availability decision (§72): B — PARTIAL HISTORY; DATA COLLECTION MUST CONTINUE.**

What becomes possible now:
- Intraday feature computation, dataset assembly and **exploratory** diagnostics are available on real data today.
- Daily/event-anchored research remains the statistically defensible formulation, as it was.

What does not:
- **No intraday model can be qualified from 53 sessions in one regime.** The calendar ceiling is MARGINAL at every horizon, and the 0.93–0.99 autocorrelation means the effective sample is far below the row count.

What would change it: continuous session capture. At 25 instruments × 390 minutes, **six months of sessions (~120 trading days) would give genuinely contiguous runs, a workable fold count and regime variety** — roughly 0.15 GB of bars, comfortably inside SQLite.

---

## AH. Recommended Next Step

**Start capturing continuous intraday data.** The feature layer, labels, dataset builder and quality audit are in place and tested; the binding constraint is now purely the calendar. That requires the Phase 25.9E deployment gap to close — a supervised session-runner process with the gateway authenticated — after which `archive_operational_bars()` turns each session's captured minutes into durable research data.

Model quality remains the blocker for Phase 25.95, and this phase does not pretend otherwise.

---

## Required Model-Research Readiness Matrix

| Horizon | Labels available | Effective sample | Feature coverage | Research readiness |
|---|---:|---:|---:|---|
| 5m | 21,809 resolved | 9,933 blocks / 649 runs | 100% on core returns | **MARGINAL** |
| 15m | 20,941 resolved | 2,764 blocks / 307 runs | 100% on core returns | **MARGINAL** |
| 30m | 19,837 resolved | 1,184 blocks / 218 runs | 100% on core returns | **MARGINAL** |
| 60m | 17,920 resolved | 459 blocks / 162 runs | 89.3% (`return_60m`) | **MARGINAL** |

Every verdict is capped by calendar coverage (53 sessions, 3 months). None is READY.

---

## Final Status

```
PHASE 25.9F STATUS:                COMPLETE

INTRADAY FEATURE COMPUTATION:      OPERATIONAL
FEATURE REGISTRY:                  PASS
POINT-IN-TIME:                     PASS
CLOSED-BAR SAFETY:                 PASS
FEATURE DETERMINISM:               PASS
FEATURE IDEMPOTENCY:               PASS
INCREMENTAL COMPUTATION:           PASS
RESTART EQUIVALENCE:               PASS
MISSING-DATA HANDLING:             PASS

INTRADAY LABEL INFRASTRUCTURE:     READY
RESEARCH DATASET BUILDER:          READY
NEWS / EVENT PIT JOIN:             PASS

REAL INTRADAY HISTORY:             MARGINAL
MODEL RESEARCH READINESS:          PARTIAL

DEPLOYABLE MODEL:                  NO
MODEL GOVERNANCE:                  UNCHANGED
CONFIDENCE FLOOR:                  UNCHANGED

D20 HYPOTHESIS:                    FROZEN
D20 RESULT:                        UNSEEN
D20 TEST:                          NOT EXECUTED
D20 CONSUMPTION:                   UNCONSUMED

REAL IBKR ORDER:                   NOT ATTEMPTED
BROKER SUBMISSION:                 BLOCKED
LIVE:                              DISABLED

FULL TEST SUITE:                   PASS (4,212 OK, 1 skipped)
RESEARCH INTEGRITY:                PASS
TRADING READINESS:                 PASS
LIVE SAFETY:                       PASS

PHASE 25.9G:                       READY

NEXT REQUIRED STEP:                Begin continuous intraday capture. The
                                   feature, label and dataset layers are
                                   built and tested; the only remaining
                                   constraint on intraday model research is
                                   calendar coverage (53 sessions across 3
                                   months today, ~120 sessions across 6
                                   months needed). That requires the Phase
                                   25.9E deployment gap to close: a
                                   supervised session-runner process with an
                                   authenticated gateway, after which
                                   archive_operational_bars() makes each
                                   captured session durable research data.
```
