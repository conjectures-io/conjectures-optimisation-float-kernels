"""The exact-runtime slot's scoring: calibration, the median round score, the regression guard."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from pathlib import Path

import pytest

import gate
from validator import admission

# Two models; `a` has one cell, `b` has two, so `a`'s cell weighs as much as both of `b`'s.
CELLS = ["a:b1:normal", "b:b1:normal", "b:b1:wide"]


def fake_bench(champion_ns, entry_ns, calls):
    # A bench that answers the calibration run, the champion's runs and the entry's runs.
    def bench(binary, seed, build, extra=None):
        # Answers each run with its side's fixed times and one checksum.
        calls.append((build, dict(extra or {})))
        ns = entry_ns if build else champion_ns
        return {c: (ns[c], 7) for c in CELLS}

    return bench


@pytest.fixture
def scoring(monkeypatch):
    # Three rounds, a 100 ms calibration target, the default guard.
    monkeypatch.setattr(gate, "_ROUNDS", 3)
    monkeypatch.setattr(gate, "_CELL_NS", 100_000_000)
    monkeypatch.setattr(gate, "progress", lambda *a, **k: None)


def run(monkeypatch, champion_ns, entry_ns):
    # Scores an entry with the fake bench: the reason, the verdict and the bench calls.
    calls: list = []
    monkeypatch.setattr(gate, "bench", fake_bench(champion_ns, entry_ns, calls))
    v = gate.Verdict(submission="probe", slot="exact")
    return gate.score(Path("Kernel.lean"), v), v, calls


def test_a_uniformly_faster_entry_scores_its_ratio(monkeypatch, scoring):
    # The same speed-up on every cell is the score, in every round.
    champion = {c: 10_000_000 for c in CELLS}
    reason, v, _ = run(monkeypatch, champion, {c: 8_000_000 for c in CELLS})
    assert reason is None
    assert v.ratio == pytest.approx(0.8)
    assert v.rounds == [[1_000_000_000, 800_000_000]] * 3


def test_models_weigh_equally_whatever_their_cell_count(monkeypatch, scoring):
    # Model `a` twice as fast, model `b` unchanged: half the weight on 0.5, half on 1.0.
    champion = {c: 10_000_000 for c in CELLS}
    entry = {"a:b1:normal": 5_000_000, "b:b1:normal": 10_000_000, "b:b1:wide": 10_000_000}
    reason, v, _ = run(monkeypatch, champion, entry)
    assert reason is None
    assert v.ratio == pytest.approx(math.sqrt(0.5))


def test_a_slow_group_is_rejected_by_the_guard(monkeypatch, scoring):
    # Much faster overall, but 1.2x the champion's time on `b` with wide data.
    champion = {c: 10_000_000 for c in CELLS}
    entry = {"a:b1:normal": 2_000_000, "b:b1:normal": 2_000_000, "b:b1:wide": 12_000_000}
    reason, v, _ = run(monkeypatch, champion, entry)
    assert reason is not None and reason.startswith("regression guard: b:wide")
    assert v.ratio < 0.97 and v.classes["guard"]["ok"] is False


def test_calibration_repeats_each_cell_to_the_target_for_both_sides(monkeypatch, scoring):
    # 10 ms, 30 ms and 250 ms on the champion: 10, 4 and 1 repetitions for a 100 ms target.
    champion = {"a:b1:normal": 10_000_000, "b:b1:normal": 30_000_000, "b:b1:wide": 250_000_000}
    reason, _, calls = run(monkeypatch, champion, champion)
    assert reason is None
    calibration, *timed = calls
    assert calibration == ("", {"BENCH_INNER": "1"})
    reps = {"BENCH_REPS": "a:b1:normal=10,b:b1:normal=4,b:b1:wide=1"}
    assert len(timed) == 6 and all(extra == reps for _, extra in timed)


def test_a_failed_calibration_is_the_champions(monkeypatch, scoring):
    # A champion that cannot calibrate fails as the champion, which pauses the slot.
    def bench(binary, seed, build, extra=None):
        # Every run of the champion fails.
        return "bench_incumbent killed"

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="exact")
    reason = gate.score(Path("Kernel.lean"), v)
    assert reason is not None and reason.startswith("gate error:") and v.incumbent_failed


def test_score_and_admission_use_the_same_statistic(monkeypatch, scoring):
    # Rounds of 0.6, 0.9 and 0.7: the gate's score and admission's median both give 0.7.
    factors = iter([0.6, 0.9, 0.7])
    calls: list = []

    def bench(binary, seed, build, extra=None):
        # The entry's rounds at 0.6, 0.9 and 0.7 of the champion's times.
        calls.append(build)
        f = next(factors) if build else 1.0
        return {c: (int(10_000_000 * f), 7) for c in CELLS}

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="exact")
    assert gate.score(Path("Kernel.lean"), v) is None
    e = admission.Evidence(1, "hk", v.ratio, dt.datetime.now(dt.UTC), tuple(map(tuple, v.rounds)))
    assert v.ratio == pytest.approx(0.7)
    assert statistics.median(admission._ratios(e)) == pytest.approx(v.ratio)
