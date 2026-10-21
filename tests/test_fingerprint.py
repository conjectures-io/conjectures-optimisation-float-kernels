"""Proof, measurement, scoring and rewards have separate identities."""

from pathlib import Path

import fingerprint


def test_exact_contract_and_all_toolkit_are_proof_pinned():
    files = {
        str(p.relative_to(fingerprint.HERE))
        for p in (fingerprint.HERE / "lean/contract/ExactToolkit").glob("*.lean")
    }
    assert files <= set(fingerprint.PROOF_FILES)
    assert "lean/verify/VerifyExact.lean" in fingerprint.PROOF_FILES
    assert "lean/bench/MainExact.lean" not in fingerprint.PROOF_FILES


def test_benchmark_excludes_reward_and_scoring_code():
    assert not {"validator/rewards.py", "validator/config.py", "validator/scoring.py"} & set(
        fingerprint.PINNED
    )
    assert fingerprint.proof() != fingerprint.benchmark()
    assert all((Path(fingerprint.HERE) / name).is_file() for name in fingerprint.PINNED)
