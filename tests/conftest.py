from pathlib import Path

import pytest

from billing_guard.client import RestrictedClient
from billing_guard.mock_ehr import MockEHR
from billing_guard.runner import Runner
from billing_guard.synthetic import generate


@pytest.fixture
def ehr():
    return MockEHR(generate())


@pytest.fixture
def runner(ehr, tmp_path: Path):
    return Runner(RestrictedClient(ehr), tmp_path)
