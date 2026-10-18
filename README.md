# billing-automation-showcase

A guarded way to let a script make billing changes in an EHR: a dry run first, a small first batch, a check after every change, circuit breakers, and a final submit that stays with a person.

Every patient, claim and ID in this repository is synthetic. The data is generated at runtime from a seed, and nothing here comes from a real practice.

## The problem

A billing queue in a medical office fills up with claims held for the same small reasons. The member ID on the claim is from last year's insurance card, or the rendering provider was left blank. Fixing forty of them by hand can take most of an afternoon. A script can do it in a minute, and that's where the trouble starts.

EHRs and payer portals are not built for scripts. A save can report success and keep the old value. A biller can edit the same claim while the script is working through the list. The service can have a bad ten minutes, and every call after that fails. A rule with a bug can decide that four hundred claims need the same change. None of this shows up when you try a script on a handful of claims that behave, and a bad batch costs real money: denials, rework, refunds, and sometimes a claim billed twice. The mock EHR in this repository can inject each of these faults, so the tests can show the guards catching them.

Making the script fast is easy. What makes it safe to run is that it shows you what it will do, starts small, checks its own work, stops itself when the system doesn't look the way it expected, and never sends anything to a payer on its own.

## How the guards work

```
plan (reads only) --> dry run report, nothing written
      |
      +--> run --live --> first batch (3 to 5 changes) --> stops: AWAITING_APPROVAL
                                                                 |
                                                approve RUN_ID --by NAME
                                                                 |
                                                resume RUN_ID --> rest of the plan

each change:  re-read claim, still as planned? --> write --> re-read, did it land? --> audit log
breakers:     3 failures in a row, 5 in one run, any unexpected state, a plan over 50 changes
submit:       not reachable from the tool
```

1. **Dry run first.** A run is a dry run unless you pass `--live`. It reads the claims, applies the rules, prints every planned change and records the plan in the audit log. It makes no write calls, and a test checks that the mock EHR's saved state is byte for byte the same afterwards.
2. **A small first batch, then a human OK.** A live run applies the first 3 changes (3 to 5 with `--first-batch`) and stops. You look at those claims in the EHR, then approve that run by name. Only an approved run can continue. If anything in the first batch failed, the run can't be approved; you fix the cause and plan again.
3. **Re-read before and after every change.** Before writing, the tool re-reads the claim and checks it still has the version, status and value the plan saw. After writing, it reads the claim back and confirms the new value is there. A write the EHR accepted but didn't keep is reported as a failure, not a success. Every step goes to an append-only audit log in which each line carries the hash of the line before it, so editing, deleting or reordering any line before the last one breaks the chain. (The last line is covered under Limits.)
4. **Circuit breakers.** The run stops itself after 3 failures in a row or 5 in one run. It stops on the first unexpected state: a claim someone else changed since the plan or right after the tool's write, a claim that is no longer editable, or a claim that is gone. A plan with more than 50 changes is refused before anything is written, because a sudden huge plan usually means a broken rule. Changes after the stop are left alone and listed.
5. **The final submit stays a human click.** The mock EHR has a `submit` call that stands in for the biller's button. The runner never gets a handle to it. It works through a wrapper that exposes the read calls and one field update, and nothing else. A test also walks the package's source to make sure nothing outside the mock calls `submit`. When a run finishes, the tool says how many claims are fixed and leaves submitting to you.

Two smaller choices sit underneath these:

- **Output works with claim IDs, not people.** Patient names and birth dates never appear in output or in the audit log. Member IDs are masked to their last two characters wherever they're printed or logged, through a per-field policy rather than string handling scattered through the code. The run file in the sandbox keeps the raw plan, because resume needs the real values; it sits next to the mock EHR data, not in the log.
- **The approved plan can't change underneath you.** The audit log records a digest of the plan when the run starts. Resume recomputes it from the stored plan and refuses if they differ. The tool decides what a run may do next from the audit log, so editing the run file can't approve anything, and resume only accepts an approval that names a person and directly follows a clean first batch.

| Guard | Code | Tests |
|---|---|---|
| Dry run | `runner.py` (`Runner.run`) | `tests/test_dry_run.py` |
| First batch and approval | `runner.py` (`run`, `approve`, `resume`) | `tests/test_first_batch.py` |
| Re-read and audit | `runner.py` (`_attempt`), `audit.py` | `tests/test_verification.py`, `tests/test_audit.py` |
| Circuit breakers | `breaker.py`, `runner.py` (`_apply`) | `tests/test_breakers.py` |
| No submit | `client.py` | `tests/test_no_submit.py` |

## Quick start

Python 3.10 or newer, no dependencies.

```bash
git clone https://github.com/kenechi-ucheagwu/billing-automation-showcase
cd billing-automation-showcase
python -m pip install -e .

billing-guard init-sandbox sandbox
billing-guard plan sandbox
```

The sandbox holds ten synthetic claims, nine of which need a fix:

```
CLM-0001  member_id              *********33 -> *********97  stale_member_id             PLANNED
CLM-0001  rendering_provider_id      (empty) -> PRV-09       missing_rendering_provider  PLANNED
CLM-0002  rendering_provider_id      (empty) -> PRV-01       missing_rendering_provider  PLANNED
CLM-0003  member_id              *********26 -> *********17  stale_member_id             PLANNED
...
CLM-0010  rendering_provider_id      (empty) -> PRV-04       missing_rendering_provider  PLANNED
run-bcb62cf3cb3e DRY_RUN_COMPLETE: 12 planned changes on 9 claims. Nothing was written.
Next: run again with --live to apply the first batch.
```

A live run does the first batch and stops. Use the run ID it prints:

```
$ billing-guard run sandbox --live
...
run-12a13a6c215b AWAITING_APPROVAL: 3 of 12 changes applied and verified, 0 failures, 9 not applied. 2 claims fully fixed.
Next: check these claims in the EHR, then approve this run by name and resume it.

$ billing-guard approve sandbox run-12a13a6c215b --by billing-lead
run-12a13a6c215b APPROVED: 3 of 12 changes applied and verified, 0 failures, 9 not applied. 2 claims fully fixed.
Next: resume this run.

$ billing-guard resume sandbox run-12a13a6c215b
...
run-12a13a6c215b COMPLETE: 12 of 12 changes applied and verified, 0 failures, 0 not applied. 9 claims fully fixed.
Next: review the fixed claims in the EHR and submit them there. This tool never submits.

$ billing-guard audit sandbox --verify
Audit log OK: 92 events, hash chain intact.
```

### Watching a guard fire

`init-sandbox --scenario` switches on a fault in the mock. With `flaky`, the service starts failing after five writes, which is partway through the resume:

```
$ billing-guard resume flaky.sandbox run-29aefb8dc269
CLM-0001  member_id              *********33 -> *********97  stale_member_id             APPLIED_VERIFIED
...
CLM-0004  rendering_provider_id      (empty) -> PRV-06       missing_rendering_provider  APPLIED_VERIFIED
CLM-0005  member_id              *********93 -> *********17  stale_member_id             WRITE_FAILED
CLM-0005  rendering_provider_id      (empty) -> PRV-08       missing_rendering_provider  WRITE_FAILED
CLM-0007  member_id              *********05 -> *********26  stale_member_id             WRITE_FAILED
CLM-0007  rendering_provider_id      (empty) -> PRV-03       missing_rendering_provider  SKIPPED_BREAKER_OPEN
CLM-0008  rendering_provider_id      (empty) -> PRV-08       missing_rendering_provider  SKIPPED_BREAKER_OPEN
CLM-0009  member_id              *********36 -> *********41  stale_member_id             SKIPPED_BREAKER_OPEN
CLM-0010  rendering_provider_id      (empty) -> PRV-04       missing_rendering_provider  SKIPPED_BREAKER_OPEN
run-29aefb8dc269 STOPPED_BY_BREAKER: 5 of 12 changes applied and verified, 3 failures, 4 not applied. 4 claims fully fixed.
Reason: consecutive failure limit.
Next: fix the cause, then plan again. Nothing after the stop was touched.
```

With `silent-drop`, the mock reports the first claim's writes as saved but keeps the old values. The read-back catches it and the first batch can't be approved. With `drift`, another "user" edits the first claim just before the tool writes to it, and the run stops on that claim without writing.

| Command | What it does |
|---|---|
| `init-sandbox DIR` | Create a sandbox of synthetic claims. `--seed`, `--claims`, `--scenario clean\|flaky\|silent-drop\|drift`. |
| `plan DIR` | Dry run: print and audit the planned changes. |
| `run DIR --live` | Plan, apply the first batch, stop for approval. `--first-batch 3\|4\|5`. Without `--live` it's a dry run. |
| `approve DIR RUN_ID --by NAME` | Record a person's OK for a run whose first batch is clean. |
| `resume DIR RUN_ID` | Apply the rest of an approved run, under the same checks and breakers. |
| `audit DIR` | Print the audit log and check its hash chain. `--verify` checks without printing. |

Exit codes: 0 when the run is on track, 1 when it stopped, failed or was refused, 2 for a usage or setup error.

## Limits

- The EHR here is a mock. It saves each change to its file before it answers, the way a real API's write should be saved once it returns. A real deployment needs an adapter for the real system, and real systems add problems the mock only imitates: sessions that expire, rate limits, pages that load late, and reads served from a cache.
- The read-back is only as good as the read. If the EHR answers from a cache, a change that didn't save can look saved.
- There are no automatic retries within a run. A failed change stays failed until a new run plans it again, and that run may send it again. A write that timed out may still have landed; the next plan re-reads every claim and only plans what is still wrong.
- The hash chain catches a line edited, removed or moved anywhere before the last line. It can't catch a change to the last line, lines cut off the end, a well-formed line added by someone who can write the file, or the whole file rewritten with a fresh chain. For that, ship the log to write-once storage or keep a copy of its latest hash somewhere the operator can't write.
- Approval is a name typed on the command line. It isn't authentication: nothing stops the person who ran the batch from approving it, and anyone who can write the sandbox folder can add an approval line to the log by hand.
- The wrapper keeps the runner's code away from `submit`. It isn't a security boundary against code written to get around it.
- One operator at a time. There is no lock to stop two runs from working the same queue.
- The two rules are examples. Real billing fixes need rules a biller has checked against each payer's requirements.
- This shows an engineering pattern. It is not a compliance program and doesn't replace a practice's own billing policies.

## Where it came from

I run IT operations at a primary-care practice and write its billing automation in Python. I'm also a nursing student. This is a standalone version of the guard pattern I use there, written from scratch against a mock. None of the practice's code or data is in it.

Its sibling repository, [phi-guardrails](https://github.com/kenechi-ucheagwu/phi-guardrails), keeps patient identifiers out of git history. This repository's CI runs that scanner over its own history.

## What's next

- An adapter for a FHIR test server, so the same runner drives a standard API instead of the mock.
- A run lock, so two people can't work the same queue at once.
- Shipping the audit log to append-only storage and recording its latest hash outside the file.
- A two-person rule: whoever ran the first batch can't approve it.
- Breaker limits per rule, and a report of how often each rule fires over time.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The tests build their own synthetic sandboxes in temporary folders. One of them reads the repository's files (skipping caches, virtual environments, build output and sandboxes) and fails on non-ASCII text, anything shaped like a calendar date other than the generator's fixed reference day, or runs of 15 or more digits that could be record numbers.

## License

MIT
