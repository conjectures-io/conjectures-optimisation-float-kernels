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
    assert math.isclose(sum(cells.values()), 1.0)
    # Each kernel weighs the same, however many groups time it.
    for kernel in bs.KERNELS:
        share = sum(w for c, w in cells.items() if c.startswith(f"{kernel}:"))
        assert math.isclose(share, 0.25)


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


def test_each_kernel_spends_about_its_work_budget(spec):
    plan = bs.make_plan(spec, 3, 500_000)
    for kernel in bs.KERNELS:
        work = sum(c["work"] for c in plan["cases"] if c["kernel"] == kernel and c["timed"])
        assert 0.8 * 3 * 500_000 <= work <= 1.3 * 3 * 500_000, (kernel, work)


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
        lambda s: s["kernel_weights"].pop("bmm"),
        lambda s: s["work"].update(add=0),
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


def test_the_gate_scores_a_suite_by_cells_and_keeps_the_plan_out(monkeypatch):
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
    cells = bs.cells(bs.load(PUBLIC))
    assert set(v.classes) == set(cells)
    specials = sum(w for c, w in cells.items() if c.endswith(":specials"))
    expected = math.exp(specials * math.log(4.0) + (1 - specials) * math.log(0.5))
    assert math.isclose(v.ratio, expected, rel_tol=1e-3)
    assert math.isclose(v.classes["sum:specials"]["ratio"], 4.0, rel_tol=1e-3)
    assert len(v.rounds) == 3 and all(r[0] == 1.0 for r in v.rounds)
    published = json.dumps(v.__dict__)
    # Results are public, the plan is not: no parameters, sizes or per-case seeds.
    for secret in ("center", "spread", "specRate", "length", "batch", "work"):
        assert f'"{secret}"' not in published
    assert v.suite["version"] == "public-v2" and len(v.suite["rounds"]) == 3


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
        # Faster overall, but a whole group slower than the champion: refused.
        (lambda c: 13.0 if ":specials:" in c["name"] else 6.0, False),
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
    assert e.suite is not None and len(e.suite.rounds) == 6
    assert (admission.assess(e, 0.97).outcome == "qualifies") is promoted
