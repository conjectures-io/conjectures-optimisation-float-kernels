"""Statistical admission: the bootstrap decides who beats the champion, not one lucky round."""

from __future__ import annotations

import datetime as dt
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
