"""Structured events to Axiom, in the conjectures platform's envelope, never in the way.

The platform (conjectures-validator, `conjectures_subnet/axiom`) ships its events to one Axiom
dataset, and this competition's four processes ship theirs to the same one, so a dashboard can
read both. That only works if every record has the platform's shape exactly:

    {**details, "_time": <ISO-8601 UTC, stamped at emit>, "severity": debug|info|warning|error|
     critical, "source": <process>, "event_type": <what happened>, "environ": AXIOM_ENVIRON}

with the envelope written last, so a details key named `source` cannot displace the label every
query filters on. Every event here also carries `competition: "floatlib"` and, in the two
chain processes, `netuid` and `network`.

Environment (the platform's names and meanings):

* `AXIOM_TOKEN`, `AXIOM_DATASET` -- both required; either absent means a no-op client.
* `AXIOM_ENVIRON` -- free-form deployment tag on every event (default `default`).
* `AXIOM_URL` -- API base (default https://api.axiom.co), for the EU region or a proxy.

Two rules, the platform's too, because telemetry that can stall or break the thing it watches is
worse than none: `emit` never blocks (a bounded queue, drained by a daemon thread that batches
and POSTs gzipped NDJSON, flushed at exit) and never raises (a full queue or a failed POST drops
and counts). Standard library only: this runs in the gate worker, next to hostile Lean, and
shipping a log line is not a reason to grow its dependencies.

Explicit events come from `get_events()`; `init()` at each entry point also bridges loguru and
stdlib-logging records at ERROR and above (event_type `log_error`) and uncaught exceptions
(`log_error`, critical), so an unhandled problem shows up without instrumenting its call site.
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import queue
import sys
import threading
import traceback
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.client import HTTPResponse
from types import TracebackType
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeAlias, cast

from loguru import logger

if TYPE_CHECKING:
    from loguru import Message

AXIOM_API_URL: Final = "https://api.axiom.co"
DEFAULT_ENVIRON: Final = "default"
COMPETITION: Final = "floatlib"

# The platform's `Severity` values, verbatim: one ladder whichever side emitted the event.
Severity: TypeAlias = Literal["debug", "info", "warning", "error", "critical"]
SEVERITIES: Final[tuple[Severity, ...]] = ("debug", "info", "warning", "error", "critical")

# One per process: deploy/pm2.config.js runs exactly these four.
Source: TypeAlias = Literal[
    "competition-submission-api",
    "competition-gate-worker",
    "competition-chain-watcher",
    "competition-weight-setter",
]
SOURCES: Final[tuple[Source, ...]] = (
    "competition-submission-api",
    "competition-gate-worker",
    "competition-chain-watcher",
    "competition-weight-setter",
)

# The closed vocabulary of what happened. Adding one is a new thing a dashboard can be built on.
EventType: TypeAlias = Literal[
    "service_started",
    "service_stopped",
    "service_misconfigured",
    "submission_claimed",
    "gate_verdict",
    "submission_requeued",
    "gate_validator_error",
    "registrations_recorded",
    "chain_read_failed",
    "weights_planned",
    "weights_set",
    "weights_skipped",
    "weights_failed",
    "log_error",
]

Details: TypeAlias = dict[str, object]
Event: TypeAlias = dict[str, object]

# The platform's transport constants.
QUEUE_SIZE: Final = 10_000
BATCH_EVENTS: Final = 100
FLUSH_SECONDS: Final = 2.0
TIMEOUT_SECONDS: Final = 10.0
SHUTDOWN_SECONDS: Final = 5.0
USER_AGENT: Final = "conjectures-floatlib-axiom/1"

# A failed ingest is reported through loguru at WARNING: below the bridge's ERROR floor, so a
# dead Axiom can never enqueue events about failing to enqueue events.
_FAILURE_LOG_INTERVAL: Final = 50
_SHUTDOWN: Final = object()


class Transport(Protocol):
    # One POST. Raises on failure; the client counts and drops. Tests pass a fake.

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> None:
        # POST one gzipped batch; raises on any failure, which the client counts and drops.
        ...


def urllib_transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> None:
    # The default transport: one HTTPS POST with the standard library, no new dependency.
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    response = cast(HTTPResponse, urllib.request.urlopen(request, timeout=timeout))
    with response:
        _ = response.read()  # an unread body leaks the connection


def _fallback(value: object) -> str:
    # UUIDs, datetimes, Paths, enum members: their string form, rather than a dropped batch.
    return str(value)


def encode(events: list[Event]) -> bytes:
    # The ingest body: gzipped NDJSON, one event per line.
    lines = "\n".join(json.dumps(event, default=_fallback) for event in events)
    return gzip.compress(lines.encode("utf-8"))


class Client(Protocol):
    @property
    def enabled(self) -> bool:
        # Whether events go anywhere.
        ...

    def ingest(self, *, severity: Severity, source: str, event_type: str, details: Details) -> None:
        # Queue one event; never blocks, never raises.
        ...

    def close(self) -> None:
        # Flush what is queued, then stop.
        ...

    def stats(self) -> dict[str, int]:
        # Counts of events sent, dropped, failed and still queued.
        ...


class NoopClient:
    # Every process's client until both AXIOM_TOKEN and AXIOM_DATASET are set.

    @property
    def enabled(self) -> bool:
        # Never: there is nowhere to send.
        return False

    def ingest(self, *, severity: Severity, source: str, event_type: str, details: Details) -> None:
        # Drop the event.
        del severity, source, event_type, details

    def close(self) -> None:
        # Nothing to flush.
        return None

    def stats(self) -> dict[str, int]:
        # All zero: nothing is ever sent.
        return {"sent": 0, "dropped": 0, "failed": 0, "queued": 0}


class AxiomClient:
    # Batches events on a daemon thread and POSTs them to `{url}/v1/datasets/{dataset}/ingest`.
    #
    #     The thread is a daemon and `close()` is registered with atexit: a clean exit flushes, and
    #     one that cannot flush within `shutdown_seconds` exits anyway rather than hanging.

    def __init__(
        self,
        *,
        dataset: str,
        token: str,
        environ: str = DEFAULT_ENVIRON,
        api_url: str = AXIOM_API_URL,
        transport: Transport = urllib_transport,
        queue_size: int = QUEUE_SIZE,
        batch_events: int = BATCH_EVENTS,
        flush_seconds: float = FLUSH_SECONDS,
        timeout_seconds: float = TIMEOUT_SECONDS,
        shutdown_seconds: float = SHUTDOWN_SECONDS,
        start: bool = True,
    ) -> None:
        # A bounded queue and the daemon thread that drains it to the dataset's ingest URL.
        if not dataset:
            raise ValueError("dataset is required")
        if not token:
            raise ValueError("token is required")
        self.url: str = (
            f"{api_url.rstrip('/')}/v1/datasets/{urllib.parse.quote(dataset, safe='')}/ingest"
        )
        self.environ: str = environ
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-ndjson",
            "Content-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        }
        self._transport: Transport = transport
        self._batch: int = max(1, batch_events)
        self._flush: float = max(0.01, flush_seconds)
        self._timeout: float = timeout_seconds
        self._shutdown: float = shutdown_seconds
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max(1, queue_size))
        self._stop: threading.Event = threading.Event()
        self._closed: bool = False
        # Only callers write `_dropped`, only the drain thread `_sent`/`_failed`; int increments
        # are atomic under the GIL.
        self._sent: int = 0
        self._dropped: int = 0
        self._failed: int = 0
        self._streak: int = 0
        self._thread: threading.Thread = threading.Thread(
            target=self._drain, name="axiom-ingest", daemon=True
        )
        if start:
            self._thread.start()
            _ = atexit.register(self.close)

    @property
    def enabled(self) -> bool:
        # Always: both credentials were given.
        return True

    def envelope(
        self, *, severity: Severity, source: str, event_type: str, details: Details
    ) -> Event:
        # The record as Axiom stores it; stamped now, not when the batch finally lands.
        return {
            **details,
            "_time": datetime.now(UTC).isoformat(),
            "severity": str(severity),
            "source": source,
            "event_type": event_type,
            "environ": self.environ,
        }

    def ingest(self, *, severity: Severity, source: str, event_type: str, details: Details) -> None:
        # Queue one event and return. Drops, and counts, rather than blocking or raising.
        try:
            event = self.envelope(
                severity=severity, source=source, event_type=event_type, details=details
            )
            if self._closed:
                raise queue.Full
            self._queue.put_nowait(event)
        except Exception:  # noqa: BLE001 - a telemetry bug must never become the caller's
            self._dropped += 1

    def _drain(self) -> None:
        # The daemon thread: batch events by count or age and POST each batch.
        pending: list[Event] = []
        while True:
            try:
                item = self._queue.get(timeout=self._flush)
            except queue.Empty:
                item = None
            else:
                if isinstance(item, dict):
                    pending.append(item)  # pyright: ignore[reportUnknownArgumentType]
            stopping = item is _SHUTDOWN or self._stop.is_set()
            if stopping:
                # Take everything accepted before close(), so a clean exit loses nothing.
                while True:
                    try:
                        queued = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(queued, dict):
                        pending.append(queued)  # pyright: ignore[reportUnknownArgumentType]
            if pending and (stopping or item is None or len(pending) >= self._batch):
                self._post(pending)
                pending = []
            if stopping:
                return

    def _post(self, events: list[Event]) -> None:
        # No retry: holding a batch while newer events pile onto a bounded queue turns one
        # failed flush into a wave of drops, and the next flush is seconds away anyway.
        try:
            self._transport(self.url, encode(events), self._headers, self._timeout)
        except Exception as exc:  # noqa: BLE001 - the drain thread must not die
            self._failed += len(events)
            self._streak += 1
            if self._streak == 1 or self._streak % _FAILURE_LOG_INTERVAL == 0:
                logger.warning(
                    f"[axiom] ingest failed ({self._streak} consecutive), "
                    f"dropped {len(events)} event(s): {exc}"
                )
        else:
            self._sent += len(events)
            self._streak = 0

    def close(self) -> None:
        # Flush what is queued and stop, waiting at most `shutdown_seconds`. Idempotent.
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            pass  # the thread is awake and about to see _stop
        if self._thread.is_alive():
            self._thread.join(timeout=self._shutdown)

    def stats(self) -> dict[str, int]:
        # Counts of events sent, dropped, failed and still queued.
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "failed": self._failed,
            "queued": self._queue.qsize(),
        }


def client_from_env(
    env: Mapping[str, str] | None = None, *, transport: Transport = urllib_transport
) -> Client:
    # An `AxiomClient` when both credentials are set, else a `NoopClient`. Never raises:
    #     observability decides whether anyone is watching, not whether the validator runs.
    env = os.environ if env is None else env
    token = env.get("AXIOM_TOKEN", "").strip()
    dataset = env.get("AXIOM_DATASET", "").strip()
    if not token or not dataset:
        return NoopClient()
    try:
        return AxiomClient(
            dataset=dataset,
            token=token,
            environ=env.get("AXIOM_ENVIRON", "").strip() or DEFAULT_ENVIRON,
            api_url=env.get("AXIOM_URL", "").strip() or AXIOM_API_URL,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 - a bad AXIOM_URL costs the dashboard, not the gate
        logger.warning(f"[axiom] client could not be built; ingestion disabled: {exc}")
        return NoopClient()


class Events:
    # The sink call sites use: `get_events().info("gate_verdict", submission_id=…, …)`.
    #
    #     Carries the process's source and the fields every event has (`competition`, and `netuid`
    #     / `network` once `bind` has them). Never raises on the caller's behalf.

    def __init__(self, client: Client, *, source: str, **common: object) -> None:
        # The client, this process's source, and the fields every event carries.
        self.client: Client = client
        self.source: str = source
        self.common: Details = {"competition": COMPETITION, **common}

    @property
    def enabled(self) -> bool:
        # Whether events go anywhere.
        return self.client.enabled

    def bind(self, **fields: object) -> None:
        # Add fields to every later event, e.g. `netuid` once the config is parsed.
        self.common.update({k: v for k, v in fields.items() if v is not None})

    def emit(self, severity: Severity, event_type: EventType, /, **fields: object) -> None:
        # Send one event in the platform's envelope; a broken client never reaches the caller.
        try:
            self.client.ingest(
                severity=severity,
                source=self.source,
                event_type=event_type,
                details={**self.common, **fields},
            )
        except Exception:  # noqa: BLE001 - see the module docstring
            pass

    def info(self, event_type: EventType, /, **fields: object) -> None:
        # An info-level event.
        self.emit("info", event_type, **fields)

    def warning(self, event_type: EventType, /, **fields: object) -> None:
        # A warning-level event.
        self.emit("warning", event_type, **fields)

    def error(self, event_type: EventType, /, **fields: object) -> None:
        # An error-level event.
        self.emit("error", event_type, **fields)

    def critical(self, event_type: EventType, /, **fields: object) -> None:
        # A critical-level event.
        self.emit("critical", event_type, **fields)

    def close(self) -> None:
        # Flush and stop the client; never raises.
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


# A no-op sink until an entry point calls init(): importing a module that emits, in a test or a
# CLI, starts no thread and sends nothing.
_events: Events = Events(NoopClient(), source="unset")
_bridges: list[Callable[[], None]] = []


def get_events() -> Events:
    # This process's sink: a no-op until init() runs.
    return _events


def _severity_for(level: int) -> Severity:
    # The platform severity for a log record at ERROR or above.
    return "critical" if level >= logging.CRITICAL else "error"


def _loguru_sink(message: Message) -> None:
    # Nothing here may raise, or log at ERROR and above.
    try:
        record = message.record
        exception = record["exception"]
        fields: Details = {
            "message": str(record["message"]),
            "logger": str(record["name"]),
            "module": str(record["module"]),
            "function": str(record["function"]),
            "line": record["line"],
        }
        if exception is not None and exception.value is not None:
            fields["exception"] = "".join(
                traceback.format_exception(exception.type, exception.value, exception.traceback)
            )
        _events.emit(_severity_for(record["level"].no), "log_error", **fields)
    except Exception:  # noqa: BLE001
        pass


class _LoggingHandler(logging.Handler):
    # stdlib logging at ERROR+: uvicorn's own error log and db/'s `logging` loggers.

    def emit(self, record: logging.LogRecord) -> None:  # pyright: ignore[reportImplicitOverride]
        # Forward one ERROR-or-above stdlib record as a log_error event.
        try:
            fields: Details = {
                "message": record.getMessage(),
                "logger": record.name,
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            }
            if record.exc_info and record.exc_info[1] is not None:
                fields["exception"] = "".join(traceback.format_exception(*record.exc_info))
            _events.emit(_severity_for(record.levelno), "log_error", **fields)
        except Exception:  # noqa: BLE001
            pass


def _install_bridges() -> None:
    # Forward ERROR logs from loguru and stdlib logging, and uncaught exceptions, as events.
    sink_id = logger.add(_loguru_sink, level="ERROR", format="{message}", catch=True)
    _bridges.append(lambda: logger.remove(sink_id))

    handler = _LoggingHandler(level=logging.ERROR)
    root = logging.getLogger()
    root.addHandler(handler)
    _bridges.append(lambda: root.removeHandler(handler))

    previous = sys.excepthook

    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        # Report an uncaught exception, then hand it on to the previous hook.
        if not issubclass(kind, KeyboardInterrupt):
            _events.critical(
                "log_error",
                message=f"uncaught {kind.__name__}: {value}",
                exception="".join(traceback.format_exception(kind, value, tb)),
            )
        previous(kind, value, tb)

    sys.excepthook = hook

    def restore() -> None:
        # Put back the previous excepthook.
        sys.excepthook = previous

    _bridges.append(restore)


def init(
    source: Source,
    *,
    env: Mapping[str, str] | None = None,
    client: Client | None = None,
    **common: object,
) -> Events:
    # Build this process's sink from the environment and bridge its error logs.
    #
    #     Called once, first thing, by each entry point. Without AXIOM_TOKEN and AXIOM_DATASET the
    #     sink is a no-op and no bridge is installed, so behaviour is exactly as before.
    global _events
    reset()
    _events = Events(client or client_from_env(env), source=source, **common)
    if _events.enabled:
        _install_bridges()
    return _events


def reset() -> None:
    # Flush and drop the current sink and its bridges. For init() and for tests.
    global _events
    while _bridges:
        try:
            _bridges.pop()()
        except Exception:  # noqa: BLE001
            pass
    _events.close()
    _events = Events(NoopClient(), source="unset")


def config_error(exc: BaseException) -> str:
    # A misconfiguration as a bounded string, for `service_misconfigured`.
    return f"{type(exc).__name__}: {exc}"[:2000]


__all__ = [
    "AXIOM_API_URL",
    "COMPETITION",
    "SEVERITIES",
    "SOURCES",
    "AxiomClient",
    "Client",
    "EventType",
    "Events",
    "NoopClient",
    "Severity",
    "Source",
    "Transport",
    "client_from_env",
    "config_error",
    "encode",
    "get_events",
    "init",
    "reset",
]
