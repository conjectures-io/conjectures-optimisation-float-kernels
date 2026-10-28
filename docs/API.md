# Public API

Production origin: **https://conjectures.io**. Base path: `/v1/competitions/torchlean`.
Reads are public. Uploads use the signed miner client described in the [manual](../MANUAL.md).
The central API's `/openapi.json` is the detailed schema reference.

| Method and path (relative to the base) | Purpose |
|---|---|
| `POST /submissions` | Upload the complete `Kernel.lean`; returns its ID and queued proof state. |
| `GET` base path | Competition overview, upload state, queue depth and boards. |
| `GET /health` | Service/policy readiness and processing counts. |
| `GET /boards` | Active boards, model/hardware weights, thresholds, rewards and references. |
| `GET /leaderboard?board=mlp` | Challengers against that board's current reference. |
| `GET /leaderboard?board=mlp&promotion_id=21` | Challengers against a historical promotion. Optional `config_hash` selects a benchmark configuration. |
| `GET /champions?board=mlp` | Promotion history, current status, reward ranks and source availability. |
| `GET /submissions?state=benchmarking` | Submissions, optionally filtered by processing state. |
| `GET /submissions/123` | Proof status and per-board progress/decisions. |
| `GET /submissions/123/report` | Detailed timing evidence, checksums, policy and recorded decisions. |
| `GET /submissions/123/source` | `Kernel.lean` download when the publication policy permits it. |

`board` defaults to `overall`; the board list supplies active names. A `promotion_id` identifies
a particular board takeover, not a submission. A shipped reference has null submission/promotion
IDs and `shipped: true`. Reads have no pagination currently. Pending values can be null;
an empty list means no records, not a failed request.

## Upload response

HTTP 201:

```json
{
  "competition": "torchlean",
  "submission": 123,
  "source_sha256": "<sha256 of Kernel.lean>",
  "proof_state": "queued",
  "status_url": "/v1/competitions/torchlean/submissions/123",
  "intake_mode": "subnet"
}
```

Uploads contain exactly one multipart file part named `Kernel.lean`. The client signs its hash,
competition slug, hotkey and timestamp using sr25519 and sends `X-Conjectures-Hotkey`,
`X-Conjectures-Timestamp` and `X-Conjectures-Signature`. See the [canonical message](../miner/sig.py)
when implementing another client.

Proof states are `queued`, `running`, `proved`, `rejected`. The submission's separate
`processing_state` also describes benchmarking and finalization. A proved entry need not win.

## Sources and errors

Only former champions no longer current on **any** board have public source downloads.
The policy applies by source hash, including other submissions of the same code. Other sources
return 403 `SOURCE_WITHHELD`. Downloads are attachments with `Cache-Control: no-store`.

Errors use `{type, title, status, detail, reason_code}`. Unknown records or unserved competitions
return 404; unavailable competition storage or evidence returns 503. An identical upload from
the same hotkey returns 409 `DUPLICATE_SUBMISSION` with its existing submission ID and state.
