"""The developer quality screen must detect erased signals and incomplete diagnostics."""

import copy
import math
import runpy
from pathlib import Path

import pytest

AUDIT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/audit-exact-workloads.py"))


def record():
    stats = dict(size=2, zeros=0, nonfinite=0, sum=2.0, sum_squares=2.0, max_abs=1.0)
    pair = dict(activation=stats.copy(), changed=2, delta_squares=1.0)
    return dict(
        seed=42,
        cell="mlp:b1:normal",
        sample=0,
        layer=1,
        final=True,
        base=stats,
        redraw=copy.deepcopy(pair),
        negate=copy.deepcopy(pair),
    )


def complete_records():
    result = []
    for family in ["mlp", "cnn", "mnet", "nmlp", "mixer"]:
        for batch in [1, 8] if family in ["mlp", "nmlp"] else [1, 4]:
            for kind in ["normal", "wide", "sparse"]:
                for sample in range(1 if family in ["mlp", "nmlp"] else batch):
                    for layer in [0, 1]:
                        r = record()
                        r.update(
                            cell=f"{family}:b{batch}:{kind}",
                            sample=sample,
                            layer=layer,
                            final=layer == 1,
                        )
                        result.append(r)
    return result


def test_relative_change_and_weak_signal_detection():
    r = record()
    assert AUDIT["relative_change"](r, "redraw") == pytest.approx(math.sqrt(0.5))
    assert not AUDIT["quality_issues"]([r], 1e-3)
    r["redraw"].update(changed=0, delta_squares=0.0)
    assert len(AUDIT["quality_issues"]([r], 1e-3)) == 1
    r["redraw"].update(changed=2, delta_squares=1e-12)
    assert len(AUDIT["quality_issues"]([r], 1e-3)) == 1


def test_zero_base_is_not_silently_treated_as_a_finite_ratio():
    r = record()
    r["base"]["sum_squares"] = 0.0
    assert AUDIT["relative_change"](r, "redraw") is None
    r["redraw"].update(changed=0, delta_squares=0.0)
    assert AUDIT["relative_change"](r, "redraw") == 0.0
    assert AUDIT["quality_issues"]([r], 1e-3)


def test_complete_model_records_pass():
    AUDIT["validate_records"](complete_records(), [42])


@pytest.mark.parametrize("problem", ["duplicate", "missing_layer", "missing_model", "nonfinite"])
def test_incomplete_or_corrupt_records_fail(problem):
    records = complete_records()
    if problem == "duplicate":
        records.append(copy.deepcopy(records[0]))
    elif problem == "missing_layer":
        records.pop(0)
    elif problem == "missing_model":
        records = [r for r in records if r["cell"] != "mlp:b1:normal"]
    else:
        records[0]["base"]["sum_squares"] = float("nan")
    with pytest.raises(ValueError):
        AUDIT["validate_records"](records, [42])
