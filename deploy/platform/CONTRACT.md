# Serving FloatLib through the conjectures platform: the interface

What this repository exposes to `conjectures-validator` (the platform) so it can serve the FloatLib competition the
way it serves DEFLATE: the database it connects to, the intake it writes, the rows it reads and the snapshot it
serves. The platform side was read at `conjectures-validator` commit `d24e86c` (2026-09-30).

## Summary

- **The model is DEFLATE's.** This repository runs the gate worker, the chain watcher and the weight setter
  against its own Postgres; the platform queues submissions into that database and reads what the weight setter
  publishes, connecting as `platform_api` (grants in `platform_api.sql`, checked by
  `tests/test_platform_grants.py`).
- **One entry, one board.** The slug `floatlib` is the full entry (slot `full`): one `Kernel.lean` with FloatLib's
  `add` and `mul` and TorchLean's `sum` and `bmm`, proved and timed together, and the only board that pays. The
  single-kernel practice boards (`floatlib-add`, `-mul`, `-fma`, `-div`, `-sqrt`, `-sum`, `-bmm`) are unpaid and
  served by this repository's own API.
- **The signing contract is shared.** A miner signs the same five lines for the platform and for this repository's
  own API, so one client works against either.

## Connection

| Setting (platform `.env`) | Value |
|---|---|
| `COMPETITIONS_ENABLED` | `1` |
| `COMPETITION_SLUG` | `floatlib` |
| `COMPETITION_NAME` | `FloatLib` |
| `COMPETITION_DATABASE_URL` | `postgresql+psycopg://platform_api:<password>@conjectures_floatlib_db:5432/<POSTGRES_DB>` |

On the FloatLib host:

```bash
set -a; . ./.env; set +a                 # PLATFORM_API_PASSWORD, and COMPOSE_FILE with the overlay
docker compose up -d db                  # joins ${PLATFORM_NETWORK:-conjectures-api_default}
docker compose up migrate                # head: 0017
deploy/platform/grant.sh                 # creates/rotates platform_api, resets its grants
```

Re-run `grant.sh` after every migration.

## Schema check

The platform's `compression_store.py` requires four columns: `submission_files.content`,
`submissions.verification_attempt`, `submissions.account_id` and `weight_sets.api_snapshot`. All four exist from
migration `0006` on.

## Intake: `POST /v1/competitions/floatlib/submissions`

| | FloatLib |
|---|---|
| File parts | exactly one, `Kernel.lean`: 1 byte to 256,000 bytes, valid UTF-8 |
| Digest | sha256(Kernel.lean) |
| Signed message | `conjectures-competition-submit-v1` / `competition: floatlib` / `digest` / `hotkey` / `timestamp` |
| Headers | `X-Conjectures-Hotkey`, `X-Conjectures-Timestamp`, `X-Conjectures-Signature` |
| Insert | `submissions(hotkey, digest, account_id)`; `slot` defaults to `'full'` (migration 0011), so a slotless insert lands on the full entry. The unique key is `(hotkey, slot, digest)` (`uq_submissions_hotkey_slot_digest`) |
| Files | one `submission_files` row, `name = 'Kernel.lean'` (enforced by `ck_submission_files_name`) |
| Entitlement | queued + verifying submissions stay below the hotkey's unclaimed registrations; this repository's own API checks it and inserts in one transaction under a per-hotkey advisory lock (`enqueue()` in `validator/db/submissions.py`) |

`miner/sig.py` and `validator/service/sig.py` build the signed message byte for byte, and
`tests/test_miner_sig.py` holds them to it.

## Reads

### `submissions`

| | Columns |
|---|---|
| FloatLib | `id`, `hotkey`, `slot`, `digest`, `submitted_at`, `state` (`queued`, `verifying`, `accepted`, `rejected`, `error`), `exit_code`, `stage`, `reason`, `ratio`, `classes`, `rounds`, `seed`, `incumbent_sha256`, `champion_sha256`, `verifier_fingerprint`, `gate_seconds`, `report`, `account_id`, `worker_id`, `claimed_at`, `verification_attempt`, `gate_errors`, `finished_at` |
| DEFLATE columns absent here | `baseline_key`, `baseline_active`, `source_sha256`, `static_verified_at`, `lean_verified_at`, `aggregation_id`, `admission_check_id` |

- Every row has a `hotkey`: there are no baselines.
- `report` is the gate's verdict JSON in full.
- The paid board is `slot = 'full'`.
- The board counts accepted rows whose `champion_sha256` is the slot's current champion's; `incumbent_sha256` and
  `verifier_fingerprint` are provenance.

### `promotions`

`(id, slot, submission_id, champion_sha256, promoted_at, failures, demoted_at)`, one row per submission that became its slot's champion
(`submission_id` unique, `champion_sha256` its `Kernel.lean` digest, index `ix_promotions_slot_id`). The gate
worker inserts it in the same transaction as the verdict, when an accepted entry timed against the current champion
has a ratio at or below `promotion_bar` and a positive 5th-percentile bootstrapped gain. A slot's champion is its
latest promotion not demoted; with none, the shipped champion holds. `failures` is a historical count from
the previous automatic-demotion policy. Failures now pause the slot without demoting its champion;
`demoted_at` changes only through an explicit operator demotion. `platform_api` may read it (`SELECT`): the
platform serves every board's champion, its promotion date and the history of champions, while the published
snapshot names the paid board's `leader` and its paid champions (`kings`).

### `slot_pauses`

Migration `0017` records paused slots, their waiting challenger, reference identity, failure phase
and reason, and retry deadline. These rows schedule retries without disqualifying submissions.
Successful reference benchmarking clears the pause. Rewards are preserved by default, or temporarily
redirected with `WEIGHT_PAUSED_SLOT_POLICY=treasury`. Published slots carry `paused` and
`paused_reward_policy`; the platform does not need direct access to this scheduling table.

### Scoring

- **No aggregation or admission tables.** FloatLib has neither `benchmark_aggregations` nor
  `submission_admission_checks`; the admission outcome of every scored submission is in the published snapshot.
- **`score_snapshots`:** `(id, weight_set_id, slot, hotkey, submission_id, ratio, weight)`, one row per paid
  champion and one per other hotkey per paying slot (a hotkey holding two paid ranks has two rows). `weight` is that line's fraction of the competition's budget, like DEFLATE's `payable_weight`.
- **`weight_sets`:** published rows are those with `jsonb_typeof(api_snapshot) = 'object'`, as for DEFLATE. The
  weight setter reuses unchanged publications; changes and actual chain attempts create new rows, and idle polls
  never overwrite an earlier chain outcome. `computed_at` is the publication time. Migration 0013 adds the content
  hash and the lookup and cleanup indexes.
- **Snapshot retention:** old `api_snapshot` payloads expire after one hour by default; the latest valid payload
  per subnet is kept regardless of age. Rows and score history remain, and a cleared payload reads as missing.
- **`submission_state_sha256`** tracks queue and verdict changes without embedding every submission; it takes part
  in publication deduplication.

`weight_sets.api_snapshot`, built by `validator/reward.py` `publication()`:

```json
{
  "competition": "floatlib",
  "computed_at": "2026-10-07T12:00:00+00:00",
  "policy": {
    "version": "top3-kings-60-30-10-v1",
    "competition_share": 0.2,
    "slot_shares": {"full": 1.0},
    "winner_share": 1.0,
    "rank_shares": [0.6, 0.3, 0.1],
    "promotion_bar": 0.97,
    "admission": {"version": "paired-median-bootstrap-v1", "draws": 2000, "quantile": 0.05}
  },
  "slots": {
    "full": {
      "share": 1.0,
      "leader": 42,
      "kings": [42, 31, 17],
      "items": [
        {"hotkey": "5F…", "submission_id": 42, "ratio": 0.56, "weight": 0.6, "rank": 1, "admission": "leader"},
        {"hotkey": "5E…", "submission_id": 31, "ratio": 0.95, "weight": 0.3, "rank": 2, "admission": null},
        {"hotkey": "5D…", "submission_id": 17, "ratio": 0.91, "weight": 0.1, "rank": 3, "admission": null},
        {"hotkey": "5G…", "submission_id": 47, "ratio": 0.99, "weight": 0.0, "rank": null, "admission": "above-bar"}
      ]
    }
  }
}
```

- **One paying slot, three paid champions:** `leader` is the full entry's champion; `kings` lists the paid
  champions newest first (the latest three promotions not demoted), and each one's item carries its `rank` and its
  `weight`, `winner_share × rank_shares[rank − 1]` of the competition's budget (`slot_shares`), in `slots.full`.
  A rank no one holds yet goes to the treasury; while the shipped champion holds, `leader` is null, `kings` empty
  and the budget goes to the treasury. With `paused_reward_policy` `treasury`, a paused slot's rank-1 item has
  weight 0 and the other ranks are still paid. A full entry's `ratio` is its total time over its champion's across all twelve timed input types
  (`add`, `mul`, `sum`, `bmm`, three each); the champion's own ratio is against the champion it took over from.
- **Lower ratio is faster:** `ratio` is submission time over champion time.
- **Admission outcomes:** `leader` (the champion), `qualifies`, `not-significant`, `above-bar`, `no-evidence`, or
  `null` before the first evidence.
- **Not in the snapshot:** `observed_submissions` (DEFLATE's staleness check) and `bounty`.

### How FloatLib's reads differ from DEFLATE's

| DEFLATE | FloatLib |
|---|---|
| two metrics: `balanced_time_ratio` and `mean_file_compression_pct` | one metric, `ratio` (lower is better), against `promotion_bar` |
| `/pareto` and `on_frontier` | king of the hill: the champion, replaced automatically by an entry 3% faster |
| `pareto_weight` / `improvement_weight` / `combined_weight` / `payable_weight` | `weight` |
| pipeline stages static, lean, benchmark, aggregation | the gate's stages: intake, policy, statement, kernel, axioms, score |
| files `parse.rs` and `Parse.lean` | one file, `Kernel.lean` |

This repository's own API serves FloatLib-shaped reads under the same paths, from `validator/service/schemas.py`
and `routes.py`:
- `GET /v1/competitions/{slug}/submissions/{id}`
- `GET .../submissions/{id}/report`
- `GET .../leaderboard`

## Weights

- **One weight setter per hotkey:** only one process may call `set_weights` per validator hotkey on SN66.
- **The split:** the treasury (uid 121) takes 80% and the competition 20% (`COMPETITION_BPS` = 2,000 in
  `validator/treasury.py`), as in DEFLATE. The platform's `allocation.py` holds `TREASURY_BPS` plus every
  `COMPETITION_BPS` to 10,000.
- **Dry run by default:** the weight setter records every vector and submits one only with `WEIGHT_DRY_RUN=0`.

## Observability

- **Event format:** FloatLib's four processes send Axiom events in the platform's envelope (`validator/axiom.py`),
  with the same `source` names and `event_type` vocabulary as DEFLATE, and `competition: "floatlib"`.
- **Settings:** the same as DEFLATE's: `AXIOM_TOKEN`, `AXIOM_DATASET`, `AXIOM_ENVIRON`, `AXIOM_URL`.

## What this repository provides

- Migration `0006`: `submission_files`, `submissions.report`, `submissions.account_id`, `weight_sets.api_snapshot`;
  migrations up to `0014` for slots, provenance, snapshot retention and `champion_sha256`; `0015` for `promotions`; `0016` for historical failure counts and demotion; `0017` for slot pauses.
- The gate worker reads `Kernel.lean` from `submission_files`.
- `platform_api.sql`, `grant.sh`, the compose overlay, and `tests/test_platform_grants.py`.
- The platform's signed message and request shape, in the miner client and in this repository's own API.
- The published snapshot on every changed weight set.
- The treasury split and the dry-run default in the weight setter.
- Axiom events.
