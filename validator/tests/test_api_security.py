"""What the API refuses - bad shape, stale signature, big body, flood - before it costs anything."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from conftest import kernel_file, post_submission, register

VALIDATOR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VALIDATOR))

from service import security, sig  # noqa: E402
from service.settings import MAX_FILE_BYTES, MAX_REQUEST_BYTES  # noqa: E402

ALICE = sig.load_keypair("//Alice")
BOB = sig.load_keypair("//Bob")


def reason(response) -> str:
    return response.json()["detail"]["reason"]


# ── Signatures ────────────────────────────────────────────────────────────


def test_someone_elses_signature_is_refused(client, store, tmp_path):
    register(store, ALICE.ss58_address)
    r = post_submission(client, ALICE, kernel_file(tmp_path, "a"), signer=BOB)
    assert r.status_code == 401 and "does not verify" in reason(r)


def test_a_signature_over_different_files_is_refused(client, store, tmp_path):
    # The digest binds the signature to this file; signing one and uploading another must fail.
    register(store, ALICE.ss58_address)
    d = kernel_file(tmp_path, "a")
    r = post_submission(client, ALICE, d, data=b"another kernel")
    assert r.status_code == 401 and "does not verify" in reason(r)


@pytest.mark.parametrize("skew", [-3600, -301, 301, 3600])
def test_a_stale_or_future_timestamp_is_refused(client, store, tmp_path, skew):
    # Both directions: stale is a replay, future outlives the round it was signed for.
    register(store, ALICE.ss58_address)
    stamp = int(time.time()) + skew
    r = post_submission(client, ALICE, kernel_file(tmp_path, "a"), timestamp=stamp)
    assert r.status_code == 401 and "from the validator's clock" in reason(r)


def test_a_captured_request_cannot_be_replayed_later(client, store, tmp_path, monkeypatch):
    # The exact bytes of a valid request, sent again once the window has passed.
    register(store, ALICE.ss58_address)
    d = kernel_file(tmp_path, "a", "RATIO=0.92")
    stamp = int(time.time())
    assert post_submission(client, ALICE, d, timestamp=stamp).status_code == 200
    monkeypatch.setattr(time, "time", lambda: stamp + 3600)
    replay = post_submission(client, ALICE, d, timestamp=stamp)
    assert replay.status_code == 401 and "from the validator's clock" in reason(replay)


# ── Shapes ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hotkey", ["not-an-address", "0x1234", "l" * 48, "5" * 200])
def test_a_malformed_hotkey_is_refused_before_any_crypto(client, tmp_path, hotkey):
    d = kernel_file(tmp_path, "a")
    r = client.post(
        "/submit",
        data={"hotkey": hotkey, "signature": "ab" * 64, "timestamp": int(time.time())},
        files={
            "Kernel.lean": ("Kernel.lean", (d / "Kernel.lean").read_bytes()),
        },
    )
    assert r.status_code == 400 and "ss58" in reason(r)


@pytest.mark.parametrize("signature", ["00", "0x" + "z" * 128, "ab" * 63])
def test_a_malformed_signature_is_refused(client, tmp_path, signature):
    d = kernel_file(tmp_path, "a")
    r = client.post(
        "/submit",
        data={
            "hotkey": ALICE.ss58_address,
            "signature": signature,
            "timestamp": int(time.time()),
        },
        files={
            "Kernel.lean": ("Kernel.lean", (d / "Kernel.lean").read_bytes()),
        },
    )
    assert r.status_code == 400 and "64 hex" in reason(r)


def test_missing_fields_are_named_but_never_echoed(client):
    r = client.post("/submit", data={"hotkey": ALICE.ss58_address})
    assert r.status_code == 422
    # The field names, and nothing of their values - the signature is in there.
    assert "signature" in reason(r) and "timestamp" in reason(r)


def test_an_empty_field_counts_as_a_missing_one(client, tmp_path):
    # An empty field counts as absent; still refused by name, with nothing echoed.
    d = kernel_file(tmp_path, "a")
    r = client.post(
        "/submit",
        data={"hotkey": "", "signature": "ab" * 64, "timestamp": int(time.time())},
        files={
            "Kernel.lean": ("Kernel.lean", (d / "Kernel.lean").read_bytes()),
        },
    )
    assert r.status_code == 422 and "hotkey" in reason(r)


def test_an_oversized_file_is_refused(client, store):
    register(store, ALICE.ss58_address)
    r = client.post(
        "/submit",
        data={
            "hotkey": ALICE.ss58_address,
            "signature": "ab" * 64,
            "timestamp": int(time.time()),
        },
        files={
            "Kernel.lean": ("Kernel.lean", b"a" * (MAX_FILE_BYTES + 1)),
        },
    )
    assert r.status_code == 413


def test_an_oversized_body_is_refused_on_its_headers(client):
    # Refused from Content-Length, so a huge upload is never spooled to disk.
    r = client.post(
        "/submit",
        data={"hotkey": ALICE.ss58_address},
        files={"Kernel.lean": ("Kernel.lean", b"a" * (MAX_REQUEST_BYTES + 1024))},
    )
    assert r.status_code == 413 and "at most" in reason(r)


def test_an_empty_file_is_refused(client, store, tmp_path):
    register(store, ALICE.ss58_address)
    d = tmp_path / "empty"
    d.mkdir()
    (d / "Kernel.lean").write_bytes(b"")
    r = post_submission(client, ALICE, d)
    # Refused on emptiness before the signature is even checked.
    assert r.status_code == 400 and "non-empty" in reason(r)


def test_a_file_that_is_not_utf8_is_refused(client, store, tmp_path):
    # The gate reads Kernel.lean as text; bytes it cannot decode never reach the queue.
    register(store, ALICE.ss58_address)
    d = tmp_path / "latin1"
    d.mkdir()
    (d / "Kernel.lean").write_bytes(b"import Contract\n-- caf\xe9\n")
    r = post_submission(client, ALICE, d)
    assert r.status_code == 400 and "UTF-8" in reason(r)
    assert client.get("/health").json()["queued"] == 0


def test_an_unknown_submission_is_a_404(client):
    assert client.get("/submissions/999999").status_code == 404


# ── Flooding ──────────────────────────────────────────────────────────────


def test_the_rate_limit_is_per_hotkey_and_counted_in_the_store(client, store, tmp_path):
    register(store, ALICE.ss58_address)
    d = kernel_file(tmp_path, "a", "RATIO=0.92")
    limit = store.rate.limit
    codes = [post_submission(client, ALICE, d).status_code for _ in range(limit + 1)]
    assert codes[-1] == 429
    # And the count is in Postgres, not in this process: a restart does not reset it.
    assert store.rate.peek(f"hotkey:{ALICE.ss58_address}") >= limit


def test_the_rate_limit_survives_a_new_app_instance(client, store, settings, tmp_path):
    # Why the counter is in the database: a second or restarted API sees the same budget.
    from fastapi.testclient import TestClient
    from service.api import create_app

    register(store, ALICE.ss58_address)
    d = kernel_file(tmp_path, "a", "RATIO=0.92")
    for _ in range(store.rate.limit):
        post_submission(client, ALICE, d)
    with TestClient(app=create_app(settings, store)) as second:
        assert post_submission(second, ALICE, d).status_code == 429


def test_security_headers_and_a_request_id_are_always_set(client):
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert len(r.headers["X-Request-Id"]) == 12


def test_no_interactive_docs_or_schema_are_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ── The checks themselves ─────────────────────────────────────────────────


def test_freshness_accepts_inside_the_window_and_refuses_outside():
    assert security.check_fresh(1000, 300, now=1000.0) == 1000
    assert security.check_fresh(1000, 300, now=1299.0) == 1000
    with pytest.raises(security.Invalid):
        security.check_fresh(1000, 300, now=1301.0)
    with pytest.raises(security.Invalid):
        security.check_fresh(1000, 300, now=699.0)
