"""Central intake consumes eligibility; verdict publication must not charge it again."""

import pytest
import sqlalchemy as sa

import db as store_pkg
from conftest import register
from db import SubmissionState, models


@pytest.mark.parametrize("state", [SubmissionState.ACCEPTED, SubmissionState.REJECTED])
def test_verdict_does_not_require_an_unused_registration(store, state):
    sid, _ = store.submissions.add("authorized-at-intake", "a" * 64, slot="full")
    assert store.submissions.finish(sid, state, ratio=0.9) == state.value
    with store_pkg.session_scope(store.sessions) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(models.EntitlementClaim)) == 0


def test_finishing_and_reverifying_preserve_historical_claims(store):
    sid, _ = store.submissions.add("historical", "a" * 64, slot="full")
    registration = register(store, "historical")
    with store_pkg.session_scope(store.sessions) as session:
        session.add(models.EntitlementClaim(registration_id=registration, submission_id=sid))
    store.submissions.finish(sid, SubmissionState.ACCEPTED, ratio=0.9)
    store.submissions.requeue(sid)
    retry = store.submissions.claim_next("retry-worker")
    assert retry.id == sid
    store.submissions.finish(
        sid, SubmissionState.ACCEPTED, expected_attempt=retry.verification_attempt, ratio=0.9
    )
    with store_pkg.session_scope(store.sessions) as session:
        claims = session.scalars(sa.select(models.EntitlementClaim)).all()
        assert [(c.registration_id, c.submission_id) for c in claims] == [(registration, sid)]


def test_interruption_retries_same_id_and_refuses_stale_result(store):
    sid, _ = store.submissions.add("authorized-at-intake", "a" * 64, slot="full")
    first = store.submissions.claim_next("worker")
    assert store.submissions.requeue_interrupted("worker") == 1
    retry = store.submissions.claim_next("worker")
    assert retry.id == sid and retry.verification_attempt != first.verification_attempt
    with pytest.raises(RuntimeError, match="stale worker claim"):
        store.submissions.finish(
            sid, SubmissionState.ACCEPTED, expected_attempt=first.verification_attempt, ratio=0.9
        )
    store.submissions.finish(
        sid, SubmissionState.ACCEPTED, expected_attempt=retry.verification_attempt, ratio=0.9
    )
