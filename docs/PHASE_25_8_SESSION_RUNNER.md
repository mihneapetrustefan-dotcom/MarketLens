# Phase 25.8 — Scheduled, Session-Aware Intraday Trading Loop

**Status** COMPLETE in code · **Real IBKR session** UNVERIFIED
**Written** 2026-09-12 · **Branch** `ibkr-paper-validation-fixes`

> **THIS IS AN AUTOMATED SESSION RUNNER.**
> **THIS IS NOT LIVE AUTONOMOUS TRADING.**

---

## A. Executive summary

The loop was a manual batch advancer. A human ran
`run_trading_loop.py --cycles 1` and a cycle happened. Phase 25.8
makes it a session runner: it starts when the market opens, ticks on a
real clock, observes current prices, refreshes on cadences, advances
the existing loop, and closes cleanly.

It does not make the project trade more. It makes it **run correctly**.

---

## B. The defect this phase exists to close

```python
for index in range(args.cycles):
    moment = now + timedelta(seconds=index * args.cycle_seconds)
```

With the 900-second default, `--cycles 4` ran four cycles in about a
second and stamped them `now`, `+15m`, `+30m`, `+45m`. **Three of four
claimed to have happened at moments that had not arrived** — on real
signals, writing real rows.

Phase 25.5's anchor-drift guard did not catch it: it rejects anchors
more than **four hours** from the wall clock, and a 45-minute forward
drift sails through.

**Fixed two ways.** The CLI now asks the clock each cycle. And time is
no longer a number a caller passes: it is a `Clock`, and
`RunMode.requires_wall_clock` decides which kind you may have.

| Mode | Clock | Waits |
|---|---|---|
| `REAL_SESSION` | `WallClock` only | yes |
| `PAPER_SESSION` | `WallClock` only | yes |
| `TEST_REPLAY` | `ReplayClock` | jumps |

`clock_for()` raises `ClockModeViolation` rather than silently
correcting. A replay clock cannot reach a live session by flag, by
config, or by accident.

---

## C. Scheduling: boundaries, not sleeps

`sleep(interval)` after the work drifts. If each cycle takes 37
seconds the loop walks off its own grid and its anchors stop lining up
with the keys idempotency is derived from.

```
boundary   10:15:00
work ends  10:15:37
next       10:20:00     (waits 4m23s, not 5m)
```

**Overruns skip, never backlog.** If a tick runs past its boundary the
scheduler jumps to the next *future* boundary and counts the miss. The
loop's job is current market state; replaying four stale boundaries
would submit decisions about moments that have gone.

### Cadences

| Stage | Interval | Why |
|---|---|---|
| market data | 60s | one batched request, 1/50 of budget |
| bars | 60s | a minute cannot complete faster |
| features | 300s | shortest strategy horizon is intraday 5m |
| signals | 300s | evaluated on completed 5m of bars |
| portfolio | 300s | revalue on the signal grid |
| risk | 300s | periodic, **plus** before every order |
| reconciliation | 900s | broker truth, expensive |

---

## D. Health and failure classification

Stage failures are classified, not fatal. A runner that exits on the
first hiccup is worse than one that keeps observing and refuses to
act.

| Condition | Result |
|---|---|
| market data fails | stage FAILED, trading blocked, session continues |
| no fresh price | DEGRADED, no price-dependent decision |
| blind > 15 min | FAILED — not endlessly "degraded" while acting on nothing |
| no deployable model | reported, observing only, **not** a failure |
| loop cycle fails | new exposure blocked |
| market closes | session ends cleanly |

`HealthState` is Phase 13's, reused. `overall` is the worst reading,
never an average.

---

## E. What is genuinely automated now

| Was manual every cycle | Now |
|---|---|
| launching the loop | session runner ticks on its own |
| checking whether the market is open | `MarketSessionView` each tick |
| refreshing prices | market-data cadence |
| deciding when to reconcile | reconciliation cadence |
| recalculating risk | periodic risk cadence |

## F. What remains human, and legitimately so

| Action | Classification |
|---|---|
| IBKR browser login | **required governance** — no credential may live here |
| model promotion | **required governance** — `--approved-by` |
| `--allow-paper-orders` | **required governance** — running is not permission |
| keeping a host awake | **deployment dependency** |

**The runtime is a genuine external dependency.** GitHub Actions
cannot hold a Client Portal session, so this needs a local supervised
process or a scheduled task on the machine running the gateway. That
is documented, not pretended away.

---

## G. Known limitations

1. **Real IBKR session UNVERIFIED.** Written on a Saturday with the
   gateway down. Verified against the mock and a deterministic replay
   session only.
2. **Session granularity is coarse.** OPEN / CLOSED / UNKNOWN.
   Pre-market, after-hours, holidays and early closes are declared in
   `MarketStatus` but never guessed, because no venue calendar
   supports them yet.
3. **Intraday features are a cadence and a boundary, not a
   computation.** The refresh runs on schedule and reads completed
   bars; the feature engine itself is unchanged, per §14's
   instruction not to rebuild it.
4. **Signal evaluation is wired but produces nothing**, because no
   model is deployable. That is Phase 25.9, and the loop correctly
   reports it instead of lowering a threshold.
5. **Single-process assumption.** The loop's own atomic cycle claim
   prevents duplicate *cycles*; a durable lease for duplicate
   *runners* is not implemented.

---

## H. Phase 26 gate

Shadow trading needs fresh data, live session operation, fresh
decisions, reliable timestamps and recovery. This phase supplies all
but one: **fresh decisions**.

With no deployable model, no legitimate decision is generated, so
there is nothing for shadow to compare against. Phase 26 therefore
remains blocked on **model quality (25.9)**, not on the loop.
