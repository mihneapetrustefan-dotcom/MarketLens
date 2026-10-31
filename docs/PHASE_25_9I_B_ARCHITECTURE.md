# Phase 25.9I-B — Dynamic Universe & Opportunity Admission

**Status: IMPLEMENTED, INACTIVE IN PRODUCTION. `adaptive_universe_enabled` is OFF.**
Written 2026-09-28.

The rule this phase replaces:

> "There are 31 configured symbols, therefore all 31 are the session universe."

with:

> **31 is a maximum capacity, not a required count.** A session that watches 5 of 31
> slots is a correct session. Unused capacity is a normal, often desirable state.

Nothing here controls capture, Portfolio, Risk, orders or subscriptions. The selector
produces a record of a decision; a record is not an instruction.

---

## A. The four layers, kept apart on purpose

| Layer | What it means | Size today |
|---|---|---|
| **Parent universe** | Instruments the system is *allowed* to evaluate | 130 |
| **Session universe** | Instruments selected as worth watching *today* | 0–31 |
| **Qualified opportunity** | A candidate that passed admission | ≤ session universe |
| **Funded position** | What Portfolio and Risk actually back with capital | **not this phase** |

Collapsing these is how a system ends up buying something because it found it
interesting. They are four types, and the boundary between the third and fourth belongs
to 25.9I-D.

---

## B. Parent universe

**Artifact:** `config/parent_universe_v1.json` — built by `scripts/build_parent_universe.py`,
read-only against the research cache.

**Rule (as-of bounded, outcome-free):** US-listed stocks (`asset_class='stock'`,
exchange `US_AND_INTL`) with ≥150 daily candles at or before `as_of` and volume on ≥90%
of them, ranked within sector by **median daily dollar volume** over the trailing 60
candles, at most 10 per sector.

**Result:** **130 members across 15 sectors**, 186 excluded with reasons (91 on
coverage/metadata, 95 on the per-sector cap). Members sha256
`53ad22ddbc29c753…`, as-of `2026-08-27`.

**What it excludes by construction:** returns, signals, model output, labels — the same
exclusions `build_capture_universe.py` states, for the same reason. A universe chosen by
what performed well makes every later model look good.

`ParentUniverse.load` recomputes the members' hash and **refuses a file that no longer
matches it**, so an edited artifact cannot silently change history.

`config/capture_universe_v1.json` is **untouched** and remains the operational capture
universe. A test asserts its sha256 still begins `66b2c3098f3c93a7`.

---

## C. SPY and benchmarks

**Evidence from the repository, not assumption:**
- `benchmark-spy` has **0 rows** in the instrument master (`portfolio/exposure.py` says so explicitly).
- `capture/universe.py` skips `benchmark-*` by name when creating instrument rows.
- `features/intraday.py` computes market context "on the benchmark, never on self".
- No trading-eligibility path references it as tradeable.

**Decision:** SPY is a **reference instrument**. It is carried in a separate `benchmarks`
list, feeds the `relative_strength` score component, and **never consumes a session
slot**. Two tests enforce it. If a strategy ever needs SPY as a trade candidate, that is
a deliberate change with its own evidence — not a silent side effect of this phase.

---

## D. Eligibility — three answers, never two

`ELIGIBLE` / `INELIGIBLE` / `INSUFFICIENT_DATA`, each with reasons and the measurements
used. **Unknown is never good**: an instrument whose data could not be measured is
`INSUFFICIENT_DATA`, which is not admissible.

| Filter | Default | Source |
|---|---|---|
| security type / currency / asset class | STK / USD / stock | artifact |
| minimum daily candles | 60 | research cache, ≤ as_of |
| minimum median dollar volume | 1,000,000 | research cache, ≤ as_of |
| minimum last price | 3.00 | research cache, ≤ as_of |
| maximum data age | **None (unset)** | see below |
| sector metadata required | yes | instrument master |

**Why `max_data_age_days` defaults to unset:** as this was written the research cache
ended 2026-08-27 while the calendar read 2026-09-28. Any staleness bound would have
marked the entire universe ineligible. The honest default reports the age and lets the
operator set the bound; a hard-coded number that happens to make the output look healthy
would be worse than none.

**Named as unsupported** rather than silently skipped: spread quality, borrow
availability, halt status, corporate-action validity. This repository has no data for
them, and a filter against a field that does not exist always passes.

---

## E. Scoring — attention, not alpha

The score ranks what deserves watching, from data this project has: **liquidity (0.35),
data completeness (0.20), abnormal volume (0.20), volatility (0.15), relative strength
vs the benchmark (0.10)**.

**Seven components are declared UNAVAILABLE, not zero:** `expected_return`,
`model_confidence`, `event_relevance`, `spread_quality`, `execution_cost`,
`market_data_cost`, `correlation_context`. Each carries a stated reason.

The first two matter most: this repository has **no promoted model** (2 trained models,
2 evaluations, 5 signals, no promotion table). §23 forbids dressing a heuristic as a
qualified alpha model, so the component is absent rather than defaulted. `0.0` would read
as "the model sees no edge"; unavailable reads as "there is no model", and only the
second is true.

Missing components are dropped from the weighted mean, the remaining weights are
renormalised, and `completeness` reports the share actually measured. **An instrument
scored on two of six components is visibly partial, not merely low.**

---

## F. Admission — separate from scoring

`PASS` / `WATCH` / `REJECT` / `INSUFFICIENT_DATA`, each with reasons, thresholds, config
version, `as_of`, validity and lineage.

- `pass_threshold` 0.55, `watch_threshold` 0.40, `min_completeness` 0.40 — **uncalibrated
  defaults**, documented as such. There is no history of dynamic selection to calibrate
  against, and calibrating against returns is the lookahead this phase forbids.
- A high score is **not** admission: completeness, capacity and concentration all sit in
  between.

**Capacity:** PASS candidates ranked by score, then instrument id (deterministic ties).
Selected up to `max_capacity` (default 31). Beyond that, a **waitlist**, recorded as
`CAPACITY_REACHED` — "passed quality, lost the capacity competition", which is not
rejection. **No fallback lowers a threshold to fill slots.**

**Sector concentration** (`max_per_sector` 6, optional `max_sector_fraction`) can only
**subtract**. If healthcare has nothing above the bar, healthcare contributes nothing and
the slots stay empty. Forcing inclusion for visual balance is how a weak instrument
enters a portfolio wearing a diversification excuse.

---

## G. Expiry, intraday admission, anti-churn

**Expiry** uses the I-A `OpportunityValidity` (TTL and/or session close), with a reason
recorded for each. Price- and thesis-based invalidation belong to 25.9I-E, where a thesis
exists.

**A defect found and fixed during implementation:** a revision dropped expired members
from the held list and then immediately **re-admitted them from their own stale
assessments**, which made expiry decorative. Re-entry now requires a fresh assessment at
the new `as_of`; a carried assessment that has expired is downgraded to `REJECT` with the
reason "validity expired … re-entry needs a fresh assessment". A test covers it.

**Intraday admission** consumes **free capacity only** — this phase never evicts a held
instrument, because eviction without a thesis is churn with extra steps. Newly admitted
instruments are flagged `requires_market_data_activation`: the honest statement that the
system now wants data it does not have. **No subscription is opened, priced or changed;
25.9I-C owns that.**

**Anti-churn** (`replacement_margin`, `min_residency`, `cooldown`) exists with
**all-zero defaults**, i.e. inactive, and fails closed when residency is unknown.

---

## H. As-of discipline

`MarketHistory` opens the database **read-only** and puts `timestamp <= as_of` in every
query. Four tests attack it:

- future candles in the database cannot change an earlier selection (compared against a
  physically truncated database — identical members, identical content hash)
- a 20:00 close cannot reach a 13:25 pre-open decision
- benchmark reads are bounded too
- same inputs + same `as_of` + same config → same members, same content hash, same
  snapshot id

`content_hash` deliberately excludes wall-clock build time, so reproducibility is
testable rather than asserted.

---

## I. Persistence

I-A deferred this deliberately; the shape is now known, so five append-only tables exist
(`src/data_access/adaptive_universe_schema.py`):

`adaptive_parent_universe_versions`, `adaptive_session_snapshots`,
`adaptive_session_members`, `adaptive_admission_decisions`, `adaptive_expiry_events`.

- **Idempotent:** ids derive from content (snapshot from session+as_of+revision,
  admission from instrument+as_of+outcome), written with `INSERT OR IGNORE`. Proven on
  real data: two `--persist` runs → 1 snapshot, 31 members, 130 decisions.
- **Append-only:** a revision is a new snapshot naming its predecessor. The 09:20
  universe stays readable after 14:05 replaced it.
- **Non-destructive:** `CREATE TABLE IF NOT EXISTS` only; a test proves an unrelated
  pre-existing table survives initialization untouched.
- **Bounded:** the repository writes to nothing but `adaptive_*`, asserted by a test that
  reads every write statement in the module.

---

## J. Feature flag and production boundary

`adaptive_universe_enabled` remains **OFF by default**. With it off — which is every
deployment — capture uses `config/capture_universe_v1.json` exactly as before, no session
runner is redirected, no subscription changes, and Portfolio, Risk and Execution are
untouched.

`scripts/adaptive_universe.py` is a **dry-run diagnostic by default**; `--persist` is the
only way it writes, and then only to the five adaptive tables. It reports the flag rather
than requiring it, because a diagnostic that only works when a feature is enabled is
useless for checking whether the feature should be enabled.

---

## K. Shadow comparison

`--shadow` reports overlap, additions, drops and drop reasons against the fixed capture
universe. It **makes no performance claim**: deciding which universe is "better" needs
future returns, and using them is exactly the lookahead this phase forbids. The
comparison validates selection integrity, not profitability.

---

## L. What remains

| Phase | Owns |
|---|---|
| **25.9I-C** | COLD/WARM/HOT driving real subscriptions, streaming transport, live cost accounting. **Externally blocked** on the market-data cost answer (ticket #561227) |
| **25.9I-D** | Capital allocation and risk budgeting — the qualified→funded boundary |
| **25.9I-E** | Position thesis, revision calibration, price/thesis-based expiry |
| **25.9I-F** | Event-driven reassessment feeding `event_relevance` |
| **25.9I-G** | Shadow/simulation validation and attribution |
| **25.9I-H** | Controlled IBKR paper integration behind the freshness gate |

Also deferred, with the interface preserved: **correlation/redundancy control**. Daily
closes could support a correlation matrix, but a redundancy rule needs a threshold, and
there is no evidence for one yet. `correlation_context` is declared unavailable rather
than fabricated.
