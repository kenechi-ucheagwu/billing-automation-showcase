"""The only handle the runner gets to the EHR.

It exposes the read calls and the single field update. Submit is not on the
list, so the runner has no way to send a claim to a payer.
"""

from typing import Any

from .mock_ehr import MockEHR


class RestrictedClient:
    """Expose only reads and the guarded field update, including to getattr.

    This is an application boundary, not isolation from hostile Python code
    using object.__getattribute__ or inspecting method globals.
    """

    __slots__ = ("__ehr",)

    def __init__(self, ehr: MockEHR):
        object.__setattr__(self, "_RestrictedClient__ehr", ehr)

    def __getattribute__(self, name: str) -> Any:
        if name not in {"list_claims", "get_claim", "get_patient", "update_claim"}:
            raise AttributeError(f"restricted client: {name} is not allowed")
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("restricted client is immutable")

    def list_claims(self, status: str | None = None) -> list[dict[str, Any]]:
        return object.__getattribute__(self, "_RestrictedClient__ehr").list_claims(status)

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        return object.__getattribute__(self, "_RestrictedClient__ehr").get_claim(claim_id)

    def get_patient(self, patient_id: str) -> dict[str, Any]:
        return object.__getattribute__(self, "_RestrictedClient__ehr").get_patient(patient_id)

    def update_claim(self, claim_id: str, field: str, value: str, expected_version: int) -> int:
        return object.__getattribute__(self, "_RestrictedClient__ehr").update_claim(
            claim_id, field, value, expected_version)
