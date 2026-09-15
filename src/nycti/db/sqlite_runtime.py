"""Conservative file-backed SQLite settings for one bot replica."""
from sqlalchemy import event


def configure_sqlite(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            if cursor.fetchone()[0].lower() != "wal":
                raise RuntimeError("File-backed SQLite requires WAL support")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        # One pooled connection serializes in-process read/modify/write transactions.
        connection.exec_driver_sql("BEGIN")
