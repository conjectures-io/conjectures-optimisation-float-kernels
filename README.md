# TorchLean kernel competition (on FloatLib)

## Quick Summary

- A proof-gated speed competition whose goal is faster, proven [TorchLean](https://github.com/lean-dojo/TorchLean) kernels, built from faster [FloatLib](https://github.com/lean-dojo/FloatLib) arithmetic. **One entry, one board:** a miner submits one Lean file with four kernels and their proofs: binary32 `add` and `mul` equal to FloatLib's IEEE spec, and TorchLean's `sum` and batched matrix multiply `bmm` equal to TorchLean's own specification, bit for bit, built from that `add` and `mul`. The gate checks all four proofs, re-checks them in the Lean kernel, audits axioms, then times all four against the incumbent's. Lower total time wins, and the entry's leader is paid the competition's share.
- **Scoring:** twelve timed input types (three per kernel), the ratio being the entry's total time over the incumbent's, so every kernel counts by the time it takes and a faster `add` or `mul` counts twice: directly, and inside `sum` and `bmm`. The incumbent is the best proven work so far: `tuned` addition and `mul/fast` multiplication in all four places (`kernels/full/incumbent`), at 0.56x of FloatLib's own `add`/`mul` with TorchLean's loops (`kernels/full/baseline`), so copying the published examples wins nothing. A full entry takes about 3 minutes to check. Shares of the incumbent's time: `bmm` 38%, `add` 23%, `sum` 21%, `mul` 18%; an entry may improve only one kernel and keep the incumbent's code for the rest (`bmm` alone must reach 0.92x of the incumbent's to clear the bar).
- **TorchLean without a TorchLean dependency:** the rules for `sum` and `bmm` are restated over FloatLib in `ContractTensor`, so validators need neither TorchLean nor a C compiler; `lean/stage2/Stage2/Link.lean` proves them equal to TorchLean's by definition.
- **Practice boards, unpaid:** every kernel also has a single-kernel board (`add`, `mul`, `fma`, `div`, `sqrt`, `sum`, `bmm`) for trying one idea at a time. Best proven there: `add` 0.50x (`tuned`; an unproved textbook adder reaches 0.40x), `mul` 0.55x, `sum` 0.52x, `bmm` 0.57x.
- **Miners: read [`MANUAL.md`](MANUAL.md)** for setup, what to submit, how to measure, what to prove and what gets rejected; section 14 covers the TorchLean slots. This README is the overview for operators and reviewers.
- One command sets up a fresh clone (about 15 minutes); one command tests the whole environment against 48 expected verdicts across the full entry and the practice boards (about 30 minutes).
- A validator service is included: miners submit signed kernels over HTTP (`miner/submit.py`), a worker runs the gate, and a leaderboard ranks them.

```bash
git clone <this repo> && cd conjectures-floatlib-competition
./setup.sh                                          # elan + Lean v4.34.0, FloatLib + Mathlib, build, .venv
export PATH=$HOME/.elan/bin:$PATH
.venv/bin/python selftest.py                        # 48/48 cases as expected = ready
.venv/bin/python gate.py kernels/full/incumbent     # a full entry: add, mul, sum, bmm (the default slot)
.venv/bin/python gate.py --slot add kernels/add/fastlog   # one kernel on its practice board
.venv/bin/python gate.py --bench my-entry           # time an unproved entry; never a verdict
.venv/bin/python gate.py --slot mul kernels/mul/incumbent   # the multiplication slot
```

## How the gate works

```mermaid
flowchart LR
    M["miner: Kernel.lean<br/>def add : B32 → B32 → B32<br/>theorem add_spec : ∀ x y, add x y = Spec.add x y"] --> I["0 intake<br/>one file, ≤ 256 KB"]
    I --> P["1 policy<br/>imports Contract only; no escape hatch or<br/>elaboration-time code; options and attributes allow-listed"]
    P --> S["2 statement<br/>theorem accepted : Obligation Submission.add := Submission.add_spec<br/>built in bwrap into an overlay, own modules only"]
    S --> K["3 kernel<br/>leanchecker replays Submission and Verify"]
    K --> A["4 axioms<br/>propext, Classical.choice, Quot.sound only"]
    A --> B["5 score<br/>pinned incumbent vs submission, fresh seed,<br/>3 timed operand classes, one CPU, 25 rounds;<br/>every class checksum must agree"]
```

Every run writes `logs/gate-<utc>.log` (every command and its exit code) and one
`results/<slot>-<submission>-<mode>-<utc>.json` verdict: the stage reached, the reason, the seed, the
ratio, per-class nanoseconds and spreads, and the sha256 of the pinned incumbent binary.

## The ladder

The `add` kernels in `kernels/add/`; same seed, 500k pairs per operand class, 25 rounds, one CPU:

| kernel | what | verdict | time vs incumbent |
|---|---|---|---|
| `incumbent` | `ExecFloat.add`, proof `add_eq_spec` | accepted | 1.00x |
| `word` | `Configured.Backend.wordAdd` called directly | accepted | 1.00x: dispatch is free |
| `direct` | the word kernel `addFiniteImpl?` on raw bits, no carrier codec | accepted | 0.80x |
| `mirror` | `addFiniteImpl?` step for step on bare words, no `Option` or `Model` | accepted | 0.78x |
| `fastlog` | `mirror` with a binary-search `log2` in `roundProduct` | accepted | 0.53x |
| `tuned` | `fastlog` with a one-compare special test and a rounding step on machine words | accepted | **0.50x** |
| `textbook-unproved` | guard/round/sticky adder, specials to `ExecFloat.add` | rejected at policy (`sorry`) | 0.40x with `--bench` |
| `wrong` | subtraction with the addition theorem | rejected at statement | |
| `cheat` | the host FPU with `native_decide` | rejected at policy | |

Where the incumbent's time goes: the carrier codec and the `Option`/`Model` wrappers around the
word kernel cost about 20%; `UInt64.log2`, which the runtime computes one bit at a time, about 30%.
None of the four accepted faster kernels needed new floating-point reasoning: each proves it
equals `wordAdd` and reuses `wordAdd_eq_spec`. Below 0.50x the cost is the algorithm itself, and
beating it means proving a new rounding argument, FloatLib's own being 563 lines.

## Security model

| threat | defence |
|---|---|
| a proof that does not prove the obligation | the gate's own `theorem accepted : Obligation Submission.add := Submission.add_spec` must typecheck |
| `sorry`, `native_decide`, custom axioms | `#print axioms accepted` must list only Lean's three |
| a forged compiled `.olean` whose proof never passed the kernel | `leanchecker` replays every declaration of `Submission` and `Verify` through the kernel |
| miner code running during the build (`#eval`, `run_cmd`, macros, `@[command_elab]`, `initialize`) | policy bans them, and every build and bench runs in bwrap: read-only root, no network |
| a build-time file read reaching a secret (`include_str`, and any read at all) | policy bans `include_str`; the sandbox hides the real HOME (the validator wallet) and the repo root (its `.env`, its stored submissions) behind tmpfs and restores only the toolchain and the Lean workspace read-only, and the build runs with a minimal environment, not the validator's |
| a build or bench exhausting the validator's memory or process table | every sandboxed command runs in a systemd scope: 4096 MB for builds and kernel checks, 512 MB for benches, no swap, 256 tasks; the OOM kill rejects the submission, and a host that cannot apply the caps fails the gate as a validator error |
| a slow file holding the single gate worker, resubmitted | every command the entry controls (its builds, kernel replays and bench runs) shares one time budget, `GATE_BUDGET_SECONDS` (600; a full entry uses about 120); running out of it or over a memory cap rejects the entry, and every verdict, accepted or rejected, spends a registration |
| a build filling the validator's disk | every sandboxed command runs under `prlimit`, so no file it writes can pass `GATE_FILE_MB` (512; a bench binary is about 150), and the entry's build layer is measured after each build against `GATE_BUILD_DISK_MB` (1024); either rejects the entry. The gate deletes its own per-run logs and results after `GATE_KEEP_DAYS` (14) |
| build-time meta code a word list could miss (`simproc`, `run_tac`) or an option hidden behind a comment | policy bans the simproc family, `run_tac` and `register_`/`_elab`/`_syntax_` declarations; it reads every `set_option` with comments removed, caps the values (no unlimited `maxHeartbeats 0`) and refuses one it cannot read |
| work left running to slow the incumbent's turn | every command runs in its own pid namespace, so nothing it starts outlives it (`tests/test_gate_limits.py` checks this in the real sandbox) |
| a build overwriting trusted files | builds write into a throwaway overlay; any file there not stemmed `Submission`, `Verify`, `Main` or `bench` rejects; FloatLib and Mathlib sit read-only under `.lake/packages` |
| a promoted champion's file, a miner's, building into the shared tree | the shared build tree is refreshed from the shipped incumbent only; a champion's bench is built in its own throwaway overlay under the same file and disk checks, and only the binary is kept under `pinned/` |
| slowing the incumbent (`csimp`, `@[export]`) | the incumbent is a separate binary built from `kernels/add/incumbent` and pinned, rebuilt only when its sources change |
| tuning for known operands | a fresh seed per run; three timed classes (near-one, wide-gap, cancellation) plus raw bit patterns checksummed |

`kernels/exploits/*` has one file per threat. Thirteen are rejected at policy (among them `simproc`,
`runtac`, `heartbeats` and `hiddenoption`), `sorryax` names the escape hatch past the word list and
is rejected at the axiom audit, and the two that
matter most are also rejected by the later stages when policy is skipped: `exploits/forge` at
`kernel`, `exploits/tamper` at `statement`. `exploits/membomb` is proved and correct but allocates
gigabytes per call; the bench memory cap rejects it at `score`, and charges it a registration. `selftest.py` runs all of these.

## Timing

Incumbent and submission run as separate processes, alternating over 25 rounds, each round the
fastest of 3 in-process sweeps, pinned to one CPU with `taskset`. The incumbent against itself
reads 0.997 to 1.006x over four runs; with 9 rounds it swung to 0.956x, and unpinned it drifts by
about 3%. A 3% promotion bar needs the 25 rounds and the pinned CPU. Knobs: `BENCH_SEED`,
`BENCH_ROUNDS`, `BENCH_PAIRS`, `BENCH_CPU`, `GATE_SANDBOX`, `GATE_BUILD_MEMORY_MB`,
`GATE_BENCH_MEMORY_MB`, `GATE_TASKS_MAX`; `LOG_LEVEL` (INFO) sets the console, while `logs/`
keeps everything at DEBUG.

A one-hour study (`validator/tools/timing_study.py`, 2026-09-24) alternated 86 full gate runs, each on a
fresh seed:

| submission | runs | mean | stdev | min | max | at or below 0.97 |
|---|---:|---:|---:|---:|---:|---:|
| `incumbent` against itself | 43 | 1.0005 | 0.0043 | 0.9932 | 1.0135 | 0 |
| `fastlog` | 43 | 0.5340 | 0.0044 | 0.5244 | 0.5563 | 43 |

No run of the incumbent against itself came near the 3% bar (closest 0.993, about 5 standard
deviations away), and `fastlog` cleared it every time. Single classes vary more than the total
(incumbent self-ratio per class 0.98 to 1.02), which is why the score sums all three.

That study measured the `add` board, the one that paid then. The paid board is now the full
entry; `validator/tools/timing_study.py` runs on it by default (`STUDY_SLOT` picks another board), and it
must be re-run there, and on the validator's own machine, before the bar is final.

`lake build micro && lean/.lake/build/bin/micro` breaks one addition down on the same operands:

| operands | loop only | host FPU (unchecked) | certified word kernel | `ExecFloat.add` | textbook adder |
|---|---|---|---|---|---|
| random bit patterns | 5.2 ns | 17.0 ns | 27.2 ns | 27.2 ns | 18.9 ns |
| near one | 5.2 ns | 8.4 ns | 32.5 ns | 32.6 ns | 15.3 ns |

## Running a validator

The gate runs on the host (it needs bubblewrap and the Lean toolchain); Postgres runs in
Docker; the API, the gate worker and the two chain workers run under pm2.

```bash
./setup.sh --chain                          # everything above, plus the service and the bittensor SDK
$EDITOR .env                                # POSTGRES_PASSWORD, the validator wallet, NETUID, BENCH_CPU
docker compose up -d db                     # Postgres 17 on 127.0.0.1:POSTGRES_PORT (default 5433)
docker compose up migrate                   # alembic upgrade head
pm2 start deploy/pm2.config.js              # the four floatlib-* processes below
curl http://127.0.0.1:9300/health           # {"ok":true,"queued":0,"promotion_bar":0.97,...}
```

Each process runs from `validator/` (`cd validator && python -m service.api`); pm2 sets that
directory itself and names them `floatlib-*`, so they sit beside another competition's on one host.

| process | what it does |
|---|---|
| `floatlib-submission-api` (`python -m service.api`) | the platform's layout on `SERVICE_PORT` (9300), one competition slug per slot (`floatlib` is the full entry; `floatlib-add`, `-mul`, `-fma`, `-div`, `-sqrt`, `-sum`, `-bmm` the practice boards): `POST /v1/competitions/{slug}/submissions` (signed `X-Conjectures-*` headers, one `Kernel.lean` part), `GET …/submissions/{id}`, `GET …/submissions/{id}/report`, `GET …/leaderboard`, `GET …/champions`; plus `GET /health`, `GET /ready` (example responses in MANUAL section 12) |
| `floatlib-gate-worker` (`python -m service.worker`) | takes the gate's lock first (while an operator run or another worker holds it, it claims nothing and waits), then claims queued submissions oldest first, writes their `Kernel.lean` from the store, points the gate at the slot's current champion, runs `gate.py <dir> --verdict` and stores the verdict and its full JSON as the report, promoting an entry that beats the champion; exit 2 or no verdict is a validator error, requeued uncharged and behind fresh work; a submission that hits one `SERVICE_MAX_GATE_ERRORS` times (3) ends in `error`, uncharged, so nothing is retried forever, and sending the same file again requeues it; when the reference cannot build or benchmark, only its slot pauses and the same challenger retries without penalty (see below); at startup it requeues the claims its previous run left; it backs off up to 5 minutes while errors repeat |
| `floatlib-chain-watcher` (`python -m workers.chain_watcher`) | streams subnet registrations into the store; without it nobody can submit |
| `floatlib-weight-setter` (`python -m workers.weight_setter`) | every tick, scores the paying board (the full entry, whose champion's author takes the competition's budget: `validator/admission.py` and the rule in `validator/reward.py`) and publishes changed results, reusing unchanged snapshots; once an epoch, inside the margin before the boundary, it submits the vector unless in dry run |

**Weights.** The vector is the whole validator's, as in the DEFLATE competition
(`validator/treasury.py`): the treasury, uid 121 on netuid 66, takes 80%; the competition's 20% goes
to miners by score, and whatever the competition does not pay a registered miner goes to the
treasury. On netuid 66 both numbers are code constants, and an `.env` value that disagrees refuses
to start; off mainnet `WEIGHT_TREASURY_UID` and `WEIGHT_COMPETITION_SHARE` set them. A scoring
failure pays the treasury the whole epoch rather than skipping it. The epoch is timed from the
chain's own `LastMechansimStepBlock`.

**Dry run is the default** (`WEIGHT_DRY_RUN=1`): the setter records every vector and submits none.
Only one process may set weights per validator hotkey, and two would overwrite each other's vector
every epoch, so `WEIGHT_DRY_RUN=0` belongs on that one process only. **Pausing a round** is `WEIGHT_BURN_MODE=1`: the competition's share burns to
`WEIGHT_BURN_UID` (default 121, the treasury itself) and the treasury is paid as usual.

**Snapshot storage:** unchanged polls reuse the latest stored publication, even after a restart.
Changes to scoring, policy, the vector, dry-run mode or submission state publish a new snapshot.
Every actual chain submission gets its own history row. The original publication time and
chain outcome are preserved when an idle poll reuses it.

The weight setter checks for expired API payloads every 60 seconds, clearing up to 10 per
batch after one hour. It always keeps the latest valid snapshot per subnet, even when older.
Only `api_snapshot` is cleared; weight-set rows and score history remain. Configure this with
`API_SNAPSHOT_RETENTION_SECONDS`, `API_SNAPSHOT_CLEANUP_INTERVAL_SECONDS` and
`API_SNAPSHOT_CLEANUP_BATCH_SIZE`. Cleanup failures are logged and retried independently of
weight setting. Consumers must handle an expired snapshot by restarting pagination.
Apply migration 0013 before restarting the weight setter. No gate rebuild is needed.

### Unavailable champions

A champion or shipped baseline that cannot build or finish its benchmark pauses its slot.
The same challenger stays first in that slot, queued and uncharged. The worker retries normal
verification after `CHAMPION_RETRY_SECONDS` (default 300 seconds after failure). Later submissions
cannot overtake it. Other slots run during the cooldown and receive a turn between retries;
actual verification remains serial and bounded by the existing timeouts.

There is no automatic demotion for these failures. Successful champion benchmarking clears the
pause. A challenger rejected before testing the champion is handled normally; the slot stays
paused and advances to its next challenger. With no queued challengers, it waits for new work.
Pauses survive restarts; warnings occur on entry or a changed failure, with a recovery message
on resume. Repeated identical failures are debug-level in the log, but every failed attempt
sends a `slot_paused` Axiom event at error severity and recovery sends `slot_recovered`
(see Observability); alert on `slot_paused`.

`WEIGHT_PAUSED_SLOT_POLICY=preserve` keeps normal rewards (default). Set it to `treasury` to
redirect a paused paid slot's miner share to the treasury until recovery, without changing its
champion. Changes apply on the next scoring tick and permitted chain update, not retroactively.
Retry timestamps and error details do not create new scoring snapshots.

Run these commands as the worker user, from the repository, with its database environment loaded
(e.g. `set -a; . ./.env; set +a`):

```bash
.venv/bin/python validator/tools/slots.py status
.venv/bin/python validator/tools/slots.py retry --slot full
.venv/bin/python validator/tools/slots.py demote --slot full --submission-id 123 --reason "audit result"
```

Mutating commands refuse while the gate is busy; retry after it finishes. Demotion requires the
current champion's exact ID. Its predecessor (or shipped baseline) is used on the waiting
challenger's next attempt; the slot remains paused until the replacement succeeds. These tools
and the worker use the same host gate lock; do not run gate workers on multiple hosts sharing
one queue.

**Deploy:** stop the gate and weight workers, deploy, apply migration 0017 with `docker compose run --build --rm migrate`,
then restart both workers. No historical submissions are reverified automatically. Existing
promotion failure counts are retained for history, but no longer trigger demotion.

**Changing the verifier** does not retire accepted verdicts or stop scoring. Fingerprints
and incumbent hashes are informational provenance. The gate worker records observed hashes
and warns once per change per slot, with that state surviving restarts (migration 0012).
There is no automatic reverification. If an operator identifies affected results, the
following explicit command requeues accepted submissions from another verifier; each keeps
its registration. While deliberately requeued, a submission is temporarily off the board:

```bash
.venv/bin/python fingerprint.py                # the current fingerprint
.venv/bin/python validator/tools/reverify.py             # accepted submissions from another verifier
.venv/bin/python validator/tools/reverify.py --apply     # requeue them for the gate worker
```

**Promotion is automatic.** Every entry is timed against its slot's current champion. An accepted
entry at or below the promotion bar (0.97) whose bootstrapped gain over the champion is significant
becomes the new champion in the same transaction as its verdict (a `promotions` row), and the next
entry must beat it by 3%. The gate worker writes the champion's `Kernel.lean` to
`$SERVICE_FILES/champions/<slot>/Kernel.lean` and points the gate at it
(`GATE_INCUMBENT_<SLOT>`, plain `GATE_INCUMBENT` for `add`); the gate rebuilds that slot's pinned
binary under `pinned/<slot>/` on its next run. Until a submission is promoted, the slot's shipped
champion holds: the gate's default, or `GATE_INCUMBENT_<SLOT>` in `.env`. Every verdict records the
champion's source hash (`champion_sha256`), and the board counts only ratios against the current
one. A re-verified champion is timed against the champion before it, never against itself, and
stays champion. A gate, contract or bench hotfix keeps the champion's source, so it keeps the
board; the fingerprint and the incumbent binary's hash are provenance only.

### Serving through the conjectures platform

The add board is meant to be served by conjectures.io (`conjectures-validator`), the way DEFLATE
is: the platform queues submissions into this database and reads what the weight setter publishes,
while the gate, the chain watcher and the weight setter stay here. This repository's side is ready:

- the store keeps each `Kernel.lean` (`submission_files`), the gate's report, the platform account
  and the published snapshot (`weight_sets.api_snapshot`), from migration 0006;
- `deploy/platform/platform_api.sql` is the platform role's exact grants, `deploy/platform/grant.sh`
  creates the role (`PLATFORM_API_PASSWORD`) and resets them, and `deploy/platform/compose.yaml`
  attaches the database to the platform stack's network
  (`COMPOSE_FILE=compose.yaml:deploy/platform/compose.yaml`);
- miners sign the platform's message, and this API takes the platform's request.

The platform side is not: its competition adapter is written for DEFLATE's two files and
compression metrics, and it serves one competition per process.
[`deploy/platform/CONTRACT.md`](deploy/platform/CONTRACT.md) lists exactly what it must change, and
the emissions decision that comes with it. Until then miners submit to a FloatLib validator's API.

### Observability

With `AXIOM_TOKEN` and `AXIOM_DATASET` set, the four processes send structured events to the
platform's Axiom dataset in its envelope (`validator/axiom.py`, ported from DEFLATE), tagged
`competition: "floatlib"`: `service_started`/`stopped`/`misconfigured`, `submission_claimed`,
`gate_verdict`, `submission_requeued`, `gate_validator_error`, `slot_paused` (error, once per
failed reference attempt, so it repeats every `CHAMPION_RETRY_SECONDS` while a slot stays down;
`first` marks the pause's start), `slot_recovered` (info), `registrations_recorded`,
`chain_read_failed`, `weights_planned`/`set`/`skipped`/`failed`, and every ERROR log line as
`log_error`. Without them events are off. Sending never blocks or raises.

The service layer is ported from our DEFLATE competition (`conjectures-optimisation-deflate`):
registrations and entitlement claims, the Postgres rate limiter, signed timestamps, the queue with
stale-claim recovery, the weight-setting audit trail, the treasury split, the platform's wire
contract and the Axiom events. What is new is the single-file submission, one slug per slot, the
ratio-based leaderboard per slot filtered to its current incumbent, and the per-slot king-of-the-hill
scorer in `validator/reward.py`.

```bash
# The tests: a scratch database on the compose Postgres, a stub gate, one real-gate test.
set -a; . ./.env; set +a
.venv/bin/python -m pytest -q                  # 139 tests, about 2 minutes
.venv/bin/python -m pytest -q -m "not slow"    # 138 tests, seconds
```

Without Docker, `validator/tools/test-env.sh test` downloads a local Postgres into `~/.local/share` and runs
the same tests against it.

CI (`.github/workflows/ci.yml`) runs ruff and the fast tests against a Postgres service container
on every push and pull request, and nightly (or on demand) the whole `setup.sh`, the real-gate
test and the self-test. A hosted runner has no user systemd session, so that job lifts the gate's
memory caps; everything else runs as on a validator.

## The TorchLean slots: `sum` and `bmm`

The competition's goal. A miner submits a faster `sum` or batched matmul (`bmm`) over binary32 and a
Lean proof that it equals TorchLean's reference, bit for bit, accumulation order included. The gate
model is stage 1's (fixed statement, kernel replay, allowed axioms, timed against an incumbent);
MANUAL section 14 is the miner's guide.

**Rules without a TorchLean dependency.** `lean/contract/ContractTensor.lean`, the one module a
`sum` or `bmm` submission may import, restates TorchLean's `reduceSumLeftSpec` and `bmmSpec` (with
its index helpers and `getD`) over FloatLib, so the gate and validators need neither TorchLean nor a
C compiler. `lean/stage2/Stage2/Link.lean` proves each restated rule equal to TorchLean's by
definition, and that passing the gate's check is meeting TorchLean's own obligation;
`tests/test_torchlean_link.py` keeps that proof building. `Contract.lean` stays untouched: the
scalar slots must compile exactly as calibrated.

**Timing.** One sweep is 500,000 scalar steps, as for every slot: `sum` adds 500 vectors of 1,000
elements (`lean/bench/BenchTensor.lean`); `bmm` multiplies 2 batches of 50×100 by 100×50 matrices,
500,000 multiply-adds (`lean/bench/BenchBmm.lean`). Both are timed on dense, ReLU-like (half
zeros) and wide-range data.

**Ladder.** The incumbents fold FloatLib's own `add`/`mul` (1.00x). `sum/tuned` (0.52x) and
`bmm/fast` (0.57x) are stage 1's winners inside TorchLean's loops, proved by composition alone.
The `-wrong` fixtures compute the same values in another order and cannot be proved.

The sections below are the research behind these slots (`lean/stage2/`, built with TorchLean).

### The contract

`lean/stage2/Stage2/Contract.lean` states two obligations against
[TorchLean](https://github.com/lean-dojo/TorchLean)'s `NN/Runtime/Autograd/Engine/Cuda/KernelSpec.lean`:

| Slot | Kernel type | Must equal |
|---|---|---|
| sum | `Vector Binary32 n → Binary32` | `reduceSumLeftSpec`: a left fold of `ExecFloat.add` from +0 |
| bmm | `(batch m n p) → Vector Binary32 (batch*m*n) → Vector Binary32 (batch*n*p) → Vector Binary32 (batch*m*p)` | `bmmSpec`: row-major, each output a left fold of `add acc (mul a b)` from +0 |

Equality is exact, so the accumulation order is part of the contract. TorchLean's scalar is
FloatLib's `ExecFloat.Binary 8 23`, the same type as stage 1's `Binary32`.

### What is proved

`Stage2/Kernels.lean` implements both slots in the specs' order with stage 1's best proven scalar
kernels (`Scalar/TunedAdd.lean` from `kernels/add/tuned`, `Scalar/FastMul.lean` from
`kernels/mul/fast`, namespaces renamed; `tests/test_stage2_copies.py` fails if they drift).
`Stage2/Proofs.lean` proves `SumObligation sumFast` and `BmmObligation bmmFast` in about 40 lines,
by composition of the stage-1 theorems with list lemmas, and checks that they rest only on
`propext`, `Classical.choice` and `Quot.sound`.

### What was measured

One pinned core, best of 5–7 runs; ratios against the proved stage-1 composition unless noted.
Every variant produces the reference checksum, so all are bit-identical to the spec.

| Kernel | Ratio |
|---|---|
| sum, proved composition, vs `ExecFloat` reference | 0.53 |
| bmm 64³ and 128³, proved composition, vs reference | 0.57 |
| raw `UInt32` word loops (no `Binary32` wrapper) | 0.92–0.96 |
| fused dot, product passed to the add as fields | 0.87–0.91 |
| fused sum, accumulator kept unpacked | 0.99–1.15 |
| matmul, four output chains interleaved | 1.06–1.07 |
| **matmul 64³, half of A zero, skipping `acc + (±0 · finite)`** | **0.48** |

Findings:

- Most of a tensor kernel's speed comes from its scalar ops, so the stage-2 incumbent has to be
  built from the current stage-1 winners; otherwise the slot pays for copying.
- On dense data, loop structure and fusion are worth 5–10% at most: the scalar op is
  throughput-bound, not latency-bound.
- The real stage-2 headroom is data-aware exact shortcuts on realistic inputs. Post-ReLU
  activations are about half zeros; stage 1 never times zeros. So the `sum` and `bmm` benches time
  a dense class and a ReLU-like class.

The zero-skip kernel (`lean/stage2/bench/Fused.lean`, `mmSkip`) is not proved. It needs two
FloatLib facts: a signed zero times a finite value is a signed zero, and a finite nonzero plus a
signed zero is unchanged.

### Building

```bash
cd lean
lake build Stage2 stage2bench fused       # the proofs and both benches
taskset -c 3 .lake/build/bin/stage2bench  # reference vs proved composition
taskset -c 3 .lake/build/bin/fused        # unproved headroom probes
```

This needs a C compiler on `PATH` as `cc` (`build-essential` or clang): TorchLean compiles a small
C library that Lean loads even for plain imports. The gate's own targets do not.

### Other directions measured

Spikes run outside this repo (2026-09-28/29), kept here as findings; their code was throwaway.

- **CROWN certificates as a slot.** TorchLean's exact-rational `crown_query_v1` checker
  (`acceptsText_sound`) costs about 0.9 µs per rational multiply-add compiled. Checking one
  certificate for a 784-100-100-10 MLP took 4–5 minutes (784-50-10: about 2 minutes; 50-50-50-50-10:
  about 2 s); a kernel-checked `decide` only works on toy nets (6-16-16-4 needed 21 GB). The only
  free knob for a miner is the per-neuron alpha. Too slow to verify per submission at a useful size.
- **End-to-end TorchLean training.** An 8-32-32-1 MLP, 40 steps in IEEE mode, with `tuned`'s
  rounding swapped into FloatLib's `roundProduct` via `@[implemented_by]`: bit-identical output,
  same time (9.8 s vs 9.9 s); hardware `Float32` takes 7.9 s. Framework overhead is about 80% of
  the runtime, so scalar kernels can give at most about 1.26x there. Arithmetic-bound loops like
  this stage's are where the gains show.
- **zkVMs (RISC Zero, RV32IM).** Rust ports of `tuned` and `mul/fast` ran 1.7–3.1x *slower* in
  cycles than Rust's soft-float (`compiler-builtins`: add 76–102, mul 60–70 cycles), because they
  lean on 64-bit operations; a hand-written 32-bit kernel managed 0.86–1.09x. The value there is a
  proved, deterministic soft-float, not speed.
- **Reference cost.** One `ExecFloat` multiply plus add, as `bmmSpec` does it, takes about 80 ns.

## Layout

```
README.md, MANUAL.md     this overview (operators, reviewers) and the miner manual
setup.sh                 one-command setup (--service, --chain for validators; --check reports only)
gate.py                  the gate: stages, sandbox, lock, logs, JSON verdict; --bench for unproved kernels
selftest.py              the environment test: every kernel in kernels/ against its expected verdict
fingerprint.py           the verifier fingerprint stamped on every verdict

lean/                    the Lean workspace; FloatLib pinned at 0d91825, nothing in it edited
  contract/              Contract.lean: Binary32 and the scalar slots' obligations; ContractTensor.lean:
                         the TorchLean slots' rules (sumSpec, bmmSpec), restated over FloatLib
  verify/                one statement check and axiom report per slot: VerifyFull (the full entry),
                         Verify (add), VerifyMul, VerifyFma, VerifyDiv, VerifySqrt, VerifySum, VerifyBmm
  bench/                 the shared timing core, one bench binary per slot, BenchDivSqrt and
                         BenchTensor, BenchBmm (the newer slots' classes, each in its own module),
                         Micro (the cost breakdown)
  stage2/                TorchLean: the link proving the sum and bmm rules are TorchLean's, and the
                         research kernels and benchmarks behind them
                         (needs a C compiler; the gate never builds it)
kernels/                 every kernel the gate is tested on, and expected.tsv (their expected verdicts)
  full/                  the full entry: incumbent (the best proven add and mul in all four places), FloatLib's baseline, a wrong one
  add/ mul/ fma/ div/    per practice board: the incumbent, the ladder of proven faster kernels,
  sqrt/ sum/ bmm/        wrong proofs
  exploits/              one attack per file, each rejected

miner/                   the miner client (submit.py: submit, status, report, leaderboard) and its signer (sig.py)
validator/               the validator service, run from this directory by pm2
  service/               the submission API (the platform's layout) and the gate worker
  db/                    the Postgres store: submissions and their files, registrations, entitlements,
                         rate limits, weight sets
  chain/, workers/       the chain watcher and the weight setter (bittensor SDK)
  admission.py           the statistical admission that picks who leads
  reward.py              the reward rule (only the full entry pays, king of the hill), and the published snapshot
  treasury.py            the validator's vector: the treasury's share and the competition's
  axiom.py, log.py       events to the platform's Axiom dataset; console logging
  tools/                 evidence.py (the evidence pack), reverify.py, timing_study.py, test-env.sh
deploy/                  Postgres config, the Alembic migrations, pm2.config.js
  platform/              serving through conjectures.io: the platform role's grants, grant.sh, the
                         network overlay, and CONTRACT.md (what the platform must change)
compose.yaml, .env.example   Postgres under Docker, and the validator's settings
requirements/            pinned Python dependencies: gate.txt, miner.txt, service.txt, chain.txt

tests/                   the service, security, schema, chain, weight-setter, treasury, events, platform-grant,
                         signer and gate-intake tests, and a check that lean/stage2/Scalar still
                         matches the kernels it copies
scripts/                 fetch-pinned-deps.py (setup.sh's checkout of the pinned Lean dependencies)
evidence/                EVIDENCE.md and evidence.json, generated by validator/tools/evidence.py
.github/workflows/ci.yml lint and fast tests on every push; full setup, real-gate test and self-test nightly
```

Generated at run time and git-ignored: `lean/.lake/` (dependencies and build), `lean/work/` (the
kernel under test), `pinned/` (each slot's incumbent binary), `logs/`, `results/`, `.venv/`.

## Status

- Working and tested live end to end: the gate (six stages, sandbox, time budget, memory and disk caps), the
  timing, the 48-case self-test, the manual, and the validator service (API, gate worker, store, chain workers).
- One entry, one board: the full entry (`add`, `mul`, `sum`, `bmm`) is the paid board, against the best proven
  entry as its shipped champion; every kernel also has an unpaid practice board (see
  [The TorchLean slots](#the-torchlean-slots-sum-and-bmm) and MANUAL section 15).
- Rewards: the treasury takes 80% of the validator's weight and the full entry's champion's author 20%; an entry
  that beats the champion by 3%, with statistical admission, becomes the champion automatically (MANUAL section 12).
- The database, grants, wire contract, treasury split and events match DEFLATE's production setup
  ([`deploy/platform/CONTRACT.md`](deploy/platform/CONTRACT.md)); the weight setter runs in dry run by default.
