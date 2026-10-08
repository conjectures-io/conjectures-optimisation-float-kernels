"""Publication reuse must preserve chain history and pagination payloads."""

from concurrent.futures import ThreadPoolExecutor

import sqlalchemy as sa
from test_weight_setter import FakeChain, accept, at_epoch_boundary, config, weight_sets

import reward
from db import models
from db.scoring import ScoringDb
from workers.weight_setter import run, step


def publish(store, **changes):
    fields = dict(
        netuid=2,
        block=100,
        uids=[0],
        weights=[1.0],
        summary="same",
        accepted=False,
        api_snapshot={"computed_at": "now", "items": []},
        reuse_unchanged=True,
    )
    fields.update(changes)
    return store.scoring.publish_weight_set(**fields)


def test_reuse_survives_restart_and_ignores_clock(store):
    first, fresh = publish(store)
    assert fresh
    store.scoring = ScoringDb(store.sessions)
    assert publish(store, block=101, api_snapshot={"computed_at": "later", "items": []}) == (
        first,
        False,
    )
    assert len(weight_sets(store)[0]) == 1


def test_concurrent_publishers_create_only_one_row(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(store), range(2)))
    assert len({sid for sid, _ in results}) == 1
    assert sum(fresh for _, fresh in results) == 1


def test_changes_and_rollbacks_publish_new_rows(store):
    first, _ = publish(store)
    second, fresh = publish(store, api_snapshot={"items": [1]})
    assert fresh and second != first
    third, fresh = publish(store)
    assert fresh and third != first
    assert publish(store, dry_run=True)[1]
    assert publish(store, netuid=3)[1]


def test_pruning_preserves_recent_latest_and_other_subnets(store):
    old, _ = publish(store)
    latest, _ = publish(store, api_snapshot={"items": [1]})
    other, _ = publish(store, netuid=3)
    with store.engine.begin() as conn:
        conn.execute(sa.text("UPDATE weight_sets SET created_at = now() - interval '2 hours'"))
    recent, _ = publish(store, api_snapshot={"items": [2]})
    assert store.scoring.prune_api_snapshots(netuid=2, retention_seconds=3600, batch_size=1) == 1
    assert store.scoring.prune_api_snapshots(netuid=2, retention_seconds=3600, batch_size=1) == 1
    assert store.scoring.prune_api_snapshots(netuid=2, retention_seconds=3600, batch_size=1) == 0
    with store.engine.connect() as conn:
        rows = dict(
            conn.execute(sa.select(models.WeightSet.id, models.WeightSet.api_snapshot)).all()
        )
    assert rows[old] is None and rows[latest] is None
    assert rows[recent] and rows[other] and len(rows) == 4
    with store.engine.begin() as conn:
        conn.execute(sa.text("UPDATE weight_sets SET created_at = now() - interval '2 hours'"))
    assert store.scoring.prune_api_snapshots(netuid=2, retention_seconds=3600, batch_size=10) == 0


def test_wait_does_not_overwrite_chain_outcome_and_attempts_remain_distinct(store):
    accept(store, "fast", 0.5, uid=1)
    chain = FakeChain(hotkeys={"fast": 1}, block=at_epoch_boundary())
    first = step(chain, store, config(), reward.ScoringConfig())
    second = step(chain, store, config(), reward.ScoringConfig())
    assert first.weight_set_id != second.weight_set_id and len(chain.submitted) == 2
    chain.block -= 50
    waiting = step(chain, store, config(), reward.ScoringConfig())
    assert waiting.weight_set_id == second.weight_set_id
    rows, snaps = weight_sets(store)
    assert len(rows) == len(snaps) == 2
    assert all(row.accepted and row.error is None for row in rows)


def test_queue_change_publishes_even_without_weight_change(store):
    chain = FakeChain(block=at_epoch_boundary() - 50)
    first = step(chain, store, config(), reward.ScoringConfig())
    again = step(chain, store, config(), reward.ScoringConfig())
    assert again.weight_set_id == first.weight_set_id
    store.submissions.add("new", "a" * 64, "full")
    changed = step(chain, store, config(), reward.ScoringConfig())
    assert changed.weight_set_id != first.weight_set_id


def test_latest_valid_payload_survives_a_later_failed_publication(store):
    valid, _ = publish(store)
    publish(store, api_snapshot=None)
    with store.engine.begin() as conn:
        conn.execute(sa.text("UPDATE weight_sets SET created_at = now() - interval '2 hours'"))
    store.scoring.prune_api_snapshots(netuid=2, retention_seconds=3600, batch_size=10)
    with store.sessions() as session:
        assert session.get(models.WeightSet, valid).api_snapshot is not None


def test_a_cleared_payload_is_republished(store):
    old, _ = publish(store)
    with store.engine.begin() as conn:
        conn.execute(sa.text("UPDATE weight_sets SET api_snapshot = NULL"))
    new, created = publish(store)
    assert created and new != old


def test_cleanup_failure_does_not_prevent_weight_setting(store, monkeypatch):
    chain = FakeChain(block=at_epoch_boundary())

    def fail(**kwargs):
        raise RuntimeError("cleanup unavailable")

    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(store.scoring, "prune_api_snapshots", fail)
    run(chain, store, config(), reward.ScoringConfig(), sleep=stop)
    assert len(chain.submitted) == 1
    assert weight_sets(store)[0][0].accepted
