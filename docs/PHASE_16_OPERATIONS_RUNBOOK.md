# Phase 16 operations runbook

Eighteen procedures for running a trading session safely. Every one can
be rehearsed with `--mock`, which runs the whole thing against the
deterministic IBKR double — no gateway, no account, no network.

For connection-level procedures (starting the gateway, resolving a
contract, checking quotes, handling a disconnect) see
[PHASE_15_IBKR_RUNBOOK.md](PHASE_15_IBKR_RUNBOOK.md). This document
starts where that one ends.

**Real-money trading is not covered, because it does not exist.** No
procedure here can enable it, and none is missing — there is no
adapter that accepts a real-money environment.

All commands take `--db`, `--actor` and `--mock`. `--actor` is
recorded on every action; there is no anonymous operation.

---

## 1. Check the current state

```bash
python scripts/run_operations.py --status
```

Prints the environment, the level ladder with what is implemented,
every capability's health, the limits in force, the active session and
anything recovered from the last run.

Read the health block first. `overall` is the **worst** capability, and
`new orders permitted` is False unless every order-critical capability
is `HEALTHY`.

## 2. Read the readiness assessment

```bash
python scripts/run_operations.py --readiness
```

Eleven categories, each `PASS`, `CONDITIONAL`, `FAIL` or `UNKNOWN`.
`UNKNOWN` blocks — an unmeasured category is not a passing one. There
is deliberately no single score, because security and execution are not
substitutable and one number would imply they are.

## 3. Check the gates for a level

```bash
python scripts/run_operations.py --gates --level 5
```

Prints each gate with its measurement, its threshold and its direction.
A gate with no measurement is reported as blocking.

For any level from 4 up this ends with a refusal that is not about the
gates: the level has no execution path. That refusal stands even when
every gate passes.

## 4. Request a promotion

```bash
python scripts/run_operations.py --request-level 3 --actor alice \
  --reason "paper run clean for six weeks"
```

Records the request and prints its id. **A request alone grants
nothing.**

## 5. Approve a promotion

```bash
python scripts/run_operations.py --approve <request_id> --actor bob \
  --approval-hours 168
```

Must be a **different actor** from the requester; the same actor is
refused. Approvals expire — set `--approval-hours` deliberately rather
than accepting a default that outlives the reason for it.

## 6. Revoke an approval

```bash
python scripts/run_operations.py --revoke <request_id> --actor bob \
  --reason "withdrawn after review"
```

A reason is required: an approval withdrawn with no explanation leaves
the next operator unable to tell whether it was a mistake or a
decision. The effective level drops immediately.

Do not wait for an approval to expire when you have decided it was
wrong. Expiry is a backstop against inattention, not a way to reverse
a judgement.

## 7. Open a trading session

```bash
python scripts/run_operations.py --start-session --actor alice \
  --strategy strat-1 --model-version m-1 --capital-limit 25000
```

Runs the eight preflight checks and prints each one as `pass`, `FAIL`
or `NOT MEASURED`. All eight must pass **and** be measured. An
unmeasured check blocks, because not knowing whether the account
reconciles is not the same as knowing that it does.

On success it prints the session id and the configuration fingerprint.
The configuration is frozen from that moment.

## 8. When preflight fails

Fix the cause, do not bypass the check. The common ones:

| Check | Usual cause | Fix |
|---|---|---|
| `broker_connected` | gateway down or not authenticated | Phase 15 runbook §1, §9 |
| `market_data_live` | no instrument mapped, or a delayed/frozen quote | resolve a contract (Phase 15 §4) |
| `reconciliation_clean` | never run, or an open mismatch | procedure 13 below |
| `no_unknown_orders` | an order in `UNKNOWN` | procedure 14 below |
| `capital_configured` | no notional cap set | set `--capital-limit` |
| `kill_switch_off` | emergency stop active | procedure 12 |

`NOT MEASURED` on `reconciliation_clean` means no reconciliation has
ever run against this account — not that it is clean.

## 9. Pause a session

```bash
python scripts/run_operations.py --pause --actor alice \
  --reason "stepping away from the desk"
```

Stops new submissions. Positions, working orders and history are
untouched. Resumable.

## 10. Resume a session

```bash
python scripts/run_operations.py --resume --actor alice
```

Only a `PAUSED` session resumes. Re-run `--status` afterwards and check
`may submit` before assuming anything is flowing again.

## 11. Close a session routinely

```bash
python scripts/run_operations.py --stop --actor alice --reason "end of day"
```

A reason is required. The session summary records orders submitted,
fills, open orders, unknown orders and whether reconciliation was
clean. `is_clean_close` is False if anything is still open or unknown —
which is information, not a failure.

## 12. Emergency stop

```bash
python scripts/run_operations.py --emergency-stop --actor alice \
  --reason "unexplained position delta"
```

Immediate, and **terminal**. Positions and history are untouched; the
session cannot be resumed, and continuing requires opening a new one.
That asymmetry is deliberate: a stop taken in alarm should cost a
deliberate restart, not a keystroke.

## 13. Reconcile, and handle a mismatch

```bash
python scripts/run_ibkr.py --reconcile
```

Mismatches are graded. `CRITICAL` (position, cash, duplicate fill,
unknown broker order) blocks execution until resolved. `INFO` (price,
status) does not.

**Do not let the system resolve a critical finding — it cannot.**
Resolution requires a human actor and a note, because automatically
correcting an unexplained capital or position discrepancy destroys the
evidence of its cause.

Establish the cause first: compare against the IBKR statement, check
for a fill applied twice, check for an order placed outside this
system.

## 14. Investigate an unknown order

```bash
python scripts/run_ibkr.py --resolve-unknown
```

Asks the broker what happened to each order in `UNKNOWN`. **It never
resubmits.** An `UNKNOWN` order is one whose request left but whose
answer did not; treating it as failed is how a duplicate reaches the
venue.

## 15. Clear a latched limit

```bash
python scripts/run_operations.py --clear-limits --actor alice \
  --reason "reviewed the day, cause understood, resuming"
```

Loss and drawdown limits latch. They do not clear when the market moves
back, and clearing them requires an actor and a reason — both are
recorded. Do this only after establishing why the limit was hit.

## 16. The daily report

```bash
python scripts/run_operations.py --daily-report
```

Trades closed and open, net P&L, wins and losses, win rate, average win
and loss, profit factor, median slippage, and every signal that did not
become a trade with whether the **system** or the **market** prevented
it.

Fields report `None` where nothing was measured. A daily report that
filled gaps with zeros would be most confident exactly when
instrumentation had failed.

## 17. Compare environments

```bash
python scripts/run_operations.py --compare
```

Backtest against paper against live. Read the `conclusive` flag first:
it is almost always False, and it says why. Below 30 trades and 60
days the comparison means nothing.

Only **mechanical** metrics — slippage, latency, rejection and fill
rates — are reported as drift. Return differences over a short period
are noise, and labelling them drift would invite acting on them.

## 18. Recover after a restart

```bash
python scripts/run_operations.py --status
```

The `RECOVERED` block reports what came back: approvals, the active
approval, the session, the day's loss state and any latched breaches.

A latched breach survives a restart. So does an active session — it is
not silently resumed, it is reported, and you decide. An abandoned
session left in `CREATED` does not block a new one; only `ACTIVE` and
`PAUSED` sessions do.

---

## Troubleshooting

| Message | Cause |
|---|---|
| `no active session` | nothing is open; `--start-session` first |
| `Session NOT started` | a preflight check failed or was unmeasured — procedure 8 |
| `already active` | stop the existing session before opening another; two sessions would each hold their own limits and neither would see the other's |
| `may not approve it` | the approver is the requester — procedure 5 |
| `no execution path` | the level is not implemented; this is not a gate failure and no configuration fixes it |
| `latched:` in a limit refusal | a loss or drawdown limit is holding — procedure 15 |
| `market data is not live enough to trade on` | no instrument mapped, or the quote is delayed/frozen |

## What is not covered

Real-money trading, autonomous strategy modification, autonomous model
promotion, and autonomous capital management. None of these exists, and
no procedure here can enable any of them.
