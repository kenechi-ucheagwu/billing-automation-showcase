from billing_guard.audit import AuditLog
from billing_guard.cli import show
from billing_guard.runner import RunStatus


def test_default_is_read_only_and_prints_and_audits_every_item(ehr, runner, tmp_path, capsys):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    ehr.save(before)
    result = runner.run()
    ehr.save(after)
    assert result.status == RunStatus.DRY_RUN_COMPLETE
    assert ehr.writes == 0
    assert before.read_bytes() == after.read_bytes()
    assert show(result) == 0
    output = capsys.readouterr().out
    for change in result.plan:
        assert f"{change.claim_id}  {change.field}" in output
    events = AuditLog(tmp_path / "audit.jsonl").events()
    assert len([e for e in events if e["event"] == "planned_item"]) == len(result.plan)
    assert not any(e["event"] == "write_started" for e in events)
