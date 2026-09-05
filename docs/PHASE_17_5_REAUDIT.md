# Phase 17.5 — post-remediation re-audit

Date: 2026-09-05 · Commit audited: `1682a99` · Baseline: Phase 17 at `890a229`

This is a verification gate, not a development phase. Nothing was
built. Two stale sentences in existing audit documents were corrected
(§11). Everything else here is observation.

**One methodological correction to Phase 17 up front.** That audit
measured the database at `data/marketlens.db` — a *local development
copy*, 82 tables, 38 empty. Production lives in the `db-latest` GitHub
Release asset and has **47 tables, 5 empty**. Every row count below was
taken from the production asset downloaded today (213.7 MB, published
2026-09-04T23:06Z), not from the local file. Several Phase 17 findings
read differently once measured against the right database, and those
are marked.

---

## 1. Phase 17 findings review

Phase 17 raised one critical architectural gap, four high-severity
items, five medium, and five low. It also made exactly one change
(`src/execution/intake.py`) and documented the rest.

The headline finding was that the repository held **two systems that
barely touched each other** — a scheduled news pipeline (Phases 1–9)
producing data, and an unscheduled quant/execution stack (Phases 10–16)
designed to consume it, joined only by a human clicking buttons. The
symptom was a system that worked and produced almost nothing: 5
signals, 5 predictions, 2 models, 0 orders.

That framing survives re-audit. What changed is that **the joint now
runs**, and the numbers behind it moved by two orders of magnitude.

---

## 2. Fixes verified

### The pipeline connected — verified in production

| Table | Phase 17 | Now | Change |
|---|---:|---:|---:|
| `signals` | 5 | **408** | ×82 |
| `predictions` | 5 | **549** | ×110 |
| `trained_models` | 2 | 4 | +2 |
| `canonical_events` | 581 | **1,037** | +456 |
| `research_observations` | 581 | **1,049** | +468 |
| `price_candle_cache` | 87,003 | **116,719** | +29,716 |
| `article_entities` | — | 44,336 | — |
| `recommendations` | 22,725 | 33,589 | +10,864 |
| empty tables | 38 of 82 *(local)* | **5 of 47** *(production)* | — |

Signal status is no longer uniform: **10 active**, 396 suppressed, 2
superseded. Phase 17 recorded that *every* signal was suppressed. Every
suppression now carries a stated reason, and all of them are the same
one — `information is N days old, limit 7.0` — which is the intended
staleness guard doing its job rather than a silent drop.

Model effective sample rose from 4 clusters to **116**, crossing the
30-cluster threshold below which the evaluator refuses to call a result
anything but descriptive.

### TD-01 — risk decisions could not reach execution · **FIXED**

`src/execution/intake.py` present, 20 tests. `from_decision()` takes
`risk_approved` from the `RiskDecision` object; the signature carries
no override parameter and a test inspects the signature to keep it that
way. Verified by re-reading the file at HEAD.

The stronger verification is negative: **no file under `src/execution`,
`src/risk`, `src/portfolio`, `src/paper`, `src/brokers` or
`src/pointintime` was modified by any remediation commit.** The diff
from `890a229` to HEAD is +3,227/−115 across 32 files, all of them
pipeline, modeling, dashboard, news-schema, workflow or test files. The
trading-safety surface was not touched, so it cannot have regressed.

### TD-02 — two competing article schemas · **FIXED, verified in production**

Phase 17 verified this against a local copy. It is now verified against
the production asset:

| Check | Result |
|---|---:|
| `articles` rows | 48,906 |
| `news_articles` rows | **48,955** |
| legacy rows missing from canonical | **0** |
| `duplicate_of` pointing nowhere | **0** |
| titleless canonical rows | **0** |
| canonical rows carrying sentiment | 48,955 |
| duplicate canonical `article_id` | **0** |

The daily sync step in `daily.yml` is running — canonical's latest
article is `2026-09-04T22:00:00Z`, matching legacy.

**The predicted retention divergence is now observable, not
hypothetical.** Canonical holds **49 rows the legacy table no longer
has**, and reaches back to **2023-12-15** where legacy now starts at
**2026-07-07**. `archive_old_articles.py` prunes `articles`; nothing
prunes `news_articles`. This is the designed behaviour (spec §50) and
it is exactly what makes TD-02b a decision rather than a rename — see
§3.

### TD-05 — the Phase 4–12 pipeline was manual · **FIXED IN CODE, NOT YET EXERCISED**

`pipeline.yml` is registered and active with both crons. It has run
**zero times**. Its first scheduled fire is **Sunday 2026-09-06 02:00
UTC — tomorrow.**

`predict.yml` has run once, successfully, at 2026-09-04T14:02Z; the
newest prediction is stamped 14:02:25, which is how 549 predictions
exist.

So the row counts in §2 came from **manual runs**, not from the
automation. The automation is shipped and GitHub has parsed and
registered it, which is real but is not the same as proven. **The
Sunday run is the actual test of TD-05**, and until it completes this
item is verified as *written*, not as *working*.

### TD-12 — unpinned requirements · **FIXED**

`feedparser==6.0.14`, `pandas==3.0.5`, `yfinance==1.7.0`, with the rule
for moving one written into the file. The register still said OPEN;
corrected today (§11).

### TD-14 — tests needing pandas/feedparser · **FIXED (prior)**

Full suite runs clean; see §9.

### Fixes with no register entry, verified here

- **The 96 MB write guard.** Raised consistently: 1,400 MB projection
  refusal in `backfill_article_entities.py`, `fuse_events.py`,
  `populate_events.py`; 1,800 MB **warning** — not an error — in
  `archive_old_articles.py` and `reduce_database_size.py`. The database
  is at 213.7 MB, so headroom is large. The distinction matters: the
  original bug was a hard stop, and only the Release asset's real 2 GB
  ceiling now justifies one.
- **Inference stage.** `src/modeling/inference.py`, 21 tests. Verified
  that it builds the design matrix from the model's stored
  `feature_names_json` and never re-derives an ordering — coefficients
  are positional, and a re-derived order would produce plausible wrong
  numbers rather than an error.
- **Signal labelling.** `signalLabel()` used by all three render sites.
- **Event confidence spread.** Widened — see NEW-04 for the caveat.

---

## 3. Remaining issues

### TD-02b — seven readers still on the legacy table · **NOT FIXED, now unblocked**

The blocker Phase 17 recorded (canonical empty in production) is gone.
The semantic decision it carried is now sharper, not softer: repointing
the dashboard would silently start showing articles back to 2023,
because canonical accumulates and legacy is pruned to 60 days. That is
a visible product change and it should be a deliberate one.

### TD-03 — two competing trade-idea schemas · **NOT FIXED, severity increased**

`src/dashboard.py` issues 12 queries against `recommendations` and 4
against `signals`. Both are now live and growing: 33,589 recommendations
and 408 signals. In Phase 17 this was one populated table beside a
near-empty one, so a reader could guess which mattered. It is now two
populated tables describing the same concept with different schemas and
different lifecycles, presented on one page. The duplicate got worse by
the pipeline succeeding.

### Unchanged and still accurate

| Item | Status | Evidence at HEAD |
|---|---|---|
| TD-04 two order lifecycles | ACCEPTED | unchanged; no orders exist anywhere |
| TD-06 scripts reimplement libraries | OPEN | both pairs still divergent |
| TD-07 `fusion/clustering.py` unused | OPEN | still not imported by `fusion/engine.py` |
| TD-08 legacy Phase 2 collectors | OPEN | `api_collector.py`, `web_scraper.py` test-only |
| TD-10 two import conventions | ACCEPTED | unchanged |
| TD-11 stale git worktree | OPEN | still pinned at `c3256a3` |
| TD-13 CI 3.11 vs dev 3.12 | OPEN | 3.11 uniformly across all 25 workflows |
| TD-15 `archive_old_articles.py` hardcoded archive dir | OPEN | `ARCHIVE_DIR` still `REPO_ROOT/data/archives` at line 49 |

### TD-09 — six unused feature tables · **PARTIALLY NO LONGER RELEVANT**

Measured against production rather than the local copy:

- **`fusion_contradictions` has a producer after all.** It holds 1 row,
  matching exactly one canonical event with `corroboration_state =
  'contradicted'`. Phase 17 called this out as the notable case; it was
  an artefact of measuring the wrong database.
- **`allocation_changes` and `allocation_proposals` do not exist in
  production.** They are local-only schema.
- Genuinely empty in production: `event_corrections`,
  `event_instruments`, `event_sectors`, `ingestion_checkpoints`,
  `raw_articles`. Five, not six, and `raw_articles` is superseded by
  design.

---

## 4. Regressions

**None survive in `HEAD`.**

One was introduced and caught inside the remediation window and is
recorded because it is evidence about the test suite rather than about
the code. The first version of the signal-labelling change joined
`instruments`/`securities`/`companies` directly into the signals query.
`_rows()` swallows `sqlite3.OperationalError` into `[]`, so against a
database holding signals but no instrument registry **every signal
disappeared** — not unlabelled, gone, silently. An existing test
(`test_signals_available_and_instrument_present`) failed with
`'crypto-BTC' not found in []` before it shipped. The lookup is now
built separately and guarded, with two tests that drop all three
registry tables and assert the signal still appears.

A decorative feature was able to delete the data it decorated, and the
suite stopped it. That is the load-bearing result.

---

## 5. Architecture status — **COHERENT**

| Requirement | Verdict | Evidence |
|---|---|---|
| One coherent architecture | PASS with a known seam | The Phase 1–9 / 10–16 split persists by design (TD-10, accepted). The joint between them now runs. |
| One source of truth per domain | **2 of 3 resolved** | Article: canonical is complete and synced, legacy still the write path (TD-02b). Event: two-layer model, correct as designed. **Trade idea: still duplicated (TD-03).** |
| No unnecessary duplicate systems | PARTIAL | TD-03, TD-04, TD-06 stand. |
| No broken dependencies | PASS | Full suite imports clean; 2,903 tests. |
| No architecture bypasses | PASS | See §6. |
| No obsolete MT5 implementation | **PASS** | `grep -riE "mt5\|metatrader"` across `src`, `scripts`, `.github` returns three hits, all inside `scripts/audit_live_safety.py` — the search that proves the absence. |
| IBKR the only broker | **PASS** | Live-safety audit Q10: no second broker planned or stubbed. |

---

## 6. Trading safety status — **PASS, and untouched**

`scripts/audit_live_safety.py`: **16 of 16 questions PASS**, exit 0.

```
Q1  a real-money broker or account cannot be constructed        PASS
Q2  allow_real_orders is False and has no setter                PASS
Q3  MARKETLENS_ALLOW_REAL_ORDERS=1 grants nothing               PASS
Q4  IBKR_ENVIRONMENT=live refused however it is spelled         PASS
Q5  a session cannot be configured for real money               PASS
Q6  no implemented execution level is real money                PASS
Q7  approving level 7 still yields a non-real-money level       PASS
Q8  the governor never reports real money as reachable          PASS
Q9  nobody can approve their own promotion request              PASS
Q10 no second broker is planned or stubbed                      PASS
Q11 no MT5 reference remains in src, tests or scripts           PASS
Q12 no literal credential is assigned anywhere in src           PASS
Q13 the IBKR config carries no credential field at all          PASS
Q14 .env is gitignored and .env.example is not                  PASS
Q15 no .env file is tracked by git                              PASS
Q16 no real-money capital default ships in the code             PASS
```

**The chain, re-walked:**

`Signal` → `Portfolio` → `Risk` → `OrderIntent` → `intake.from_decision()`
→ `orchestrator.submit_intent()` → `safety.assert_not_real_money()` →
23 validation checks → state machine → `_submit()` →
`gateway.submit_order()` → IBKR paper.

- **One** call site reaches a venue: `src/execution/orchestrator.py:549`,
  inside `_submit()`, documented as *"the only method in this file that
  can reach a venue"*.
- The hard stop runs **first**, before policy, idempotency or market
  status: `orchestrator.py:304`. It raises rather than returning a
  verdict, so a caller cannot ignore it by forgetting to check.
- `risk_approved is None` fails closed — not consulted is not approved.
- The lifecycle is walked, never jumped: `VALIDATING` → `APPROVED` →
  `SUBMITTING`, each transition recorded, because a gap in the history
  is a gap in the record of why the order was allowed.
- An adapter that raises yields `UNKNOWN`, never `FAILED` — the
  difference is whether a retry is permitted, and a raised exception
  does not prove the venue was not reached.

**Duplicate protection** — verified structurally and empirically.
`idempotency_key` covers account, instrument, side, quantity, order
type, TIF, both prices, intent id and intent version; a duplicate
returns the existing order rather than a rejection, because the caller
asked for a thing that has happened. Zero duplicate rows anywhere in
production: predictions per `(observation, model)`, signal identity
hashes, canonical article ids, price candles, research observations —
all confirmed unique.

**Reconciliation.** `StateMachine.force()` is the one path that may
contradict the lifecycle, and it is correctly scoped: two callers, both
in `src/execution/reconciliation.py`, a reason is mandatory
(`ValueError` without one), and the transition is recorded like any
other prefixed `reconciliation:`. A corrected book always shows that it
was corrected.

**No inbound attack surface.** No web framework is imported anywhere;
no socket is bound; no server is started. The "API" is provider
integrations outbound and module boundaries inbound.

**Nothing is at risk today.** `order_intents`, `risk_decisions`,
`portfolios`, `positions` and the entire Phase 14/16 execution schema
**do not exist in the production database**. No order has ever been
created. This is the correct state for a system whose signal layer only
started producing this week.

---

## 7. Data / quant integrity

### Lineage — **complete**

```
signal → observation_id → research_observation → prediction → trained_model
```

- 408 of 408 signals join to a `research_observation`.
- 408 of 408 signals join to a `prediction` through `observation_id`.
- 0 signals lack an `observation_id`.
- 0 orphan predictions — every one names a `trained_model_id` that exists.

Every signal on the dashboard can be traced to the model that produced
it. One note: the link is **indirect**. `signals` carries
`observation_id` but no `prediction_id` or `trained_model_id`, so if two
models ever score the same observation the join becomes ambiguous. Not
a problem today (one model family, no overlapping scores) and worth
knowing before a second family exists.

### Look-ahead protection — **intact**

`src/pointintime/view.py` still raises `LookAheadViolation` structurally
rather than by convention. Consumers unchanged and now including the new
inference stage: `features/engine.py`, `impact/calculations.py`,
`impact/engine.py`, **`modeling/inference.py`**, `scripts/compute_features.py`.

That the new code adopted the guard rather than working around it is
the specific thing worth verifying about a new stage, and it did.

### Timestamp semantics — **unchanged and correct**

`event_time` / `publication_time` / `ingestion_time` / `detection_time`
remain four distinct columns on `events`. `predictions` keeps
`information_cutoff` apart from `predicted_at`. Signals carry
`source_information_cutoff` separately from `created_at` — which is
precisely what makes the staleness suppression meaningful.

### Feature/label separation — **intact**

Inference reads `feature_names_json` off the stored model. No
re-derivation, so a feature added later cannot silently shift the
coefficient vector.

### Model quality — **the substantive finding of this re-audit**

All four trained models fail their own evaluator:

| Model | Trained | Sample | Clusters | r² | Directional acc. | Beats baselines |
|---|---|---:|---:|---:|---:|:---:|
| `tm-0be69458` | 08-29 10:27 | 320 | 4 | −0.197 | 0.600 | **no** |
| `tm-99df38e0` | 08-29 11:41 | 320 | 4 | −0.197 | 0.600 | **no** |
| `tm-6666c760` | 09-04 10:11 | 551 | 99 | −0.313 | 0.410 | **no** |
| `tm-37886ecf` | 09-04 13:29 | 677 | **116** | −0.239 | **0.413** | **no** |

A negative r² means the model is worse than predicting the mean.
Directional accuracy of 0.413 is worse than a coin flip. Effective
sample improved a great deal; predictive skill did not appear.

This is not a regression — it is the same conclusion Phase 17 reached
(*absence of signal, not overfitting*), now measured on 29× the data
and holding. The honest reading is that **more data did not help**, and
that is worth more than the earlier "wait for more data" verdict.

The problem is what the pipeline does with it — see NEW-01.

---

## 8. Security status — **PASS**

| Check | Result |
|---|---|
| Literal credentials in `src`/`scripts`/`tests` | **none** (regex sweep + audit Q12) |
| `.env` tracked by git | **no** (audit Q15) |
| `.env` gitignored, `.env.example` not | **yes** (audit Q14) |
| IBKR config carries a credential field | **no such field exists** (audit Q13) |
| Credentials in the public dashboard | **none** — `docs/index.html` (986 KB, GitHub Pages) swept for api key / secret / token / password / provider env names / SMTP / email addresses: zero hits |
| Inbound network surface | **none** — no framework, no bound socket |
| Dependencies | **pinned** as of `f1c0868` |
| Secrets in logs | no credential-shaped literal reaches a log call |

The dashboard is public and contains market data, signals and model
evaluations only. No account identifier, position, order or credential
appears — which is unsurprising, since none of those exist in the
production database.

---

## 9. Test results

```
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -t .

Ran 2903 tests in 127.694s
OK (skipped=1)
exit 0
```

Zero failures, zero errors. (Ten lines match `^ERROR:` — all of them
deliberate simulated-outage log output from provider-failure tests, not
test results.)

**CI is green on the audited commit.** The `Tests` workflow run for the
push of `1682a99` completed `success` at 2026-09-04T17:03:12Z.

**+88 tests since Phase 17** (2,815 → 2,903), concentrated exactly where
the remediation landed:

| File | Tests |
|---|---:|
| `tests/modeling/test_inference.py` | 21 |
| `tests/scripts/test_migrate_news_to_canonical.py` | 18 |
| `tests/test_dashboard_prices.py` | 16 |
| `tests/test_dashboard_signal_labels.py` | 13 |
| `tests/scripts/test_populate_events.py` | 13 |
| `tests/scripts/test_cache_price_candles_coverage.py` | 8 |

Plus `tests/execution/test_phase17_intake.py` (20) from Phase 17 itself.

Lint / type checks: the project has never configured a linter or type
checker, so there is nothing to run and nothing regressed. `tests.yml`
does run `src/check_registry_collisions.py` as a CI step, which passes.

---

## 10. New issues introduced or discovered after Phase 17

### NEW-01 — inference has no model quality gate · **HIGH**

`load_model()` in `src/modeling/inference.py:161` selects with
`ORDER BY trained_at DESC LIMIT 1`. There is no filter on `status`, on
`r_squared`, or on `beats_all_baselines`.

Every model in the database has `beats_all_baselines = 0` and a negative
r². The newest — directional accuracy 0.413 — produced **422 of the 549
predictions**, which became signals, **10 of which are active on the
public dashboard right now**.

*Directly caused by the remediation.* Before the inference stage
existed, model output reached only the held-out test slice. It now
reaches a public page automatically on every pipeline run.

*No capital is at risk* — nothing connects to execution, and the model
detail page does render **"nu bate baseline"** in red. But the signals
page carries no such caveat: a reader sees `Apple SHORT −0.56%` with
nothing saying the model behind it is worse than the mean.

This is an honesty defect, not a safety one. It is listed as a blocker
because the spec's standing constraint is *do not automatically promote
models*, and while there is no formal champion concept here, the
practical effect — an unvalidated model's output reaching users without
review — is what that constraint exists to prevent.

**Not fixed in this phase.** Both plausible remedies are product
decisions: refusing to score below a quality bar would stop the newly
working pipeline producing anything at all, and adding a caveat to the
signals page is a feature. Neither belongs in a verification gate.

### NEW-02 — signal confidence does not discriminate · **MEDIUM**

403 of 408 signals carry confidence exactly `0.30`; the other 5 carry
`0.15`. Two values across 408 signals. Confidence cannot rank or size
anything in this state, and any downstream consumer that weights by it
is weighting by a constant.

### NEW-03 — the price-coverage fix is shipped but unexercised · **LOW**

97 of 389 registry instruments still have no candles: 39
`US_AND_INTL`, 32 `BVB`, 26 `CRYPTO`. `--include-unstudied` runs only
in `pipeline.yml` stage 4, which has never run. The 32 BVB instruments
are permanently outside Polygon's coverage, so tomorrow's Sunday run
addresses at most **65** of the 97.

### NEW-04 — the event confidence spread widened but stayed narrow · **LOW**

1,517 events span 0.45–0.56 across 10 distinct values, clustered on
0.47–0.52. The source-tier signal moves confidence by roughly ±0.05.
Better than the flat distribution that prompted the fix; not yet a
spread that separates a well-sourced event from a poorly-sourced one.
All 1,517 events are `extraction_tier = deterministic_rule`, which
bounds how much spread is available.

### NEW-05 — documentation drift · **LOW**

`COMPLETE_SYSTEM_AUDIT.md`, `DATABASE_AUDIT.md`,
`TEST_COVERAGE_AUDIT.md` and `PRODUCTION_READINESS.md` cite **2,815
tests** (now 2,903) and **82 tables, 38 empty** (production: **47
tables, 5 empty**). The table figures were measured against a local
development database. Left uncorrected here beyond the two lines in
§11, because rewriting four documents is not a verification activity —
but the 82/38 figure should not be quoted again.

---

## 11. Changes made during this phase

Two sentences, both corrections of fact:

- `docs/TECHNICAL_DEBT_REGISTER.md` — TD-12 marked **FIXED** with the
  pinned versions and commit; it still read OPEN after `f1c0868`
  pinned them.
- `docs/SECURITY_AUDIT.md` §7 — dependencies described as pinned
  rather than unpinned.

No code was changed. No test was changed. No workflow was changed.

---

## 12. Final verdict

# READY TO CONTINUE

The remediation did what it claimed. The critical Phase 17 finding is
closed and structurally defended. The migration is verified complete
against the production database rather than a copy. The pipeline that
had produced 5 signals in five phases has produced 408, with full
lineage and no duplicates. The trading-safety surface was not touched by
any remediation commit and re-audits clean on all 16 questions. 2,903
tests pass; CI is green on the audited commit.

Nothing found in this re-audit puts capital at risk, because nothing in
this system can reach money — which remains the correct state.

**Two things temper that verdict, and neither blocks continuing:**

1. **TD-05 is verified as written, not as working.** `pipeline.yml` has
   run zero times. Tomorrow's 02:00 UTC run is its first real test.
2. **The models do not work.** Four models, all with negative r², all
   failing their own baselines, on 29× the data Phase 17 had. That is
   now a measured conclusion rather than a small-sample caveat. The
   *plumbing* is proven; the *prediction* is not, and NEW-01 means the
   pipeline currently publishes the latter as though it were validated.

---

## 13. Required next steps

**Critical / high — do these before building anything new:**

1. **NEW-01 — decide what inference does with a model that fails its
   baselines.** Either gate `load_model()` on `beats_all_baselines`, or
   carry the caveat onto the signals page. Doing neither means the
   system publishes unvalidated predictions automatically, every week,
   with no reader able to tell.
2. **Watch the Sunday 02:00 UTC pipeline run** and confirm all eleven
   stages report. This is the verification TD-05 is still missing.

**Medium — the architectural debt that got worse by succeeding:**

3. **TD-03 — declare `signals` canonical** and relabel
   `recommendations` as history in the UI. Both tables are now
   populated and growing; the ambiguity is live.
4. **TD-02b — repoint the seven legacy readers**, now unblocked.
   Dashboard last, and decide deliberately whether it should show
   articles back to 2023.
5. **NEW-02 — investigate why signal confidence takes two values.**

**Low — cheap, and each removes a small trap:**

6. TD-15 — derive the archive directory from the database path, so a
   destructive script can be rehearsed on a copy.
7. TD-11 — `git worktree remove` the stale worktree.
8. TD-13 — align CI to Python 3.12.
9. NEW-05 — refresh the four documents citing 2,815 tests and 82 tables.

**Explicitly not recommended:** merging the two order lifecycles
(TD-04), normalising the import conventions (TD-10), or deleting
`fusion/clustering.py` (TD-07) on static analysis alone. Each was
considered and each remains correctly deferred.
