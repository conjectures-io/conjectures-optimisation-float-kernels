"""Copies of an accepted Kernel.lean are refused before the gate."""

from __future__ import annotations

import hashlib

from conftest import register
from service import duplicates, worker

ORIGINAL = b"""-- a fast add
/-- Doc comment with a "quote" and -- dashes. /- nested -/ still inside -/
def add (x y : Float) : Float := x + y  -- trailing
def msg : String := "keep -- this and /- this -/"
"""


def add(store, name, body, slot="full"):
    register(store, name, uid=len(name), block=1000 + sum(name.encode()))
    return store.submissions.add(name, hashlib.sha256(body).hexdigest(), slot, body)[0]


def test_normalising_strips_comments_and_whitespace_but_never_strings():
    text = duplicates.normalized(ORIGINAL.decode())
    assert text == (
        'def add (x y : Float) : Float := x + y def msg : String := "keep -- this and /- this -/"'
    )
    reflowed = b"def add (x y : Float) : Float :=\n    x + y\n\n" + ORIGINAL.splitlines()[-1]
    assert duplicates.normalized_digest(reflowed) == duplicates.normalized_digest(ORIGINAL)
    # A change inside a string literal or in code is a different file.
    changed = ORIGINAL.replace(b"keep -- this", b"keep -- that")
    assert duplicates.normalized_digest(changed) != duplicates.normalized_digest(ORIGINAL)
    assert duplicates.normalized_digest(ORIGINAL.replace(b"x + y", b"y + x")) != (
        duplicates.normalized_digest(ORIGINAL)
    )


def test_an_unclosed_comment_or_a_quote_inside_one_cannot_hide_code():
    a = duplicates.normalized('/- a "b -/ def f := 1 /- "c -/ def g := 2')
    assert a == "def f := 1 def g := 2"
    assert duplicates.normalized("def f := 1 /- open") == "def f := 1"


def test_copies_of_an_accepted_kernel_are_rejected_without_a_gate_run(
    store, settings, stub_gate, monkeypatch
):
    body = b"RATIO=0.95 -- the original\n"
    first = add(store, "first", body)
    assert worker.drain(store, settings) == 1
    assert store.submissions.get(first).state == "accepted"

    gated = []
    original = worker.run_gate

    def counting(directory, verdict, slot, env):
        gated.append(int(directory.name))
        return original(directory, verdict, slot, env)

    monkeypatch.setattr(worker, "run_gate", counting)
    same = add(store, "same", body)
    recommented = add(store, "recommented", b"RATIO=0.95   -- copied, nothing changed\n\n")
    different = add(store, "different", b"RATIO=0.96\n")
    assert worker.drain(store, settings) == 3
    for sub_id in (same, recommented):
        row = store.submissions.get(sub_id)
        assert row.state == "rejected" and row.stage == "intake"
        assert f"identical to accepted submission {first}" in row.reason
    # Only the genuinely different file reached the gate.
    assert gated == [different]
    assert store.submissions.get(different).state == "accepted"


def test_a_copy_of_a_rejected_kernel_still_goes_through_the_gate(store, settings, stub_gate):
    first = add(store, "first", b"REJECT\n")
    assert worker.drain(store, settings) == 1
    assert store.submissions.get(first).state == "rejected"
    again = add(store, "again", b"REJECT -- try again\n")
    assert worker.drain(store, settings) == 1
    row = store.submissions.get(again)
    assert row.state == "rejected" and row.stage != "intake"


def test_a_re_verified_copy_accepted_before_the_rule_is_not_rejected(
    store, settings, stub_gate, monkeypatch
):
    # Two identical entries accepted before this rule existed, like PROD's #155 and #165.
    with monkeypatch.context() as before_the_rule:
        before_the_rule.setattr(duplicates, "identical_to", lambda store, sub: None)
        first = add(store, "first", b"RATIO=0.95\n")
        copy = add(store, "copy", b"RATIO=0.95 -- copy\n")
        assert worker.drain(store, settings) == 2
    # Re-verifying the copy (reverify.py requeues accepted entries) gates it again as before.
    store.submissions.requeue(copy)
    assert store.submissions.get(copy).state == "queued"
    assert worker.drain(store, settings) == 1
    row = store.submissions.get(copy)
    assert row.state == "accepted" and row.stage == "score"
    assert store.submissions.get(first).state == "accepted"
