"""The kings export: each paid champion's file and the exact source it beat, read-only."""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from pathlib import Path

import db as store_pkg
from db import models

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validator" / "tools"))
import kings  # noqa: E402


def _promote(store, hotkey: str, kernel: bytes, beat_sha: str, *, demoted: bool = False) -> int:
    # An accepted full entry with its file, promoted after beating the source hashed `beat_sha`.
    sub_id, _ = store.submissions.add(hotkey, hashlib.sha256(kernel).hexdigest(), "full", kernel)
    with store_pkg.session_scope(store.sessions) as session:
        row = session.get(models.Submission, sub_id)
        row.state, row.ratio, row.champion_sha256 = "accepted", 0.9, beat_sha
        session.add(
            models.Promotion(
                slot="full",
                submission_id=sub_id,
                champion_sha256=hashlib.sha256(kernel).hexdigest(),
                demoted_at=dt.datetime.now(dt.timezone.utc) if demoted else None,
            )
        )
    return sub_id


def test_kings_are_written_with_what_each_beat(store, tmp_path):
    shipped = hashlib.sha256(kings.SHIPPED.read_bytes()).hexdigest()
    a = _promote(store, "hk-a", b"-- A", shipped)
    b = _promote(store, "hk-b", b"-- B", hashlib.sha256(b"-- A").hexdigest())
    c = _promote(store, "hk-c", b"-- C", hashlib.sha256(b"-- B").hexdigest(), demoted=True)
    d = _promote(store, "hk-d", b"-- D", hashlib.sha256(b"-- C").hexdigest())
    e = _promote(store, "hk-e", b"-- E", hashlib.sha256(b"-- D").hexdigest())
    written = kings.export(store, tmp_path)
    # Newest first, demoted ones skipped, as many as are paid.
    assert [k["id"] for k in written] == [e, d, b]
    assert c and a
    files = {k["id"]: k["dir"] for k in written}
    assert (files[e] / "Kernel.lean").read_bytes() == b"-- E"
    assert (files[e] / "beat" / "Kernel.lean").read_bytes() == b"-- D"
    # D beat C, since demoted: still the file it was timed against.
    assert (files[d] / "beat" / "Kernel.lean").read_bytes() == b"-- C"
    assert (files[b] / "beat" / "Kernel.lean").read_bytes() == b"-- A"


def test_the_first_king_beat_the_shipped_incumbent(store, tmp_path):
    shipped = hashlib.sha256(kings.SHIPPED.read_bytes()).hexdigest()
    a = _promote(store, "hk-a", b"-- A", shipped)
    (k,) = kings.export(store, tmp_path)
    assert k["id"] == a and k["beat"] == "shipped"
    assert (k["dir"] / "beat" / "Kernel.lean").read_bytes() == kings.SHIPPED.read_bytes()


def test_nothing_is_written_without_a_promotion(store, tmp_path):
    assert kings.export(store, tmp_path) == []
