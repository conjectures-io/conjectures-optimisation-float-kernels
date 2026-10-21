"""Statistical admission: the bootstrap decides who beats the champion, not one lucky round."""

from __future__ import annotations

import datetime as dt
import json
import math
import random

import admission

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
BAR = 0.97


def evidence(sid: int, true_ratio: float, *, noise: float = 0.01, rounds: int = 25, at: int = 0):
    # Paired rounds around `true_ratio`, each run jittered like a pinned core (about 1%).
    rng = random.Random(sid)
    pairs = []
    for _ in range(rounds):
        inc = 1e7 * (1 + rng.gauss(0, noise))
        pairs.append((inc, 1e7 * true_ratio * (1 + rng.gauss(0, noise))))
    headline = min(s for _, s in pairs) / min(i for i, _ in pairs)
    return admission.Evidence(
        sid, f"hk{sid}", headline, T0 + dt.timedelta(minutes=at), tuple(pairs)
    )


def test_a_clearly_faster_kernel_qualifies():
    d = admission.assess(evidence(1, 0.80), BAR)
    assert d.outcome == "qualifies" and d.gain_p05 is not None and d.gain_p05 > 0.15


def test_the_champion_against_itself_does_not_qualify():
    # A lucky headline ratio under the bar is not enough without significance.
    e = evidence(2, 1.0, noise=0.03)
    e = admission.Evidence(e.submission_id, e.hotkey, 0.96, e.submitted_at, e.rounds)
    assert admission.assess(e, BAR).outcome == "not-significant"


def test_a_copy_of_the_champion_does_not_qualify():
    # Timed against the champion it copies, a copy sits at 1.00: above the bar.
    assert admission.assess(evidence(3, 1.0), BAR).outcome == "above-bar"


def test_above_the_bar_and_thin_evidence_never_qualify():
    assert admission.assess(evidence(1, 0.99), BAR).outcome == "above-bar"
    assert admission.assess(evidence(2, 0.5, rounds=3), BAR).outcome == "no-evidence"


def test_decisions_are_reproducible():
    e = evidence(4, 0.9)
    assert admission.assess(e, BAR) == admission.assess(e, BAR)


# ── Suite evidence ───────────────────────────────────────────


def suite_evidence(sid: int, speed, *, cells=6, per_cell=4, rounds=25, noise=0.01, ratio=None):
    # A suite verdict's evidence: `speed(cell, case)` is the case's true ratio to the champion.
    rng = random.Random(sid)
    names = [f"k:g{c}" for c in range(cells)]
    case_cells = [c for c in range(cells) for _ in range(per_cell)]
    true = [speed(c, j) for c in range(cells) for j in range(per_cell)]
    rows = tuple(
        tuple((1e6 * (1 + rng.gauss(0, noise)), 1e6 * t * (1 + rng.gauss(0, noise))) for t in true)
        for _ in range(rounds)
    )
    best = [min(r[k][1] for r in rows) / min(r[k][0] for r in rows) for k in range(len(true))]
    cell_ratios = tuple(
        math.exp(sum(math.log(best[k]) for k in range(len(true)) if case_cells[k] == c) / per_cell)
        for c in range(cells)
    )
    weights = tuple([1 / cells] * cells)
    headline = math.exp(sum(w * math.log(r) for w, r in zip(weights, cell_ratios)))
    suite = admission.SuiteEvidence(tuple(names), weights, cell_ratios, tuple(case_cells), rows)
    return admission.Evidence(sid, f"hk{sid}", headline if ratio is None else ratio, T0, (), suite)


def test_a_kernel_faster_on_every_kind_of_input_qualifies():
    d = admission.assess(suite_evidence(10, lambda c, j: 0.85), BAR)
    assert d.outcome == "qualifies" and d.gain_p05 > 0.1


def test_a_fast_path_that_slows_one_cell_is_refused():
    # Much faster overall, but one kind of input pays for it.
    e = suite_evidence(11, lambda c, j: 1.3 if c == 0 else 0.6)
    assert e.ratio < BAR and admission.assess(e, BAR).outcome == "regressed"


def test_a_gain_from_a_few_lucky_cases_is_not_significant():
    # One case of the run is very fast and carries the headline; resampling the cases exposes it.
    e = suite_evidence(12, lambda c, j: 0.2 if (c, j) == (0, 0) else 1.01, noise=0.002)
    assert e.ratio < BAR
    assert admission.assess(e, BAR).outcome == "not-significant"


def test_suite_copies_and_thin_suite_evidence_never_qualify():
    assert admission.assess(suite_evidence(13, lambda c, j: 1.0), BAR).outcome == "above-bar"
    thin = suite_evidence(14, lambda c, j: 0.5, rounds=3)
    assert admission.assess(thin, BAR).outcome == "no-evidence"


def test_suite_decisions_are_reproducible():
    e = suite_evidence(15, lambda c, j: 0.9)
    assert admission.assess(e, BAR) == admission.assess(e, BAR)


def test_suite_evidence_is_read_from_a_verdict():
    verdict = {
        "classes": {"add:a": {"ratio": 0.9}, "sum:b": {"ratio": 1.01}},
        "suite": {
            "cells": {"sum:b": 0.5, "add:a": 0.5},
            "cases": ["add:a", "sum:b", "sum:b"],
            "rounds": [[[10, 9], [10, 10], [20, 21]]],
        },
    }
    s = admission.suite_evidence(json.dumps(verdict))
    assert s.cells == ("add:a", "sum:b") and s.case_cells == (0, 1, 1)
    assert s.cell_ratios == (0.9, 1.01) and s.rounds[0][2] == (20.0, 21.0)
    assert admission.suite_evidence(json.dumps({"classes": {}})) is None
    assert admission.suite_evidence(None) is None and admission.suite_evidence("{") is None
