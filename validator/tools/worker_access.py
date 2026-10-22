"""Provision independently revocable worker logins using WORKER_ADMIN_DATABASE_URL."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

GROUPS = {"benchmark": "tensor_benchmark_access", "auditor": "tensor_auditor_access"}
NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")


def validate_login(login):
    if not NAME.fullmatch(login) or login.startswith(("pg_", "tensor_")):
        raise ValueError(
            "use a lowercase login name, up to 63 characters, outside reserved prefixes"
        )


def provision(conn, action, login, *, kind=None, environments=(), password=None):
    """Caller supplies a transaction. Credentials never enter SQLAlchemy statement logging."""
    validate_login(login)
    if action in ("create", "rotate") and not password:
        raise ValueError("a generated password is required")
    if any(not e or len(e) > 128 or not re.fullmatch(r"[a-zA-Z0-9_.-]+", e) for e in environments):
        raise ValueError("invalid environment name")
    record = conn.execute(
        text("SELECT kind,enabled FROM worker_logins WHERE login=:n FOR UPDATE"), {"n": login}
    ).first()
    if action == "create":
        if (
            kind not in GROUPS
            or record
            or conn.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:n)"), {"n": login}
            )
        ):
            raise ValueError("invalid kind or login already exists; refusing to repurpose it")
        if (kind == "benchmark") != bool(environments):
            raise ValueError("benchmark logins need environments; auditors must not have them")
        with conn.connection.driver_connection.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(login), sql.Literal(password))
            )
            cur.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(GROUPS[kind]), sql.Identifier(login)
                )
            )
        conn.execute(
            text("INSERT INTO worker_logins(login,kind) VALUES(:n,:k)"), {"n": login, "k": kind}
        )
    else:
        if not record:
            raise ValueError("not a provisioned worker login")
        kind = record.kind
        if action in ("rotate", "environments") and not record.enabled:
            raise ValueError("login is revoked; create a new login")
        if action == "rotate":
            with conn.connection.driver_connection.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(login), sql.Literal(password)
                    )
                )
        elif action == "revoke":
            with conn.connection.driver_connection.cursor() as cur:
                cur.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(login)))
                cur.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(GROUPS[kind]), sql.Identifier(login)
                    )
                )
            conn.execute(
                text("UPDATE worker_logins SET enabled=false WHERE login=:n"), {"n": login}
            )
        elif action != "environments":
            raise ValueError("unknown action")
    if action in ("create", "environments"):
        if kind != "benchmark" and environments:
            raise ValueError("auditors have no benchmark environment assignments")
        conn.execute(text("DELETE FROM worker_environments WHERE login=:n"), {"n": login})
        for environment in sorted(set(environments)):
            conn.execute(
                text("INSERT INTO worker_environments(login,environment) VALUES(:n,:e)"),
                {"n": login, "e": environment},
            )
    conn.execute(
        text("UPDATE worker_logins SET updated_at=clock_timestamp() WHERE login=:n"), {"n": login}
    )


def write_credentials(path, values):
    """Publish a complete 0600 file without replacing any existing destination."""
    fd, tmp = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(values, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "environments", "rotate", "revoke", "list"))
    parser.add_argument("--login")
    parser.add_argument("--kind", choices=GROUPS)
    parser.add_argument("--environment", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host", help="worker-facing database host, including tunnel endpoint")
    parser.add_argument("--port", type=int)
    parser.add_argument("--sslmode", choices=("require", "verify-ca", "verify-full", "disable"))
    args = parser.parse_args()
    if args.action != "list" and not args.login:
        parser.error("--login is required")
    if args.action in ("create", "rotate"):
        if not args.output or args.output.exists():
            parser.error("--output must name a new credentials file")
        if not args.output.parent.is_dir():
            parser.error("credentials output directory does not exist")
    admin = os.environ.get("WORKER_ADMIN_DATABASE_URL")
    if not admin:
        parser.error("set WORKER_ADMIN_DATABASE_URL in the administrator environment")
    engine = None
    try:
        engine = create_engine(admin, hide_parameters=True)
        if args.action == "list":
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT w.login,w.kind,w.enabled,"
                            "coalesce(array_agg(e.environment ORDER BY e.environment) "
                            "FILTER(WHERE e.environment IS NOT NULL),'{}'::text[]) AS environments "
                            "FROM worker_logins w LEFT JOIN worker_environments e USING(login) "
                            "GROUP BY w.login ORDER BY w.login"
                        )
                    )
                    .mappings()
                    .all()
                )
                print(json.dumps([dict(r) for r in rows]))
            return
        password = secrets.token_urlsafe(36)
        with engine.begin() as conn:
            provision(
                conn,
                args.action,
                args.login,
                kind=args.kind,
                environments=args.environment,
                password=password,
            )
            environments = list(
                conn.scalars(
                    text(
                        "SELECT environment FROM worker_environments "
                        "WHERE login=:n ORDER BY environment"
                    ),
                    {"n": args.login},
                )
            )
        # Password changes do not invalidate established sessions. Terminate after commit.
        if args.action in ("revoke", "rotate"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE usename=:n AND pid<>pg_backend_pid()"
                    ),
                    {"n": args.login},
                )
        if args.action in ("create", "rotate"):
            url = make_url(admin).set(username=args.login, password=password)
            if args.host:
                url = url.set(host=args.host)
            if args.port:
                url = url.set(port=args.port)
            if args.sslmode:
                url = url.update_query_dict({"sslmode": args.sslmode})
            values = {"DATABASE_URL": url.render_as_string(hide_password=False)}
            if len(environments) == 1:
                values["BENCHMARK_ENVIRONMENT"] = environments[0]
            write_credentials(args.output, values)
        print(f"{args.action}: {args.login}; no credentials printed")
    except Exception as exc:
        # DB driver errors can contain connection details: never print their raw messages.
        print(
            f"operation failed ({type(exc).__name__}); inspect role state with list. "
            "If credentials were not written, rotate into a new file."
        )
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
