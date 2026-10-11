from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path

from .ydb_repository import YdbConnection


def _statements(text: str) -> list[str]:
    sql = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    return [part.strip() + ";" for part in sql.split(";") if part.strip()]


def _apply(connection: YdbConnection, statements: list[str]) -> None:
    for statement in statements:
        if statement.startswith("ALTER TABLE farm_session_ids ADD COLUMN updated_at "):
            connection.add_column_if_missing(statement)
        else:
            connection.execute_scheme(statement)


def _backfill_retention_columns(connection: YdbConnection) -> None:
    """Populate the TTL clock for rows created before the column existed."""
    connection.execute(
        """
        DECLARE $updated_at AS Timestamp;
        UPDATE farm_session_ids
        SET updated_at = $updated_at
        WHERE updated_at IS NULL;
        """,
        {"$updated_at": datetime.now(UTC)},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the idempotent AION2 Economy YDB schema.")
    parser.add_argument("--schema", required=True, type=Path)
    args = parser.parse_args(argv)
    connection = YdbConnection(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"])
    statements = _statements(args.schema.read_text(encoding="utf-8"))
    _apply(connection, statements)
    _backfill_retention_columns(connection)
    print(f"schema_ready statements={len(statements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
