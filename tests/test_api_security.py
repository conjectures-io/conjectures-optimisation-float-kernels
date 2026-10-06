"""What the API refuses - bad shape, stale signature, big body, flood - before it costs anything."""

from __future__ import annotations

import time

import pytest

from conftest import api, kernel_file, post_submission, register
from service import security, sig
from service.settings import MAX_FILE_BYTES, MAX_REQUEST_BYTES

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


def raw(client, headers: dict[str, str], files=None):
    # A submission exactly as given: no signing, for the shape checks that run before crypto.
    return client.post(f"{api()}/submissions", headers=headers, files=files)


def signed(hotkey: str, signature: str = "ab" * 64) -> dict[str, str]:
    # The three signed headers, with any values a test wants.
    return {
        "X-Conjectures-Hotkey": hotkey,
        "X-Conjectures-Signature": signature,
        "X-Conjectures-Timestamp": str(int(time.time())),
    }


def kernel_part(d) -> dict:
    # The one file part a submission carries.
    return {"Kernel.lean": ("Kernel.lean", (d / "Kernel.lean").read_bytes())}


@pytest.mark.parametrize("hotkey", ["not-an-address", "0x1234", "l" * 48, "5" * 200])
def test_a_malformed_hotkey_is_refused_before_any_crypto(client, tmp_path, hotkey):
    r = raw(client, signed(hotkey), kernel_part(kernel_file(tmp_path, "a")))
    assert r.status_code == 400 and "ss58" in reason(r)


@pytest.mark.parametrize("signature", ["00", "0x" + "z" * 128, "ab" * 63])
def test_a_malformed_signature_is_refused(client, tmp_path, signature):
    r = raw(client, signed(ALICE.ss58_address, signature), kernel_part(kernel_file(tmp_path, "a")))
    assert r.status_code == 400 and "64 hex" in reason(r)


def test_missing_fields_are_named_but_never_echoed(client, tmp_path):
    headers = {"X-Conjectures-Hotkey": ALICE.ss58_address}
    r = raw(client, headers, kernel_part(kernel_file(tmp_path, "a")))
    assert r.status_code == 422
    # The field names, and nothing of their values - the signature is in there.
    assert "signature" in reason(r).lower() and "timestamp" in reason(r).lower()


def test_an_empty_field_counts_as_a_missing_one(client, tmp_path):
    # An empty header counts as absent; still refused by name, with nothing echoed.
    r = raw(client, signed(""), kernel_part(kernel_file(tmp_path, "a")))
    assert r.status_code in (400, 422) and "hotkey" in reason(r)


def test_an_oversized_file_is_refused(client, store):
    register(store, ALICE.ss58_address)
    files = {"Kernel.lean": ("Kernel.lean", b"a" * (MAX_FILE_BYTES + 1))}
    r = raw(client, signed(ALICE.ss58_address), files)
    assert r.status_code == 413


def test_an_oversized_body_is_refused_on_its_headers(client):
    # Refused from Content-Length, so a huge upload is never spooled to disk.
    files = {"Kernel.lean": ("Kernel.lean", b"a" * (MAX_REQUEST_BYTES + 1024))}
    r = raw(client, signed(ALICE.ss58_address), files)
    assert r.status_code == 413 and "at most" in reason(r)


# ── Competitions and slots ────────────────────────────────────────────────


def test_each_slot_is_its_own_competition_slug(client, store, tmp_path):
    # floatlib-mul queues a mul submission, and reads it back only under its own slug.
    register(store, ALICE.ss58_address)
    r = post_submission(client, ALICE, kernel_file(tmp_path, "a"), slot="mul")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["competition"] == "floatlib-mul" and body["slot"] == "mul" and body["created"]
    sid = body["submission"]
    assert client.get(f"{api('mul')}/submissions/{sid}").json()["slot"] == "mul"
    assert client.get(f"{api()}/submissions/{sid}").status_code == 404


def test_a_signature_for_one_slot_does_not_submit_to_another(client, store, tmp_path):
    # The slug is part of what is signed, so an add signature cannot queue a mul submission.
    register(store, ALICE.ss58_address)
    d = kernel_file(tmp_path, "a")
    r = post_submission(client, ALICE, d, slot="mul", signed_for="floatlib")
    assert r.status_code == 401 and "does not verify" in reason(r)


def test_another_competitions_slug_is_not_served(client, tmp_path):
    r = client.post(
        "/v1/competitions/deflate/submissions", files=kernel_part(kernel_file(tmp_path, "a"))
    )
    assert r.status_code in (404, 422)
    assert client.get("/v1/competitions/deflate/leaderboard").status_code == 404


def test_the_report_is_the_gates_verdict(client, store, drain, tmp_path):
    register(store, ALICE.ss58_address)
    sid = post_submission(client, ALICE, kernel_file(tmp_path, "a", "RATIO=0.9")).json()[
        "submission"
    ]
    assert client.get(f"{api()}/submissions/{sid}/report").json() == {"report": None}
    drain()
    assert '"ratio": 0.9' in client.get(f"{api()}/submissions/{sid}/report").json()["report"]


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
    assert client.get(f"{api()}/submissions/999999").status_code == 404


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
