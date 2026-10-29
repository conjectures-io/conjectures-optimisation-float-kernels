# Central API database contract

The HTTP API is implemented in `conjectures-validator`; this repository owns the competition
schema and workers. The public slug and signed-message competition are `torchlean`.
API and competition schema/grant releases must be deployed together.

## Intake

One signed UTF-8 Kernel.lean, 1–256000 bytes. Use the existing five-line signature message with competition slug `torchlean`. Validate signature
age, current recorded membership and the configured registration cutoff in the central API.
Retired competition databases are not queried.

In one transaction:
1. Acquire `pg_advisory_xact_lock(hashtextextended('tensor:intake',0))` (also used for board activation).
2. Select the latest registration for the hotkey under that lock, check its current UID ownership
   and cutoff; never fall back to an older unused one. Row-update grants on registrations are
   not required.
3. Insert immutable `sources(sha256,content)` with ON CONFLICT DO NOTHING.
4. Insert `submissions(hotkey,registration_id,source_sha256,account_id)`, RETURNING id.

Unique registration_id prevents a second upload regardless of proof status. Unique hotkey/source
rejects duplicates even after re-registration. Sources verify their SHA-256 in a DB trigger.
Use DB reception time; do not supply arbitrary IDs or dates.

`tensor_0002` adds `submissions.intake_mode`, default `subnet`. Production uploads require a
non-null registration ID. Explicit development uploads use `intake_mode=dev` with a null
registration ID, enforced together by a check constraint. The central API permits this bypass
only when `APP_MODE=DEV` and `TORCHLEAN_DEV_SKIP_REGISTRATION=1`; signatures, duplicate and request
limits remain enforced. Never manufacture chain registrations to implement the bypass. No entitlement_claims, slot,
submission_files or competition-side rate-limit table exists. The central API owns request limits.

## Reads

- Submission proof: submissions.state = queued/running/proved/rejected and proof_report JSON.
- Evaluation outcomes: evaluations per submission/board/generation; raw jobs via evaluation_jobs.
  Since `tensor_0003` a candidate that ran out of time is decided `timed-out`, with a `timeout`
  summary in the decision; `benchmark-failed` is any other candidate failure. A failed job's
  `error` keeps the worker's `timeout` and `partial` (calibration, repetitions, completed rounds,
  the round in progress) besides `kind` and `reason`; it may say `truncated` instead.
- History: promotions -> evaluations -> submissions -> sources. A baseline is a source,
  not a fabricated miner submission. Current champion is the latest eligible promotion: exclude both promotion-level disqualification
  and an active `source_disqualifications` row for its source hash.
- A board's challengers: evaluations filtered by board and reference_promotion_id.
- Rewards: SELECT publication,publication_hash FROM coordinator_state WHERE id=1.
- Chain audit: weight_sets contains actual attempts and compact policy inputs only.
- Champion audits: `audits`, `audit_attempts` and `audit_cursors` are independent, report-only
  records. `promotions.source_environment` freezes verification/specification provenance.
  Audit results do not change eligibility or publication; explicit operator source restrictions do.

Board names are text; standard config generates overall/mlp/resnet/mobilenet/normalized_mlp/mixer.
Published board/rank allocations are configurable. Missing ranks and unregistered recipients route
to treasury. Supersession preserves history; operator disqualification excludes a rank.

Apply platform_api.sql to reset privileges. The API can insert source/intake fields, read evidence,
and read publication fields. It cannot forge proofs, benchmark outcomes, promotions or leadership.

## Central API compatibility

The central TorchLean API also supports explicit development intake: `intake_mode=dev` with a
null registration ID, no fabricated chain registration. Production rows use `intake_mode=subnet`.
Its read routes require coordinator public configuration/board hashes and source restrictions.
The `tensor_0001` schema and grants in this checkout do not yet supply all of those fields/reads;
merge and apply the companion intake migration and API grants before enabling that API release.
The central API owns its own rate-limit migrations; it never migrates the competition database.
