# Miner guide

Submit one `Kernel.lean` with four exact binary32 kernels and their proofs.
Only the full entry participates in the paid competition.

## 1. Set up

Run from the repository root on Linux x86_64. Setup needs network access for
pinned dependencies and build caches; gate sandboxes have no network access.
On Ubuntu, install the base packages and bubblewrap build dependencies:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv curl git util-linux ca-certificates \
  build-essential meson ninja-build pkg-config libcap-dev

./setup.sh
source .venv/bin/activate
export PATH="$HOME/.elan/bin:$PATH"
```

Setup installs Lean through the user's `~/.elan` and dependencies under
`lean/.lake`. Python packages and pinned bubblewrap are installed under `.venv`.
It can be rerun after an interruption. Allow disk space for Lean, Mathlib,
dependency builds, and the gate's temporary overlays.

```bash
./setup.sh --check
python selftest.py
```

`--check` reports whether required tools are installed; the self-test exercises the real gate
against the fixtures in `kernels/expected.tsv`. Close competing CPU-heavy work
before assessing timing failures.

The gate uses `.venv/bin/bwrap` directly. Resource caps require a working user
systemd session. For a worker account outside an interactive session, an operator
can enable lingering and configure its user bus; see
[Operations](docs/OPERATIONS.md#host-and-worker-account).

## 2. Start a full entry

```bash
cp -r kernels/full/incumbent my-entry
```

The upload is the UTF-8 file `my-entry/Kernel.lean` (at most 256,000 bytes), with
`import ContractTensor` and these definitions in `namespace Submission`:

| Definition | Required theorem |
| --- | --- |
| `add` | `add_spec` |
| `mul` | `mul_spec` |
| `sum` | `sum_spec` |
| `bmm` | `bmm_spec` |

Use the reference file for exact types and the
[proof guide](docs/PROOFS.md) for obligations and proof patterns. You may improve
one kernel and keep the others. Faster scalar arithmetic can also improve tensor
kernels that call it.

Equality covers special values and signed zeros. Tensor proofs cover all vector
lengths and matrix shapes, not just the benchmark sizes. Changing the accumulation
order generally changes floating-point results.

## 3. Benchmark, then verify

```bash
python gate.py --slot full --bench my-entry
python gate.py --slot full my-entry
```

`--bench` times an implementation before its proofs are complete. It does not
verify correctness or qualify an entry for promotion. Output checksum checks still run. Complete the
proofs and use the full gate before uploading.

A local run compares against the shipped reference by default. The server uses
its current champion; a local improvement need not beat that champion. Passing
the local gate does not guarantee promotion or rewards on the server.

For controlled comparisons between local revisions:

```bash
BENCH_SEED=42 python gate.py --slot full --bench my-entry
```

Inspect all class timings, not only the headline ratio. See
[Benchmarking](docs/BENCHMARKING.md) for aggregation and statistical admission.

The paid board is scored on a private suite of inputs: ML-like magnitudes,
zeros, subnormals, NaN and infinities, paired operands and many vector lengths and
matrix shapes. Tune on the public suite with the same groups:

```bash
GATE_FULL_SUITE=suites/public-v2.json BENCH_SEED=42 python gate.py --slot full --bench my-entry
```

A fast path that slows any group by more than 5% against the champion is not
promoted, however fast the rest is. See
[Suite scoring](docs/BENCHMARKING.md#suite-scoring).
Logs are under `logs/`; verdicts and paired timings are under `results/`.

## 4. Submit

You need a hotkey registered on the competition's subnet and its signing key.
The coldkey is used for chain registration; the CLI signs uploads with the
hotkey file:

```bash
python miner/submit.py submit my-entry \
  --hotkey ~/.bittensor/wallets/<wallet>/hotkeys/<hotkey>
```

The CLI defaults to `https://conjectures.io` and prints the submission ID. To use
an alternative API, pass `--url` on each command:

```bash
python miner/submit.py submit my-entry --hotkey <hotkey-file> \
  --url https://devs-conjectures.rebelhub.eu
```

Eligibility rules:

- You can upload once per registration. Any stored entry since your latest
  registration uses that allowance, including queued, rejected, and error entries.
- Normally your latest registration must be after the configured cutoff date.
- If the operator enables the DEFLATE check, the API instead checks whether you
  submitted to DEFLATE since that registration. If so, the upload is refused.
  If the DEFLATE connection fails, the cutoff date applies.
- Identical files from the same hotkey return `409 DUPLICATE_SUBMISSION`, including
  after re-registration. The error identifies the existing entry.
- Refusals before insertion do not create an entry. Once stored, an interrupted
  verification retries the same entry; it does not free the upload allowance.
- Only `floatlib` full entries accept uploads. Practice slots are read-only.

Uploads are signed and rate limited. The CLI supplies the timestamp and signature;
keep your machine's clock accurate.

## 5. Inspect your entry

```bash
python miner/submit.py status <submission-id>
python miner/submit.py report <submission-id>
python miner/submit.py leaderboard
```

| State | Meaning |
| --- | --- |
| `queued` | Waiting for verification, including retries after reference failures |
| `verifying` | Claimed by the gate worker |
| `accepted` | Passed the gate; may or may not have become champion |
| `rejected` | Failed verification or an enforced resource limit; inspect the reason |
| `error` | Repeated validator failures; ask an operator to recover the existing entry |

If the champion cannot build or finish its benchmark, the worker keeps your entry
queued and retries periodically. An operator can investigate and repair the problem.

The central API returns refusals as `application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "…",
  "status": 402,
  "detail": "…",
  "reason_code": "NO_ENTITLEMENT"
}
```

Use `reason_code` for programmatic handling and `detail` for the explanation.
The [central API reference](https://github.com/conjectures-io/conjectures-validator/blob/main/docs/COMPETITION_API.md)
is the authoritative HTTP contract.

## 6. Promotion and rewards

A submission becomes champion only if all proofs pass, its timing ratio is at
most the promotion bar (default `0.97`, meaning at least 3% faster), and a
statistical test of the paired benchmark rounds confirms a speed advantage.
The test reduces the chance that timing noise causes a promotion; it does not eliminate measurement uncertainty.

The latest three champions receive 60%, 30%, and 10% of the competition
budget, excluding any explicitly removed by an operator. Empty ranks and unpaid
shares go to treasury. One hotkey holding several ranks receives their combined
shares. Superseding a champion moves it down the
reward ranks. An operator can explicitly demote a champion, removing it from
those ranks.

The default leaderboard shows challengers against the current champion. The API
also supports `/v1/competitions/floatlib/leaderboard?champion_id=<id>` for a past
champion. `/v1/competitions/floatlib/champions` lists champion IDs and reward
ranks. The shipped baseline currently has no selectable submission ID.

See the [live competition](https://conjectures.io/competitions/floatlib) for current
champions and measurements.

## 7. Licence

This repository is under the [MIT licence](LICENSE). The shipped kernels and the contracts also
reproduce or restate code from FloatLib and TorchLean, both MIT-licensed, so their notices apply as
well: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) has them, and [REUSE.toml](REUSE.toml) says
which files each covers. If you start from a shipped kernel, as `cp -r kernels/full/incumbent`
does, your entry contains that code, and those notices go with it.

When you submit a `Kernel.lean`, you license it under the MIT licence, so that the competition can
store, build, benchmark and publish it, and a promoted entry can become the reference later entries
are timed against. Submit only code you have the right to license this way.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Import or banned-name rejection | Check the [policy](docs/PROOFS.md#source-policy), including comments |
| Statement fails to typecheck | Read the Lean errors and compare theorem types with the full reference |
| Extra axioms | Replace unsupported proof techniques; inspect the [axiom rules](docs/PROOFS.md#gate-stages) |
| Checksum mismatch | Reproduce with the recorded seed and inspect the relevant class |
| Bubblewrap failure | Rerun setup and check host namespace permissions |
| Systemd resource-limit failure | Check the worker account's user session and bus |
| Timing changes across runs | Use an idle pinned CPU and repeat comparisons |

For proof techniques see [Proofs](docs/PROOFS.md). For single-kernel development
see [Local experiments](docs/LOCAL-EXPERIMENTS.md).
