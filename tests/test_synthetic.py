from billing_guard.synthetic import generate


def test_deterministic_and_distinct():
    assert generate(7) == generate(7)
    assert generate(7) != generate(8)


def test_runtime_identities():
    data = generate()
    for patient in data["patients"]:
        assert patient["first_name"] and patient["last_name"]
        assert sum(c["active"] for c in patient["coverages"]) == 1
        for coverage in patient["coverages"]:
            assert coverage["member_id"].startswith("SYN")
            assert 6 <= len(coverage["member_id"][3:]) <= 9
            assert coverage["member_id"][3:].isdigit()
