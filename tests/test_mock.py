import os

import pytest

from billing_guard.mock_ehr import (ConflictError, MockEHR, NotEditableError, NotFoundError,
                                    TransientError)
from billing_guard.synthetic import generate

GOOD = generate(claims=1)


def test_copy_isolation(ehr):
    listed = ehr.list_claims()
    claim = ehr.get_claim(listed[0]["claim_id"])
    patient = ehr.get_patient(claim["patient_id"])
    listed[0]["version"] = 99
    claim["version"] = 88
    patient["coverages"].clear()
    assert ehr.get_claim(claim["claim_id"])["version"] == 1
    assert ehr.get_patient(claim["patient_id"])["coverages"]


def test_optimistic_write_and_allowlist(ehr):
    claim = ehr.list_claims()[0]
    assert ehr.update_claim(claim["claim_id"], "rendering_provider_id", "PRV-03", 1) == 2
    with pytest.raises(ConflictError):
        ehr.update_claim(claim["claim_id"], "rendering_provider_id", "PRV-03", 1)
    with pytest.raises(ValueError):
        ehr.update_claim(claim["claim_id"], "status", "submitted", 2)
    ehr.set_status(claim["claim_id"], "locked")
    with pytest.raises(NotEditableError):
        ehr.update_claim(claim["claim_id"], "rendering_provider_id", "PRV-03", 3)


def test_missing_and_fault_hooks(ehr):
    with pytest.raises(NotFoundError):
        ehr.get_claim("unknown")
    with pytest.raises(NotFoundError):
        ehr.get_patient("unknown")
    with pytest.raises(NotFoundError):
        ehr.update_claim("unknown", "member_id", "", 1)
    ehr.fail_next_reads(1)
    with pytest.raises(TransientError):
        ehr.list_claims()
    claim = ehr.list_claims()[0]
    ehr.fail_next_writes(1)
    with pytest.raises(TransientError):
        ehr.update_claim(claim["claim_id"], "rendering_provider_id", "PRV-03", 1)
    assert ehr.get_claim(claim["claim_id"])["version"] == 1


def test_fail_writes_after_lets_some_through(ehr):
    ids = [claim["claim_id"] for claim in ehr.list_claims()[:3]]
    ehr.fail_writes_after(2, count=1)
    ehr.update_claim(ids[0], "rendering_provider_id", "PRV-03", 1)
    ehr.update_claim(ids[1], "rendering_provider_id", "PRV-03", 1)
    with pytest.raises(TransientError):
        ehr.update_claim(ids[2], "rendering_provider_id", "PRV-03", 1)
    assert ehr.update_claim(ids[2], "rendering_provider_id", "PRV-03", 1) == 2


def test_persistence(ehr, tmp_path):
    path = tmp_path / "ehr.json"
    ehr.fail_next_writes(2)
    ehr.save(path)
    loaded = MockEHR.load(path)
    assert loaded.snapshot() == ehr.snapshot()
    assert loaded.writes == 0


def test_human_operation_only_moves_ready_claims(ehr):
    claims = ehr.list_claims()[:2]
    ehr.set_status(claims[0]["claim_id"], "ready_to_submit")
    getattr(ehr, "submit")([c["claim_id"] for c in claims])
    assert ehr.get_claim(claims[0]["claim_id"])["status"] == "submitted"
    assert ehr.get_claim(claims[1]["claim_id"])["status"] == "needs_review"
    assert ehr.submits == 1


def test_file_backed_mock_saves_each_write_before_returning(ehr, tmp_path):
    path = tmp_path / "ehr.json"
    ehr.save(path)
    live = MockEHR.load(path)
    claim_id = live.list_claims()[0]["claim_id"]
    live.update_claim(claim_id, "rendering_provider_id", "PRV-03", 1)
    assert MockEHR.load(path).get_claim(claim_id)["rendering_provider_id"] == "PRV-03"


def test_failed_save_undoes_the_write(ehr, tmp_path, monkeypatch):
    path = tmp_path / "ehr.json"
    ehr.save(path)
    raw = path.read_bytes()
    live = MockEHR.load(path)
    claim_id = live.list_claims()[0]["claim_id"]

    def broken(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(MockEHR, "_write", broken)
    with pytest.raises(TransientError):
        live.update_claim(claim_id, "rendering_provider_id", "PRV-03", 1)
    assert live.get_claim(claim_id)["version"] == 1
    assert path.read_bytes() == raw


@pytest.mark.parametrize("data", [
    [],
    {"patients": None, "claims": []},
    {"patients": [], "claims": [{"claim_id": 5}]},
    {"patients": [{"patient_id": "PT-0001", "coverages": "none"}], "claims": []},
    {**GOOD, "claims": [{**GOOD["claims"][0], "version": True}]},
    {**GOOD, "claims": [{**GOOD["claims"][0], "status": "paid"}]},
    {**GOOD, "faults": {"read_errors": "1"}},
    {**GOOD, "faults": {"unknown": 1}},
])
def test_malformed_data_is_rejected(data):
    with pytest.raises(ValueError, match="malformed"):
        MockEHR(data)


def test_failed_replace_rolls_back_fault_counters_and_cleans_up(ehr, tmp_path, monkeypatch):
    path = tmp_path / "ehr.json"
    ehr.fail_next_reads(1)
    ehr.fail_next_writes(1)
    ehr.save(path)
    live = MockEHR.load(path)
    claim_id = next(iter(ehr.claims))
    real_replace = os.replace

    def broken(source, target):
        if str(source).endswith(".tmp"):
            raise PermissionError("file in use")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(TransientError, match="storage error"):
        live.list_claims()
    with pytest.raises(TransientError, match="storage error"):
        live.update_claim(claim_id, "rendering_provider_id", "PRV-03", 1)
    monkeypatch.undo()
    assert live.snapshot() == MockEHR.load(path).snapshot()
    assert not (tmp_path / "ehr.json.tmp").exists()
