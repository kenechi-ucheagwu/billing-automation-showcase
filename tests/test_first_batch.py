import json

import pytest

from billing_guard.audit import verify
from billing_guard.client import RestrictedClient
from billing_guard.mock_ehr import MockEHR
from billing_guard.runner import ItemStatus, Runner, RunStatus


@pytest.mark.parametrize("size", [3, 4, 5])
def test_exact_first_batch_then_approval_and_resume(ehr, tmp_path, size):
    runner = Runner(RestrictedClient(ehr), tmp_path, first_batch=size)
    first = runner.run(live=True)
    assert first.status == RunStatus.AWAITING_APPROVAL
    assert first.verified == size == ehr.writes
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert ehr.writes == size
    assert runner.approve(first.run_id, "Operator").status == RunStatus.APPROVED
    complete = runner.resume(first.run_id)
    assert complete.status == RunStatus.COMPLETE
    assert all(s == ItemStatus.APPLIED_VERIFIED for s in complete.items)
    assert ehr.writes == len(complete.plan)
    assert ehr.submits == 0


@pytest.mark.parametrize("size", [2, 6])
def test_invalid_batch_size(ehr, tmp_path, size):
    with pytest.raises(ValueError, match="3..5"):
        Runner(RestrictedClient(ehr), tmp_path, first_batch=size)
    assert ehr.writes == 0


def test_repeated_and_unusual_orders_never_grant_extra_writes(ehr, runner):
    dry = runner.run()
    assert runner.approve(dry.run_id, "Operator").status == RunStatus.REFUSED
    assert runner.resume(dry.run_id).status == RunStatus.REFUSED
    first = runner.run(live=True)
    for _ in range(4):
        assert runner.resume(first.run_id).status == RunStatus.REFUSED
        assert runner.approve(first.run_id, " ").status == RunStatus.REFUSED
        assert ehr.writes == 3
    runner.approve(first.run_id, "Operator")
    assert runner.approve(first.run_id, "Operator").status == RunStatus.REFUSED
    runner.resume(first.run_id)
    writes = ehr.writes
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert runner.approve(first.run_id, "Operator").status == RunStatus.REFUSED
    assert ehr.writes == writes
    events = runner.audit.events()
    run_events = [e for e in events if e["run_id"] == first.run_id]
    approval = next(i for i, e in enumerate(run_events) if e["event"] == "approval")
    assert sum(e["event"] == "write_started" for e in run_events[:approval]) == 3


def test_unknown_run_and_path_traversal_refused(runner):
    for run_id in ("run-" + "a" * 12, "../escape"):
        assert runner.approve(run_id, "Operator").status == RunStatus.REFUSED
        assert runner.resume(run_id).status == RunStatus.REFUSED


def test_failed_first_batch_cannot_be_approved_or_resumed(ehr, runner):
    ehr.fail_next_writes(1)
    first = runner.run(live=True)
    assert first.status == RunStatus.FIRST_BATCH_FAILED
    assert first.failures == 1
    assert runner.approve(first.run_id, "Operator").status == RunStatus.REFUSED
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert ehr.writes == 3


def test_small_plan_still_requires_approval(ehr, runner):
    for claim in ehr.list_claims()[1:]:
        ehr.set_status(claim["claim_id"], "draft")
    first = runner.run(live=True)
    assert first.status == RunStatus.AWAITING_APPROVAL
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    runner.approve(first.run_id, "Operator")
    assert runner.resume(first.run_id).status == RunStatus.COMPLETE


def test_no_work_completes_without_writes(ehr, runner):
    for claim in ehr.list_claims():
        ehr.set_status(claim["claim_id"], "draft")
    assert runner.run(live=True).status == RunStatus.COMPLETE
    assert ehr.writes == 0


def test_plan_edit_after_approval_refuses_resume(ehr, runner, tmp_path):
    result = runner.run(live=True)
    runner.approve(result.run_id, "Operator")
    path = tmp_path / "runs" / (result.run_id + ".json")
    stored = json.loads(path.read_text())
    stored["plan"][-1]["after"] = "PRV-99"
    path.write_text(json.dumps(stored), encoding="ascii")
    refused = runner.resume(result.run_id)
    assert refused.status == RunStatus.REFUSED
    assert "digest mismatch" in refused.reason
    assert ehr.writes == 3


def test_fake_state_flags_do_not_authorize_writes(ehr, runner, tmp_path):
    result = runner.run(live=True)
    path = tmp_path / "runs" / (result.run_id + ".json")
    stored = json.loads(path.read_text())
    stored.update(status="approved", approved_by="Operator", next_index=0)
    path.write_text(json.dumps(stored), encoding="ascii")
    assert runner.resume(result.run_id).status == RunStatus.REFUSED
    assert ehr.writes == 3


def test_interrupted_resume_cannot_repeat_writes(ehr, runner):
    result = runner.run(live=True)
    runner.approve(result.run_id, "Operator")
    runner.audit.append(result.run_id, "resume", remaining=9)
    assert runner.resume(result.run_id).status == RunStatus.REFUSED
    assert ehr.writes == 3


def test_save_failure_leaves_nothing_verified_or_approvable(ehr, tmp_path, monkeypatch):
    path = tmp_path / "ehr.json"
    ehr.save(path)
    raw = path.read_bytes()
    live = MockEHR.load(path)
    runner = Runner(RestrictedClient(live), tmp_path)

    def broken(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(MockEHR, "_write", broken)
    first = runner.run(live=True)
    assert first.verified == 0
    assert first.status == RunStatus.STOPPED_BY_BREAKER
    assert runner.approve(first.run_id, "Operator").status == RunStatus.REFUSED
    assert path.read_bytes() == raw


def plan_digest(runner, run_id):
    return next(e["plan_digest"] for e in runner.audit.events()
                if e["run_id"] == run_id and e["event"] == "run_started")


def test_last_line_rewritten_as_approval_is_refused(ehr, runner, tmp_path):
    first = runner.run(live=True)
    path = tmp_path / "audit.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    event = json.loads(lines[-1])
    event.update(event="approval", approved_by="Operator",
                 plan_digest=plan_digest(runner, first.run_id))
    lines[-1] = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    path.write_bytes(b"".join(lines))
    # The chain can't see a change to its own last line; the approval check can.
    assert verify(path).ok
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert ehr.writes == 3


@pytest.mark.parametrize("failed_first_batch,approved_by", [(False, None), (False, " "),
                                                           (True, "Operator")])
def test_appended_approval_must_name_someone_after_a_clean_batch(
        ehr, runner, failed_first_batch, approved_by):
    if failed_first_batch:
        ehr.fail_next_writes(1)
    first = runner.run(live=True)
    fields = {"plan_digest": plan_digest(runner, first.run_id)}
    if approved_by is not None:
        fields["approved_by"] = approved_by
    runner.audit.append(first.run_id, "approval", **fields)
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert ehr.writes == 3
