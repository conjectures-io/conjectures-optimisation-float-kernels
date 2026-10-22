# FloatLib kernel competition

Optimise four binary32 kernels and prove that their results are exactly equal to
FloatLib and TorchLean's specifications. Miners upload one Lean file containing
`add`, `mul`, `sum`, and batched matrix multiplication (`bmm`), together with their
proofs.

The central [Conjectures API](https://github.com/conjectures-io/conjectures-validator)
receives submissions. This repository provides the contracts, local verification
and benchmarking tools, miner CLI, and competition workers.

## How the competition works

1. The gate checks the file's policy, all four proof statements, compiled Lean
   declarations, and allowed axioms.
2. It benchmarks the submission alongside the current champion and compares output
   checksums on generated inputs.
3. A verified submission becomes champion if its timing ratio is at most the
   promotion bar (default `0.97`) and the paired statistical test establishes a
   speed advantage.
4. The latest three champions share the competition budget `60/30/10`, excluding
   any explicitly removed by an operator.
   Empty ranks go to treasury. On subnet 66, the competition budget is 20% of the
   validator's weight.

An entry can pass verification without becoming champion. Its proofs must cover
all inputs required by the contract. Its timing results apply to the benchmark
inputs and the validator's hardware.

See the [live competition](https://conjectures.io/competitions/floatlib) for current
champions and measurements. Repository examples illustrate implementation and
proof techniques; they are not a live leaderboard.

## Miner quick start

On Linux x86_64, clone this repository and run from its root:

```bash
./setup.sh
source .venv/bin/activate
export PATH="$HOME/.elan/bin:$PATH"

cp -r kernels/full/incumbent my-entry
python gate.py --slot full --bench my-entry
python gate.py --slot full my-entry
```

Setup installs the Lean dependencies, project-local bubblewrap, and Python
packages for the gate and miner CLI. See the [miner guide](MANUAL.md) for system
packages and sandbox requirements.

The copied entry is the **shipped reference**. Local runs use it by default;
server verification uses the current champion, which may have changed since the
repository was released.

Edit `my-entry/Kernel.lean`, benchmark your changes, complete all proofs, then
submit using an eligible registered hotkey:

```bash
python miner/submit.py submit my-entry \
  --hotkey ~/.bittensor/wallets/<wallet>/hotkeys/<hotkey>
python miner/submit.py status <submission-id>
python miner/submit.py report <submission-id>
```

The CLI defaults to `https://conjectures.io`; `--url` selects another API origin.
Only entries containing all four kernels can be uploaded. Single-kernel slots remain available for local
experiments and historical reads.

## Native macOS development

For the exact-runtime kernels, proofs, audits and model benchmark:

```bash
./setup-macos.sh --check
./setup-macos.sh --dev
```

Install Xcode Command Line Tools (`xcode-select --install`) and Python 3.11+ first.
For example, `brew install python@3.12`; `PYTHON` selects a particular interpreter.
If an existing `.venv` uses Apple's Python 3.9, preserve it with
`mv .venv .venv-python39-backup` before rerunning setup.

The default setup builds the native Lean tools and installs their basic Python
dependencies; `--dev` adds test and plotting dependencies. Existing
`lean/work/Submission.lean` is preserved. To select another package explicitly:

```bash
./setup-macos.sh --source kernels/exact/cached-conv/Kernel.lean
lean/.lake/build/bin/exactAudit 8 971
BENCH_INNER=1 BENCH_SEED=42 lean/.lake/build/bin/benchExact
```

The sandboxed gate and workers require Linux. The research comparison runner
also runs natively on macOS, without CPU affinity; it records that limitation
in the experiment manifest. Keep each machine's results in a separate directory.

## Documentation

| Guide | Contents |
| --- | --- |
| [Miner guide](MANUAL.md) | Setup, full-entry workflow, eligibility, submission and results |
| [Proofs and policy](docs/PROOFS.md) | Exact obligations, proof patterns, permitted code and gate stages |
| [Benchmarking](docs/BENCHMARKING.md) | Workloads, timing aggregation and statistical promotion |
| [Operations](docs/OPERATIONS.md) | Database, workers, API access, reward switching and recovery |
| [Local experiments](docs/LOCAL-EXPERIMENTS.md) | Single-kernel slots and example implementations |
| [Exact runtime slot](docs/EXACT-RUNTIME.md) | Twelve proved neural-network kernels and their local model-suite benchmark |
| [Exact-runtime workers](docs/EXACT-WORKERS.md) | Optional model-family boards, distributed benchmark jobs and configurable two-rank rewards; API integration pending |
| [Research background](docs/RESEARCH.md) | TorchLean link proofs and exploratory targets |
| [Platform contract](deploy/platform/CONTRACT.md) | Database integration and access grants |
| [Central API reference](https://github.com/conjectures-io/conjectures-validator/blob/main/docs/COMPETITION_API.md) | HTTP endpoints, schemas and refusal codes |

## Main source files

- [Scalar contract](lean/contract/Contract.lean) and
  [tensor contract](lean/contract/ContractTensor.lean): required semantics.
- [Full reference entry](kernels/full/incumbent/Kernel.lean): a starting implementation
  with all four proofs.
- [gate.py](gate.py): verification, sandboxing and benchmark orchestration.
- [admission.py](validator/admission.py) and [reward.py](validator/reward.py):
  promotion and reward rules.
- [BenchFull.lean](lean/bench/BenchFull.lean): full-entry benchmark.

## Development checks

```bash
.venv/bin/pip install -r requirements/test.txt ruff==0.16.9
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q -m "not slow"
```

Database tests require a separate test database; see [Operations](docs/OPERATIONS.md#development-tests).
For a real toolchain and sandbox check, run `.venv/bin/python selftest.py`. It
verifies the fixtures listed in `kernels/expected.tsv`, including rejection cases.
Timing assertions can fail on a busy host.

CI runs lint and fast tests on pushes and pull requests. Scheduled and manually
triggered gate jobs also run setup, real-gate tests, and the self-test.
