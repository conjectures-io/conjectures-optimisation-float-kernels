# TorchLean Axiom monitoring

Workers use the central validator's event envelope: `_time`, `severity`, `source`, `event_type`,
`environ`, plus `competition: "torchlean"`, `host`, and `pid`. The existing bounded background
transport batches gzipped NDJSON and never raises on an ingestion failure. No credentials means
no ingestion or heartbeat thread. ERROR/CRITICAL console logs also reach Axiom as `log_error`.

## Configure workers

Set on the coordinator, benchmark hosts and the independent auditor:

```dotenv
AXIOM_TOKEN=xaat-INGEST_ONLY_TOKEN
AXIOM_DATASET=conjectures-validator
AXIOM_ENVIRON=dev
AXIOM_URL=https://api.axiom.co
```

Choose the API URL for your Axiom region. Use a dataset-scoped **ingest-only** token. Ansible
reads `vault_axiom_token` from the environment's encrypted Vault and renders separate mode-0600
`coordinator-axiom.env`, `benchmark-axiom.env` and `auditor-axiom.env` files in the application's
private home configuration directory. Nonsecret inventory variables are `axiom_dataset`,
`axiom_environ` and `axiom_url`. No token defaults to disabled. Tokens stay outside sandboxed Lean
processes. This branch supplies code/configuration only; no live monitors or hosts were changed.

The sources are `torchlean-coordinator`, `torchlean-benchmark-worker` and
`torchlean-audit-worker`. Each emits start/stop, a heartbeat every 60 seconds (including while
busy), and `service_failed` on abnormal startup or exit. Benchmark events include `job_id`;
audit events include `audit_id`, `source_hash`, `promotion_id`, board, attempt and seed.
The board/promotion identifies discovery; one audited source can hold several boards.

## Audit signals

| Event | Severity | Meaning |
| --- | --- | --- |
| `audit_started` / `audit_clean` | info | Attempt began / matching results committed |
| `audit_failed` | critical | Complete, matching audit evidence found a real output mismatch and the result was committed |
| `audit_error` | error | Infrastructure failure, timeout, incompatible/missing evidence or source/identity problem; retry follows existing policy |
| `audit_result_discarded` | warning | Ownership/lease was lost before saving; never reported as a confirmed mismatch |
| `audit_waiting` | warning | Old pending/retrying task, possibly requiring another pinned checkout; checked at most once per minute |

Only a successful fenced DB write emits a terminal audit outcome. Source files, lease tokens,
raw requests and full audit input samples are not included in these events. Reports stay in the
DB and private artifact directory. Audit results **do not change champions, eligibility or payouts**.

Changing this auditor implementation changes its coverage-profile identity. On deployment it
will backfill historical promotions for the new profile; expect clean events for past champions.

## Monitors and webhook

The [monitor definitions](../deploy/axiom/monitors.json) cover:

1. A match monitor for confirmed audit mismatches.
2. A five-minute, per-host/per-source error-count threshold (above zero).
3. A five-minute audit-backlog threshold.
4. Missing auditor heartbeat for five minutes, pinned to a required hostname. It checks
   `count() < 1` without grouping, so a completely absent host still returns zero.

Render ready-to-review Axiom v2 monitor bodies (no network calls):

```bash
python scripts/render-axiom-monitors.py \
  --dataset conjectures-validator --environment dev \
  --audit-host nl-vmv3-heavy --notifier-id YOUR_NOTIFIER_ID > /tmp/torchlean-monitors.json
```

Use the auditor's real `hostname`, not its inventory alias. Create these monitors in Axiom's UI
using the rendered queries/settings, or POST each rendered body to `/v2/monitors` with a separate
management token. Keep that token on the operator machine, never on workers. For additional
required benchmark/coordinator hosts, copy the heartbeat monitor and change its source/host
filters. Install heartbeat monitors after verifying ingestion; suspend them for planned downtime.

In Axiom **Monitors → Manage notifiers**, create a Custom webhook (or a native Slack/Discord
notifier), enter its destination URL and required authorization headers, and attach its ID to
these monitors. Leave the default JSON payload initially; it contains the matched event or
threshold details. Threshold monitors notify on state changes rather than every poll. The mismatch
monitor sends each confirmed event; repeated mismatch events for distinct audits are intentional.
Normal candidate benchmark refusals are warnings; reference/infrastructure failures are errors,
and a benchmark checksum mismatch is critical. This keeps ordinary rejected entries out of the
operational-error monitor. No worker calls a webhook directly, so notification outages cannot
affect auditing.

Provide the ingest token via Vault, the dataset/API region, notification destination and optional
headers, then the notifier ID. The webhook URL/authorization belongs in Axiom, not the worker Vault.
No management token is needed when creating monitors in the UI.

Official references: [monitors](https://axiom.co/docs/monitor-data/monitors),
[threshold settings](https://axiom.co/docs/monitor-data/threshold-monitors),
[custom webhooks](https://axiom.co/docs/monitor-data/custom-webhook-notifier),
[create-monitor API](https://axiom.co/docs/restapi/endpoints/createMonitor).
