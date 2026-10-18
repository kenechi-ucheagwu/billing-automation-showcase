import json
import subprocess
import sys
from pathlib import Path

import pytest

from billing_guard.cli import main
from billing_guard.mock_ehr import MockEHR


def latest_run(directory):
    events = [json.loads(line) for line in (directory / "audit.jsonl").read_text().splitlines()]
    return next(e["run_id"] for e in reversed(events) if e["event"] == "run_started")


def test_clean_end_to_end(tmp_path, capsys):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory), "--seed", "7"]) == 0
    assert main(["plan", str(directory)]) == 0
    original = (directory / "ehr.json").read_bytes()
    assert main(["run", str(directory)]) == 0
    assert (directory / "ehr.json").read_bytes() == original
    assert main(["run", str(directory), "--live"]) == 0
    run_id = latest_run(directory)
    assert main(["approve", str(directory), run_id, "--by", "Operator"]) == 0
    assert main(["resume", str(directory), run_id]) == 0
    assert main(["audit", str(directory), "--verify"]) == 0
    output = capsys.readouterr().out
    for key in ("DRY_RUN_COMPLETE", "AWAITING_APPROVAL", "APPROVED", "COMPLETE",
                "Audit log OK", "This tool never submits"):
        assert key in output
    data = json.loads((directory / "ehr.json").read_text())
    for patient in data["patients"]:
        for key in ("first_name", "last_name", "birth_date"):
            assert patient[key] not in output
        assert all(c["member_id"] not in output for c in patient["coverages"])


@pytest.mark.parametrize("scenario,key", [("flaky", "consecutive failure limit"),
                                          ("silent-drop", "VERIFY_FAILED"),
                                          ("drift", "claim changed since planning")])
def test_failure_scenarios(tmp_path, capsys, scenario, key):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory), "--scenario", scenario]) == 0
    code = main(["run", str(directory), "--live"])
    if scenario == "flaky":
        assert code == 0
        run_id = latest_run(directory)
        assert main(["approve", str(directory), run_id, "--by", "Operator"]) == 0
        code = main(["resume", str(directory), run_id])
    assert code == 1
    assert key in capsys.readouterr().out
    assert main(["audit", str(directory), "--verify"]) == 0


def test_usage_exit_code(tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(["run", str(tmp_path), "--first-batch", "2"])
    assert caught.value.code == 2


@pytest.mark.parametrize("size", [4, 5])
def test_flaky_scenario_waits_for_configured_first_batch(tmp_path, size, capsys):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory), "--scenario", "flaky"]) == 0
    assert main(["run", str(directory), "--live", "--first-batch", str(size)]) == 0
    run_id = latest_run(directory)
    assert main(["approve", str(directory), run_id, "--by", "Operator"]) == 0
    assert main(["resume", str(directory), run_id]) == 1
    assert "consecutive failure limit" in capsys.readouterr().out


def test_existing_sandbox_is_preserved(tmp_path, capsys):
    assert main(["init-sandbox", str(tmp_path)]) == 0
    raw = (tmp_path / "ehr.json").read_bytes()
    assert main(["init-sandbox", str(tmp_path)]) == 2
    assert (tmp_path / "ehr.json").read_bytes() == raw
    assert "not empty" in capsys.readouterr().out


def test_missing_audit_or_sandbox_is_a_setup_error(tmp_path, capsys):
    assert main(["audit", str(tmp_path), "--verify"]) == 2
    assert main(["plan", str(tmp_path)]) == 2
    assert "run init-sandbox first" in capsys.readouterr().out


def test_module_entrypoint_in_real_process(tmp_path):
    command = [sys.executable, "-m", "billing_guard", "init-sandbox", str(tmp_path)]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "synthetic claims" in result.stdout


def test_invalid_audit_exit_code(tmp_path, capsys):
    (tmp_path / "audit.jsonl").write_text("invalid\n", encoding="ascii")
    assert main(["audit", str(tmp_path), "--verify"]) == 1
    assert "Audit log FAILED at line 1" in capsys.readouterr().out


def test_first_batch_is_on_disk_when_the_command_returns(tmp_path):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory)]) == 0
    assert main(["run", str(directory), "--live"]) == 0
    run_id = latest_run(directory)
    plan = json.loads((directory / "runs" / (run_id + ".json")).read_text())["plan"]
    data = json.loads((directory / "ehr.json").read_text())
    claims = {claim["claim_id"]: claim for claim in data["claims"]}
    for change in plan[:3]:
        assert claims[change["claim_id"]][change["field"]] == change["after"]
    for change in plan[3:]:
        assert claims[change["claim_id"]][change["field"]] == change["before"]


def test_mistyped_run_id_is_not_echoed_or_logged(tmp_path, capsys):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory)]) == 0
    assert main(["plan", str(directory)]) == 0
    raw = json.loads((directory / "ehr.json").read_text())["claims"][0]["member_id"]
    capsys.readouterr()
    assert main(["resume", str(directory), raw]) == 1
    assert main(["approve", str(directory), raw, "--by", "Operator"]) == 1
    output = capsys.readouterr().out
    assert raw not in output + (directory / "audit.jsonl").read_text()
    assert "run-invalid REFUSED" in output


@pytest.mark.parametrize("data", [{"patients": None, "claims": []}, [],
                                  {"patients": [], "claims": [{"claim_id": 5}]}])
def test_damaged_ehr_file_is_refused_cleanly(tmp_path, capsys, data):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory)]) == 0
    (directory / "ehr.json").write_text(json.dumps(data), encoding="ascii")
    assert main(["plan", str(directory)]) == 1
    assert main(["run", str(directory), "--live"]) == 1
    assert "damaged" in capsys.readouterr().out
    assert json.loads((directory / "ehr.json").read_text()) == data


@pytest.mark.parametrize("command", ["plan", "run"])
def test_dry_run_leaves_the_ehr_file_alone_even_when_a_read_fails(tmp_path, capsys, command):
    directory = tmp_path / "sandbox"
    assert main(["init-sandbox", str(directory)]) == 0
    path = directory / "ehr.json"
    ehr = MockEHR.load(path)
    ehr.fail_next_reads(1)
    ehr.save(path)
    raw = path.read_bytes()
    assert main([command, str(directory)]) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert path.read_bytes() == raw
    assert sorted(p.name for p in directory.iterdir()) == ["audit.jsonl", "ehr.json", "runs"]
