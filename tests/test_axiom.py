"""Axiom events: the platform's envelope, off without credentials, and never in the way.

No network: a capturing transport stands in for the ingest endpoint, and a real `AxiomClient`
(its queue, its drain thread, its gzip) runs in front of it, so what the tests read back is
exactly the body Axiom would receive. The weight setter and gate worker tests run the real store
(Postgres) with a fake chain and the stub gate.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from unittest.mock import Mock

import pytest
from loguru import logger

import axiom
from service import sig

ALICE = sig.load_keypair("//Alice")

# The platform's Severity values (conjectures_subnet/axiom/labels.py), verbatim.
PLATFORM_SEVERITIES = {"debug", "info", "warning", "error", "critical"}
ENVELOPE = {"_time", "severity", "source", "event_type", "environ"}


class Capture:
    """The ingest endpoint: remembers every POST, or fails every one when told to."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.posts: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> None:
        if self.fail:
            raise OSError("connection refused")
        self.posts.append((url, body, dict(headers)))

    def records(self) -> list[dict[str, object]]:
        lines = [gzip.decompress(body).decode() for _, body, _ in self.posts]
        return [json.loads(line) for chunk in lines for line in chunk.splitlines()]


def axiom_client(capture: Capture, **kwargs: object) -> axiom.AxiomClient:
    return axiom.AxiomClient(
        dataset="conjectures",
        token="xaat-test",
        environ="dev",
        transport=capture,
        flush_seconds=0.05,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


@pytest.fixture(autouse=True)
def _fresh_sink() -> Iterator[None]:
    axiom.reset()
    yield
    axiom.reset()


def flushed(capture: Capture) -> list[dict[str, object]]:
    axiom.get_events().close()
    return capture.records()


def of_type(records: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    return [r for r in records if r["event_type"] == event_type]


# ── The envelope ──────────────────────────────────────────────────────────


def test_the_envelope_is_the_platforms_and_is_written_last():
    capture = Capture()
    events = axiom.init("competition-gate-worker", client=axiom_client(capture), netuid=577)
    events.warning("gate_verdict", submission_id=7, source="spoofed", severity="spoofed")
    events.bind(network="test")
    events.info("service_started", when=dt.date(2026, 9, 24))
    first, second = flushed(capture)

    assert ENVELOPE <= set(first)
    assert first["source"] == "competition-gate-worker"  # a details key cannot displace it
    assert first["severity"] == "warning" and first["event_type"] == "gate_verdict"
    assert first["environ"] == "dev" and first["competition"] == "torchlean"
    assert first["netuid"] == 577 and first["submission_id"] == 7
    stamped = dt.datetime.fromisoformat(str(first["_time"]))
    assert stamped.utcoffset() == dt.timedelta(0)
    assert second["network"] == "test" and second["when"] == "2026-09-24"  # str() fallback
    assert {r["severity"] for r in (first, second)} <= PLATFORM_SEVERITIES
    assert set(axiom.SEVERITIES) == PLATFORM_SEVERITIES


def test_one_gzipped_ndjson_post_to_the_dataset_with_a_bearer_token():
    capture = Capture()
    c = axiom.AxiomClient(
        dataset="a b", token="xaat-1", api_url="https://api.eu.axiom.co/", transport=capture
    )
    for _ in range(3):
        c.ingest(severity="info", source="competition-weight-setter", event_type="x", details={})
    c.close()
    assert len(capture.posts) == 1
    url, body, headers = capture.posts[0]
    assert url == "https://api.eu.axiom.co/v1/datasets/a%20b/ingest"
    assert headers["Authorization"] == "Bearer xaat-1"
    assert headers["Content-Type"] == "application/x-ndjson"
    assert headers["Content-Encoding"] == "gzip"
    assert len(gzip.decompress(body).decode().splitlines()) == 3
    assert c.stats() == {"sent": 3, "dropped": 0, "failed": 0, "queued": 0}


# ── Off unless configured ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"AXIOM_TOKEN": "xaat-1"},
        {"AXIOM_DATASET": "d"},
        {"AXIOM_TOKEN": " ", "AXIOM_DATASET": "d"},
    ],
)
def test_without_both_credentials_nothing_is_built_or_bridged(env):
    before = len(logger._core.handlers)  # pyright: ignore[reportAttributeAccessIssue]
    hook = sys.excepthook
    events = axiom.init("competition-chain-watcher", env=env)
    assert isinstance(events.client, axiom.NoopClient) and not events.enabled
    assert len(logger._core.handlers) == before  # pyright: ignore[reportAttributeAccessIssue]
    assert sys.excepthook is hook
    events.error("chain_read_failed", error="nobody is listening")  # and nothing happens


def test_both_credentials_build_a_client_with_the_platforms_defaults():
    built = axiom.client_from_env(
        {"AXIOM_TOKEN": "xaat-1", "AXIOM_DATASET": "conjectures"}, transport=Capture()
    )
    assert isinstance(built, axiom.AxiomClient)
    assert built.url == "https://api.axiom.co/v1/datasets/conjectures/ingest"
    assert built.environ == "default"
    built.close()


# ── Never in the way ──────────────────────────────────────────────────────


def test_a_dead_endpoint_drops_and_counts_and_never_raises():
    capture = Capture(fail=True)
    c = axiom_client(capture)
    for _ in range(5):
        c.ingest(severity="error", source="competition-gate-worker", event_type="x", details={})
    c.close()
    c.close()  # idempotent
    assert c.stats()["failed"] == 5 and c.stats()["sent"] == 0


def test_a_full_queue_drops_rather_than_blocks():
    c = axiom_client(Capture(), queue_size=1, start=False)
    for _ in range(3):
        c.ingest(severity="info", source="competition-gate-worker", event_type="x", details={})
    assert c.stats()["dropped"] == 2
    c.close()


def test_a_broken_client_cannot_reach_the_caller():
    broken = Mock(enabled=True)
    broken.ingest.side_effect = RuntimeError("telemetry bug")
    broken.close.side_effect = RuntimeError("telemetry bug")
    events = axiom.Events(broken, source="competition-weight-setter")
    events.error("weights_failed", error="x")
    events.close()


# ── The log bridge ────────────────────────────────────────────────────────


def test_errors_logged_anywhere_arrive_as_log_error():
    capture = Capture()
    axiom.init("competition-submission-api", client=axiom_client(capture))
    logger.warning("not an error")
    logger.error("the store refused")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("tick failed")
    logging.getLogger("uvicorn.error").error("asgi exploded")
    logger.critical("disk full")
    records = of_type(flushed(capture), "log_error")

    assert [r["message"] for r in records] == [
        "the store refused",
        "tick failed",
        "asgi exploded",
        "disk full",
    ]
    assert [r["severity"] for r in records] == ["error", "error", "error", "critical"]
    first = records[0]
    assert first["source"] == "competition-submission-api"
    assert first["function"] == "test_errors_logged_anywhere_arrive_as_log_error"
    assert first["module"] == "test_axiom" and isinstance(first["line"], int)
    assert "ValueError: boom" in str(records[1]["exception"])
    assert records[2]["logger"] == "uvicorn.error"


def test_reset_removes_the_bridge():
    capture = Capture()
    axiom.init("competition-submission-api", client=axiom_client(capture))
    axiom.reset()
    logger.error("after reset")
    assert capture.records() == []
