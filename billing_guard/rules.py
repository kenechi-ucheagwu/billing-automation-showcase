"""Rules that spot a held claim and say which field to change.

A rule only proposes an edit. Whether and when it is applied is the runner's
decision. To add a rule, write a function with the same signature and add it
to RULES.
"""

from typing import Any, Callable

Edit = tuple[str, str] | None
Rule = Callable[[dict[str, Any], dict[str, Any]], Edit]


def stale_member_id(claim: dict[str, Any], patient: dict[str, Any]) -> Edit:
    """The claim still carries a member ID from coverage that is no longer active."""
    active = [coverage for coverage in patient["coverages"] if coverage["active"]]
    if len(active) != 1:
        # Zero or two active plans needs a person to sort out, not a script.
        raise ValueError("expected exactly one active coverage")
    member_id = active[0]["member_id"]
    return ("member_id", member_id) if claim["member_id"] != member_id else None


def missing_rendering_provider(claim: dict[str, Any], patient: dict[str, Any]) -> Edit:
    """The rendering provider is blank, but the encounter says who saw the patient."""
    if not claim["rendering_provider_id"] and claim["encounter_provider_id"]:
        return "rendering_provider_id", claim["encounter_provider_id"]
    return None


RULES: list[Rule] = [stale_member_id, missing_rendering_provider]
