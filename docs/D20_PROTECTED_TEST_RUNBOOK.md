# D20 Protected Test — Runbook

The exact procedure for the single protected test of the D20 reversal
hypothesis. Every command below exists in the repository as of Phase
25.9C. Steps 1–7 were dry-run on 2026-09-14; steps 8–12 have never been
run and must be run **once**.

**Do not start before `check_d20_readiness.py` says READY.** The earliest
theoretical date is **2026-09-25** (a lower bound — see §Dates).

Commands assume a POSIX shell from the repository root with
`PYTHONPATH=src`.

---

## 1. Take a production snapshot

Download the current `db-latest` release asset. Never work on
`data/marketlens.db`.

```bash
curl -sL -o /tmp/prod-snapshot.db https://github.com/mihneapetrustefan-dotcom/MarketLens/releases/download/db-latest/marketlens.db
```

## 2. Make a working copy

```bash
cp /tmp/prod-snapshot.db /tmp/d20-working.db
```

Measured: 4 seconds for 276 MB.

## 3. Refresh prices on the working copy

Requires `POLYGON_API_KEY` in your own shell.

```bash
python scripts/cache_price_candles.py --db /tmp/d20-working.db --skip-minute
```

About 245 requests at 5/min — roughly **51 minutes**. The Phase 25.9C
fix makes this refetch the forward windows; before it, all 149 protected
instruments would have been skipped.

## 4. Verify the refresh

```bash
python scripts/check_d20_readiness.py --db /tmp/d20-working.db --details
```

Expect `STALE_CACHE = 0`. Any `PRICE_VINTAGE_BREAK` means a split
happened between fetches; investigate before continuing.

## 5. Rebuild anchor-v2 labels

```bash
python scripts/build_anchor_v2_labels.py --db /tmp/d20-working.db
```

Measured: 71 seconds. Refuses `data/marketlens.db`. Deterministic and
idempotent.

## 6. Check integrity and v1 preservation

The v1 labels must be unchanged from the snapshot.

```bash
python -c "import sqlite3;[print(p, sqlite3.connect(p).execute(\"SELECT COUNT(*) FROM research_labels WHERE label_version='v1'\").fetchone()[0]) for p in ('/tmp/prod-snapshot.db','/tmp/d20-working.db')]"
```

## 7. Readiness — must say READY

```bash
python scripts/check_d20_readiness.py --db /tmp/d20-working.db
```

READY requires: ledger intact, test registered and **NOT_CONSUMED**,
spec fingerprint unchanged, nothing pending, data-quality exclusions
within 5%, at least 100 resolvable rows on 8 dates with MDE ≤ 0.20, and
today on or after the earliest theoretical date.

**If NOT READY, stop.** Nothing below may run.

## 8. Record the dataset identity

Copy the `dataset_identity` printed in step 7 into your notes. The
validator writes it into the ledger itself.

## 9. Execute exactly once

```bash
python scripts/validate_d20_reversal.py --db /tmp/d20-working.db
```

The validator re-checks the ledger and full readiness, writes **OPENING**
to `research/protected_tests/ledger.jsonl` **before** computing anything,
evaluates, then writes **CONSUMED**. It refuses any second attempt, on
any database.

## 10. Commit the ledger immediately

```bash
git add research/protected_tests/ledger.jsonl
git commit -m "D20 protected test consumed"
git push
```

The ledger is the durable lock. An uncommitted ledger on one machine
does not stop a run on another.

## 11. Persisted result

The verdict is in the working copy's `experiments` table under
`exp-d20-reversal-anchor-v2-protected`, and in the ledger's CONSUMED
entry as a result fingerprint.

## 12. Report

Write the result report from the validator output. Whatever the verdict,
it is final for this hypothesis.

---

## Dates

| | |
|---|---|
| latest required d20 session, weekday projection | 2026-09-24 |
| earliest theoretical test date (lower bound) | **2026-09-25** |
| US market holiday the calendar cannot see | 2026-09-07 (Labor Day) |
| realistic earliest date, accounting for it | **2026-09-28** |

The project's calendar deliberately has no holiday table, so projected
sessions ignore holidays and the computed date is a lower bound. The
readiness checker never relies on it alone: READY also needs the real
candles.
