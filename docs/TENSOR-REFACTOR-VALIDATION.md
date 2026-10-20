# Tensor refactor: local validation

Date: 2026-10-20. Local amd64 host, disposable PostgreSQL 17, real sandboxed Lean builds.

## Implemented

- One coordinator/proof/job/scoring/reward path; retired monolithic worker and practice dispatch removed.
- Ten-table fresh schema, frozen initial revision `tensor_0001`, one fenced SQL job protocol.
- Immutable sources and requests; completed measurements reused through evaluation-job links.
- Latest-registration intake, explicit proof leases, board activation boundaries, atomic promotion/publication.
- Configurable per-board current/previous rewards; chain-attempt records without per-block snapshot writes.
- Updated PM2, setup/build targets, migration image, grants and central API schema contract.

## Checks

- **141 fast regression tests** against migrated PostgreSQL, including concurrency, fencing and restricted-role grants.
- Real proof suite: **nine proof/attack cases** passed their expected stages (one slow pytest test, about 14 minutes).
- Migration head matches ORM metadata; disposable downgrade/re-upgrade passed.
- Migration Docker image built and applied the initial migration to an empty database.
- CI-pinned Ruff 0.16.9 lint/format, shell syntax, Compose rendering and PM2 entry-point checks.
- Research showcase executable builds using its isolated scalar reference library.

## Real worker rehearsals

The harness queues through the intake repository using a synthetic local registration. Separate
coordinator and hardware-worker processes prove and measure the entry. No HTTP intake, real chain
registration or chain weight submission is performed.

Raw observations, source snapshots and manifests are under `data/tensor-refactor/` (ignored by Git).

| Run | Duration | Proof | Jobs / decisions | Computed weights |
|---|---:|---|---:|---|
| Clean six-board copy control | 419.9s | passed | 6 / 6 | [1,0] |
| MobileNet takeover control | 339.6s | passed | 1 / 1 | [0.84, 0.16] |

### Copy control

| Board | Score | Decision |
|---|---:|---|
| overall | 0.9450 | guard-failed |
| mlp | 1.0047 | above-bar |
| resnet | 0.9820 | above-bar |
| mobilenet | 1.0142 | above-bar |
| normalized_mlp | 0.9908 | guard-failed |
| mixer | 1.0146 | above-bar |

### Genuine takeover control

Candidate: `kernels/exact/incumbent/Kernel.lean` with a harmless source comment.
Reference: `kernels/exact/slowdepthwise/Kernel.lean`, whose depthwise kernel uses the correct,
slower specification implementation. Both passed the real proof fixture suite.

The MobileNet comparison scored **0.1469**, outcome **qualifies**.
The coordinator recorded the promotion and published the current champion's 80% board allocation.
With a 20% competition share, that produces **16% miner / 84% treasury**; the missing previous
champion's share stays with treasury.

An earlier full-board rehearsal also passed. A subsequent attempt exhausted the harness's
15-minute wall-clock limit after waiting about 7.5 minutes behind the proof-suite lock. It was
repeated without the wait and passed. Its partial evidence remains under `final-rehearsal/`;
it is not counted as a successful run.

## Reproduce

Run workspace-mutating Lean checks sequentially.

```bash
export PYTHONPATH=.:validator
POSTGRES_PORT=PORT POSTGRES_PASSWORD=PASSWORD .venv/bin/pytest -q -m 'not slow'
.venv/bin/pytest -q -m slow -s
.venv/bin/python scripts/test-exact-workflow.py \
  --admin-url postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/postgres \
  --output data/my-tensor-run --minutes 15
.venv/bin/python scripts/test-exact-workflow.py \
  --admin-url postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/postgres \
  --output data/my-takeover --board mobilenet \
  --reference kernels/exact/slowdepthwise/Kernel.lean --minutes 15
```

## Limits and deployment boundary

Quick measurements use five rounds, three inner repetitions and 2 ms target cells. They validate
workflow and a large control speedup; they do not calibrate production noise thresholds.
ARM/Pi execution and real chain submission were not exercised.

The six-board run predates the isolated research-library declaration added for exactShowcase.
The takeover run uses the final build graph; sources are frozen per run. Mathematical specifications
and kernels were unchanged by this refactor.

Deployment requires a **fresh competition DB** and coordinated central API/frontend changes.
See [the schema contract](../deploy/platform/CONTRACT.md). No production systems were modified.
