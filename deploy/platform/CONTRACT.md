# Serving FloatLib from the conjectures platform: the contract

What `conjectures-validator` needs to change to serve the FloatLib competition the way it serves DEFLATE.
It also lists what this repository already provides. The platform side was read at `conjectures-validator`
commit `d24e86c` (2026-09-30). This repository's side is migration `0006` and `deploy/platform/`.

## Summary

- **The model is the same as DEFLATE's.**
  - This repository runs the gate worker, the chain watcher and the weight setter against its own Postgres.
  - The platform queues submissions into that database and reads what the weight setter publishes. It
    connects as `platform_api`, whose grants are in `platform_api.sql`.
- **The database is ready.** Migration `0006` adds the four columns the platform's schema check requires.
  `tests/test_platform_grants.py` checks the role's exact privileges.
- **The platform's competition adapter is DEFLATE-specific** (`submission_api/compression_store.py`,
  `competitions.py`, `competition_sig.py`, the read views). Five things need a FloatLib variant:
  - the upload (one `Kernel.lean`, not `parse.rs` + `Parse.lean`)
  - the digest
  - the conflict key on insert
  - the read schema (a time ratio, not compression and a Pareto frontier)
  - the published snapshot's shape
- **The platform serves one competition per process.** FloatLib needs a second API deployment, or
  multi-competition support in the platform.
- **Scope:** the platform serves the `add` slot only, as the competition `floatlib`. The other four slots
  stay on this repository's own API (`floatlib-mul`, `-fma`, `-div`, `-sqrt`) until the platform supports more than
  one board per competition.

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
docker compose up migrate                # head: 0006 or later
deploy/platform/grant.sh                 # creates/rotates platform_api, resets its grants
```

Re-run `grant.sh` after every migration.

## Schema check

`compression_store.py` requires four columns: `submission_files.content`, `submissions.verification_attempt`,
`submissions.account_id` and `weight_sets.api_snapshot`. All four exist from migration `0006` on, so the check
passes as it stands.

## Intake: `POST /v1/competitions/floatlib/submissions`

| | DEFLATE today | FloatLib |
|---|---|---|
| File parts | exactly `parse.rs` and `Parse.lean` | exactly one, `Kernel.lean`: 1 byte to 256,000 bytes, valid UTF-8 |
| Digest | sha256(parse.rs ‖ Parse.lean) | sha256(Kernel.lean) |
| Signed message | `conjectures-competition-submit-v1` / `competition` / `digest` / `hotkey` / `timestamp` | **the same five lines**, `competition: floatlib` |
| Headers | `X-Conjectures-Hotkey`, `-Timestamp`, `-Signature` | the same |
| Insert | `submissions(hotkey, digest, account_id)` `ON CONFLICT (hotkey, digest)` | `submissions(hotkey, digest, account_id)`. `slot` defaults to `'add'`. The unique key is **`(hotkey, slot, digest)`** (`uq_submissions_hotkey_slot_digest`), so the conflict target must be `(hotkey, slot, digest)`. |
| Files | two `submission_files` rows | one row, `name = 'Kernel.lean'` (enforced by `ck_submission_files_name`) |
| Rate limits, entitlement, advisory lock | as implemented | unchanged: same tables and the same rule (unclaimed registrations > queued + verifying) |

The signing contract is pinned on this side. `miner/sig.py` and `validator/service/sig.py` build the platform's
message byte for byte, and `tests/test_miner_sig.py` holds them to it. FloatLib's own API already takes
exactly this request, so a miner client works against either.

## Reads

### `submissions`

| | Columns |
|---|---|
| FloatLib has | `id`, `hotkey`, `slot`, `digest`, `submitted_at`, `state` (same five values), `exit_code`, `stage`, `reason`, `ratio`, `classes`, `rounds`, `seed`, `incumbent_sha256`, `verifier_fingerprint`, `gate_seconds`, `report`, `account_id`, `worker_id`, `claimed_at`, `verification_attempt`, `gate_errors`, `finished_at` |
| DEFLATE's `SUB_FIELDS` reads, absent here | `baseline_key`, `baseline_active`, `source_sha256`, `static_verified_at`, `lean_verified_at`, `aggregation_id`, `admission_check_id` |

- A FloatLib row is public when `hotkey IS NOT NULL`, which is always: there are no baselines.
- `report` is the gate's verdict JSON in full.
- The platform should filter `slot = 'add'`.

### Scoring

- **No aggregation or admission tables.** FloatLib has neither `benchmark_aggregations` nor
  `submission_admission_checks`. The admission outcome of every scored submission is inside the published
  snapshot.
- **`score_snapshots`:** `(id, weight_set_id, slot, hotkey, submission_id, ratio, weight)`, one row per hotkey per
  slot. `weight` is that line's fraction of the competition's budget, like DEFLATE's `payable_weight`; the platform
  reads `slot = 'add'`.
- **`weight_sets`:** published rows are those with `jsonb_typeof(api_snapshot) = 'object'`, as for DEFLATE. The
  weight setter writes one every tick, before any chain check, then updates `accepted` and `error`.

`weight_sets.api_snapshot`, built by `validator/reward.py` `publication()`:

```json
{
  "competition": "floatlib",
  "computed_at": "2026-10-07T12:00:00+00:00",
  "policy": {
    "version": "king-of-the-hill-per-slot-v1",
    "competition_share": 0.2,
    "slot_shares": {"add": 0.2, "mul": 0.2, "fma": 0.2, "div": 0.2, "sqrt": 0.2},
    "winner_share": 1.0,
    "promotion_bar": 0.97,
    "admission": {"version": "paired-median-bootstrap-v1", "draws": 2000, "quantile": 0.05}
  },
  "slots": {
    "add": {
      "share": 0.2,
      "leader": 42,
      "items": [
        {"hotkey": "5F…", "submission_id": 42, "ratio": 0.53, "weight": 0.2, "admission": "leader"},
        {"hotkey": "5G…", "submission_id": 40, "ratio": 0.61, "weight": 0.0, "admission": "not-significant"}
      ]
    },
    "mul": {"share": 0.2, "leader": null, "items": []}
  }
}
```

- **Per slot:** every slot's leader gets an equal part (`slot_shares`) of the competition's budget. The platform
  serves `slots.add`; the other slots are scored and paid by the validator alongside it.
- **Lower ratio is faster:** `ratio` is submission time over incumbent time.
- **Admission outcomes:** `leader`, `not-significant`, `above-bar`, `no-evidence`, or `null` before the first
  evidence.
- **Not in the snapshot:** `observed_submissions` (DEFLATE's staleness check) and `bounty`, since FloatLib has
  no bounty yet.

### What the read views assume that does not hold here

| DEFLATE assumption | FloatLib |
|---|---|
| Two metrics: `balanced_time_ratio` and `mean_file_compression_pct` | one metric, `ratio` (lower is better), against `promotion_bar` |
| `/pareto` and `on_frontier` | none: king of the hill, one admitted leader |
| `pareto_weight` / `improvement_weight` / `combined_weight` / `payable_weight` | `weight` only |
| Source withheld while on the frontier | not decided; the simplest rule is to withhold the leader's |
| Pipeline stages static, lean, benchmark, aggregation | the gate's stages: intake, policy, statement, kernel, axioms, score |
| `description`, `files`, `metric_definitions` hard-coded | needs FloatLib values: one file `Kernel.lean`; metric `ratio` |

FloatLib's own API already serves FloatLib-shaped reads under the same paths, and a FloatLib adapter on the
platform can return the same shapes:
- `GET /v1/competitions/{slug}/submissions/{id}`
- `GET .../submissions/{id}/report`
- `GET .../leaderboard`

They are in `validator/service/schemas.py` and `routes.py`.

## Weights and emissions

- **One weight setter per hotkey.** Only one process may call `set_weights` per validator hotkey on SN66.
  Today that is DEFLATE's weight setter, which pays treasury uid 121 its 80% and DEFLATE's miners its 20%.
- **FloatLib's weight setter is ported from DEFLATE's**, with the same treasury split and code constants
  (`validator/treasury.py`). It runs in **dry run by default**: it records every vector and never submits one.
- **Before launch, decide:**
  - which process sets the combined vector, and how FloatLib's share is carved out of the treasury's or
    DEFLATE's (the platform's `allocation.py` asserts `TREASURY_BPS` + every `COMPETITION_BPS` = 10,000)
  - FloatLib's `COMPETITION_BPS` (a placeholder 2,000 in `treasury.py`)
- **Bug found on the platform side:** `emissions_worker/source.py` reads a top-level `computed_at`, but
  `GET …/weights/current` returns it under `context.computed_at`. Pointed at the real API, it always falls
  back to paying the treasury only.

## Observability

- **Event format:** FloatLib's four processes send Axiom events in the platform's envelope
  (`validator/axiom.py`). They use the same `source` names and `event_type` vocabulary as DEFLATE, and differ
  only in `competition: "floatlib"`.
- **Settings:** the same as DEFLATE's: `AXIOM_TOKEN`, `AXIOM_DATASET`, `AXIOM_ENVIRON`, `AXIOM_URL`.
- No change is needed in the platform's `labels.py`, unless dashboards want per-competition sources.

## Checklist

On the platform (`conjectures-validator`):

- [ ] A FloatLib intake:
  - one `Kernel.lean` part, UTF-8, 256,000 bytes at most
  - digest sha256(Kernel.lean)
  - the conflict target `(hotkey, slot, digest)`
  - one `submission_files` row
- [ ] FloatLib read views over the columns above, the snapshot shape above, and `slot = 'add'`.
- [ ] FloatLib's `description`, `files` and `metric_definitions`.
- [ ] A second API deployment with `COMPETITION_SLUG=floatlib`, or multi-competition support.
- [ ] An emissions decision: FloatLib's share, and which process submits the vector.
- [ ] Optionally, the `computed_at` fix in `emissions_worker/source.py`.

Already done in this repository:

- [x] Migration `0006`: `submission_files`, `submissions.report`, `submissions.account_id`,
  `weight_sets.api_snapshot`.
- [x] The gate worker reads `Kernel.lean` from `submission_files`.
- [x] `platform_api.sql`, `grant.sh`, the compose overlay, and `tests/test_platform_grants.py`.
- [x] The platform's signed message and request shape in the miner client and in FloatLib's own API.
- [x] The published snapshot on every weight set.
- [x] The treasury split and the dry-run default in the weight setter.
- [x] Axiom events.
