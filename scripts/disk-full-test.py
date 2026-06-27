#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "failure-injection.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE state(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO state(value) VALUES ('before')")
        connection.commit()
        connection.execute("PRAGMA max_page_count=2")
        failed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO state(value) VALUES (?)", ("x" * 100_000,))
            connection.commit()
        except sqlite3.DatabaseError:
            failed = True
            connection.rollback()
        rows = connection.execute("SELECT value FROM state ORDER BY id").fetchall()
        connection.close()
        if not failed or rows != [("before",)]:
            print("disk-full failure injection did not preserve atomic state")
            return 1
        print("disk-full failure injection preserved the committed state")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
