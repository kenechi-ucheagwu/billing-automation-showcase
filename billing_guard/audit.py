"""An append-only audit log where each line carries the hash of the line before.

Editing, deleting or reordering any line before the last breaks the chain,
and verify() reports where. A change to the last line, lines cut off the end,
a well-formed line added by hand, or a whole file rewritten with a fresh
chain cannot be detected from the file alone; that needs a copy of the latest
hash kept somewhere else. The runner checks the order of events on top of
this, which stops some edits (an approval written over the first batch's
result, for one), but anyone who can write this file can still add an
approval by hand. One process writes to a sandbox at a time; concurrent
writers are not supported.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import display_value

GENESIS = "0" * 64


@dataclass(frozen=True)
class Verification:
    ok: bool
    line: int | None = None
    reason: str = "OK"


def verify(path: Path) -> Verification:
    """Walk the chain from the first line and report the first line that breaks it."""
    if not path.exists():
        return Verification(True)
    prev = GENESIS
    for number, raw in enumerate(path.read_bytes().splitlines(keepends=True), 1):
        try:
            event = json.loads(raw)
            if not raw.endswith(b"\n"):
                return Verification(False, number, "incomplete line")
            if event["seq"] != number or event["prev"] != prev:
                return Verification(False, number, "sequence or previous hash mismatch")
            if not all(key in event for key in ("timestamp", "run_id", "event")):
                return Verification(False, number, "missing event fields")
        except (ValueError, KeyError, TypeError):
            return Verification(False, number, "invalid JSON event")
        prev = hashlib.sha256(raw).hexdigest()
    return Verification(True)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def events(self) -> list[dict[str, Any]]:
        result = verify(self.path)
        if not result.ok:
            raise ValueError(f"audit line {result.line}: {result.reason}")
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_bytes().splitlines()]

    def append(self, run_id: str, event: str, **fields: Any) -> None:
        """Mask sensitive values and add one line. Refuses if the chain is already broken."""
        events = self.events()
        raw_lines = self.path.read_bytes().splitlines(keepends=True) if self.path.exists() else []
        # Mask at the boundary as well as at the caller, so raw edits stay private.
        if "field" in fields:
            for key in ("before", "after", "value"):
                if key in fields:
                    fields[key] = display_value(fields["field"], fields[key])
        record = dict(fields, seq=len(events) + 1,
                      timestamp=datetime.now(timezone.utc).isoformat(),
                      run_id=run_id, event=event,
                      prev=hashlib.sha256(raw_lines[-1]).hexdigest() if raw_lines else GENESIS)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        raw = (line + "\n").encode("ascii")
        with self.path.open("ab") as stream:
            stream.write(raw)
            stream.flush()
