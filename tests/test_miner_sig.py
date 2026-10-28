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
        competition="torchlean", digest="ab" * 32, hotkey="5Hot", timestamp=1700000000
    )
    assert message == (
        "conjectures-competition-submit-v1\n"
        "competition: torchlean\n"
        f"digest: {'ab' * 32}\n"
        "hotkey: 5Hot\n"
        "timestamp: 1700000000"
    )


def test_a_miner_signature_verifies_at_the_validator_and_only_for_its_slug():
    kp = miner_sig.load_keypair("//Alice")
    fields = dict(digest="cd" * 32, hotkey=kp.ss58_address, timestamp=1700000000)
    signature = miner_sig.sign(kp, miner_sig.submit_message(competition="torchlean", **fields))
    ok = service_sig.submit_message(competition="torchlean", **fields)
    other = service_sig.submit_message(competition="floatlib", **fields)
    assert service_sig.verify(kp.ss58_address, ok, signature)
    assert not service_sig.verify(kp.ss58_address, other, signature)


@pytest.mark.parametrize("intake_mode", ["subnet", "dev"])
def test_the_client_sends_headers_and_one_file_part(tmp_path, monkeypatch, capsys, intake_mode):
    # The signed fields travel as X-Conjectures-* headers, the file as the one part Kernel.lean.
    submit = load("miner_submit", REPO / "miner" / "submit.py")
    d = tmp_path / "k"
    d.mkdir()
    (d / "Kernel.lean").write_bytes(b"import ContractExact\n")
    sent: dict = {}

    class Reply:
        status_code = 201
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "competition": "torchlean",
                "submission": 1,
                "proof_state": "queued",
                "source_sha256": "0" * 64,
                "status_url": "/v1/competitions/torchlean/submissions/1",
                "intake_mode": intake_mode,
            }

    def post(url, headers, files, timeout):
        sent.update(url=url, headers=headers, files=files)
        return Reply()

    monkeypatch.setattr(submit.requests, "post", post)
    args = submit.argparse.Namespace(dir=str(d), hotkey="//Alice", url="http://v:9300/")
    submit.cmd_submit(args)
    assert sent["url"] == "http://v:9300/v1/competitions/torchlean/submissions"
    assert set(sent["files"]) == {"Kernel.lean"}
    output = capsys.readouterr().out
    assert "proof state: queued" in output
    assert "miner/submit.py status 1 --url http://v:9300/" in output
    assert ("dev intake:" in output) == (intake_mode == "dev")
    h = sent["headers"]
    message = service_sig.submit_message(
        competition="torchlean",
        digest=service_sig.digest_of(b"import ContractExact\n"),
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
        competition="torchlean", digest="ab" * 32, hotkey=ALICE.ss58_address, timestamp=1700000000
    )
    bob = service_sig.load_keypair("//Bob")
    signature = service_sig.sign(bob, message)
    assert not service_sig.verify(ALICE.ss58_address, message, signature)


def test_malformed_signature_and_hotkey_are_rejected():
    assert not service_sig.verify(ALICE.ss58_address, "message", "not hex")
    assert not service_sig.verify("not an address", "message", "00" * 64)


@pytest.mark.parametrize(
    "content,reason",
    [(b"", "empty"), (b"\xff", "UTF-8"), (b"x" * 256_001, "256000")],
)
def test_invalid_source_is_refused_before_upload(tmp_path, monkeypatch, content, reason):
    submit = load("miner_submit_invalid", REPO / "miner" / "submit.py")
    (tmp_path / "Kernel.lean").write_bytes(content)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid input must not reach the API")

    monkeypatch.setattr(submit.requests, "post", unexpected)
    with pytest.raises(SystemExit, match=reason):
        submit.cmd_submit(
            submit.argparse.Namespace(dir=str(tmp_path), hotkey="//Alice", url="http://v")
        )


def test_status_keeps_proof_and_board_decisions_separate(monkeypatch, capsys):
    submit = load("miner_submit_status", REPO / "miner" / "submit.py")
    payload = {
        "submission": 12,
        "proof_state": "proved",
        "intake_mode": "dev",
        "evaluations": [{"board": "mlp", "state": "pending", "decision": None}],
    }

    class Reply:
        status_code = 200

        def json(self):
            return payload

    def get(url, params, timeout):
        assert url == "http://dev/v1/competitions/torchlean/submissions/12" and params is None
        return Reply()

    monkeypatch.setattr(submit.requests, "get", get)
    submit.cmd_status(submit.argparse.Namespace(id=12, url="http://dev/"))
    import json

    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize(
    ("argv", "path", "params"),
    [
        (["report", "12"], "/submissions/12/report", None),
        (["boards"], "/boards", None),
        (["leaderboard"], "/leaderboard", {"board": "overall"}),
        (["leaderboard", "--board", "mlp"], "/leaderboard", {"board": "mlp"}),
    ],
)
def test_read_commands_print_public_api_reads(monkeypatch, capsys, argv, path, params):
    submit = load("miner_submit_reads", REPO / "miner" / "submit.py")
    seen = []

    class Reply:
        status_code = 200

        def json(self):
            return {"ok": True}

    def get(url, params, timeout):
        seen.append((url, params))
        return Reply()

    monkeypatch.setattr(submit.requests, "get", get)
    monkeypatch.setattr(sys, "argv", ["submit.py", *argv, "--url", "http://dev"])
    submit.main()
    assert seen == [("http://dev/v1/competitions/torchlean" + path, params)]
    assert '"ok": true' in capsys.readouterr().out


def test_duplicate_reports_existing_submission():
    submit = load("miner_submit_duplicate", REPO / "miner" / "submit.py")

    class Reply:
        status_code = 409
        headers = {"content-type": "application/problem+json"}

        def json(self):
            return {
                "reason_code": "DUPLICATE_SUBMISSION",
                "detail": "Duplicate submission",
                "submission": 12,
                "state": "proved",
            }

    message = submit.refused("submit", Reply())
    assert "DUPLICATE_SUBMISSION" in message
    assert "Existing submission 12 (proved)" in message
