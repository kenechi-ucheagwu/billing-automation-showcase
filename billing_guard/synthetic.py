"""Invent patients and claims at runtime from a seed.

Names are built from made-up syllables rather than lists of real names, and
member IDs carry a SYN prefix. Every identity is invented, though a made-up
name can still match a real one by chance, which is one reason output names
claims by ID. The same seed always produces the same sandbox.
"""

from datetime import date, timedelta
from random import Random
from typing import Any

SYLLABLES = ("za", "vo", "ki", "lu", "xe", "qo", "ni", "fa")
# A fixed reference day keeps output identical across machines and dates.
REFERENCE_DAY = date(2026, 1, 1)


def generate(seed: int = 7, claims: int = 10) -> dict[str, Any]:
    """Return mock EHR data: one patient and one claim per index.

    Odd-numbered claims carry the patient's old member ID, and two of every
    three are missing a rendering provider, so the rules have work to find.
    """
    if claims < 0:
        raise ValueError("claims must be non-negative")
    rng = Random(seed)

    def name(parts: int) -> str:
        return "".join(rng.choice(SYLLABLES) for _ in range(parts)).capitalize()

    def member_id() -> str:
        return "SYN" + str(rng.randrange(10000000, 99999999))

    patients, records = [], []
    for index in range(1, claims + 1):
        patient_id = f"PT-{index:04d}"
        active = member_id()
        old = member_id()
        while old == active:
            old = member_id()
        age_in_days = rng.randrange(18 * 365, 90 * 365)
        patients.append({
            "patient_id": patient_id,
            "first_name": name(2),
            "last_name": name(3),
            "birth_date": (REFERENCE_DAY - timedelta(days=age_in_days)).isoformat(),
            "coverages": [
                {"plan_name": "Plan Zavoq", "member_id": active, "active": True},
                {"plan_name": "Plan Xeluq", "member_id": old, "active": False},
            ],
        })
        provider = f"PRV-{rng.randrange(1, 10):02d}"
        records.append({
            "claim_id": f"CLM-{index:04d}",
            "patient_id": patient_id,
            "service_date": (REFERENCE_DAY + timedelta(days=rng.randrange(0, 90))).isoformat(),
            "status": "needs_review",
            "member_id": old if index % 2 else active,
            "rendering_provider_id": "" if index % 3 else provider,
            "encounter_provider_id": provider,
            "version": 1,
        })
    return {"patients": patients, "claims": records}
