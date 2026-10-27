# TorchLean production launch

TorchLean replaces FloatLib on the production host (`conjectures`) and takes its 20% of the
validator's weight (`COMPETITION_BPS` in `validator/treasury.py`); the treasury keeps 80%. DEFLATE
is deprecated and gets nothing. One process sets weights for the validator hotkey: today that is
`floatlib-weight-setter`, after the cutover `tensor-weight-setter`. The central API serves intake
and reads (conjectures-validator, `docs/TORCHLEAN_API.md`); the [manual](../MANUAL.md) covers each
step in general.

## Production today

| what | where |
|---|---|
| FloatLib processes | pm2 as user `deflate`: `floatlib-gate-worker`, `floatlib-chain-watcher`, `floatlib-weight-setter` (`WEIGHT_DRY_RUN=0`, wallet `new_sn66_owner`) |
| FloatLib checkout | `/opt/conjectures-optimisation-float-kernels` on `main` (keep it for rollback) |
| Databases | FloatLib on 127.0.0.1:5434, on `conjectures-api_default`; 5435 is free |
| Central API | `/opt/conjectures-validator/current`, host `.env` (`ALLOW_ENV_FROM_GITHUB=0`), `COMPETITIONS=deflate,floatlib` |

## 1. Prepare (no effect on FloatLib or weights)

1. Check out this branch as `deflate` in a new directory, e.g. `/opt/conjectures-torchlean`,
   and run `./setup.sh` (plus `--chain` for the two chain workers).
2. `.env`: `POSTGRES_*` for a new database `tensor` on `POSTGRES_PORT=5435`,
   `COMPOSE_FILE=compose.yaml:deploy/platform/compose.yaml`, `PLATFORM_NETWORK=conjectures-api_default`,
   `NETUID=66`, `BITTENSOR_NETWORK=finney`, `BITTENSOR_ARCHIVE_NETWORK=archive`, the same
   `BITTENSOR_WALLET_*` values as FloatLib's `.env`, `WEIGHT_DRY_RUN=1`, `COMPETITION_CONFIG`, and
   `AXIOM_TOKEN`/`AXIOM_DATASET` from the API's `.env` with `AXIOM_ENVIRON=prod`.
3. Database: `docker compose up -d db`, `docker compose build migrate`,
   `docker compose run --rm migrate` (head is `tensor_0002`).
4. API role: `set -a; . ./.env; set +a; PLATFORM_API_PASSWORD=<new> deploy/platform/grant.sh`.
   Keep the password only in the API's `COMPETITION_TORCHLEAN_DATABASE_URL` (step 2.1).
5. Coordinator config with the production benchmark environments and their measured identities
   (`scripts/exact-config.py`, then review), and an execute-only login per benchmark worker
   (`deploy/platform/benchmark_worker.sql`, `validator/tools/worker_access.py`).
6. `pm2 start deploy/pm2.config.js --only tensor-coordinator,tensor-chain-watcher`. The chain
   watcher's first pass records the whole subnet with block dates from the archive node.
7. Rehearse the weights: `pm2 start deploy/pm2.config.js --only tensor-weight-setter` with
   `WEIGHT_DRY_RUN=1`. At the next epoch `weight_sets` gets a row; with no champion yet, the
   competition's 20% goes to the treasury.

## 2. Cutover

1. Immediately before approving the production deploy, edit the API's host `.env` (the API in
   production today refuses `torchlean` in `COMPETITIONS`, so not earlier):

   ```dotenv
   COMPETITIONS=deflate,floatlib,torchlean
   COMPETITION_TORCHLEAN_DATABASE_URL=postgresql+psycopg://platform_api:<password>@conjectures_tensor_db:5432/tensor
   TORCHLEAN_SUBMISSIONS_ENABLED=1
   TORCHLEAN_REGISTRATION_CUTOFF=<launch time, UTC, e.g. 2026-10-28T12:00:00Z>
   FLOATLIB_SUBMISSIONS_ENABLED=0
   ```

   The cutoff is the launch: a hotkey can enter with a registration made after it, once per
   registration. `TORCHLEAN_DEV_SKIP_REGISTRATION` must stay unset (production refuses it).
2. Merge `dev` into `main` in conjectures-validator and approve the deploy. It applies
   `V054__competition_intake_limits.sql` after backing up the database.
3. Check `GET /v1/competitions/torchlean/health` (`ok`, `policy_ready: true`) and the boards,
   then one signed upload from a hotkey registered after the cutoff (`miner/submit.py submit`).
4. Weights: `pm2 stop floatlib-weight-setter`, set `WEIGHT_DRY_RUN=0` in this `.env`, then
   `pm2 restart tensor-weight-setter --update-env`. Confirm a `weights_set` event (Axiom) or an
   accepted row in `weight_sets`, then `pm2 save`.
5. `pm2 stop floatlib-gate-worker floatlib-chain-watcher` once FloatLib's queue is empty; its
   database stays up for the historical reads.

## Rollback

Weights first: `pm2 stop tensor-weight-setter`, `pm2 start floatlib-weight-setter`. Then intake:
`TORCHLEAN_SUBMISSIONS_ENABLED=0` (and `FLOATLIB_SUBMISSIONS_ENABLED=1` with `floatlib-gate-worker`
started, if FloatLib reopens) and recreate the API container. Nothing above deletes FloatLib data.
