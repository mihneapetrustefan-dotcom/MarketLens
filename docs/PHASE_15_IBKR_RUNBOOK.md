# IBKR operator runbook (Phase 15)

Twelve procedures. Every command is safe to run: none of them can place
a real-money order, and none places a paper order unless you explicitly
open the ordering gate.

**Before anything else:** you can exercise the entire integration with
no gateway, no account and no network by adding `--mock` to any command
below. Start there.

```bash
python scripts/run_ibkr.py --mock
```

---

## 1. Starting the IBKR connection

The Client Portal Gateway is a small Java program **you** run. It holds
your IBKR session; this application never sees your credential.

1. Download the Client Portal Gateway from IBKR and unpack it.
2. Start it:
   ```bash
   ./bin/run.sh root/conf.yaml        # macOS / Linux
   bin\run.bat root\conf.yaml         # Windows
   ```
3. Open `https://localhost:5000` in a browser. Accept the self-signed
   certificate warning — the gateway serves its own certificate on
   localhost, which is why `IBKR_VERIFY_TLS=false` is permitted **only**
   for a local host.
4. Log in with your IBKR credentials **in that browser**. Nothing you
   type there reaches this application.
5. Configure this project:
   ```bash
   export IBKR_ENABLED=true
   export IBKR_ENVIRONMENT=paper
   export IBKR_ACCOUNT_ID=DU...        # your PAPER account
   ```
6. Check:
   ```bash
   python scripts/run_ibkr.py --status
   ```

Expect `connection connected`, `authenticated True`. If not, see §9.

## 2. Confirming you are on PAPER

Do this before opening the ordering gate, every time.

```bash
python scripts/run_ibkr.py --account-info
```

- IBKR **paper** account ids begin with **`DU`**.
- A **live** account id begins with `U`.

The CLI prints a warning for any account not starting with `DU`. If you
see it, stop: do not open the ordering gate.

`IBKR_ENVIRONMENT` cannot be set to `live` — the configuration raises —
but that guard does not know whether the *account id* you supplied is a
paper one. Only you can confirm that, in the IBKR portal.

## 3. Checking the account

```bash
python scripts/run_ibkr.py --account-info
```

Shows discovered accounts, cash, equity, available funds, buying power,
margin, realized and unrealized P&L, and current positions.

An empty or zero figure means IBKR did not report it, not that it is
zero — every optional field stays `n/a` rather than defaulting.

## 4. Resolving a contract

Nothing trades until its IBKR contract is resolved. `ticker` alone is
not enough.

```bash
python scripts/run_ibkr.py --resolve --symbol AAPL --instrument i-aapl
```

Three outcomes:

- **Resolved** — the mapping is saved with its `conid`, tick size and
  lot size. Done once; it persists.
- **Ambiguous** — IBKR returned several contracts and none was uniquely
  identified. **Nothing was saved and nothing will trade.** Re-run with
  a discriminator:
  ```bash
  python scripts/run_ibkr.py --resolve --symbol AAPL --instrument i-aapl \
      --exchange NASDAQ --currency USD
  ```
- **Not found** — IBKR knows no contract for that symbol and type.

## 5. Checking market data

```bash
python scripts/run_ibkr.py --quote --instrument i-aapl
```

Watch `availability`:

| Value | Meaning |
|---|---|
| `available` | live, and may back an order |
| `delayed` | **not** tradeable — fine for a dashboard, wrong for a limit price |
| `restricted` | this account lacks the subscription |
| `unavailable` | IBKR returned nothing usable |
| `unknown` | not yet checked |

An IBKR account does **not** automatically carry every market-data
permission. If you see `restricted`, subscribe in the IBKR portal or
trade only instruments you have data for.

## 6. Checking broker health

```bash
python scripts/run_ibkr.py --status
```

Reports connection state, whether the gateway is authenticated, whether
it is connected onward to IBKR, whether a **competing session** has
taken your account, the reconnect count, and both safety gates.

## 7. Dry run

Validates everything and stops before submission. Uses the same code
path a real submission would.

```bash
python scripts/run_ibkr.py --dry-run-order --instrument i-aapl \
    --quantity 1 --assume-risk-approved
```

`Actually Submitted: NO` always. If validation fails, every reason is
listed — not just the first.

`--assume-risk-approved` exists because a CLI order carries no risk
verdict. Without it the order is refused with `RISK_UNAVAILABLE`: not
consulted is deliberately **not** approval.

## 8. Enabling and disabling paper execution

Connecting is not permission to trade. After confirming §2:

```bash
export IBKR_PAPER_ORDERING_ENABLED=true
# or, for one command only:
python scripts/run_ibkr.py --submit --instrument i-aapl --quantity 1 \
    --assume-risk-approved --allow-paper-orders
```

**Keep test orders small.** `--quantity 1` is the default for a reason.

To disable:

```bash
export IBKR_PAPER_ORDERING_ENABLED=false
```

To stop **all** execution across every broker at once, use the Phase 14
kill switch:

```bash
python scripts/run_execution.py --kill-switch on --reason "..." --allow-paper
```

It stops new orders. It does **not** cancel working orders or delete
anything — deciding what to do about exposure already at IBKR is a
human decision that needs the history preserved.

## 9. Handling a disconnect

| Symptom | Meaning | Action |
|---|---|---|
| `authenticated False` | the gateway session lapsed | Log in again at `https://localhost:5000`. The session expires when idle. |
| `competing session True` | another session took the account | Close the other session. Retrying will fight it, so the adapter stops rather than looping. |
| `connection degraded` | authenticated, but the gateway is not connected onward to IBKR | Usually transient. `--status` again; the heartbeat restores it. |
| `connection disconnected` | the gateway is not running | Start it (§1). |

The adapter never loops forever: an unauthenticated gateway needs a
human at a browser, so it stops after one attempt rather than looking
like a hang.

## 10. Reconciling a mismatch

```bash
python scripts/run_ibkr.py --reconcile
```

Compares our orders, positions and cash against IBKR's. **Nothing is
auto-corrected.** Each mismatch names both values.

| Mismatch | Usual cause | What to do |
|---|---|---|
| `position_mismatch` | a fill we never received | `--reconcile` again after `--resolve-unknown`; if it persists, compare the IBKR trade log against our fills |
| `cash_mismatch` | commission reported later than the fill | often resolves itself; investigate if it grows |
| `unknown_broker_order` | an order placed outside this system | expected if you also trade manually in that account |
| `status_mismatch` | our record is stale | run `--reconcile` after a poll |
| `unknown_state` | an unresolved timeout | §11 |

Adjusting our book to make a mismatch disappear also destroys the
evidence of what caused it. Do not.

## 11. Investigating an unknown order

The dangerous case: we submitted, the request timed out, and IBKR may
or may not hold the order.

```bash
python scripts/run_ibkr.py --resolve-unknown
```

This **queries** IBKR. It never resubmits.

- `resolved -> acknowledged/working/filled` — IBKR has it; our record
  now matches.
- `resolved -> failed` — IBKR has no record; it cannot fill, so it is
  safe to treat as failed.
- `STILL UNKNOWN` — IBKR could not be asked. Nothing was learned, and
  saying so is the honest outcome. Fix the connection and re-run.

**Never resubmit a timed-out order manually.** That is how one intended
position becomes two real ones.

## 12. Recovering after a restart

Recovery is automatic and runs on every invocation. Confirm it:

```bash
python scripts/run_ibkr.py --status
```

The banner reports how many orders were recovered and how many were in
flight. Then:

```bash
python scripts/run_ibkr.py --resolve-unknown   # settle any in-flight orders
python scripts/run_ibkr.py --reconcile         # compare against IBKR
```

What recovery restores: the order book, the full state history, the
fills, the idempotency index, the IBKR order-id map, and the
seen-execution set. In-flight orders become `UNKNOWN` rather than being
assumed either way.

Re-running the same intent after a restart produces **no** second order
— it is recognised as a duplicate.

---

## Tracing any order

```bash
python scripts/run_ibkr.py --trace <order_id>
```

Prints the full chain — signal, strategy, portfolio, risk decision,
intent, our order id, client order id, IBKR order id, every state
transition with its reason, and every fill.

## Troubleshooting

| Message | Cause |
|---|---|
| `IBKR CONFIGURATION REFUSED` | `IBKR_ENVIRONMENT` is not `paper`, or TLS verification is off for a non-local host |
| `The Client Portal Gateway is not authenticated` | log in at `https://localhost:5000` |
| `has no resolved IBKR contract` | run `--resolve` for that instrument (§4) |
| `The market for this instrument is closed` | the canonical calendar says no session; add `--as-of YYYY-MM-DD` to evaluate a past session |
| `REFUSED before anything was sent` | the ordering gate is closed (§8) |
| `local request budget ... exhausted` | `IBKR_MAX_REQUESTS_PER_MINUTE` reached; it refuses rather than sleeping |

## What is not covered

Real-money trading. It does not exist in this phase and no procedure
here can enable it.
