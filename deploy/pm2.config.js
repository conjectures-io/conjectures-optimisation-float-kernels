// The validator under PM2: four processes, each doing one thing, named floatlib-* so they sit
// beside another competition's on one host.
//
//   floatlib-submission-api   serves /v1/competitions/floatlib[-mul|-fma|-div|-sqrt]/...
//   floatlib-gate-worker      drains the queue through gate.py
//   floatlib-chain-watcher    streams subnet registrations into the store
//   floatlib-weight-setter    scores the round and sets weights, once an epoch (dry run by default)
//
//   pm2 start deploy/pm2.config.js                               all four
//   pm2 start deploy/pm2.config.js --only floatlib-gate-worker    one machine's gate only
//
// They are separate processes on purpose: a gate crash must not take the API down. The
// two chain workers need the bittensor SDK (./setup.sh --chain) and nothing else does.
//
// Postgres is not here: it comes up with `docker compose up -d db` (or a managed
// instance), and the schema with `docker compose up migrate`, before any of these start.
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..");

// .env, as written by setup.sh: KEY=value lines, '#' comments.
const env = {};
const dotenv = path.join(root, ".env");
if (fs.existsSync(dotenv)) {
  for (const line of fs.readFileSync(dotenv, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_]+)=(.*)$/);
    if (m) env[m[1]] = m[2].trim();
  }
}

// What the shell exports overrides .env, so a one-off `WEIGHT_DRY_RUN=1 pm2 start ...` holds.
Object.assign(env, process.env);

const python = path.join(root, ".venv/bin/python");
const cwd = path.join(root, "validator");

module.exports = {
  apps: [
    {
      name: "floatlib-submission-api",
      cwd,
      script: python,
      // The venv's python is the program itself, not a script for pm2's own interpreter.
      interpreter: "none",
      args: "-m service.api",
      env,
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: "floatlib-gate-worker",
      cwd,
      script: python,
      // The venv's python is the program itself, not a script for pm2's own interpreter.
      interpreter: "none",
      args: "-m service.worker",
      // Each worker needs its own id: it is what the submissions table records as the
      // holder of a claim, and what tells two workers on one box apart.
      env: { ...env, SERVICE_WORKER_ID: `${require("os").hostname()}-pm2` },
      autorestart: true,
      max_restarts: 10,
      // The gate is a long subprocess; give it time to finish the one in flight.
      kill_timeout: 30000,
    },
    {
      // Without this, no hotkey has a registration and so nobody can submit at all.
      name: "floatlib-chain-watcher",
      cwd,
      script: python,
      // The venv's python is the program itself, not a script for pm2's own interpreter.
      interpreter: "none",
      args: "-m workers.chain_watcher",
      env,
      autorestart: true,
      max_restarts: 10,
    },
    {
      name: "floatlib-weight-setter",
      cwd,
      script: python,
      // The venv's python is the program itself, not a script for pm2's own interpreter.
      interpreter: "none",
      args: "-m workers.weight_setter",
      env,
      autorestart: true,
      max_restarts: 10,
    },
  ],
};
