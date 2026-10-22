# Central API contract for the replacement tensor schema

This repository does not implement the central API. Updating conjectures-validator and its
frontend is a separate coordinated rollout; old FloatLib SQL cannot target this schema.

## Intake

One signed UTF-8 Kernel.lean, 1–256000 bytes. Keep the existing five-line signature message
and coordinated competition slug. Validate signature age, membership, configured registration cutoff
and optional Deflate usage policy in the central API.

In one transaction:
1. Acquire `pg_advisory_xact_lock(hashtextextended('tensor:intake',0))` (also used for board activation).
2. Lock/select the latest eligible registration for the hotkey; never fall back to an older one.
3. Insert immutable `sources(sha256,content)` with ON CONFLICT DO NOTHING.
4. Insert `submissions(hotkey,registration_id,source_sha256,account_id)`, RETURNING id.

Unique registration_id prevents a second upload regardless of proof status. Unique hotkey/source
rejects duplicates even after re-registration. Sources verify their SHA-256 in a DB trigger.
Use DB reception time; do not supply arbitrary IDs or dates. No entitlement_claims, slot,
submission_files or competition-side rate-limit table exists. The central API owns request limits.

## Reads

- Submission proof: submissions.state = queued/running/proved/rejected and proof_report JSON.
- Evaluation outcomes: evaluations per submission/board/generation; raw jobs via evaluation_jobs.
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
