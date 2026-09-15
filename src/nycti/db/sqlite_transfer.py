"""Verified, read-only export to SQLite and WAL-aware online backups.

This is a transfer utility, not a schema migration runner. Freeze production writers
before the final export; an online rehearsal is only a point-in-time snapshot.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from sqlalchemy import JSON, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.engine import URL, make_url

from nycti.db.models import Base
from nycti.db.sqlite_identity import preserve_identity
from nycti.db.sqlite_schema import configure_sqlite_schema

LEGACY_RSS_COLUMNS = {"id", "guild_id", "channel_id", "feed_url", "title", "created_by_id", "created_at"}


class TransferError(ValueError):
    """A content-free transfer diagnostic safe to show on the CLI."""


@contextmanager
def new_destination(destination: Path):
    destination = destination.absolute()
    if destination.exists():
        raise FileExistsError("Destination already exists; never overwrite a database")
    fd, name = tempfile.mkstemp(prefix=".nycti-copy-", suffix=".db", dir=destination.parent)
    os.close(fd)  # mkstemp creates the private artifact with mode 0600.
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, destination)  # Atomic publication; fails if another writer created it.
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def _utc(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return _utc(value).isoformat()
    raise TypeError("Unsupported database value")


def _rows(connection, table):
    null_flags = [column.is_(None).label("_sqlnull_" + column.name)
                  for column in table.c if isinstance(column.type, JSON)]
    # Locale-dependent PostgreSQL ordering must match SQLite's byte ordering for hashes.
    collation = "C" if connection.dialect.name == "postgresql" else "BINARY"
    ordering = [column.collate(collation) if isinstance(column.type, String) else column
                for column in table.primary_key.columns]
    statement = select(*table.c, *null_flags).order_by(*ordering)
    return connection.execution_options(stream_results=True).execute(statement).mappings()


def _digest_update(digest, row):
    data = json.dumps(dict(row), sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"), default=_json_default).encode()
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _verify_sqlite(path: Path) -> None:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise TransferError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TransferError("SQLite foreign key check failed")


def copy_to_sqlite(source_url: str, destination: Path) -> dict:
    url = make_url(source_url)
    backend = url.get_backend_name()
    if backend == "postgresql" or url.drivername == "postgres":
        url = url.set(drivername="postgresql+psycopg")
        source = create_engine(url, isolation_level="REPEATABLE READ", connect_args={
            "connect_timeout": 10, "options": "-c default_transaction_read_only=on",
        })
    elif backend == "sqlite":
        source_path = Path(url.database or "").absolute()
        if not source_path.is_file():
            raise TransferError("Source SQLite file must exist")
        source = create_engine("sqlite://", creator=lambda: sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro", uri=True))
    else:
        raise TransferError("Only PostgreSQL and file-backed SQLite sources are supported")
    report = {"source_backend": backend, "tables": {}, "verified": False}
    try:
        with new_destination(destination) as temporary, source.connect() as src:
            if backend == "postgresql" or url.drivername.startswith("postgresql"):
                src.exec_driver_sql("SET TRANSACTION READ ONLY")
                src.exec_driver_sql("SET LOCAL statement_timeout = '60s'")
            else:
                src.exec_driver_sql("PRAGMA query_only=ON")
                src.exec_driver_sql("BEGIN")
            inspector = inspect(src)
            actual_tables = set(inspector.get_table_names())
            extra_tables = actual_tables - set(Base.metadata.tables)
            if set(Base.metadata.tables) - actual_tables or extra_tables - {"rss_feed_subscriptions"}:
                raise TransferError("Source table set differs from this revision; migration refused")
            source_metadata = MetaData()
            if "rss_feed_subscriptions" in extra_tables:
                legacy = Table("rss_feed_subscriptions", source_metadata, autoload_with=src)
                if set(legacy.c.keys()) != LEGACY_RSS_COLUMNS:
                    raise TransferError("Legacy RSS schema differs; migration refused")
                for column in legacy.c:
                    column.type = column.type.as_generic()
                    # Archived rows are copied explicitly; PostgreSQL defaults such as
                    # nextval(...::regclass) are not executable SQLite expressions.
                    column.server_default = None
                configure_sqlite_schema(source_metadata)
                report["archived_legacy_tables"] = [legacy.name]
            for table in Base.metadata.sorted_tables:
                table.to_metadata(source_metadata)
            for table in Base.metadata.sorted_tables:
                if {c["name"] for c in inspector.get_columns(table.name)} != set(table.c.keys()):
                    raise TransferError(f"Source schema differs for {table.name}; migration refused")
            target_metadata = MetaData()
            for table in source_metadata.sorted_tables:
                cloned = table.to_metadata(target_metadata)
                for column in cloned.c:
                    if isinstance(column.type, JSON):
                        column.type = JSON(none_as_null=True)
            target = create_engine(URL.create("sqlite", database=str(temporary)))
            try:
                with target.begin() as dst:
                    target_metadata.create_all(dst)
                    for original in source_metadata.sorted_tables:
                        table = target_metadata.tables[original.name]
                        digest = hashlib.sha256()
                        count = 0
                        with _rows(src, original) as rows:
                            for batch in rows.partitions(256):
                                values = []
                                for row in batch:
                                    _digest_update(digest, row)
                                    item = {c.name: _utc(row[c.name]) for c in original.c}
                                    for column in original.c:
                                        if isinstance(column.type, JSON) and item[column.name] is None:
                                            item[column.name] = None if row["_sqlnull_" + column.name] else JSON.NULL
                                    values.append(item)
                                dst.execute(table.insert(), values)
                                count += len(values)
                        check = hashlib.sha256()
                        copied = 0
                        with _rows(dst, table) as rows:
                            for row in rows:
                                _digest_update(check, row)
                                copied += 1
                        if copied != count or check.digest() != digest.digest():
                            raise TransferError(f"Content verification failed for {table.name}")
                        report["tables"][table.name] = {"rows": count, "sha256": digest.hexdigest()}
                        identity = preserve_identity(src, dst, original, table)
                        if identity is not None:
                            report["tables"][table.name]["identity_high_water"] = identity
            finally:
                target.dispose()
            _verify_sqlite(temporary)
            report["verified"] = True
    finally:
        source.dispose()
    return report


def backup_sqlite(source: Path, destination: Path) -> dict:
    source = source.absolute()
    if not source.is_file():
        raise TransferError("Source SQLite file must exist")
    with new_destination(destination) as temporary:
        deadline = time.monotonic() + 60

        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise TimeoutError("Backup exceeded time limit")

        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256, progress=progress)
        _verify_sqlite(temporary)
        size = temporary.stat().st_size
    return {"verified": True, "bytes": size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("copy")
    export.add_argument("--source-env", default="DATABASE_URL")
    export.add_argument("--destination", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (copy_to_sqlite(os.environ[args.source_env], args.destination)
                  if args.command == "copy" else backup_sqlite(args.source, args.destination))
    except Exception as error:
        # SQL errors can contain credentials/row contents; do not print the exception.
        detail = str(error) if isinstance(error, TransferError) else type(error).__name__
        parser.exit(1, f"Transfer failed: {detail}; destination was not overwritten.\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
