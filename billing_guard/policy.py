"""Which fields are masked wherever the tool prints or logs a value.

Member IDs identify a patient's insurance, so output and the audit log show
only their last two characters. Claim and provider IDs print in full.
"""

MASKED_FIELDS = frozenset({"member_id"})
VISIBLE_TAIL = 2


def display_value(field: str, value: str) -> str:
    """Return the value as it may appear in output or in the audit log."""
    if field not in MASKED_FIELDS:
        return value
    if len(value) <= VISIBLE_TAIL * 2:
        # Too short to show a tail without showing most of the value.
        return "*" * len(value)
    return "*" * (len(value) - VISIBLE_TAIL) + value[-VISIBLE_TAIL:]
