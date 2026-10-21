// One replacement competition: coordinator, hardware worker, watcher, setter.
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..");
const env = {};
const dotenv = path.join(root, ".env");
if (fs.existsSync(dotenv)) {
  for (const line of fs.readFileSync(dotenv, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_]+)=(.*)$/);
    if (m) env[m[1]] = m[2].trim();
  }
}
Object.assign(env, process.env);
env.PYTHONPATH = [root, path.join(root, "validator")].join(path.delimiter);
const python = path.join(root, ".venv/bin/python");
module.exports = {
  apps: [
    ["tensor-coordinator", "-m workers.coordinator"],
    ["tensor-benchmark-worker", `-m workers.benchmark_worker --environment ${env.BENCHMARK_ENVIRONMENT || "local"}`],
    ["tensor-chain-watcher", "-m workers.chain_watcher"],
    ["tensor-weight-setter", "-m workers.weight_setter"],
  ].map(([name, args]) => ({
    name, args, cwd: root, script: python, interpreter: "none", env,
    autorestart: true, max_restarts: 10, kill_timeout: 10000,
  })),
};
