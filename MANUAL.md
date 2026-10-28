# Miner manual

Submit a complete kernel implementation to **https://conjectures.io**. This guide uses the
production TorchLean API; no local database or competition services are needed to submit.

## 1. Check eligibility

You need the private hotkey file for an active registration on the competition's subnet.
The latest registration must be strictly newer than the published cutoff and must not already
have a submission. An older unused registration does not qualify instead. Check the live policy:

```bash
curl -fsS https://conjectures.io/v1/competitions/torchlean
curl -fsS https://conjectures.io/v1/competitions/torchlean/boards
```

A registration allows **one submission**, regardless of whether its proof passes or fails.
If processing is interrupted, the system retries that submission; uploading another file does
not replace it. A later eligible registration allows another submission. An identical file from
the same hotkey is rejected as a duplicate even after re-registration, with the existing ID.

## 2. Install the client

Use Python 3.11 or newer. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/miner.txt
```

This installs signing and HTTP dependencies. You do not need the Bittensor SDK just to upload.
Use your Bittensor hotkey JSON file; the client signs locally and sends the signature, not the key.

## 3. Prepare your entry

```bash
cp -r kernels/exact/incumbent my-entry
```

Edit `my-entry/Kernel.lean`. It must be a nonempty UTF-8 file of at most **256,000 bytes**,
import only `ContractExact`, and define:

```lean
namespace Submission
open ExactRuntime

def kernels : Kernels := -- your complete set of 12 kernels
  …

theorem correct : Obligation kernels := by
  …

end Submission
```

The skeleton above is illustrative; the copied incumbent is a complete, proved starting point.
You may change any kernels, but must prove equality with the entire pinned specification for
all inputs. Benchmark checksums alone are insufficient. See [the kernel guide](docs/RUNTIME.md)
for the record's fields and [proof policy](docs/PROOFS.md) for allowed source constructs.

Only `Kernel.lean` is uploaded. Local notes or other files in `my-entry` are ignored.

## 4. Verify locally

On Linux, install the tools reported missing by `./setup.sh --check`, then run:

```bash
./setup.sh
export PYTHONPATH=.:validator
.venv/bin/python -m verification my-entry --verdict results/my-entry-proof.json
```

Setup installs the pinned Lean toolchain/packages, project-local sandbox and Python dependencies.
It builds the shipped package first. The first setup may take substantially longer than later runs.
Verification checks the same proof stages as the competition and writes a JSON verdict. Exit code
0 means passed, 1 means rejected, and 2 means an infrastructure problem. It does not submit work.
Linux verification needs unprivileged user namespaces and a working user systemd session for
resource limits; see [local setup troubleshooting](docs/LOCAL-EXPERIMENTS.md).

On macOS, use the native development setup:

```bash
xcode-select --install  # if Command Line Tools are not already installed
./setup-macos.sh --source my-entry/Kernel.lean
```

Use Python 3.11+ (`PYTHON=/path/to/python3.12` selects an interpreter). This builds the Lean
statement and native tools. For the full sandboxed gate and kernel replay, use Linux.

## 5. Benchmark locally

After setup, build your entry and run the model suite:

```bash
cp my-entry/Kernel.lean lean/work/Submission.lean
(cd lean && ~/.elan/bin/lake build VerifyExact benchExact)
BENCH_INNER=3 BENCH_WORKLOADS=s0:42:typical lean/.lake/build/bin/benchExact
```

Each output row contains a workload/cell name, success flag, elapsed nanoseconds and checksum.
This is a quick local measurement, not an official score: it has no reference calibration or
paired statistical decision. To inspect layer costs:

```bash
(cd lean && ~/.elan/bin/lake build exactDev)
lean/.lake/build/bin/exactDev profile cnn
```

Other profile names are `mlp`, `mnet`, `nmlp` and `mixer`. See
[benchmarking](docs/BENCHMARKING.md) and [local experiments](docs/LOCAL-EXPERIMENTS.md)
for interpretation and repeatable comparisons. Avoid concurrent builds or other CPU-heavy work.

## 6. Submit

```bash
.venv/bin/python miner/submit.py submit my-entry \
  --hotkey ~/.bittensor/wallets/my-wallet/hotkeys/my-hotkey
```

Replace the example wallet/key names with yours. The client hashes the file, signs the TorchLean
upload message and prints the submission ID. Keep your system clock synchronized; signatures
have a limited validity window. `--url` overrides the API origin when using your own instance.

A successful upload means **queued**, not verified or promoted. Keep your submitted source and ID.

## 7. Follow the result

```bash
.venv/bin/python miner/submit.py status 123
.venv/bin/python miner/submit.py report 123
.venv/bin/python miner/submit.py leaderboard --board mobilenet
```

Boards are `overall`, `mlp`, `resnet`, `mobilenet`, `normalized_mlp` and `mixer`.
Processing states include `queued`, `proving`, `waiting`, `benchmarking`, `completed`, `rejected`
and `disqualified`. Completion means decisions are available; it does not mean a takeover.
Each family can finish at a different time. Reports contain per-device measurements and decisions.

A ratio of `0.95` means 5% less runtime than that board's reference. Promotion also requires the
published confidence and regression checks; see [scoring](docs/BENCHMARKING.md). Reward shares
are configurable: use the live board/champion responses rather than assuming a fixed payout split.

## Common errors

Errors use `{type, title, status, detail, reason_code}`. The CLI prints the reason and message.

| Reason | Action |
|---|---|
| `NOT_REGISTERED` | Check the hotkey and active subnet registration. |
| `REGISTRATION_BEFORE_CUTOFF` | The latest registration is too old for this competition. |
| `NO_ENTITLEMENT` | That registration already has a submission; inspect its existing result. |
| `DUPLICATE_SUBMISSION` | Use the existing submission ID returned with the error. |
| `SIGNATURE_EXPIRED` | Synchronize your clock and retry the request. |
| `SUBMISSIONS_CLOSED` | Intake is closed; check the competition's published state. |
| `RATE_LIMITED` | Wait before retrying. |
| `COMPETITION_SCHEMA_UNAVAILABLE` | The service is unavailable; retry later. |

Public sources are available only for former champions whose code no longer holds any board.
Queued, rejected, never-champion and currently winning sources are withheld.
For integration details and history queries, see [the public API](docs/API.md).
