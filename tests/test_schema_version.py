from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from common import SCHEMA_VERSION, connect_db, initialize_schema


class SchemaVersionTests(unittest.TestCase):
    """The version is a fast path, so the risk is skipping work that was needed."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="schema-version-"))
        self.db = self.directory / "test.sqlite"

    def test_a_new_database_is_built_and_stamped(self) -> None:
        with connect_db(self.db) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("tracks", tables)
            self.assertIn("youtube_candidates", tables)
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )

    def test_an_unstamped_database_still_gets_migrated(self) -> None:
        # Every database that existed before the version did reads as 0, and
        # must be brought up to date rather than assumed current.
        with connect_db(self.db):
            pass
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version = 0")
        raw.execute("ALTER TABLE tracks DROP COLUMN youtube_licensed_topic")
        raw.commit()
        raw.close()
        with connect_db(self.db) as connection:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(tracks)")}
            self.assertIn("youtube_licensed_topic", columns)

    def test_a_current_database_skips_the_body(self) -> None:
        with connect_db(self.db):
            pass
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        executed: list[str] = []
        connection.set_trace_callback(executed.append)
        initialize_schema(connection)
        self.assertEqual(
            [s for s in executed if "CREATE" in s.upper() or "ALTER" in s.upper()],
            [],
        )

    def test_a_bumped_version_re_runs_the_body(self) -> None:
        with connect_db(self.db):
            pass
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        executed: list[str] = []
        connection.set_trace_callback(executed.append)
        initialize_schema(connection)
        self.assertTrue(any("CREATE TABLE" in s.upper() for s in executed))


if __name__ == "__main__":
    unittest.main()
