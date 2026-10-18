from billing_guard.client import RestrictedClient
from billing_guard.plan import build_plan
from billing_guard.runner import ItemStatus, Runner, RunStatus


def test_silent_drop_is_audited_and_counts_as_failure(ehr, tmp_path):
    runner = Runner(RestrictedClient(ehr), tmp_path, consecutive_limit=1)
    change = build_plan(RestrictedClient(ehr))[0]
    ehr.drop_writes(change.claim_id)
    before = ehr.get_claim(change.claim_id)
    result = runner.run(live=True)
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert result.items[0] == ItemStatus.VERIFY_FAILED
    assert result.failures == 1
    assert ehr.get_claim(change.claim_id) == before
    assert any(e["event"] == "verification" and not e["ok"] for e in runner.audit.events())
    assert ehr.writes == 1


def test_two_edits_on_one_claim_use_updated_version(ehr, runner):
    result = runner.run(live=True)
    assert result.items[:2] == [ItemStatus.APPLIED_VERIFIED] * 2
    assert result.plan[0].claim_id == result.plan[1].claim_id
    assert ehr.get_claim(result.plan[0].claim_id)["version"] == 3


def test_partial_claim_is_not_reported_as_fixed(ehr, runner):
    ehr.fail_next_writes(1)
    result = runner.run(live=True)
    assert result.verified == 2
    assert result.fixed == 1


def test_edit_right_after_write_stops_the_run(ehr, tmp_path):
    runner = Runner(RestrictedClient(ehr), tmp_path, first_batch=5)
    plan = build_plan(RestrictedClient(ehr))
    claims = [change.claim_id for change in plan]
    # A claim with one planned edit, so nothing later in the plan touches it.
    index = next(i for i, change in enumerate(plan)
                 if claims.count(change.claim_id) == 1 and change.field == "member_id")
    assert index < 4
    ehr.drift_after_next_write(plan[index].claim_id)
    result = runner.run(live=True)
    assert result.items[index] == ItemStatus.UNEXPECTED_STATE
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert result.reason == "claim changed after write"
    assert set(result.items[index + 1:]) == {ItemStatus.SKIPPED_BREAKER_OPEN}
    assert ehr.writes == index + 1
