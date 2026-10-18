"""Command-line front end for a sandbox folder.

Output names claims by ID and masks member IDs. Patient names and birth dates
stay in the sandbox's mock EHR file and are never printed.
"""

import argparse
import json
from pathlib import Path

from .audit import AuditLog, verify
from .client import RestrictedClient
from .mock_ehr import APIError, MockEHR
from .plan import build_plan
from .policy import display_value
from .runner import AUDIT_FILE, Result, Runner, RunStatus
from .synthetic import generate

EHR_FILE = "ehr.json"
SCENARIOS = ("clean", "flaky", "silent-drop", "drift")
STOPPED = {RunStatus.REFUSED, RunStatus.STOPPED_BY_BREAKER, RunStatus.FIRST_BATCH_FAILED}


class SandboxError(Exception):
    """A problem with the sandbox folder that the operator can fix."""


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="billing-guard", description="Guarded claim fixes against a synthetic mock EHR.")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-sandbox", help="create a sandbox of synthetic claims")
    init.add_argument("directory", type=Path)
    init.add_argument("--seed", type=int, default=7)
    init.add_argument("--claims", type=int, default=10)
    init.add_argument("--scenario", choices=SCENARIOS, default="clean",
                      help="switch on a fault so a guard has something to catch")

    plan = commands.add_parser("plan", help="dry run: show the planned changes")
    plan.add_argument("directory", type=Path)

    run = commands.add_parser("run", help="dry run, or with --live apply the first batch")
    run.add_argument("directory", type=Path)
    run.add_argument("--live", action="store_true", help="write to the mock EHR")
    run.add_argument("--first-batch", type=int, choices=(3, 4, 5), default=3)

    approve = commands.add_parser("approve", help="record a person's OK after the first batch")
    approve.add_argument("directory", type=Path)
    approve.add_argument("run_id")
    approve.add_argument("--by", required=True, help="name of the person approving")

    resume = commands.add_parser("resume", help="apply the rest of an approved run")
    resume.add_argument("directory", type=Path)
    resume.add_argument("run_id")

    audit = commands.add_parser("audit", help="print the audit log and check its hash chain")
    audit.add_argument("directory", type=Path)
    audit.add_argument("--verify", action="store_true", help="check the chain only")
    return root


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary(result: Result) -> str:
    if result.status == RunStatus.DRY_RUN_COMPLETE:
        return (f"{plural(len(result.plan), 'planned change')} on "
                f"{plural(result.claims, 'claim')}. Nothing was written.")
    return (f"{result.verified} of {len(result.plan)} changes applied and verified, "
            f"{plural(result.failures, 'failure')}, {result.remaining} not applied. "
            f"{plural(result.fixed, 'claim')} fully fixed.")


def next_step(result: Result) -> str:
    status = result.status
    if status == RunStatus.DRY_RUN_COMPLETE:
        if not result.plan:
            return "nothing to do."
        return "run again with --live to apply the first batch."
    if status == RunStatus.AWAITING_APPROVAL:
        return "check these claims in the EHR, then approve this run by name and resume it."
    if status == RunStatus.APPROVED:
        return "resume this run."
    if status == RunStatus.COMPLETE:
        return "review the fixed claims in the EHR and submit them there. This tool never submits."
    if status == RunStatus.COMPLETE_WITH_FAILURES:
        return ("look into the failed changes before planning again. Submitting the fixed "
                "claims is your step in the EHR.")
    return "fix the cause, then plan again. Nothing after the stop was touched."


def show(result: Result, *, table: bool = True) -> int:
    """Print a result and return the exit code: 0 on track, 1 stopped or failed."""
    if table:
        for change, status in zip(result.plan, result.items):
            before = display_value(change.field, change.before) or "(empty)"
            after = display_value(change.field, change.after)
            print(f"{change.claim_id}  {change.field:<22} {before:>11} -> {after:<11}  "
                  f"{change.rule:<27} {status.value}")
    print(f"{result.run_id} {result.status.value}: {summary(result)}")
    if result.reason:
        print(f"Reason: {result.reason}.")
    print(f"Next: {next_step(result)}")
    return 1 if result.status in STOPPED or result.failures else 0


def init_sandbox(directory: Path, seed: int, claims: int, scenario: str) -> int:
    if directory.exists() and any(directory.iterdir()):
        raise SandboxError(f"{directory} is not empty; pick a new folder")
    if claims < 1:
        raise SandboxError("--claims must be at least 1")
    ehr = MockEHR(generate(seed, claims))
    plan = build_plan(RestrictedClient(ehr))
    if scenario != "clean" and not plan:
        raise SandboxError("this sandbox plans no changes, so a fault would never show")
    if scenario == "flaky":
        # Five writes get through, past any first batch. Then three in a row fail.
        ehr.fail_writes_after(5, count=3)
    elif scenario == "silent-drop":
        ehr.drop_writes(plan[0].claim_id)
    elif scenario == "drift":
        ehr.drift_on_next_read(plan[0].claim_id)
    directory.mkdir(parents=True, exist_ok=True)
    ehr.save(directory / EHR_FILE)
    print(f"Created {directory} with {plural(claims, 'synthetic claim')} (scenario: {scenario}).")
    print("Next: plan, to see what would change.")
    return 0


def show_audit(directory: Path, verify_only: bool) -> int:
    path = directory / AUDIT_FILE
    if not path.is_file():
        raise SandboxError(f"no audit log in {directory}")
    checked = verify(path)
    if not checked.ok:
        print(f"Audit log FAILED at line {checked.line}: {checked.reason}.")
        return 1
    events = AuditLog(path).events()
    if not verify_only:
        for event in events:
            print(json.dumps(event, sort_keys=True))
    print(f"Audit log OK: {plural(len(events), 'event')}, hash chain intact.")
    return 0


def run_command(args: argparse.Namespace) -> int:
    ehr_path = args.directory / EHR_FILE
    if not ehr_path.is_file():
        raise SandboxError(f"no sandbox in {args.directory}; run init-sandbox first")
    # Only a live run or a resume may change the mock's file. A dry run and an
    # approval work on a copy, so even a consumed fault counter isn't saved.
    writes = args.command == "resume" or (args.command == "run" and args.live)
    ehr = MockEHR.load(ehr_path, save_changes=writes)
    runner = Runner(RestrictedClient(ehr), args.directory,
                    first_batch=getattr(args, "first_batch", 3))
    if args.command == "approve":
        return show(runner.approve(args.run_id, args.by), table=False)
    if args.command == "resume":
        return show(runner.resume(args.run_id))
    return show(runner.run(live=args.command == "run" and args.live))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init-sandbox":
            return init_sandbox(args.directory, args.seed, args.claims, args.scenario)
        if args.command == "audit":
            return show_audit(args.directory, args.verify)
        return run_command(args)
    except SandboxError as exc:
        print(f"Error: {exc}.")
        return 2
    except (OSError, ValueError, KeyError, TypeError, APIError):
        # Exception text can quote sandbox data, so it is not printed.
        print("Error: the sandbox data is missing or damaged. Check the folder and its audit log.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
