from billing_guard.client import RestrictedClient
from billing_guard.plan import build_plan, digest
from billing_guard.rules import missing_rendering_provider, stale_member_id


def test_rules_match_runtime_coverage_and_provider(ehr):
    claim = ehr.list_claims()[0]
    patient = ehr.get_patient(claim["patient_id"])
    active = next(c for c in patient["coverages"] if c["active"])
    assert stale_member_id(claim, patient) == ("member_id", active["member_id"])
    expected = ("rendering_provider_id", claim["encounter_provider_id"])
    assert missing_rendering_provider(claim, patient) == expected
    claim["member_id"] = active["member_id"]
    claim["rendering_provider_id"] = claim["encounter_provider_id"]
    assert stale_member_id(claim, patient) is None
    assert missing_rendering_provider(claim, patient) is None


def test_no_provider_cannot_be_invented(ehr):
    claim = ehr.list_claims()[0]
    claim["encounter_provider_id"] = ""
    assert missing_rendering_provider(claim, {}) is None


def test_only_review_claims_and_stable_digest(ehr):
    client = RestrictedClient(ehr)
    original = build_plan(client)
    assert 8 <= len(original) <= 15
    assert digest(original) == digest(build_plan(client))
    target = original[0].claim_id
    ehr.set_status(target, "draft")
    updated = build_plan(client)
    assert all(c.claim_id != target for c in updated)
    assert digest(original) != digest(updated)
