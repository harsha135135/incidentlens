"""IncidentLens's own PostgreSQL: connections and numbered SQL migrations."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

from incidentlens.config import PROJECT_ROOT

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def connect(conninfo: str, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(conninfo, autocommit=autocommit, connect_timeout=5)


def apply_migrations(conninfo: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Applies migrations/NNN_name.sql files that have not run yet, in name order, each in its
    own transaction together with its schema_migrations row. Returns the names applied."""
    applied_now = []
    with connect(conninfo, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        # One migrator at a time, so two processes starting together cannot both apply a file.
        conn.execute("SELECT pg_advisory_lock(hashtext('incidentlens.migrations'))")
        try:
            done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('incidentlens.migrations'))")
    return applied_now


def provision_logins(owner_conninfo: str, passwords: dict[str, str]) -> None:
    """Gives the NOLOGIN roles created by migration 002 a login and password. Safe to rerun.
    Passwords are passed as SQL literals built by psycopg, never written into a migration."""
    with connect(owner_conninfo, autocommit=True) as conn:
        for role, password in passwords.items():
            if role not in ("incidentlens_app", "incidentlens_collector"):
                raise ValueError(f"unexpected role {role}")
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS "
                                 "CONNECTION LIMIT 40 PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))


def schema_current(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> bool:
    """True when the newest migration file has been applied (readiness, spec §7)."""
    newest = sorted(path.name for path in migrations_dir.glob("*.sql"))[-1]
    return conn.execute("SELECT %s IN (SELECT name FROM schema_migrations)", (newest,)).fetchone()[0]
