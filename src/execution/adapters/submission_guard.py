"""
src/execution/adapters/submission_guard.py
----------------------------------------------------
The Phase 25.9E structural stop at the broker boundary.

WHAT IT IS
--------------
A gateway that is the real gateway for every READ -- session, heartbeat,
account, positions, open orders, contracts, quotes, reconciliation --
and that cannot write. `submit_order`, `cancel_order` and `modify_order`
raise `BrokerSubmissionForbidden` carrying a distinctive code, and every
attempt is counted.

WHY A WRAPPER AND NOT A FLAG
--------------------------------
A flag is a convention: every path that submits must remember to read
it. A wrapper is a property of the object the orchestrator holds, so a
path nobody thought of still reaches a method that raises. The loop's
pre-submission mode stops BEFORE calling it; this is the tripwire behind
that stop, and a test that trips it on purpose proves the wire is live.

It never contacts the venue on the write path, so there is nothing to
reconcile afterwards: the exception is raised before any request exists.
"""

from __future__ import annotations

from typing import Any, List

FORBIDDEN_CODE = "PHASE_25_9E_BROKER_SUBMISSION_FORBIDDEN"

#: Venue-writing methods. Anything else is delegated unchanged.
WRITE_METHODS = ("submit_order", "cancel_order", "modify_order")


class BrokerSubmissionForbidden(RuntimeError):
    """A venue write was attempted through the pre-submission gateway."""

    def __init__(self, method: str):
        super().__init__(f"{FORBIDDEN_CODE}: {method} is structurally "
                         f"disabled in Phase 25.9E pre-submission mode")
        self.code = FORBIDDEN_CODE
        self.method = method


class PreSubmissionGateway:
    """Every read of `inner`; no write, ever."""

    #: Read by the loop and by the audit: this gateway cannot submit.
    submission_forbidden = True

    def __init__(self, inner: Any):
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "attempts", [])

    def _refuse(self, method: str):
        self.attempts.append(method)
        raise BrokerSubmissionForbidden(method)

    def submit_order(self, *args, **kwargs):
        self._refuse("submit_order")

    def cancel_order(self, *args, **kwargs):
        self._refuse("cancel_order")

    def modify_order(self, *args, **kwargs):
        self._refuse("modify_order")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Configuration written to the wrapper reaches the real gateway,
        # so a caller swapping a calendar is not silently ignored.
        if name in ("inner", "attempts"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)

    @property
    def attempted_writes(self) -> List[str]:
        return list(self.attempts)
