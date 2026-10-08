"""The miner's signer and the validator's agree to the byte, and match the platform's message."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from service import sig as service_sig

REPO = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    # A miner module by path: miner/ is not a package, and its `sig` would shadow the service's.
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


miner_sig = load("miner_sig", REPO / "miner" / "sig.py")
ALICE = service_sig.load_keypair("//Alice")


def test_the_message_is_the_platforms_five_lines():
    # conjectures-validator's submission_api/competition_sig.py builds exactly this text.
    message = miner_sig.submit_message(
        competition="floatlib", digest="ab" * 32, hotkey="5Hot", timestamp=1700000000
    )
    assert message == (
        "conjectures-competition-submit-v1\n"
        "competition: floatlib\n"
        f"digest: {'ab' * 32}\n"
        "hotkey: 5Hot\n"
        "timestamp: 1700000000"
    )


@pytest.mark.parametrize("slot", ["full", "add", "mul", "fma", "div", "sqrt", "sum", "bmm"])
def test_both_copies_build_the_same_slug_and_message(slot):
    kernel = b"import Contract\n"
    assert miner_sig.slug_of(slot) == service_sig.slug_of(slot)
    assert service_sig.slot_of(miner_sig.slug_of(slot)) == slot
    assert miner_sig.digest_of(kernel) == service_sig.digest_of(kernel)
    fields = dict(
        competition=miner_sig.slug_of(slot),
        digest=miner_sig.digest_of(kernel),
        hotkey=ALICE.ss58_address,
        timestamp=1700000000,
    )
    assert miner_sig.submit_message(**fields) == service_sig.submit_message(**fields)


def test_a_miner_signature_verifies_at_the_validator_and_only_for_its_slug():
    kp = miner_sig.load_keypair("//Alice")
    fields = dict(digest="cd" * 32, hotkey=kp.ss58_address, timestamp=1700000000)
    signature = miner_sig.sign(kp, miner_sig.submit_message(competition="floatlib", **fields))
    ok = service_sig.submit_message(competition="floatlib", **fields)
    other = service_sig.submit_message(competition="floatlib-mul", **fields)
    assert service_sig.verify(kp.ss58_address, ok, signature)
    assert not service_sig.verify(kp.ss58_address, other, signature)


def test_the_client_sends_headers_and_one_file_part(tmp_path, monkeypatch):
    # The signed fields travel as X-Conjectures-* headers, the file as the one part Kernel.lean.
    submit = load("miner_submit", REPO / "miner" / "submit.py")
    d = tmp_path / "k"
    d.mkdir()
    (d / "Kernel.lean").write_bytes(b"import Contract\n")
    sent: dict = {}

    class Reply:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "competition": "floatlib-fma",
                "submission": 1,
                "slot": "fma",
                "state": "queued",
                "digest": "0" * 64,
                "created": True,
                "slots_remaining": 0,
            }

    def post(url, headers, files, timeout):
        sent.update(url=url, headers=headers, files=files)
        return Reply()

    monkeypatch.setattr(submit.requests, "post", post)
    args = submit.argparse.Namespace(dir=str(d), hotkey="//Alice", url="http://v:9300/", slot="fma")
    submit.cmd_submit(args)
    assert sent["url"] == "http://v:9300/v1/competitions/floatlib-fma/submissions"
    assert set(sent["files"]) == {"Kernel.lean"}
    h = sent["headers"]
    message = service_sig.submit_message(
        competition="floatlib-fma",
        digest=service_sig.digest_of(b"import Contract\n"),
        hotkey=h["X-Conjectures-Hotkey"],
        timestamp=int(h["X-Conjectures-Timestamp"]),
    )
    assert service_sig.verify(h["X-Conjectures-Hotkey"], message, h["X-Conjectures-Signature"])


def test_the_full_entry_is_the_competition_itself():
    # `floatlib` is the paid full entry; every practice board is `floatlib-<slot>`, and the full
    # entry has no second slug.
    assert miner_sig.slug_of("full") == "floatlib" and miner_sig.slug_of("add") == "floatlib-add"
    assert service_sig.slot_of("floatlib") == "full"
    assert service_sig.slot_of("floatlib-full") is None
