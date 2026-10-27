# Operations

See the [manual](../MANUAL.md) for fresh database provisioning, coordinator configuration,
worker startup, dry-run weights and operator disqualification.

The supported entry points are coordinator, benchmark worker, chain watcher and weight setter.
The former monolithic gate worker and practice-slot recovery tools have been retired.

## Events

With `AXIOM_TOKEN` and `AXIOM_DATASET` set, each entry point sends structured events to the
platform's Axiom dataset (`validator/axiom.py`), every one tagged `competition: "torchlean"`.
Without them nothing is sent. ERROR logs and uncaught exceptions arrive as `log_error`.

| source | events |
|---|---|
| `competition-coordinator` | `proof_checked` (proved, rejected, or retry when the infrastructure failed), `evaluation_planned`, `evaluation_decided`, `evaluation_quarantined` (unreadable results; never the miner's fault), `champion_promoted`, `rewards_published`, `board_paused`/`board_resumed`, `benchmark_stalled`; from the operator tools, `promotion_disqualified` and `source_disqualification_set` |
| `competition-benchmark-worker` | `benchmark_job_finished` per job, tagged with the worker's environment; a host needs the token to report |
| `competition-chain-watcher` | `registrations_recorded`, `chain_read_failed` |
| `competition-weight-setter` | `weights_planned`/`weights_set`/`weights_skipped`/`weights_failed` |

Each has `service_started`, `service_stopped` (with `reason`) and `service_misconfigured`.
Warnings mark the miner's failures (a rejected proof, a crashing candidate); errors mark ours.
The champion auditor sends nothing: its source file is part of its audit identity.
