import ast
from pathlib import Path

import pytest

from billing_guard.client import RestrictedClient
from billing_guard.runner import Runner


@pytest.mark.parametrize("name", ["submit", "save", "edit_claim", "_ehr", "__dict__", "__class__",
                                  "_RestrictedClient__ehr", "__getattribute__", "faults"])
def test_wrapper_refuses_non_allowlisted_attributes(ehr, name):
    client = RestrictedClient(ehr)
    with pytest.raises(AttributeError, match="not allowed"):
        getattr(client, name)


def test_wrapper_cannot_be_rebound_and_runner_requires_it(ehr, tmp_path):
    client = RestrictedClient(ehr)
    with pytest.raises(AttributeError, match="immutable"):
        client.submit = lambda: None
    with pytest.raises(TypeError, match="RestrictedClient"):
        Runner(ehr, tmp_path)


def test_package_never_calls_submit_outside_mock():
    for path in (Path(__file__).resolve().parents[1] / "billing_guard").glob("*.py"):
        if path.name == "mock_ehr.py":
            continue
        tree = ast.parse(path.read_text(encoding="ascii"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "submit"]
        assert not calls, path
