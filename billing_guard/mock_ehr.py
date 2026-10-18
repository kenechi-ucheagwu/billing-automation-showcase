"""A stand-in for an EHR claims API, with faults you can switch on.

Reads return copies, writes use a version number the caller must match, and
the fault hooks reproduce what goes wrong with real systems: calls that fail,
saves that report success but keep the old value, and claims that someone
else edits while a batch is running.

A mock loaded from a file writes every change back to that file before the
call returns, the way a real API's write is saved once it answers. If that
save fails, the change is undone and the call raises.
"""

import json
import os
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any


class APIError(Exception):
    """An API operation did not complete normally."""


class TransientError(APIError):
    """A call failed in a way that might work on retry."""


class ConflictError(APIError):
    """The claim's version no longer matches the version the caller sent."""


class NotEditableError(APIError):
    """The claim's status no longer allows edits."""


class NotFoundError(APIError):
    """The claim or patient does not exist."""


EDITABLE_FIELDS = frozenset({"member_id", "rendering_provider_id"})
STATUSES = frozenset({"draft", "needs_review", "ready_to_submit", "submitted", "locked"})


def no_faults() -> dict[str, Any]:
    return {"read_errors": 0, "write_errors": 0, "silent_drop": [], "fail_after": None,
            "writes_seen": 0, "drift_on_read": None, "drift_after_write": None}


def _check(condition: bool) -> None:
    if not condition:
        raise ValueError("mock EHR data is malformed")


def _strings(record: dict[str, Any], *keys: str) -> bool:
    return all(isinstance(record.get(key), str) for key in keys)


def validate(data: Any) -> None:
    """Check the shape of mock EHR data, so a damaged file fails before any call."""
    _check(isinstance(data, dict))
    patients, claims = data.get("patients"), data.get("claims")
    _check(isinstance(patients, list) and isinstance(claims, list))
    for patient in patients:
        _check(isinstance(patient, dict) and _strings(patient, "patient_id")
               and isinstance(patient.get("coverages"), list))
        for coverage in patient["coverages"]:
            _check(isinstance(coverage, dict) and _strings(coverage, "member_id")
                   and isinstance(coverage.get("active"), bool))
    for claim in claims:
        _check(isinstance(claim, dict)
               and _strings(claim, "claim_id", "patient_id", "status", "member_id",
                            "rendering_provider_id", "encounter_provider_id")
               and claim["status"] in STATUSES and type(claim.get("version")) is int)
    faults = data.get("faults", {})
    _check(isinstance(faults, dict) and set(faults) <= set(no_faults()))
    merged = {**no_faults(), **faults}
    _check(all(type(merged[key]) is int for key in ("read_errors", "write_errors", "writes_seen")))
    _check(merged["fail_after"] is None or type(merged["fail_after"]) is int)
    _check(isinstance(merged["silent_drop"], list)
           and all(isinstance(claim_id, str) for claim_id in merged["silent_drop"]))
    _check(all(merged[key] is None or isinstance(merged[key], str)
               for key in ("drift_on_read", "drift_after_write")))


class MockEHR:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        validate(data)
        self._set_state(data)
        self.path = path
        self.reads = self.writes = self.submits = 0

    def _set_state(self, data: dict[str, Any]) -> None:
        self.patients = {p["patient_id"]: deepcopy(p) for p in data["patients"]}
        self.claims = {c["claim_id"]: deepcopy(c) for c in data["claims"]}
        self.faults = {**no_faults(), **deepcopy(data.get("faults", {}))}

    def _commit(self, rollback: dict[str, Any]) -> None:
        """Save the state to the backing file, if there is one, before a call returns.

        `rollback` is the state from before the call. If the save fails, memory
        goes back to it, so memory and file never disagree.
        """
        if self.path is None:
            return
        try:
            self._write(self.path)
        except OSError:
            self._set_state(rollback)
            raise TransientError("storage error") from None

    def _read(self) -> None:
        self.reads += 1
        if self.faults["read_errors"]:
            before = self.snapshot()
            self.faults["read_errors"] -= 1
            self._commit(before)
            raise TransientError("injected read error")

    def list_claims(self, status: str | None = None) -> list[dict[str, Any]]:
        self._read()
        return deepcopy([claim for claim in self.claims.values()
                         if status is None or claim["status"] == status])

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        self._read()
        if self.faults["drift_on_read"] == claim_id:
            before = self.snapshot()
            self.faults["drift_on_read"] = None
            self.edit_claim(claim_id, "rendering_provider_id", "PRV-99")
            self._commit(before)
        if claim_id not in self.claims:
            raise NotFoundError("claim missing")
        return deepcopy(self.claims[claim_id])

    def get_patient(self, patient_id: str) -> dict[str, Any]:
        self._read()
        if patient_id not in self.patients:
            raise NotFoundError("patient missing")
        return deepcopy(self.patients[patient_id])

    def update_claim(self, claim_id: str, field: str, value: str, expected_version: int) -> int:
        """Set one field and return the new version.

        A claim listed in the silent-drop fault reports success and keeps the
        old value, the way a page can show "saved" when nothing was saved.
        """
        self.writes += 1
        if field not in EDITABLE_FIELDS:
            raise ValueError("field is not allowed")
        if claim_id not in self.claims:
            raise NotFoundError("claim missing")
        claim = self.claims[claim_id]
        if claim["status"] != "needs_review":
            raise NotEditableError("claim is not editable")
        if claim["version"] != expected_version:
            raise ConflictError("claim version changed")
        before = self.snapshot()
        seen = self.faults["writes_seen"]
        self.faults["writes_seen"] += 1
        after = self.faults["fail_after"]
        if self.faults["write_errors"] and (after is None or seen >= after):
            self.faults["write_errors"] -= 1
            self._commit(before)
            raise TransientError("injected write error")
        if claim_id not in self.faults["silent_drop"]:
            claim[field] = value
            claim["version"] += 1
        if self.faults["drift_after_write"] == claim_id:
            self.faults["drift_after_write"] = None
            self.edit_claim(claim_id, "rendering_provider_id", "PRV-99")
        self._commit(before)
        return expected_version + 1

    def submit(self, claim_ids: list[str]) -> None:
        """Send ready claims to the payer. This is the biller's button, not the tool's."""
        before = self.snapshot()
        self.submits += 1
        for claim_id in claim_ids:
            claim = self.claims[claim_id]
            if claim["status"] == "ready_to_submit":
                claim["status"] = "submitted"
                claim["version"] += 1
        self._commit(before)

    # Test and scenario hooks. The runner never sees these.

    def edit_claim(self, claim_id: str, field: str, value: Any) -> None:
        """Change a claim as another user would, bumping its version."""
        self.claims[claim_id][field] = value
        self.claims[claim_id]["version"] += 1

    def set_status(self, claim_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError("unknown status")
        self.edit_claim(claim_id, "status", status)

    def fail_next_reads(self, count: int) -> None:
        self.faults["read_errors"] = count

    def fail_next_writes(self, count: int) -> None:
        self.faults["fail_after"] = None
        self.faults["write_errors"] = count

    def fail_writes_after(self, after: int, count: int) -> None:
        """Let `after` writes through, then fail the next `count`."""
        self.faults["fail_after"] = after
        self.faults["write_errors"] = count

    def drop_writes(self, claim_id: str) -> None:
        self.faults["silent_drop"].append(claim_id)

    def drift_on_next_read(self, claim_id: str) -> None:
        """Have another user edit this claim just before the tool next reads it."""
        self.faults["drift_on_read"] = claim_id

    def drift_after_next_write(self, claim_id: str) -> None:
        """Have another user edit this claim right after the tool's next write to it."""
        self.faults["drift_after_write"] = claim_id

    def snapshot(self) -> dict[str, Any]:
        return deepcopy({"patients": list(self.patients.values()),
                         "claims": list(self.claims.values()), "faults": self.faults})

    def _write(self, path: Path) -> None:
        # Write a temporary file and swap it in, so a crash never leaves half a file.
        temp = path.with_name(path.name + ".tmp")
        try:
            temp.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="ascii")
            os.replace(temp, path)
        except OSError:
            with suppress(OSError):
                temp.unlink(missing_ok=True)
            raise

    def save(self, path: Path) -> None:
        self._write(path)

    @classmethod
    def load(cls, path: Path, *, save_changes: bool = True) -> "MockEHR":
        """Load a mock that saves every change back to `path`.

        With save_changes=False the mock works on a copy in memory and never
        touches the file, which is what a dry run needs.
        """
        data = json.loads(path.read_text(encoding="ascii"))
        return cls(data, path=path if save_changes else None)
