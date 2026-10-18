from billing_guard.policy import display_value
from billing_guard.synthetic import generate


def test_masking_is_field_based_and_retains_only_suffix():
    raw = generate()["patients"][0]["coverages"][0]["member_id"]
    masked = display_value("member_id", raw)
    assert masked[-2:] == raw[-2:]
    assert set(masked[:-2]) == {"*"}
    assert len(masked) == len(raw)
    assert display_value("rendering_provider_id", "PRV-03") == "PRV-03"


def test_short_values_are_fully_masked():
    assert display_value("member_id", "AB12") == "****"
    assert display_value("member_id", "") == ""
