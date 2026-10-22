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
delete env.WORKER_ADMIN_DATABASE_URL;
env.PYTHONPATH = [root, path.join(root, "validator")].join(path.delimiter);
const restrictedEnv = Object.fromEntries(Object.entries(env).filter(([key]) =>
  !["DATABASE_URL", "PGUSER", "PGPASSWORD"].includes(key) && !key.startsWith("POSTGRES_")));
const python = path.join(root, ".venv/bin/python");
const auditor = [];
if (env.TENSOR_AUDITOR_ENABLED === "1") {
  if (!env.TENSOR_AUDITOR_CREDENTIALS) throw new Error("set TENSOR_AUDITOR_CREDENTIALS");
  auditor.push(["tensor-auditor", ["-m", "workers.exact_auditor", "--credentials", env.TENSOR_AUDITOR_CREDENTIALS]]);
}
const benchmarkArgs = ["-m", "workers.benchmark_worker"];
if (env.TENSOR_BENCHMARK_CREDENTIALS) {
  benchmarkArgs.push("--credentials", env.TENSOR_BENCHMARK_CREDENTIALS);
} else {
  benchmarkArgs.push("--environment", env.BENCHMARK_ENVIRONMENT || "local");
}
module.exports = {
  apps: [
    ["tensor-coordinator", "-m workers.coordinator"],
    ["tensor-benchmark-worker", benchmarkArgs],
    ["tensor-chain-watcher", "-m workers.chain_watcher"],
    ["tensor-weight-setter", "-m workers.weight_setter"],
    ...auditor,
  ].map(([name, args]) => ({
    name, args, cwd: root, script: python, interpreter: "none",
    env: name === "tensor-auditor" ||
      (name === "tensor-benchmark-worker" && env.TENSOR_BENCHMARK_CREDENTIALS) ? restrictedEnv : env,
    autorestart: true, max_restarts: 10, kill_timeout: 10000,
  })),
};
