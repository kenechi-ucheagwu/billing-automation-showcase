from copy import deepcopy

import pytest

from billing_guard.breaker import Breaker
from billing_guard.client import RestrictedClient
from billing_guard.mock_ehr import TransientError
from billing_guard.plan import build_plan
from billing_guard.runner import ItemStatus, Runner, RunStatus


def test_three_write_errors_leave_remainder_untouched(ehr, runner):
    before = deepcopy(ehr.claims)
    ehr.fail_next_writes(3)
    result = runner.run(live=True)
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert result.reason == "consecutive failure limit"
    assert ehr.writes == 3
    assert ehr.claims == before
    assert all(s == ItemStatus.SKIPPED_BREAKER_OPEN for s in result.items[3:])
    assert runner.approve(result.run_id, "Operator").status == RunStatus.REFUSED


def test_success_resets_streak():
    breaker = Breaker()
    assert breaker.record(False) is None
    assert breaker.record(False) is None
    assert breaker.record(True) is None
    assert breaker.streak == 0 and breaker.total == 2
    assert breaker.record(False) is None
    assert breaker.record(False) is None
    assert breaker.record(False) == "consecutive failure limit"


def test_runner_success_resets_streak(ehr, tmp_path, monkeypatch):
    runner = Runner(RestrictedClient(ehr), tmp_path, first_batch=5)
    original = ehr.update_claim
    calls = 0

    def intermittent(*args):
        nonlocal calls
        calls += 1
        if calls in (1, 2, 4, 5):
            raise TransientError("synthetic error")
        return original(*args)

    monkeypatch.setattr(ehr, "update_claim", intermittent)
    result = runner.run(live=True)
    assert result.status == RunStatus.FIRST_BATCH_FAILED
    assert result.failures == 4 and calls == 5


def test_total_cap_with_interspersed_successes(ehr, tmp_path, monkeypatch):
    runner = Runner(RestrictedClient(ehr), tmp_path, consecutive_limit=3, total_limit=2,
                    first_batch=5)
    original = ehr.update_claim
    calls = 0

    def alternating(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in (1, 3):
            raise TransientError("synthetic error")
        return original(*args, **kwargs)

    monkeypatch.setattr(ehr, "update_claim", alternating)
    result = runner.run(live=True)
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert result.reason == "total failure limit"
    assert result.items[:3] == [ItemStatus.WRITE_FAILED, ItemStatus.APPLIED_VERIFIED,
                               ItemStatus.WRITE_FAILED]
    assert calls == 3


@pytest.mark.parametrize("kind", ["version", "value", "missing", "draft", "ready_to_submit",
                                  "submitted", "locked"])
def test_unexpected_state_stops_before_write(ehr, runner, monkeypatch, kind):
    target = build_plan(RestrictedClient(ehr))[0]
    original = ehr.get_claim
    changed = False

    def drift(claim_id):
        nonlocal changed
        if not changed:
            changed = True
            if kind == "version":
                ehr.edit_claim(target.claim_id, target.field, target.before)
            elif kind == "value":
                ehr.claims[target.claim_id][target.field] = target.after
            elif kind == "missing":
                del ehr.claims[target.claim_id]
            else:
                ehr.set_status(target.claim_id, kind)
        return original(claim_id)

    monkeypatch.setattr(ehr, "get_claim", drift)
    result = runner.run(live=True)
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert result.items[0] == ItemStatus.UNEXPECTED_STATE
    assert ehr.writes == 0


def test_oversized_plan_refused_and_audited(ehr, tmp_path):
    runner = Runner(RestrictedClient(ehr), tmp_path, plan_cap=4)
    result = runner.run(live=True)
    assert result.status == RunStatus.REFUSED
    assert result.reason == "plan size exceeds cap"
    assert ehr.writes == 0
    assert any(e["event"] == "breaker_trip" for e in runner.audit.events())


def test_prewrite_read_errors_count_and_leave_everything_untouched(ehr, runner, monkeypatch):
    before = deepcopy(ehr.claims)

    def error(claim_id):
        raise TransientError("synthetic read error")

    monkeypatch.setattr(ehr, "get_claim", error)
    result = runner.run(live=True)
    assert result.reason == "consecutive failure limit"
    assert result.items[:3] == [ItemStatus.READ_FAILED] * 3
    assert ehr.writes == 0 and ehr.claims == before


def test_postwrite_read_error_is_not_verified(ehr, runner, monkeypatch):
    original = ehr.get_claim
    calls = 0

    def error_once(claim_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TransientError("synthetic read error")
        return original(claim_id)

    monkeypatch.setattr(ehr, "get_claim", error_once)
    result = runner.run(live=True)
    assert result.items[0] == ItemStatus.READ_FAILED
    # The uncertain write advanced the version, so another edit must stop.
    assert result.items[1] == ItemStatus.UNEXPECTED_STATE
    assert ehr.writes == 1


def test_resume_rechecks_remaining_claims(ehr, runner):
    first = runner.run(live=True)
    runner.approve(first.run_id, "Operator")
    target = first.plan[3]
    ehr.edit_claim(target.claim_id, target.field, target.before)
    before = deepcopy(ehr.claims)
    result = runner.resume(first.run_id)
    assert result.status == RunStatus.STOPPED_BY_BREAKER
    assert ehr.writes == 3
    assert ehr.claims == before


def test_planning_read_failure_refuses_without_writes(ehr, runner):
    ehr.fail_next_reads(1)
    assert runner.run(live=True).status == RunStatus.REFUSED
    assert ehr.writes == 0


def test_isolated_failure_after_approval_is_reported(ehr, runner):
    first = runner.run(live=True)
    runner.approve(first.run_id, "Operator")
    ehr.fail_next_writes(1)
    result = runner.resume(first.run_id)
    assert result.status == RunStatus.COMPLETE_WITH_FAILURES
    assert result.items[3] == ItemStatus.WRITE_FAILED
    assert result.failures == 1
    assert all(s == ItemStatus.APPLIED_VERIFIED for s in result.items[4:])
