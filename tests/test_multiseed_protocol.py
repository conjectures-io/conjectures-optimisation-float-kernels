"""The queued batch protocol, hierarchical admission and malformed worker evidence."""

import copy
from pathlib import Path

import benchmark
import pytest
import sandbox
from config import MeasurementRequest, measurement_hash
from multiseed import calibrated_counts, workloads
from scoring import assess


@pytest.fixture
def pair():
    req = dict(
        environment="test",
        identity="a" * 64,
        seed=123,
        candidate_hash="b" * 64,
        reference_hash="c" * 64,
        cells=["mlp:b1:normal"],
        timing=dict(
            rounds=11,
            inner=3,
            cell_ms=50,
            run_seconds=300,
            build_seconds=480,
            benchmark_seconds=900,
        ),
        benchmark_identity="d" * 64,
        workloads=workloads(123),
    )
    cells = {
        f"{w['id']}|mlp:b1:normal": dict(
            reference_ns=1000, candidate_ns=800, reference_checksum=42, candidate_checksum=42
        )
        for w in req["workloads"]
    }
    result = dict(
        request_hash=measurement_hash(req),
        provenance=dict(identity="a" * 64, machine="test"),
        workloads=req["workloads"],
        repetitions=dict.fromkeys(cells, 1),
        rounds=[dict(reference_first=i % 2 == 0, cells=copy.deepcopy(cells)) for i in range(11)],
    )
    plan = dict(
        strata=[dict(environment="test", identity="a" * 64, seed=123, weight=1)],
        cells={"mlp:b1:normal": 1},
        timing=req["timing"],
        benchmark_identity="d" * 64,
        admission=dict(bar=0.97, guard=1.1, draws=2000, quantile=0.05, min_rounds=5),
    )
    return req, result, plan


def test_qualifies(pair):
    req, res, plan = pair
    out = assess(plan, [(req, res)])
    assert out["score"] == pytest.approx(0.8)
    assert out["outcome"] == "qualifies"


@pytest.mark.parametrize("kind", ["copy", "regression"])
def test_not_qualify(pair, kind):
    req, res, plan = pair
    for r in res["rounds"]:
        for c in r["cells"].values():
            c["candidate_ns"] = 1000 if kind == "copy" else 1200
    assert assess(plan, [(req, res)])["outcome"] == (
        "above-bar" if kind == "copy" else "guard-failed"
    )


@pytest.mark.parametrize(
    "bad", ["missing", "checksum", "metadata", "reps", "order", "identity", "duplicate"]
)
def test_invalid(pair, bad):
    req, res, plan = pair
    if bad == "missing":
        res["rounds"][0]["cells"].pop("s0|mlp:b1:normal")
    if bad == "checksum":
        res["rounds"][0]["cells"]["s0|mlp:b1:normal"]["candidate_checksum"] = 99
    if bad == "metadata":
        res["workloads"] = []
    if bad == "reps":
        res["repetitions"].pop("s0|mlp:b1:normal")
    if bad == "order":
        res["rounds"][0]["reference_first"] = False
    if bad == "identity":
        res["provenance"]["identity"] = "e" * 64
    with pytest.raises(ValueError):
        assess(plan, [(req, res)] * (2 if bad == "duplicate" else 1))


def test_two_devices(pair):
    req, res, plan = pair
    second = copy.deepcopy(req)
    second["environment"] = "test2"
    r2 = copy.deepcopy(res)
    r2["request_hash"] = measurement_hash(second)
    plan["strata"][0]["weight"] = 0.5
    plan["strata"].append(dict(environment="test2", identity="a" * 64, seed=123, weight=0.5))
    out = assess(plan, [(req, res), (second, r2)])
    assert out["outcome"] == "qualifies"
    assert len(out["strata"]) == 2


def test_measurement_order_does_not_change_admission(pair):
    req, res, plan = pair
    second = copy.deepcopy(req)
    second["environment"] = "test2"
    r2 = copy.deepcopy(res)
    r2["request_hash"] = measurement_hash(second)
    plan["strata"][0]["weight"] = 0.5
    plan["strata"].append(dict(environment="test2", identity="a" * 64, seed=123, weight=0.5))
    pairs = [(req, res), (second, r2)]
    assert assess(plan, pairs) == assess(plan, pairs[::-1])


@pytest.mark.parametrize("field", ["seed", "profile", "weight", "id"])
def test_request_cannot_override_workload_policy(pair, field):
    req, _, _ = pair
    req["workloads"][0][field] = 0
    with pytest.raises(ValueError, match="workload plan"):
        MeasurementRequest.parse(req)


def test_calibration_uses_actual_previous_counts_and_rounds_up():
    assert calibrated_counts({"a": (30_000_000, 0)}, 50) == {"a": 2}
    assert calibrated_counts({"a": (60_000_000, 0)}, 50, {"a": 2}) == {"a": 2}
    assert calibrated_counts({"a": (1, 0)}, 50) == {"a": 1_000_000}


@pytest.mark.parametrize("bad", ["duplicate", "missing", "unknown", "checksum"])
def test_binary_protocol_rejects_bad_rows(pair, monkeypatch, bad):
    req, _, _ = pair
    lines = [f"{w['id']}|mlp:b1:normal true 100 42" for w in req["workloads"]]
    if bad == "duplicate":
        lines.append(lines[0])
    elif bad == "missing":
        lines.pop()
    elif bad == "unknown":
        lines[0] = "s0|unknown true 100 42"
    else:
        lines[0] = "s0|mlp:b1:normal true 100 -1"
    monkeypatch.setattr(sandbox, "run", lambda *a, **kw: (0, "\n".join(lines)))
    with pytest.raises(benchmark.MeasurementFailure):
        benchmark.run_binary(
            Path("unused"), req, {}, 1, "reference", benchmark.Deadline("benchmark", 10)
        )


def test_binary_protocol_translates_workloads_and_repetitions(pair, monkeypatch):
    req, _, _ = pair
    req["cells"] = ["resnet:b1:normal"]

    def run(command, timeout, **kw):
        env = kw["env_extra"]
        assert env["BENCH_CELLS"] == "cnn:b1:normal"
        assert env["BENCH_REPS"] == "s0|cnn:b1:normal=7"
        assert env["BENCH_WORKLOADS"] == ",".join(
            f"{w['id']}:{w['seed']}:{w['profile']}" for w in req["workloads"]
        )
        return 0, "\n".join(f"{w['id']}|cnn:b1:normal true 100 42" for w in req["workloads"])

    monkeypatch.setattr(sandbox, "run", run)
    rows = benchmark.run_binary(
        Path("unused"),
        req,
        {"s0|resnet:b1:normal": 7},
        3,
        "reference",
        benchmark.Deadline("benchmark", 10),
    )
    assert set(rows) == {f"s{i}|resnet:b1:normal" for i in range(6)}
