"""SQLite lifetimes and file URIs shared by the local application."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


def readonly_uri(path: str | Path) -> str:
    # as_uri escapes ?, # and spaces, including Windows drive letters.
    return Path(path).resolve().as_uri() + "?mode=ro"


@contextmanager
def connect(database, **kwargs):
    """Commit/rollback like sqlite3's context manager, then always close."""
    connection = sqlite3.connect(database, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
