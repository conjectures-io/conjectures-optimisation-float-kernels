"""The miner's signer and the validator's agree to the byte, and match the platform's message."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
                "competition": "floatlib",
                "submission": 1,
                "slot": "full",
                "state": "queued",
                "digest": "0" * 64,
                "created": True,
                "slots_remaining": 0,
            }

    def post(url, headers, files, timeout):
        sent.update(url=url, headers=headers, files=files)
        return Reply()

    monkeypatch.setattr(submit.requests, "post", post)
    args = submit.argparse.Namespace(
        dir=str(d), hotkey="//Alice", url="http://v:9300/", slot="full"
    )
    submit.cmd_submit(args)
    assert sent["url"] == "http://v:9300/v1/competitions/floatlib/submissions"
    assert set(sent["files"]) == {"Kernel.lean"}
    h = sent["headers"]
    message = service_sig.submit_message(
        competition="floatlib",
        digest=service_sig.digest_of(b"import Contract\n"),
        hotkey=h["X-Conjectures-Hotkey"],
        timestamp=int(h["X-Conjectures-Timestamp"]),
    )
    assert service_sig.verify(h["X-Conjectures-Hotkey"], message, h["X-Conjectures-Signature"])


def test_client_url_defaults_to_central_api_and_can_be_overridden(monkeypatch):
    submit = load("miner_submit_defaults", REPO / "miner" / "submit.py")
    seen = []
    monkeypatch.setitem(submit.CMDS, "status", lambda args: seen.append(args.url))
    monkeypatch.setattr(sys, "argv", ["submit.py", "status", "49"])
    submit.main()
    monkeypatch.setattr(sys, "argv", ["submit.py", "status", "49", "--url", "http://dev:8000"])
    submit.main()
    assert seen == ["https://conjectures.io", "http://dev:8000"]


def test_wrong_hotkey_signature_is_rejected():
    message = service_sig.submit_message(
        competition="floatlib", digest="ab" * 32, hotkey=ALICE.ss58_address, timestamp=1700000000
    )
    bob = service_sig.load_keypair("//Bob")
    signature = service_sig.sign(bob, message)
    assert not service_sig.verify(ALICE.ss58_address, message, signature)


def test_malformed_signature_and_hotkey_are_rejected():
    assert not service_sig.verify(ALICE.ss58_address, "message", "not hex")
    assert not service_sig.verify("not an address", "message", "00" * 64)
