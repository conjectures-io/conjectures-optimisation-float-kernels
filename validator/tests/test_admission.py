"""Statistical admission: the bootstrap decides who may lead, not one lucky round."""

from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admission  # noqa: E402

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


def outcomes(decisions):
    return {d.submission_id: d.outcome for d in decisions}


def test_a_clearly_faster_kernel_leads():
    decisions = admission.decide([evidence(1, 0.80)], BAR)
    assert outcomes(decisions) == {1: "leader"} and admission.leader(decisions) == 1
    assert decisions[0].gain_p05 is not None and decisions[0].gain_p05 > 0.15


def test_the_incumbent_against_itself_does_not_qualify():
    # A lucky headline ratio under the bar is not enough without significance.
    e = evidence(2, 1.0, noise=0.03)
    e = admission.Evidence(e.submission_id, e.hotkey, 0.96, e.submitted_at, e.rounds)
    assert outcomes(admission.decide([e], BAR)) == {2: "not-significant"}


def test_a_later_copy_of_the_leader_does_not_take_the_lead():
    first, copy = evidence(1, 0.80, at=0), evidence(2, 0.80, at=5)
    decisions = admission.decide([copy, first], BAR)
    assert outcomes(decisions) == {1: "leader", 2: "not-significant"}
    assert admission.leader(decisions) == 1
    assert decisions[1].reference == 1


def test_a_real_improvement_takes_the_lead():
    decisions = admission.decide([evidence(1, 0.80, at=0), evidence(2, 0.70, at=5)], BAR)
    assert outcomes(decisions) == {1: "leader", 2: "leader"}
    assert admission.leader(decisions) == 2 and decisions[1].reference == 1


def test_above_the_bar_and_thin_evidence_never_lead():
    decisions = admission.decide([evidence(1, 0.99), evidence(2, 0.5, rounds=3)], BAR)
    assert outcomes(decisions) == {1: "above-bar", 2: "no-evidence"}
    assert admission.leader(decisions) is None


def test_decisions_are_reproducible():
    subs = [evidence(i, 0.8 - 0.01 * i, at=i) for i in range(1, 6)]
    assert admission.decide(subs, BAR) == admission.decide(list(reversed(subs)), BAR)
