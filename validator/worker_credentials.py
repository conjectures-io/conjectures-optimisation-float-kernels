"""Load administrator-generated worker credentials, shared by service launchers."""

import json
import os
import stat


def load(path):
    if path is None:
        return
    with open(path) as f:
        if stat.S_IMODE(os.fstat(f.fileno()).st_mode) & 0o077:
            raise ValueError("worker credentials must have mode 0600")
        values = json.load(f)
    if not isinstance(values, dict) or not isinstance(values.get("DATABASE_URL"), str):
        raise ValueError("invalid worker credentials")
    if set(values) - {"DATABASE_URL", "BENCHMARK_ENVIRONMENT"}:
        raise ValueError("unexpected worker credential keys")
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("invalid worker credential value")
    # A local process manager may inherit the administrator's deployment environment.
    # Keep only the restricted URL in the worker and its child processes.
    for key in (
        "WORKER_ADMIN_DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "PGUSER",
        "PGPASSWORD",
    ):
        os.environ.pop(key, None)
    os.environ.update(values)
