import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
FIXTURES_DIR = Path(
    os.environ.get("FIXTURES_DIR", Path(__file__).resolve().parent.parent.parent / "fixtures")
)
DB_PATH = DATA_DIR / "hourspan.db"


def connect(isolation_level: str | None = "") -> sqlite3.Connection:
    """Open the shared DB.

    isolation_level=None → autocommit mode, callers issue explicit
    BEGIN IMMEDIATE / COMMIT / ROLLBACK for all-or-nothing writes.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
