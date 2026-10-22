"""Suites and their plans: what a suite file may say, what a plan draws, what a verdict keeps."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

import benchmark_suite as bs
import fingerprint
import gate

PUBLIC = Path(__file__).resolve().parents[1] / "suites" / "public-v2.json"


@pytest.fixture
def spec() -> dict:
    return bs.load(PUBLIC)


def test_the_public_suite_loads_and_covers_every_kernel(spec):
    cells = bs.cells(spec)
    assert {c.split(":")[0] for c in cells} == set(bs.KERNELS)
    assert len(cells) == len(set(cells))


def test_plans_are_reproducible_and_change_with_the_seed(spec):
    assert bs.make_plan(spec, 7, 500_000) == bs.make_plan(spec, 7, 500_000)
    assert bs.make_plan(spec, 7, 500_000) != bs.make_plan(spec, 8, 500_000)


def test_every_cell_gets_its_cases_and_every_kernel_a_checksum_case(spec):
    plan = bs.make_plan(spec, 1, 500_000)
    names = [c["name"] for c in plan["cases"]]
    assert len(names) == len(set(names))
    timed = {c["name"].rsplit(":", 1)[0] for c in plan["cases"] if c["timed"]}
    assert timed == set(bs.cells(spec))
    assert {c["name"] for c in plan["cases"] if not c["timed"]} == {
        f"{k}:check" for k in bs.KERNELS
    }


def test_each_kernel_spends_at_most_its_work_budget_and_clean_cases_all_of_theirs(spec):
    plan = bs.make_plan(spec, 3, 500_000)
    for kernel in bs.KERNELS:
        timed = [c for c in plan["cases"] if c["kernel"] == kernel and c["timed"]]
        budget = spec["work"][kernel] * 500_000
        assert 0.5 * budget <= sum(c["work"] for c in timed) <= 1.3 * budget, kernel
        clean = [c for c in timed if bs.slow_share(c) == 0]
        per_case = budget / len(timed)
        assert clean and all(0.5 * per_case <= c["work"] <= 2 * per_case for c in clean), kernel


def _case(kernel: str, **params) -> dict:
    # One drawn case with the defaults' encoding, a size and the given overrides.
    size = {"length": 1000, "m": 1, "n": 1, "p": 1, "batch": 100, "work": 100_000}
    if kernel == "bmm":
        size = {"length": 1, "m": 32, "n": 64, "p": 32, "batch": 1, "work": 32 * 64 * 32}
    return {"name": f"{kernel}:t:0", "kernel": kernel, **bs._encode(bs.DEFAULTS, kernel), **size,
            **params}  # fmt: skip


def test_slow_share_follows_where_accumulations_are_poisoned():
    assert bs.slow_share(_case("sum")) == 0
    # A NaN on every eighth position poisons nearly all of a 1000-long sum, but a ±0 does not.
    nan_stride = {"specRate": 1, "specPlace": 1, "specPeriod": 8, "specMask": bs.SPECIALS["nan"]}
    assert bs.slow_share(_case("sum", **nan_stride)) > 0.95
    assert bs.slow_share(_case("sum", **{**nan_stride, "specMask": bs.SPECIALS["zero"]})) == 0
    # One NaN in a million: rare in a 1000-long sum, so little of it is slow.
    assert bs.slow_share(_case("sum", specRate=1)) < 0.01
    # Every value near the largest finite one: sums overflow at once; ordinary values never do.
    assert bs.slow_share(_case("sum", center=253, spread=1)) > 0.95
    assert bs.slow_share(_case("bmm", center=200, spread=2)) > 0.95
    assert bs.slow_share(_case("bmm", center=127, spread=20)) == 0
    # No product overflows at exponent 189, but a 173-long dot product of them does.
    assert bs.slow_share(_case("bmm", center=189, spread=1, n=173)) > 0.5
    assert bs.slow_share(_case("bmm", center=189, spread=1, n=2)) < 0.05
    # The whole exponent range: one huge addend does not overflow a short sum, many do a long one.
    assert bs.slow_share(_case("sum", center=127, spread=126, length=300)) < 0.05
    assert bs.slow_share(_case("sum", center=127, spread=126, length=4000)) > 0.5
    # For add and mul, the share is the share of special operands.
    assert math.isclose(bs.slow_share(_case("mul", specRate=1000)), 0.01 * 3 / 5)


def test_a_slow_case_gets_fewer_steps_so_it_takes_about_as_long_as_a_clean_one(spec):
    clean = bs._equal_time(_case("sum"), spec, 100_000)
    poisoned = bs._equal_time(
        _case("sum", specRate=1, specPlace=1, specPeriod=8, specMask=bs.SPECIALS["nan"]),
        spec, 100_000,
    )  # fmt: skip
    assert clean["work"] == 100_000
    # Whole vectors only: its estimated time is between half and all of a clean case's.
    cost = 1 + spec["fallback_cost"]["sum"] * bs.slow_share(poisoned)
    assert 50_000 <= poisoned["work"] * cost <= 100_000
    assert poisoned["length"] == 1000


@pytest.mark.parametrize("work", [1, 1_000, 30_000])
def test_small_budgets_still_draw_nonempty_cases(spec, work):
    for case in bs.make_plan(spec, 5, work)["cases"]:
        assert case["work"] >= 1 and case["batch"] >= 1 and case["length"] >= 1


def test_drawn_sizes_vary_across_seeds(spec):
    lengths = {
        c["length"]
        for seed in range(20)
        for c in bs.make_plan(spec, seed, 500_000)["cases"]
        if c["kernel"] == "sum"
    }
    shapes = {
        (c["m"], c["n"], c["p"])
        for seed in range(20)
        for c in bs.make_plan(spec, seed, 500_000)["cases"]
        if c["kernel"] == "bmm"
    }
    assert len(lengths) > 100 and len(shapes) > 100
    assert min(lengths) < 8 and max(lengths) > 4096


def test_relations_apply_to_scalar_kernels_only(spec):
    plan = bs.make_plan(spec, 11, 500_000)
    assert all(c["relation"] == 0 for c in plan["cases"] if c["kernel"] in ("sum", "bmm"))
    assert any(c["relation"] != 0 for c in plan["cases"] if c["kernel"] in ("add", "mul"))


def test_draw_forms():
    import random

    rng = random.Random(0)
    assert bs.draw(rng, 5) == 5
    assert all(3 <= bs.draw(rng, [3, 9]) <= 9 for _ in range(50))
    assert bs.draw(rng, {"choices": ["a"]}) == "a"
    assert all(10 <= bs.draw(rng, {"loguniform": [10, 1000]}) <= 1000 for _ in range(50))


@pytest.mark.parametrize(
    "breakage",
    [
        lambda s: s.pop("version"),
        # Weights would say the score is not total time over total time.
        lambda s: s.update(kernel_weights=dict.fromkeys(bs.KERNELS, 1)),
        lambda s: s["groups"][0].update(weight=2),
        lambda s: s["work"].update(add=0),
        lambda s: s.pop("fallback_cost"),
        lambda s: s["fallback_cost"].update(sum=-1),
        lambda s: s["groups"].append(copy.deepcopy(s["groups"][0])),
        lambda s: s["groups"][0].update(name="check"),
        lambda s: s["groups"][0].update(name="a:b"),
        lambda s: s["groups"][0]["families"][0].update(colour="red"),
        lambda s: s["groups"][0].update(cases=0),
        lambda s: s["sizes"].pop("sum"),
        lambda s: s.update(groups=[g for g in s["groups"] if "kernels" in g]),
    ],
)
def test_a_broken_suite_is_refused(breakage):
    spec = json.loads(PUBLIC.read_text())
    breakage(spec)
    with pytest.raises(bs.SuiteError):
        bs.validate(spec)


def test_a_parameter_drawn_out_of_range_is_refused(spec):
    bad = copy.deepcopy(spec)
    bad["groups"][0]["families"] = [{"center": [250, 300]}]
    with pytest.raises(bs.SuiteError):
        for seed in range(10):
            bs.make_plan(bad, seed, 500_000)


def test_without_a_suite_the_gate_keeps_the_legacy_bench(monkeypatch):
    monkeypatch.delenv("GATE_FULL_SUITE", raising=False)
    assert bs.load() is None and bs.file_sha256() is None


def test_the_suite_counts_in_the_fingerprint_by_hash(monkeypatch, tmp_path):
    monkeypatch.delenv("GATE_FULL_SUITE", raising=False)
    legacy = fingerprint.current()
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    public = fingerprint.current()
    other = tmp_path / "other.json"
    other.write_text(PUBLIC.read_text().replace('"public-v2"', '"other-v2"'))
    monkeypatch.setenv("GATE_FULL_SUITE", str(other))
    assert len({legacy, public, fingerprint.current()}) == 3


def _fake_bench(plan_speed):
    # A bench whose times are each case's work times a speed that depends on who runs it.
    def bench(binary, seed, build, plan=None):
        cases = json.loads(plan)["cases"]
        return {
            c["name"]: (int(c["work"] * plan_speed(c, bool(build))) if c["timed"] else 0, 99)
            for c in cases
        }

    return bench


def test_the_gate_scores_a_suite_like_the_legacy_classes_and_keeps_the_plan_out(monkeypatch):
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    monkeypatch.setattr(gate, "_ROUNDS", 3)
    monkeypatch.setattr(gate, "_PAIRS", 20_000)

    # Twice as fast everywhere except specials, where it is four times slower.
    def speed(case, entry):
        if not entry:
            return 10.0
        return 40.0 if ":specials:" in case["name"] else 5.0

    monkeypatch.setattr(gate, "bench", _fake_bench(speed))
    v = gate.Verdict(submission="probe", slot="full")
    assert gate.score(Path("Kernel.lean"), v) is None
    spec = bs.load(PUBLIC)
    assert set(v.classes) == set(bs.cells(spec))
    # Total time over total time: the specials cases count by the time they take.
    timed = [c for c in bs.make_plan(spec, v.seed, 20_000)["cases"] if c["timed"]]
    inc = sum(int(c["work"] * 10.0) for c in timed)
    sub = sum(int(c["work"] * speed(c, True)) for c in timed)
    assert math.isclose(v.ratio, sub / inc)
    assert math.isclose(v.classes["sum:specials"]["ratio"], 4.0, rel_tol=1e-3)
    assert math.isclose(v.classes["sum:ml"]["incumbent_ns"], 10.0, rel_tol=1e-3)
    # The paired evidence admission reads is the legacy one: per round, the two totals.
    assert v.rounds == [[inc, sub]] * 3
    published = json.dumps(v.__dict__)
    # Results are public, the plan is not: no parameters, sizes, per-case seeds or cases.
    for secret in ("center", "spread", "specRate", "length", "batch", "work", "sum:ml:0"):
        assert f'"{secret}"' not in published
    assert v.suite == {"version": "public-v2", "sha256": spec["sha256"]}


def test_a_missing_case_rejects_the_entry_but_pauses_on_the_incumbent(monkeypatch):
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    monkeypatch.setattr(gate, "_ROUNDS", 2)
    monkeypatch.setattr(gate, "_PAIRS", 20_000)
    full = _fake_bench(lambda c, entry: 10.0)

    def drops(entry_side):
        def bench(binary, seed, build, plan=None):
            res = full(binary, seed, build, plan)
            if bool(build) == entry_side:
                res.pop(next(iter(res)))
            return res

        return bench

    monkeypatch.setattr(gate, "bench", drops(True))
    v = gate.Verdict(submission="probe", slot="full")
    assert "every case" in gate.score(Path("Kernel.lean"), v) and not v.incumbent_failed
    monkeypatch.setattr(gate, "bench", drops(False))
    v = gate.Verdict(submission="probe", slot="full")
    assert gate.score(Path("Kernel.lean"), v).startswith("gate error") and v.incumbent_failed


def test_a_checksum_mismatch_on_the_check_case_rejects(monkeypatch):
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    monkeypatch.setattr(gate, "_ROUNDS", 2)
    monkeypatch.setattr(gate, "_PAIRS", 20_000)
    full = _fake_bench(lambda c, entry: 10.0)

    def bench(binary, seed, build, plan=None):
        res = full(binary, seed, build, plan)
        if build:
            res["mul:check"] = (0, 98)
        return res

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="full")
    assert "checksum mismatch on class mul:check" in gate.score(Path("Kernel.lean"), v)


def test_practice_slots_ignore_the_suite(monkeypatch):
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    monkeypatch.setattr(gate, "_ROUNDS", 2)
    seen = []

    def bench(binary, seed, build, plan=None):
        seen.append(plan)
        return {"near-one": (100, 7)}

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="add")
    assert gate.score(Path("Kernel.lean"), v) is None and seen == [None] * 4 and not v.suite


def _suite_verdict(monkeypatch, entry_speed) -> gate.Verdict:
    # A gate verdict scored on the public suite with a fake bench.
    monkeypatch.setenv("GATE_FULL_SUITE", str(PUBLIC))
    monkeypatch.setattr(gate, "_ROUNDS", 6)
    monkeypatch.setattr(
        gate, "bench", _fake_bench(lambda c, entry: entry_speed(c) if entry else 10.0)
    )
    v = gate.Verdict(submission="probe", slot="full", passed=True)
    assert gate.score(Path("Kernel.lean"), v) is None
    return v


@pytest.mark.parametrize(
    ("speed", "promoted"),
    [
        (lambda c: 8.0, True),
        # Faster on most cases, but slow enough on specials that the total is above the bar.
        (lambda c: 60.0 if ":specials:" in c["name"] else 8.0, False),
    ],
)
def test_the_worker_and_the_weight_setter_judge_suite_verdicts_alike(
    monkeypatch, tmp_path, speed, promoted
):
    import datetime as dt
    from dataclasses import asdict
    from types import SimpleNamespace

    import admission
    from db import models
    from db.scoring import evidence_of
    from service import worker

    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(asdict(_suite_verdict(monkeypatch, speed))))
    fields = worker.recorded(path)
    at = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    assert worker.beats_champion(SimpleNamespace(id=7, hotkey="hk", submitted_at=at), fields) is (
        promoted
    )
    row = models.Submission(
        id=7, hotkey="hk", submitted_at=at, ratio=fields["ratio"], rounds=fields["rounds"],
        report=fields["report"],
    )  # fmt: skip
    e = evidence_of(row)
    assert len(e.rounds) == 6
    assert (admission.assess(e, 0.97).outcome == "qualifies") is promoted
