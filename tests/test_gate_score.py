"""The score stage: whose fault a failed bench is, and what the gate reads from a bench's output."""

from __future__ import annotations

from pathlib import Path

import gate


def test_an_incumbent_bench_failure_is_the_champions_not_the_entrys(monkeypatch):
    # The pinned incumbent's bench runs with no build layer; its failure is a gate error.
    def bench(binary, seed, build):
        return "bench_incumbent killed" if not build else {"near-one": (100, 7)}

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="add")
    reason = gate.score(Path("Kernel.lean"), v)
    assert reason is not None and reason.startswith("gate error:") and v.incumbent_failed


def test_an_entry_bench_failure_is_the_entrys(monkeypatch):
    # The entry's own bench runs in its layer; its failure rejects the entry.
    def bench(binary, seed, build):
        return "bench killed" if build else {"near-one": (100, 7)}

    monkeypatch.setattr(gate, "bench", bench)
    v = gate.Verdict(submission="probe", slot="add")
    reason = gate.score(Path("Kernel.lean"), v)
    assert reason == "bench killed" and not v.incumbent_failed


def test_a_stray_output_line_is_skipped_not_parsed(monkeypatch):
    # A line that only looks like a row, as a panic message could print, never crashes the parse.
    out = "near-one x 1 2\nnear-one 1 2 3\nnear-one true 123 456\n"
    monkeypatch.setattr(gate, "run", lambda cmd, timeout, **kw: (0, out))
    assert gate.bench(Path("bench"), 1, "") == {"near-one": (123, 456)}


def test_a_panic_aborts_the_bench(monkeypatch):
    # With LEAN_ABORT_ON_PANIC a panic ends the process, so its message never counts as output.
    seen: dict[str, str] = {}

    def run(cmd, timeout, **kw):
        seen.update(kw["env_extra"])
        return 0, ""

    monkeypatch.setattr(gate, "run", run)
    gate.bench(Path("bench"), 1, "")
    assert seen["LEAN_ABORT_ON_PANIC"] == "1"
