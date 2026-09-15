"""Preserve generated-ID high water marks across a database copy."""
from sqlalchemy import func, select, text


def preserve_identity(source, destination, source_table, target_table) -> int | None:
    if not target_table.dialect_options["sqlite"]["autoincrement"]:
        return None
    column = list(source_table.primary_key.columns)[0]
    high = source.scalar(select(func.max(column))) or 0
    if source.dialect.name == "postgresql":
        sequence = source.execute(text("""
            SELECT s.schemaname, s.sequencename
            FROM pg_sequences s
            JOIN pg_namespace n ON n.nspname = s.schemaname
            JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = s.sequencename
            WHERE c.oid = CAST(pg_get_serial_sequence(:table, :column) AS regclass)
        """), {"table": source_table.name, "column": column.name}).one_or_none()
        if sequence is None:
            raise ValueError(f"Missing generated-ID sequence for {source_table.name}")
        preparer = source.dialect.identifier_preparer
        qualified = preparer.quote_schema(sequence.schemaname) + "." + preparer.quote(sequence.sequencename)
        # Identifiers come from the catalog and are dialect-quoted, never raw user SQL.
        state = source.execution_options(stream_results=False).exec_driver_sql(
            f"SELECT last_value, is_called FROM {qualified}"
        ).one()
        if state.is_called:
            high = max(high, state.last_value)
    else:
        has_sequences = source.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='sqlite_sequence'"))
        if has_sequences:
            saved = source.scalar(text("SELECT seq FROM sqlite_sequence WHERE name=:table"), {"table": source_table.name})
            high = max(high, saved or 0)
    destination.execute(text("DELETE FROM sqlite_sequence WHERE name=:table"), {"table": target_table.name})
    destination.execute(text("INSERT INTO sqlite_sequence(name,seq) VALUES (:table,:high)"),
                        {"table": target_table.name, "high": high})
    return high
