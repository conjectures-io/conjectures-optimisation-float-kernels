# TorchLean kernel competition

Make CPU inference faster while proving that every output matches the pinned specification,
bit for bit. Submit one Lean file containing all **12 kernels** and a correctness proof.
You may improve any subset of the kernels; each submission is evaluated as a complete package.

Five model-family boards—MLP, ResNet, MobileNet, normalized MLP and Mixer—and an overall
board each have their own champion. The same entry can win several boards. Rewards go to
the current and previous eligible champion on each board, with allocations published by the API.

## Enter the competition

Python 3.11+ is enough to install the upload client; Lean is needed to develop and check kernels.
From this checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/miner.txt
cp -r kernels/exact/incumbent my-entry
# Edit my-entry/Kernel.lean and prove Submission.correct.
.venv/bin/python miner/submit.py submit my-entry \
  --hotkey ~/.bittensor/wallets/my-wallet/hotkeys/my-hotkey
.venv/bin/python miner/submit.py status 123
```

Replace `123` with the ID printed after upload. The client uses **https://conjectures.io** and
`/v1/competitions/torchlean`. Production uploads require an eligible, unused subnet registration
and a valid hotkey signature. Check the competition's open state and registration cutoff before
submitting. Each registration permits one submission; rejection still uses that registration.

Read the **[miner manual](MANUAL.md)** for eligibility, setup, verification, local benchmarks,
submission tracking and errors. Start from the [shipped kernel package](kernels/exact/incumbent/Kernel.lean).

## How entries are judged

1. The gate checks the source policy, the complete correctness statement, independent Lean kernel
   replay and permitted axioms.
2. Candidate and champion run the same generated model workloads on each required CPU environment.
3. Each board compares paired runtime ratios. Promotion requires the configured improvement,
   statistical confidence and regression checks. A score below 1 means faster.

The specification fixes rounding and exceptional values. Exact dot products round once; pooling,
affine operations and normalization have their own pinned rounding boundaries. This is CPU
**inference**, not training or backpropagation.

## Documentation

- [Manual](MANUAL.md): compete using the production API.
- [Benchmarking and scoring](docs/BENCHMARKING.md): workloads, ratios and promotion.
- [Kernel guide](docs/RUNTIME.md) and [contract](docs/CONTRACT.md): what to implement and prove.
- [Public API](docs/API.md): board history, reports and source downloads.
- [Operations](docs/OPERATIONS.md): run your own competition instance.
- [Axiom monitoring](docs/AXIOM-MONITORING.md): worker events, heartbeat alerts and webhook notifications.
- [Documentation index](docs/README.md): proofs, audits, workers and development tools.
