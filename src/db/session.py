"""
ARTA Platform — Async Database Session Factory
Uses asyncpg driver with SQLAlchemy 2.0 async engine.
"""
from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://arta:arta@localhost:5432/arta",
)

engine = create_async_engine(
    _DATABASE_URL,
    echo=os.environ.get("SQL_ECHO", "").lower() == "true",
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a scoped async session, auto-closes on exit."""
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def init_db() -> None:
    """Called once at startup to verify connectivity + apply idempotent migrations."""
    import logging
    from pathlib import Path
    from sqlalchemy import text as _text
    log = logging.getLogger("arta.db")

    async with engine.begin() as conn:
        await conn.execute(_text("SELECT 1"))

    # Phase Gap-1.1: Apply idempotent migrations from src/db/migrations/
    # Each migration is safe to re-run (uses IF NOT EXISTS etc.).
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    if not migrations_dir.is_dir():
        return
    repo_root = Path(__file__).resolve().parents[2]
    for mig in sorted(migrations_dir.glob("*.sql")):
        try:
            # F8-10: Resolve psql `\i path/to/file.sql` REPL meta-commands
            # before splitting on semicolons. Without this, statement loop
            # treats `\i ...` as a SQL statement and emits "syntax error
            # at or near \" on every container startup.
            sql_text = _expand_psql_includes(mig.read_text(), repo_root)
            # F16-1: Open a fresh transaction per statement instead of one
            # `engine.begin()` for the whole file. The previous shape lost
            # everything after the first non-idempotent statement
            # (e.g. `CREATE TYPE risk_priority AS ENUM (...)` which lacks
            # `IF NOT EXISTS`) — Postgres aborted the transaction and the
            # per-statement try/except was useless because every subsequent
            # `execute()` returned `InFailedSQLTransactionError`.
            for stmt in _split_sql_statements(sql_text):
                if not stmt.strip():
                    continue
                try:
                    async with engine.begin() as conn:
                        # F17-1: exec_driver_sql bypasses SQLAlchemy's
                        # text() bind-parameter parser. Migrations are
                        # raw SQL with literal data (e.g. JSON seeds
                        # containing `"base_url":"http://localhost:11434"`)
                        # — text() interprets `:11434` as a bind param and
                        # raises `A value is required for bind parameter
                        # '0'`. exec_driver_sql treats the string opaquely.
                        await conn.exec_driver_sql(stmt)
                except Exception as exc:
                    msg = str(exc)[:200]
                    # F20-40: Downgrade EXPECTED migration noise to DEBUG so
                    # genuine schema bugs stay visible at WARNING. When postgres
                    # data persists across container restarts (the common
                    # `down/up` case without `down -v`), schema.sql's CREATE
                    # TABLE / TRIGGER / INDEX / VIEW + seed INSERT statements
                    # all re-run and produce ~50 WARNING lines per restart for
                    # well-known idempotency-violation patterns. Drowns the
                    # logs and hides real failures.
                    expected = (
                        "already exists" in msg          # DuplicateTableError, DuplicateObjectError
                        or "duplicate key value" in msg  # UniqueViolationError on seed re-INSERT
                    )
                    level = log.debug if expected else log.warning
                    level("migration %s statement failed (continuing): %s",
                          mig.name, msg)
            log.info("Applied migration %s", mig.name)
        except Exception as exc:
            log.warning("Could not apply migration %s: %s", mig.name, exc)


def _expand_psql_includes(sql: str, repo_root, _visited: set | None = None) -> str:
    """F8-10: Expand `\\i path` directives in migration SQL.

    psql's REPL meta-commands (lines starting with `\\`) are not valid SQL —
    they're only understood by the interactive psql client. When init_db
    submits them via SQLAlchemy they raise a syntax error. This resolver
    inlines the referenced file so the rest of the migration loader can run
    the combined content as ordinary SQL.

    F9-3: `_visited` tracks already-expanded files (by resolved absolute
    path) so a cyclic include-chain doesn't recurse forever and crash
    container startup with RecursionError.
    """
    import logging as _logging
    import re as _re
    from pathlib import Path as _Path

    _log = _logging.getLogger("arta.db")
    if _visited is None:
        _visited = set()

    out_lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        m = _re.match(r"^\\i\s+(\S+)", stripped)
        if not m:
            out_lines.append(line)
            continue
        ref = _Path(m.group(1))
        if not ref.is_absolute():
            ref = repo_root / ref
        try:
            resolved = str(ref.resolve())
        except OSError:
            resolved = str(ref)
        if resolved in _visited:
            # F9-3: cycle — file already expanded somewhere up the chain
            _log.warning("\\i cycle detected for %s — skipping recursive include", resolved)
            out_lines.append(f"-- cycle detected: skipping repeat include of {resolved}")
            continue
        try:
            included = ref.read_text()
        except Exception:
            # Comment the directive out so the rest of the migration is still valid SQL
            out_lines.append(f"-- could not include {ref}: file not found")
            continue
        _visited.add(resolved)
        out_lines.append(f"-- BEGIN INCLUDE {ref}")
        # Recursively expand in case the included file also has \i
        out_lines.append(_expand_psql_includes(included, repo_root, _visited))
        out_lines.append(f"-- END INCLUDE {ref}")
    return "\n".join(out_lines)


def _split_sql_statements(sql: str) -> list:
    """Split a .sql file into statements on top-level semicolons.

    F15-1: Honour PostgreSQL dollar-quoted strings (`$$ ... $$` and tagged
    `$func$ ... $func$`). The previous line-by-line splitter chopped
    `CREATE FUNCTION ... AS $$ BEGIN ... END; $$ LANGUAGE plpgsql;` into
    3 invalid pieces — every container startup logged
      `unterminated dollar-quoted string at or near "$$"`
      `syntax error at or near "EXCEPTION"`
    and the trigger functions silently failed to install.
    """
    import re as _re
    # Strip block comments first (they can't span dollar quotes — comments
    # are always top-level).
    sql = _re.sub(r"/\*.*?\*/", "", sql, flags=_re.DOTALL)

    out: list[str] = []
    buf: list[str] = []
    in_dollar: str | None = None  # current dollar-tag (e.g. "" for $$, "fn" for $fn$)
    in_line_comment = False        # `-- ...` to end of line
    in_single_quote = False        # '...' standard SQL string
    i = 0
    n = len(sql)
    _tag_re = _re.compile(r"\$([A-Za-z0-9_]*)\$")

    while i < n:
        c = sql[i]

        # Newline always ends a `--` comment
        if c == "\n":
            in_line_comment = False
            buf.append(c)
            i += 1
            continue

        # Skip everything inside a line comment
        if in_line_comment:
            buf.append(c)
            i += 1
            continue

        # Detect `--` start of line comment (only outside strings/dollar quotes)
        if c == "-" and i + 1 < n and sql[i + 1] == "-" and in_dollar is None and not in_single_quote:
            in_line_comment = True
            buf.append(c)
            i += 1
            continue

        # Track standard single-quoted strings (only outside dollar quotes)
        if c == "'" and in_dollar is None:
            # Handle SQL-escaped quote `''` as a literal — toggle stays consistent
            in_single_quote = not in_single_quote
            buf.append(c)
            i += 1
            continue

        # Dollar-quote start/end (skip if currently inside a regular string)
        if c == "$" and not in_single_quote:
            m = _tag_re.match(sql, i)
            if m:
                tag = m.group(1)
                if in_dollar is None:
                    in_dollar = tag
                elif in_dollar == tag:
                    in_dollar = None
                buf.append(m.group(0))
                i += len(m.group(0))
                continue

        # Top-level semicolon → emit statement
        if c == ";" and in_dollar is None and not in_single_quote:
            buf.append(c)
            chunk = "".join(buf).strip()
            if chunk:
                out.append(chunk)
            buf = []
            i += 1
            continue

        buf.append(c)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


async def close_db() -> None:
    """Called at shutdown to dispose the engine connection pool."""
    await engine.dispose()
