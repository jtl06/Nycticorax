"""SQLite variants preserving the PostgreSQL model contract."""
from datetime import timezone

from sqlalchemy import DateTime, Integer
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, _dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def process_result_value(self, value, _dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def configure_sqlite_schema(metadata):
    for table in metadata.tables.values():
        primary_key = list(table.primary_key.columns)
        if len(primary_key) == 1 and type(primary_key[0].type) is Integer and primary_key[0].autoincrement:
            table.dialect_options["sqlite"]["autoincrement"] = True
        for column in table.c:
            if isinstance(column.type, DateTime) and column.type.timezone:
                column.type = column.type.with_variant(UTCDateTime(), "sqlite")
