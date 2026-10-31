"""
src/research/protected_ledger.py
-------------------------------------------
The single-use record for protected tests (Phase 25.9C, §12).

WHY THE LOCK CANNOT LIVE IN THE DATABASE
--------------------------------------------
Phase 25.9B locked the D20 test with a row in the `experiments` table.
But the prescribed procedure runs the test on a WORKING COPY of the
production database, and a working copy is disposable. Throw it away,
take a fresh snapshot, and the lock is gone -- run, see the result,
change something, rerun, and nothing would object.

So consumption is recorded in `research/protected_tests/ledger.jsonl`,
a git-tracked, append-only file. Deleting a line is visible in git
history, and every entry carries the hash of the one before it, so an
edit or removal breaks the chain and `verify()` refuses.

LIFECYCLE
-------------
  REGISTERED   frozen spec fingerprint, written before any data exists
  OPENING      written BEFORE the statistic is computed
  CONSUMED     written after, with a fingerprint of the result

OPENING before computing matters: a crash between computing and
recording would otherwise leave a result that was seen but never
marked as spent. Once OPENING exists the test is consumed, whether or
not CONSUMED follows.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

DEFAULT_LEDGER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "research", "protected_tests", "ledger.jsonl")

REGISTERED = "REGISTERED"
OPENING = "OPENING"
CONSUMED = "CONSUMED"
SPENDING_STATES = (OPENING, CONSUMED)


class LedgerError(RuntimeError):
    """The ledger is malformed, tampered with, or refuses an action."""


def _digest(entry: Dict[str, object]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def read(path: str = DEFAULT_LEDGER) -> List[Dict[str, object]]:
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError as error:
                raise LedgerError(f"line {number} is not valid JSON: {error}")
    return entries


def verify(path: str = DEFAULT_LEDGER) -> List[Dict[str, object]]:
    """Every entry hashes correctly and links to its predecessor."""
    entries = read(path)
    previous = ""
    for index, entry in enumerate(entries):
        if entry.get("previous_hash", "") != previous:
            raise LedgerError(f"entry {index} does not link to the entry before it; "
                              f"the ledger was edited or a line was removed")
        if entry.get("entry_hash") != _digest(entry):
            raise LedgerError(f"entry {index} does not match its own hash; it was edited")
        previous = entry["entry_hash"]
    return entries


def append(test_id: str, state: str, fields: Dict[str, object],
           path: str = DEFAULT_LEDGER,
           now: Optional[datetime] = None) -> Dict[str, object]:
    entries = verify(path)
    entry = {
        "test_id": test_id,
        "state": state,
        "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
        "previous_hash": entries[-1]["entry_hash"] if entries else "",
        **fields,
    }
    entry["entry_hash"] = _digest(entry)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def status(test_id: str, path: str = DEFAULT_LEDGER) -> Dict[str, object]:
    """REGISTERED / CONSUMED / UNREGISTERED for one test, plus the registration."""
    entries = [e for e in verify(path) if e.get("test_id") == test_id]
    registration = next((e for e in entries if e["state"] == REGISTERED), None)
    spent = [e for e in entries if e["state"] in SPENDING_STATES]
    if registration is None:
        state = "UNREGISTERED"
    elif spent:
        state = "CONSUMED"
    else:
        state = "NOT_CONSUMED"
    return {"state": state, "registration": registration, "spending_entries": spent}


def register(test_id: str, spec_fingerprint: str, fields: Dict[str, object],
             path: str = DEFAULT_LEDGER) -> Dict[str, object]:
    current = status(test_id, path)
    if current["registration"] is not None:
        raise LedgerError(f"{test_id} is already registered; a changed hypothesis "
                          f"needs a new test id, never a re-registration")
    return append(test_id, REGISTERED, {"spec_fingerprint": spec_fingerprint, **fields}, path)


def require_openable(test_id: str, spec_fingerprint: str,
                     path: str = DEFAULT_LEDGER) -> Dict[str, object]:
    """Raise unless the test is registered, unspent, and its spec is unchanged."""
    current = status(test_id, path)
    if current["state"] == "UNREGISTERED":
        raise LedgerError(f"{test_id} has no registration; it cannot be opened")
    if current["state"] == "CONSUMED":
        raise LedgerError(f"{test_id} is CONSUMED; a protected test runs exactly once")
    registered = current["registration"]["spec_fingerprint"]
    if registered != spec_fingerprint:
        raise LedgerError(f"spec fingerprint {spec_fingerprint} differs from the registered "
                          f"{registered}; the hypothesis changed, so this needs a new test id")
    return current
