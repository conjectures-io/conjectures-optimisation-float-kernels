"""Real proof/security checks, kept sequential with the checkout build lock."""

import pytest

import selftest


@pytest.mark.slow
def test_tensor_proof_and_attack_fixtures():
    assert selftest.main() == 0
