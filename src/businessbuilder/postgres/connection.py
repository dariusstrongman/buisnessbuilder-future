from __future__ import annotations

import os
import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def connect_postgres(
    dsn: str | None = None,
    *,
    schema: str | None = None,
    application_name: str = "businessbuilder-staging",
    ensure_schema: bool = True,
) -> psycopg.Connection[dict[str, Any]]:
    """Open a PostgreSQL connection without ever constructing or logging a credential URL."""
    schema_name = schema or os.environ.get("DB_SCHEMA", "businessbuilder")
    if not _SAFE_IDENTIFIER.fullmatch(schema_name):
        raise ValueError("DB_SCHEMA must be a lowercase PostgreSQL identifier")

    if dsn:
        connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    elif os.environ.get("DATABASE_URL"):
        connection = psycopg.connect(
            os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row
        )
    else:
        required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"missing PostgreSQL settings: {', '.join(missing)}")
        connection = psycopg.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            sslmode=os.environ.get("DB_SSLMODE", "require"),
            application_name=application_name,
            connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
            autocommit=True,
            row_factory=dict_row,
        )

    with connection.cursor() as cursor:
        if ensure_schema:
            cursor.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(schema_name)
                )
            )
        cursor.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
        )
    return connection
