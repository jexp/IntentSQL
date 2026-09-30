"""Text-declared columns can contain numeric SQLite aggregate inputs."""

import tempfile
import unittest
from pathlib import Path

from intentsql.database import connect
from intentsql.skills.schema import numeric_profile


class NumericProfileTests(unittest.TestCase):
    def test_numeric_text_with_sparse_bad_values_is_visible_and_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE observations (measurement TEXT)")
                conn.executemany("INSERT INTO observations VALUES (?)",
                                 [(str(i),) for i in range(1, 20)] + [("unknown",)])
                profile = numeric_profile(conn, "observations", "measurement")
                sqlite_average = conn.execute(
                    'SELECT AVG("measurement") FROM "observations"').fetchone()[0]
            self.assertEqual((profile.numeric_values, profile.nonnumeric_values), (19, 1))
            self.assertTrue(profile.compatible)
            self.assertEqual(sqlite_average, 9.5)  # SQLite includes bad text as zero.

    def test_non_numeric_text_is_rejected_without_schema_shortcuts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE observations (measurement VARCHAR(20))")
                conn.executemany("INSERT INTO observations VALUES (?)",
                                 [("small",), ("large",)])
                self.assertFalse(numeric_profile(
                    conn, "observations", "measurement").compatible)
                with self.assertRaises(ValueError):
                    numeric_profile(conn, "observations", "missing")


if __name__ == "__main__":
    unittest.main()
