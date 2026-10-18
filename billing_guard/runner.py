"""Plan, then apply changes in a small first batch, an approval and a resume.

The audit log decides what a run is allowed to do next. The run file holds
only the plan with its raw values, so editing it can't grant permission to
write, and a changed plan no longer matches the digest the log recorded. A
run that was interrupted can't be resumed; plan again instead.
"""

import json
import re
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .breaker import Breaker
from .client import RestrictedClient
from .mock_ehr import APIError, ConflictError, NotEditableError, NotFoundError
from .plan import Change, build_plan, digest
from .rules import RULES

AUDIT_FILE = "audit.jsonl"
RUNS_DIR = "runs"
RUN_ID = re.compile(r"run-[a-f0-9]{12}")
# Stands in for a run ID that was typed wrong. What was typed is never echoed
# or logged, since a pasted value could be anything, including patient data.
INVALID_RUN_ID = "run-invalid"


class ItemStatus(str, Enum):
    PLANNED = "PLANNED"
    APPLIED_VERIFIED = "APPLIED_VERIFIED"
    VERIFY_FAILED = "VERIFY_FAILED"
    WRITE_FAILED = "WRITE_FAILED"
    READ_FAILED = "READ_FAILED"
    UNEXPECTED_STATE = "UNEXPECTED_STATE"
    SKIPPED_BREAKER_OPEN = "SKIPPED_BREAKER_OPEN"
    NOT_STARTED = "NOT_STARTED"


class RunStatus(str, Enum):
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    FIRST_BATCH_FAILED = "FIRST_BATCH_FAILED"
    STOPPED_BY_BREAKER = "STOPPED_BY_BREAKER"
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_FAILURES = "COMPLETE_WITH_FAILURES"
    REFUSED = "REFUSED"


FAILURES = {ItemStatus.VERIFY_FAILED, ItemStatus.WRITE_FAILED,
            ItemStatus.READ_FAILED, ItemStatus.UNEXPECTED_STATE}
NOT_APPLIED = {ItemStatus.PLANNED, ItemStatus.NOT_STARTED, ItemStatus.SKIPPED_BREAKER_OPEN}


@dataclass
class Result:
    run_id: str
    status: RunStatus
    plan: list[Change]
    items: list[ItemStatus]
    reason: str = ""

    @property
    def claims(self) -> int:
        return len({change.claim_id for change in self.plan})

    @property
    def fixed(self) -> int:
        """A claim counts as fixed only when every planned edit on it is verified."""
        claims = {change.claim_id for change in self.plan}
        return sum(all(status == ItemStatus.APPLIED_VERIFIED
                       for change, status in zip(self.plan, self.items)
                       if change.claim_id == claim_id) for claim_id in claims)

    @property
    def verified(self) -> int:
        return self.items.count(ItemStatus.APPLIED_VERIFIED)

    @property
    def failures(self) -> int:
        return sum(status in FAILURES for status in self.items)

    @property
    def remaining(self) -> int:
        return sum(status in NOT_APPLIED for status in self.items)


class Runner:
    def __init__(self, client: RestrictedClient, sandbox: Path, *, first_batch: int = 3,
                 consecutive_limit: int = 3, total_limit: int = 5, plan_cap: int = 50):
        if type(client) is not RestrictedClient:
            raise TypeError("runner requires a RestrictedClient")
        if first_batch not in (3, 4, 5):
            raise ValueError("first batch must be within 3..5")
        if plan_cap < 1:
            raise ValueError("plan cap must be positive")
        Breaker(consecutive_limit, total_limit)  # validates the limits
        self.client = client
        self.sandbox = Path(sandbox)
        self.first_batch = first_batch
        self.consecutive_limit = consecutive_limit
        self.total_limit = total_limit
        self.plan_cap = plan_cap
        self.audit = AuditLog(self.sandbox / AUDIT_FILE)
        (self.sandbox / RUNS_DIR).mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("unknown run id")
        return self.sandbox / RUNS_DIR / (run_id + ".json")

    def _load(self, run_id: str) -> tuple[list[Change], list[dict[str, Any]]]:
        path = self._path(run_id)
        if not path.exists():
            raise ValueError("unknown run id")
        stored = json.loads(path.read_text(encoding="ascii"))["plan"]
        plan = [Change(**item) for item in stored]
        events = [e for e in self.audit.events() if e["run_id"] == run_id]
        if not events or events[0]["event"] != "run_started":
            raise ValueError("run has no audited start")
        if digest(plan) != events[0]["plan_digest"]:
            raise ValueError("stored plan digest mismatch")
        return plan, events

    def _refuse(self, run_id: str, reason: str, plan: list[Change] | None = None) -> Result:
        # A damaged chain can't safely be appended to, so leave it as evidence.
        try:
            self.audit.append(run_id, "operation_refused", reason=reason)
        except ValueError:
            pass
        changes = plan or []
        return Result(run_id, RunStatus.REFUSED, changes,
                      [ItemStatus.NOT_STARTED] * len(changes), reason)

    def run(self, *, live: bool = False) -> Result:
        """Build and audit a plan. With live=True, apply the first batch and stop."""
        run_id = "run-" + uuid.uuid4().hex[:12]
        try:
            self.audit.events()
            plan = build_plan(self.client)
        except (APIError, ValueError) as exc:
            # API exception text could quote patient data, so log only the type.
            return self._refuse(run_id, "plan read or validation failed: " + type(exc).__name__)
        self._path(run_id).write_text(
            json.dumps({"plan": [asdict(c) for c in plan]}, indent=2) + "\n", encoding="ascii")
        self.audit.append(run_id, "run_started", mode="live" if live else "dry",
                          rules=[r.__name__ for r in RULES], plan_size=len(plan),
                          plan_digest=digest(plan), first_batch=self.first_batch,
                          consecutive_limit=self.consecutive_limit,
                          total_limit=self.total_limit, plan_cap=self.plan_cap)
        for index, change in enumerate(plan):
            self.audit.append(run_id, "planned_item", index=index, **asdict(change))
        result = Result(run_id, RunStatus.DRY_RUN_COMPLETE, plan,
                        [ItemStatus.PLANNED] * len(plan))
        if len(plan) > self.plan_cap:
            # A sudden huge plan usually means a broken rule. Write nothing.
            result.status = RunStatus.REFUSED
            result.reason = "plan size exceeds cap"
            result.items = [ItemStatus.SKIPPED_BREAKER_OPEN] * len(plan)
            self.audit.append(run_id, "breaker_trip", reason=result.reason)
        elif live and plan:
            result.items = [ItemStatus.NOT_STARTED] * len(plan)
            self._apply(result, 0, min(self.first_batch, len(plan)),
                        Breaker(self.consecutive_limit, self.total_limit))
            self.audit.append(run_id, "first_batch_complete", failures=result.failures,
                              verified=result.verified)
            if result.status == RunStatus.STOPPED_BY_BREAKER:
                pass
            elif result.failures:
                result.status = RunStatus.FIRST_BATCH_FAILED
                result.reason = "the first batch had failures, so this run can't be approved"
            else:
                result.status = RunStatus.AWAITING_APPROVAL
        elif live:
            result.status = RunStatus.COMPLETE
        self._finish(result)
        return result

    @staticmethod
    def _clean_first_batch(event: dict[str, Any]) -> bool:
        return (event["event"] == "run_finished"
                and event["status"] == RunStatus.AWAITING_APPROVAL
                and event["failures"] == 0)

    def approve(self, run_id: str, approved_by: str) -> Result:
        """Record a named person's OK for a run whose first batch is clean."""
        if not RUN_ID.fullmatch(run_id):
            return self._refuse(INVALID_RUN_ID, "not a run ID")
        try:
            plan, events = self._load(run_id)
            if not approved_by.strip():
                raise ValueError("approver name is required")
            meaningful = [e for e in events if e["event"] != "operation_refused"]
            if not self._clean_first_batch(meaningful[-1]):
                raise ValueError("run is not awaiting approval with a clean first batch")
            if any(e["event"] == "approval" for e in events):
                raise ValueError("run already approved")
            self.audit.append(run_id, "approval", approved_by=approved_by.strip(),
                              plan_digest=events[0]["plan_digest"])
            return Result(run_id, RunStatus.APPROVED, plan, self._items(plan, events))
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "invalid run file"
            return self._refuse(run_id, reason)

    @staticmethod
    def _items(plan: list[Change], events: list[dict[str, Any]]) -> list[ItemStatus]:
        items = [ItemStatus.NOT_STARTED] * len(plan)
        for event in events:
            if event["event"] == "item_result":
                items[event["index"]] = ItemStatus(event["status"])
        return items

    def resume(self, run_id: str) -> Result:
        """Apply the rest of an approved run, with the same checks and breakers."""
        if not RUN_ID.fullmatch(run_id):
            return self._refuse(INVALID_RUN_ID, "not a run ID")
        try:
            plan, events = self._load(run_id)
            meaningful = [e for e in events if e["event"] != "operation_refused"]
            if meaningful[-1]["event"] != "approval":
                raise ValueError("resume needs an approved run that has not resumed yet")
            approval = meaningful[-1]
            # The approval has to name someone and directly follow a clean first
            # batch. A last line rewritten to look like an approval fails this.
            if (len(meaningful) < 2 or not self._clean_first_batch(meaningful[-2])
                    or not isinstance(approval.get("approved_by"), str)
                    or not approval["approved_by"].strip()
                    or [e["event"] for e in events].count("approval") != 1):
                raise ValueError("approval does not follow a clean first batch")
            if approval["plan_digest"] != digest(plan):
                raise ValueError("approval plan digest mismatch")
            start = events[0]
            items = self._items(plan, events)
            count = min(start["first_batch"], len(plan))
            if items[:count] != [ItemStatus.APPLIED_VERIFIED] * count:
                raise ValueError("first batch is not clean")
            result = Result(run_id, RunStatus.COMPLETE, plan, items)
            self.audit.append(run_id, "resume", remaining=len(plan) - count)
            self._apply(result, count, len(plan),
                        Breaker(start["consecutive_limit"], start["total_limit"]))
            if result.status == RunStatus.COMPLETE and result.failures:
                result.status = RunStatus.COMPLETE_WITH_FAILURES
            self._finish(result)
            return result
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "invalid run file"
            return self._refuse(run_id, reason)

    def _finish(self, result: Result) -> None:
        self.audit.append(result.run_id, "run_finished", status=result.status.value,
                          verified=result.verified, fixed_claims=result.fixed,
                          failures=result.failures, remaining=result.remaining,
                          reason=result.reason)

    def _apply(self, result: Result, start: int, end: int, breaker: Breaker) -> None:
        for index in range(start, end):
            change = result.plan[index]
            # Earlier verified edits on this claim legitimately advanced its version.
            offset = sum(c.claim_id == change.claim_id and s == ItemStatus.APPLIED_VERIFIED
                         for c, s in zip(result.plan[:index], result.items[:index]))
            status, reason = self._attempt(result.run_id, index, change, change.version + offset)
            result.items[index] = status
            self.audit.append(result.run_id, "item_result", index=index, status=status.value)
            trip = breaker.record(status == ItemStatus.APPLIED_VERIFIED)
            if status == ItemStatus.UNEXPECTED_STATE:
                # The tool's picture of the system is wrong. Stop on the first one.
                trip = reason
            if trip:
                result.status = RunStatus.STOPPED_BY_BREAKER
                result.reason = trip
                for remaining in range(index + 1, len(result.items)):
                    result.items[remaining] = ItemStatus.SKIPPED_BREAKER_OPEN
                    self.audit.append(result.run_id, "item_result", index=remaining,
                                      status=ItemStatus.SKIPPED_BREAKER_OPEN.value)
                self.audit.append(result.run_id, "breaker_trip", reason=trip,
                                  consecutive_failures=breaker.streak,
                                  total_failures=breaker.total)
                return

    def _attempt(self, run_id: str, index: int, change: Change,
                 version: int) -> tuple[ItemStatus, str]:
        """Re-read the claim, write one field, then read it back to confirm."""
        try:
            current = self.client.get_claim(change.claim_id)
        except NotFoundError:
            self._log_check(run_id, "pre_write_check", index, "claim missing")
            return ItemStatus.UNEXPECTED_STATE, "claim missing"
        except APIError:
            self._log_check(run_id, "pre_write_check", index, "API read error")
            return ItemStatus.READ_FAILED, "API read error"
        if current["status"] != "needs_review":
            reason = "claim is no longer editable"
        elif current["version"] != version or current[change.field] != change.before:
            reason = "claim changed since planning"
        else:
            reason = ""
        self._log_check(run_id, "pre_write_check", index, reason)
        if reason:
            return ItemStatus.UNEXPECTED_STATE, reason

        # Record the intent before writing, so a crash mid-write leaves a trace.
        self.audit.append(run_id, "write_started", index=index, claim_id=change.claim_id,
                          field=change.field, value=change.after, expected_version=version)
        try:
            new_version = self.client.update_claim(change.claim_id, change.field,
                                                   change.after, version)
        except (ConflictError, NotEditableError, NotFoundError):
            self._log_check(run_id, "write_result", index, "unexpected state")
            return ItemStatus.UNEXPECTED_STATE, "claim changed during write"
        except APIError:
            self._log_check(run_id, "write_result", index, "API write error")
            return ItemStatus.WRITE_FAILED, "API write error"
        self.audit.append(run_id, "write_result", index=index, ok=True, version=new_version)

        try:
            actual = self.client.get_claim(change.claim_id)
        except NotFoundError:
            self._log_check(run_id, "verification", index, "claim missing")
            return ItemStatus.UNEXPECTED_STATE, "claim missing after write"
        except APIError:
            self._log_check(run_id, "verification", index, "API read error")
            return ItemStatus.READ_FAILED, "API read error after write"
        if actual["status"] != "needs_review":
            self._log_check(run_id, "verification", index, "claim is no longer editable")
            return ItemStatus.UNEXPECTED_STATE, "claim is no longer editable"
        if actual["version"] > version + 1:
            # Someone else edited the claim after the write landed. That is an
            # unexpected state, not a failed save, so it stops the run.
            self._log_check(run_id, "verification", index, "claim changed after write")
            return ItemStatus.UNEXPECTED_STATE, "claim changed after write"
        landed = (actual[change.field] == change.after
                  and actual["version"] == new_version == version + 1)
        self._log_check(run_id, "verification", index,
                        "" if landed else "saved value or version mismatch")
        if landed:
            return ItemStatus.APPLIED_VERIFIED, ""
        return ItemStatus.VERIFY_FAILED, "verification mismatch"

    def _log_check(self, run_id: str, event: str, index: int, problem: str) -> None:
        self.audit.append(run_id, event, index=index, ok=not problem, reason=problem)
