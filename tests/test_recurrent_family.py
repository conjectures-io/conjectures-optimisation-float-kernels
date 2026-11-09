"""The `recurrent` family: its `recurrent-v1` real adapter, Lean cell names and shape audit."""

import runpy
import struct
from pathlib import Path

import benchmark
import pytest
from workload import RealWorkloadSpec, recurrent_tensor_lengths

ROOT = Path(__file__).resolve().parents[1]
LSTM, GRU = 0, 1


def spec(**changes):
    raw = dict(
        id="toy-recurrent",
        family="recurrent",
        model="lstm-gru",
        dataset="toy-sequences",
        adapter="recurrent-v1",
        weights_path="weights/recurrent-v1/toy/weights.bin",
        weights_sha256="a" * 64,
        samples_path="samples/recurrent-v1/toy/samples.bin",
        samples_sha256="b" * 64,
        asset_revision="c" * 64,
        sample_count=2,
        sample_words=6,
        samples_per_run=1,
    )
    return RealWorkloadSpec.parse(raw | changes)


def header(steps=3, features=2, outputs=4, layers=((LSTM, 5), (GRU, 3)), version=1, count=None):
    words = [version, steps, features, outputs, len(layers) if count is None else count]
    for kind, hidden in layers:
        words += [kind, hidden]
    return words


def tensors_for(words):
    """Zero tensors with the lengths the header implies, computed here independently."""
    _, _, features, outputs, layers = words[:5]
    gates = {LSTM: 4, GRU: 3}
    result = [list(words)]
    width = features
    for layer in range(layers):
        kind, hidden = words[5 + 2 * layer : 7 + 2 * layer]
        rows = gates[kind] * hidden
        result += [[0] * (rows * width), [0] * (rows * hidden), [0] * rows, [0] * rows]
        width = hidden
    return result + [[0] * (outputs * width), [0] * outputs]


def write(tmp_path, tensors, sample_words=6, sample_count=2):
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"".join(struct.pack(f"<I{len(t)}I", len(t), *t) for t in tensors))
    samples = tmp_path / "samples.bin"
    samples.write_bytes(b"\0" * (4 * sample_words * sample_count))
    return weights, samples


@pytest.mark.parametrize(
    "layers", [((LSTM, 5), (GRU, 3)), ((GRU, 64),), ((LSTM, 1),), ((GRU, 2), (LSTM, 7))]
)
def test_well_formed_recurrent_fixture_is_accepted(tmp_path, layers):
    words = header(layers=layers)
    spec().validate_assets(*write(tmp_path, tensors_for(words)))
    assert len(tensors_for(words)) == 1 + 4 * len(layers) + 2


def test_header_lengths_follow_pytorch_gate_blocks():
    # LSTM 2 -> 5 then GRU 5 -> 3, head 3 -> 4.
    assert recurrent_tensor_lengths(header(), 6) == [
        9,
        20 * 2,
        20 * 5,
        20,
        20,
        9 * 5,
        9 * 3,
        9,
        9,
        4 * 3,
        4,
    ]


def test_largest_allowed_shape_is_accepted():
    words = header(steps=256, features=1024, outputs=256, layers=((LSTM, 64), (GRU, 64)))
    assert len(recurrent_tensor_lengths(words, 256 * 1024)) == 11


@pytest.mark.parametrize(
    "words,sample_words,message",
    [
        (header(version=2), 6, "version"),
        (header(version=0), 6, "version"),
        (header(layers=((2, 5),)), 6, "unknown kind"),
        (header(layers=((LSTM, 5), (7, 3))), 6, "unknown kind"),
        (header(layers=((LSTM, 65),)), 6, "hidden size"),
        (header(layers=((GRU, 0),)), 6, "hidden size"),
        (header(layers=()), 6, "layer count"),
        (header(steps=0), 0, "steps"),
        (header(steps=257, features=1), 257, "steps"),
        (header(features=1025, steps=1), 1025, "features"),
        (header(features=0), 0, "features"),
        (header(outputs=0), 6, "outputs"),
        (header(outputs=257), 6, "outputs"),
        (header(), 7, "T\\*F"),
        (header(steps=2), 6, "T\\*F"),
        (header(count=1), 6, "must hold 7 words"),
        (header(layers=((LSTM, 5),), count=2), 6, "must hold 9 words"),
        (header()[:4], 6, "must hold version"),
        ([], 6, "must hold version"),
    ],
)
def test_bad_headers_are_rejected(words, sample_words, message):
    with pytest.raises(ValueError, match=message):
        recurrent_tensor_lengths(words, sample_words)


@pytest.mark.parametrize(
    "words,sample_words,message",
    [
        (header(version=2), 6, "version"),
        (header(layers=((3, 5),)), 6, "unknown kind"),
        (header(layers=((LSTM, 65),)), 6, "hidden size"),
        (header(), 4, "T\\*F"),
    ],
)
def test_bad_headers_are_rejected_by_asset_validation(tmp_path, words, sample_words, message):
    tensors = [list(words), [0] * 10]
    with pytest.raises(ValueError, match=f"toy-recurrent: .*{message}"):
        spec(sample_words=sample_words).validate_assets(
            *write(tmp_path, tensors, sample_words=sample_words)
        )


def test_too_many_layers_are_rejected(tmp_path):
    three = header(layers=((LSTM, 4), (GRU, 4), (LSTM, 4)))
    with pytest.raises(ValueError, match="layer count"):
        recurrent_tensor_lengths(three, 6)
    # In a weights file the header alone exceeds what two layers can describe.
    with pytest.raises(ValueError, match="recurrent header has 11 words"):
        spec().validate_assets(*write(tmp_path, tensors_for(three)))


@pytest.mark.parametrize(
    "damage",
    ["drop-head-bias", "extra-tensor", "short-wih", "long-whh", "swapped-gates", "short-head"],
)
def test_wrong_tensor_lengths_are_rejected(tmp_path, damage):
    words = header()
    tensors = tensors_for(words)
    if damage == "drop-head-bias":
        tensors.pop()
    elif damage == "extra-tensor":
        tensors.append([0])
    elif damage == "short-wih":
        tensors[1].pop()
    elif damage == "long-whh":
        tensors[2].append(0)
    elif damage == "swapped-gates":
        # Second layer written as an LSTM while the header says GRU.
        tensors[5:9] = tensors_for(header(layers=((LSTM, 5), (LSTM, 3))))[5:9]
    else:
        tensors[-2] = [0] * (4 * 5)
    with pytest.raises(ValueError, match="does not match recurrent adapter"):
        spec().validate_assets(*write(tmp_path, tensors))


def test_sample_file_must_match_t_times_f(tmp_path):
    weights, samples = write(tmp_path, tensors_for(header()))
    samples.write_bytes(b"\0" * (4 * 5 * 2))
    with pytest.raises(ValueError, match="sample file size"):
        spec().validate_assets(weights, samples)


def test_truncated_or_empty_weights_are_rejected(tmp_path):
    weights, samples = write(tmp_path, tensors_for(header()))
    weights.write_bytes(weights.read_bytes()[:-4])
    with pytest.raises(ValueError, match="truncated"):
        spec().validate_assets(weights, samples)
    weights.write_bytes(b"")
    with pytest.raises(ValueError, match="empty weights"):
        spec().validate_assets(weights, samples)


@pytest.mark.parametrize(
    "adapter,family",
    [
        ("recurrent-v1", "mlp"),
        ("recurrent-v1", "transformer"),
        ("image-zoo-v1", "recurrent"),
        ("uci-har-v1", "recurrent"),
        ("appliance-mixer-v1", "recurrent"),
    ],
)
def test_recurrent_adapter_is_only_for_the_recurrent_family(adapter, family):
    with pytest.raises(ValueError, match="adapter/family"):
        spec(adapter=adapter, family=family)


def test_public_recurrent_cells_map_to_lean_rnn_cells():
    assert benchmark.LEAN_MODELS["recurrent"] == "rnn"
    for batch in (1, 4):
        for kind in ("normal", "wide", "sparse"):
            assert benchmark.lean_cell(f"recurrent:b{batch}:{kind}") == f"rnn:b{batch}:{kind}"


def test_real_recurrent_rows_are_reported_under_the_rnn_normal_cell():
    request = {
        "workloads": [{"id": "draw", "seed": 7, "profile": "typical", "weight": 1.0}],
        "cells": ["recurrent:b4:wide"],
        "real_workloads": [{"id": "har-gru", "family": "recurrent"}],
    }
    assert benchmark.row_names(request) == {
        "draw|rnn:b4:wide": "draw|recurrent:b4:wide",
        "har-gru|rnn:b1:normal": "har-gru|recurrent",
    }


def shape_layer(op, dims, weights, bias, output, saved_after=0):
    return dict(
        op=op,
        dims=dims,
        weights=weights,
        bias=bias,
        parameters_finite=True,
        valid=True,
        output=output,
        saved_after=saved_after,
    )


def test_shape_audit_follows_recurrent_layers():
    step = runpy.run_path(str(ROOT / "scripts/audit-exact-models.py"))["shape_step"]
    T, F, H = 5, 3, 4
    lstm = shape_layer("lstm", [T, F, H], 4 * H * F + 4 * H * H, 8 * H, T * H)
    gru = shape_layer("gru", [T, H, H], 3 * H * H + 3 * H * H, 6 * H, T * H)
    last = shape_layer("lastStep", [T, H], 0, 0, H)
    assert step(lstm, (T * F, 0)) == (T * H, 0)
    assert step(gru, (T * H, 0)) == (T * H, 0)
    assert step(last, (T * H, 0)) == (H, 0)
    for bad, shape in [
        (lstm, (T * F + 1, 0)),
        (dict(lstm, weights=lstm["weights"] - 1), (T * F, 0)),
        (dict(gru, bias=8 * H), (T * H, 0)),
        (dict(gru, op="lstm"), (T * H, 0)),
        (last, (T * H - 1, 0)),
        (dict(last, output=T * H), (T * H, 0)),
    ]:
        with pytest.raises(AssertionError):
            step(bad, shape)
