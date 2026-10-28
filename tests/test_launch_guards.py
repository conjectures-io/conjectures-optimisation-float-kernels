"""Launch guards: copies are refused before the proof, a verdict can always be stored, a control
character is refused at intake, and production never evaluates dev uploads."""

from __future__ import annotations

import dataclasses
import json

import pytest
from duplicates import normalized, normalized_digest
from sandbox import intake
from sqlalchemy import text
from test_tensor_workflow import setup

import db
from conftest import register
from db import models as m
from db.submissions import storable

ORIGINAL = b"""-- a fast add
/-- Doc comment with a "quote" and -- dashes. /- nested -/ still inside -/
def add (x y : Float) : Float := x + y  -- trailing
def msg : String := "keep -- this and /- this -/"
"""


def references(config):
    return [(board, b["reference"]["sha256"]) for board, b in config.boards.items()]


def upload(store, hotkey, content, uid):
    register(store, hotkey, uid=uid)
    return store.submissions.add(hotkey, content)


def claim(repo, config):
    return repo.submissions.claim_proof("test", config.proof_identity, 30, references(config))


def state(store, sid):
    with db.session_scope(store.sessions) as s:
        row = s.get(m.Submission, sid)
        return row.state, row.stage, row.reason


def test_normalising_strips_comments_and_whitespace_but_never_strings():
    assert normalized(ORIGINAL.decode()) == (
        'def add (x y : Float) : Float := x + y def msg : String := "keep -- this and /- this -/"'
    )
    reflowed = b"def add (x y : Float) : Float :=\n    x + y\n\n" + ORIGINAL.splitlines()[-1]
    assert normalized_digest(reflowed) == normalized_digest(ORIGINAL)
    # A change inside a string literal or in code is a different file.
    assert normalized_digest(ORIGINAL.replace(b"keep -- this", b"keep -- that")) != (
        normalized_digest(ORIGINAL)
    )
    assert normalized_digest(ORIGINAL.replace(b"x + y", b"y + x")) != normalized_digest(ORIGINAL)


def test_an_unclosed_comment_or_a_quote_inside_one_cannot_hide_code():
    assert normalized('/- a "b -/ def f := 1 /- "c -/ def g := 2') == "def f := 1 def g := 2"
    assert normalized("def f := 1 /- open") == "def f := 1"


def test_copies_of_an_earlier_entry_are_rejected_before_the_proof(store, config):
    repo, _ = setup(store, config)
    first = upload(store, "first", ORIGINAL, 0)
    same = upload(store, "same", ORIGINAL, 1)
    recommented = upload(store, "recommented", ORIGINAL + b"\n-- copied, nothing changed\n", 2)
    changed = upload(store, "changed", ORIGINAL.replace(b"x + y", b"y + x"), 3)
    # The first submitter is proved; every copy after it is rejected without a proof run.
    assert claim(repo, config).id == first
    assert claim(repo, config) is None
    assert claim(repo, config) is None
    assert claim(repo, config).id == changed
    for sid in (same, recommented):
        assert state(store, sid) == (
            "rejected",
            "intake",
            f"identical to submission {first} up to comments and whitespace",
        )


def test_a_copy_of_an_initial_reference_is_rejected(store, config):
    repo, _ = setup(store, config)
    sid = upload(store, "alice", b"initial   reference -- mine now\n", 0)
    assert claim(repo, config) is None
    assert state(store, sid)[2] == (
        "identical to the mlp board's initial reference up to comments and whitespace"
    )


def test_a_rejected_original_does_not_block_a_later_entry(store, config):
    repo, _ = setup(store, config)
    first = upload(store, "first", ORIGINAL, 0)
    proof = claim(repo, config)
    repo.submissions.finish_proof(
        first, proof.token, config.proof_identity, {"exit_code": 1, "reason": "wrong"}, 1
    )
    later = upload(store, "later", ORIGINAL + b"-- fixed?\n", 1)
    assert claim(repo, config).id == later


def test_a_verdict_with_nul_or_a_lone_surrogate_is_stored(store, config):
    repo, _ = setup(store, config)
    sid = upload(store, "alice", ORIGINAL, 0)
    proof = claim(repo, config)
    reason = "set_option x\0y is not allowed \ud800"
    verdict = json.loads(json.dumps({"exit_code": 1, "stage": "policy", "reason": reason}))
    assert repo.submissions.finish_proof(sid, proof.token, config.proof_identity, verdict, 1)
    assert state(store, sid) == (
        "rejected",
        "policy",
        "set_option x\\x00y is not allowed \\ud800",
    )


def test_storable_reaches_keys_and_nested_values():
    assert storable({"a\0": ["b\0", {"c": "d\0"}], "n": 1}) == {
        "a\\x00": ["b\\x00", {"c": "d\\x00"}],
        "n": 1,
    }


@pytest.mark.parametrize("char", ["\0", "\x07", "\x1b", "\x7f"])
def test_control_characters_are_refused_at_intake(tmp_path, char):
    src = tmp_path / "Kernel.lean"
    src.write_text(f"import ContractExact\nset_option x{char}y 1\n")
    assert intake(src, None) == f"control character U+{ord(char):04X} at offset 33"


def test_tabs_and_line_endings_pass_intake(tmp_path):
    src = tmp_path / "Kernel.lean"
    src.write_text("import ContractExact\r\n\tdef f := 1\n")
    assert intake(src, None) is None


def test_dev_uploads_are_evaluated_only_on_a_dev_deployment(store, config):
    repo, _ = setup(store, config)
    with store.engine.begin() as c:
        c.execute(
            text("INSERT INTO sources(sha256,content) VALUES (encode(sha256(:b),'hex'),:b)"),
            {"b": b"dev entry"},
        )
        sid = c.scalar(
            text(
                "INSERT INTO submissions(hotkey,source_sha256,intake_mode) "
                "VALUES ('dev',encode(sha256(:b),'hex'),'dev') RETURNING id"
            ),
            {"b": b"dev entry"},
        )
    proof = claim(repo, config)
    assert repo.submissions.finish_proof(
        sid, proof.token, config.proof_identity, {"exit_code": 0, "passed": True}, 1
    )
    assert repo.plan(config) == []
    dev = dataclasses.replace(config, dev_intake=True)
    repo.reconcile(dev)
    assert len(repo.plan(dev)) == len(config.boards)
    with pytest.raises(ValueError, match="dev_intake"):
        type(config).parse(json.loads(json.dumps(dataclasses.asdict(config))) | {"dev_intake": 1})
