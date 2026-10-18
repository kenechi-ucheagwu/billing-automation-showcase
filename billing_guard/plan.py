"""Planned changes, and a digest that ties an approval to one exact plan."""

import hashlib
import json
from dataclasses import asdict, dataclass

from .client import RestrictedClient
from .rules import RULES


@dataclass(frozen=True)
class Change:
    """One field edit on one claim, with what the plan expects to find there."""

    claim_id: str
    field: str
    before: str
    after: str
    version: int
    rule: str


def build_plan(client: RestrictedClient) -> list[Change]:
    """Run every rule over the claims waiting for review. Reads only."""
    changes = []
    for claim in sorted(client.list_claims("needs_review"), key=lambda c: c["claim_id"]):
        patient = client.get_patient(claim["patient_id"])
        for rule in RULES:
            edit = rule(claim, patient)
            if edit:
                field, after = edit
                changes.append(Change(claim["claim_id"], field, claim[field], after,
                                      claim["version"], rule.__name__))
    return changes


def digest(plan: list[Change]) -> str:
    """SHA-256 of the plan in a canonical JSON form."""
    raw = json.dumps([asdict(c) for c in plan], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
