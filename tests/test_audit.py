import json

import pytest

from billing_guard.audit import AuditLog, verify
from billing_guard.cli import show
from billing_guard.runner import RunStatus


def full_run(runner):
    result = runner.run(live=True)
    runner.approve(result.run_id, "Operator")
    return runner.resume(result.run_id)


def test_chain_and_required_events_after_full_run(runner, tmp_path):
    result = full_run(runner)
    assert result.status == RunStatus.COMPLETE
    assert verify(tmp_path / "audit.jsonl").ok
    events = runner.audit.events()
    types = {e["event"] for e in events}
    assert {"run_started", "planned_item", "pre_write_check", "write_started", "write_result",
            "verification", "first_batch_complete", "approval", "resume", "run_finished"} <= types
    assert all(e["timestamp"].endswith("+00:00") for e in events)


@pytest.mark.parametrize("mutation", ["edit", "delete", "reorder"])
def test_middle_tampering_detected(runner, tmp_path, mutation):
    runner.run()
    path = tmp_path / "audit.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    if mutation == "edit":
        item = json.loads(lines[2])
        item["rule"] = "changed"
        lines[2] = (json.dumps(item) + "\n").encode("ascii")
    elif mutation == "delete":
        del lines[2]
    else:
        lines[2], lines[3] = lines[3], lines[2]
    path.write_bytes(b"".join(lines))
    checked = verify(path)
    assert not checked.ok
    assert checked.line == (4 if mutation == "edit" else 3)
    assert runner.run(live=True).status == RunStatus.REFUSED


def test_later_runs_preserve_every_prior_byte(runner, tmp_path):
    runner.run()
    path = tmp_path / "audit.jsonl"
    before = path.read_bytes()
    runner.run()
    assert path.read_bytes().startswith(before)
    assert len(path.read_bytes()) > len(before)
    assert verify(path).ok


def test_private_values_absent_from_audit_and_output(ehr, runner, tmp_path, capsys):
    show(runner.run())
    show(full_run(runner))
    log = (tmp_path / "audit.jsonl").read_text()
    output = capsys.readouterr().out
    for patient in ehr.patients.values():
        for key in ("first_name", "last_name", "birth_date"):
            assert patient[key] not in log + output
        for coverage in patient["coverages"]:
            assert coverage["member_id"] not in log + output
    for claim in ehr.claims.values():
        assert claim["member_id"] not in log + output
    assert "*******" in log and "*******" in output


def test_writer_boundary_masks_raw_fields(ehr, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    raw = ehr.list_claims()[0]["member_id"]
    audit.append("test", "planned_item", field="member_id", before=raw, after=raw, value=raw)
    assert raw not in audit.path.read_text()


def test_damaged_chain_blocks_approval_and_resume(runner, tmp_path):
    first = runner.run(live=True)
    runner.approve(first.run_id, "Operator")
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"invalid\n" + path.read_bytes())
    assert runner.resume(first.run_id).status == RunStatus.REFUSED
    assert runner.approve(first.run_id, "Operator").status == RunStatus.REFUSED


def test_partial_tail_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b'{"seq":')
    assert not verify(path).ok
