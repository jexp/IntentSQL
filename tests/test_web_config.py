import json
import os
from intentsql.database import connect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intentsql import web


class ConnectionStorageTests(unittest.TestCase):
    def test_delete_holds_run_lock_and_releases_it_on_failure(self):
        def check_lock(database):
            self.assertTrue(web.RUN_LOCK.locked())
            raise web.HTTPException(404, "missing")

        with patch.object(web, "database_path", side_effect=check_lock):
            with self.assertRaises(web.HTTPException):
                web.delete_database("missing.db", web.DeleteDatabase(confirm=True))
        self.assertFalse(web.RUN_LOCK.locked())
        with web.RUN_LOCK, patch.object(web, "database_path") as lookup:
            with self.assertRaises(web.HTTPException) as raised:
                web.delete_database("missing.db", web.DeleteDatabase(confirm=True))
            self.assertEqual(raised.exception.status_code, 409)
            lookup.assert_not_called()

    def test_home_versions_every_frontend_asset_from_its_content(self):
        response = web.home()
        document = response.body.decode("utf-8")
        version = web._asset_version()
        self.assertNotIn("__ASSET_VERSION__", document)
        self.assertIn(f'/static/app.js?v={version}', document)
        self.assertIn(f'/static/style.css?v={version}', document)

    def test_direct_run_path_prepares_bundled_databases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundled"
            source.mkdir()
            (source / "sample.db").write_bytes(b"SQLite fixture placeholder")
            db_dir = root / "workspace" / "databases"
            backup_dir = root / "workspace" / "backups"
            with patch.object(web, "DATA_DIR", source), patch.object(web, "DB_DIR", db_dir), \
                 patch.object(web, "BACKUP_DIR", backup_dir):
                self.assertEqual(web.database_path("sample.db"), db_dir / "sample.db")
                self.assertTrue((db_dir / "sample.db").is_file())

    def test_only_imported_working_copies_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundled"
            source.mkdir()
            db_dir = root / "workspace" / "databases"
            backup_dir = root / "workspace" / "backups"
            db_dir.mkdir(parents=True)
            (source / "sample.db").write_bytes(b"bundled")
            (db_dir / "sample.db").write_bytes(b"bundled")
            (db_dir / "imported.db").write_bytes(b"imported")
            with patch.object(web, "DATA_DIR", source), patch.object(web, "DB_DIR", db_dir), \
                 patch.object(web, "BACKUP_DIR", backup_dir):
                listed = {item["id"]: item for item in web.databases()}
                self.assertFalse(listed["sample.db"]["deletable"])
                self.assertTrue(listed["imported.db"]["deletable"])
                with self.assertRaises(web.HTTPException):
                    web.delete_database("sample.db", web.DeleteDatabase(confirm=True))
                with self.assertRaises(web.HTTPException):
                    web.delete_database("imported.db", web.DeleteDatabase(confirm=False))
                self.assertTrue(web.delete_database(
                    "imported.db", web.DeleteDatabase(confirm=True))["deleted"])
                self.assertFalse((db_dir / "imported.db").exists())
                self.assertTrue((db_dir / "sample.db").exists())

    def test_environment_key_is_never_copied_to_ui_config_or_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder)
            with patch.object(web, "WORKSPACE", location), patch.object(web, "CONFIG_FILE", location / "connection.json"):
                with patch.dict(os.environ, {"SYSTEM_ONE_API_KEY": "test-only-env-key"}):
                    response = web.save_connection(web.ConnectionInput(
                        url="https://example.test/v1/systemone", model="test-model", api_key=""))
                    saved = json.loads((location / "connection.json").read_text())
                    self.assertEqual(saved["profiles"]["jev"]["api_key"], "")
                    self.assertTrue(response["has_key"])
                    self.assertNotIn("api_key", response)
                    self.assertNotIn("test-only-env-key", json.dumps(web.get_connection()))

    def test_provider_profiles_keep_keys_separate_and_laya_needs_no_key(self):
        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder)
            with patch.object(web, "WORKSPACE", location), patch.object(web, "CONFIG_FILE", location / "connection.json"):
                with patch.dict(os.environ, {"SYSTEM_ONE_API_KEY": "jev-env-key"}):
                    web.save_connection(web.ConnectionInput(
                        provider="openjev", url="https://api.codiv.ai/v1/systemone",
                        model="openjev-latest", api_key="openjev-test-key"))
                    self.assertEqual(web.connection_settings()["api_key"], "openjev-test-key")
                    web.save_connection(web.ConnectionInput(
                        provider="laya", url="http://127.0.0.1:7861/v1/systemone",
                        model="", api_key=""))
                    self.assertEqual(web.connection_settings()["api_key"], "")
                    self.assertEqual(web.connection_settings()["model"], "")
                    self.assertFalse(web.connection_settings()["requires_key"])
                    response = web.get_connection()
                    self.assertEqual(response["provider"], "laya")
                    self.assertNotIn("openjev-test-key", json.dumps(response))
                    self.assertNotIn("jev-env-key", json.dumps(response))
                    if os.name != "nt":
                        self.assertEqual((location / "connection.json").stat().st_mode & 0o777, 0o600)
                    web.save_connection(web.ConnectionInput(
                        provider="openjev", url="https://api.codiv.ai/v1/systemone",
                        model="openjev-latest", api_key=""))
                    self.assertEqual(web.connection_settings()["api_key"], "openjev-test-key")

    def test_saved_jev_settings_override_environment_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder)
            with patch.object(web, "WORKSPACE", location), patch.object(web, "CONFIG_FILE", location / "connection.json"):
                with patch.dict(os.environ, {"SYSTEM_ONE_API_KEY": "env-key", "SYSTEM_ONE_MODEL": "env-model",
                                             "SYSTEM_ONE_URL": "https://env.example/v1/systemone"}):
                    web.save_connection(web.ConnectionInput(
                        provider="jev", url="https://saved.example/v1/systemone",
                        model="saved-model", api_key="saved-key"))
                    self.assertEqual(web.connection_settings()["api_key"], "saved-key")
                    self.assertEqual(web.connection_settings()["model"], "saved-model")
                    self.assertEqual(web.connection_settings()["url"], "https://saved.example/v1/systemone")

    def test_legacy_connection_is_migrated_without_losing_its_key(self):
        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder)
            path = location / "connection.json"
            path.write_text(json.dumps({"url": "https://api.codiv.ai/v1/systemone",
                                        "model": "openjev-latest", "api_key": "legacy-test-key"}))
            with patch.object(web, "WORKSPACE", location), patch.object(web, "CONFIG_FILE", path):
                self.assertEqual(web.connection_settings()["provider"], "openjev")
                self.assertEqual(web.connection_settings()["api_key"], "legacy-test-key")
                web.save_connection(web.ConnectionInput(provider="openjev",
                    url="https://api.codiv.ai/v1/systemone", model="openjev-latest"))
                self.assertEqual(json.loads(path.read_text())["profiles"]["openjev"]["api_key"],
                                 "legacy-test-key")

    def test_inspector_overview_returns_highest_five_primary_keys_for_every_table(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundled"
            source.mkdir()
            db_dir = root / "workspace" / "databases"
            backup_dir = root / "workspace" / "backups"
            db_dir.mkdir(parents=True)
            path = db_dir / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE alpha(id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("CREATE TABLE beta(id INTEGER PRIMARY KEY, alpha_id INTEGER, note TEXT, "
                             "FOREIGN KEY(alpha_id) REFERENCES alpha(id))")
                conn.executemany("INSERT INTO alpha(value) VALUES (?)",
                                 [(f"a{i}",) for i in range(1, 8)])
                conn.executemany("INSERT INTO beta(alpha_id, note) VALUES (?, ?)",
                                 [(i, f"b{i}") for i in range(1, 4)])
            with patch.object(web, "DATA_DIR", source), patch.object(web, "DB_DIR", db_dir), \
                 patch.object(web, "BACKUP_DIR", backup_dir):
                result = web.inspect_overview("sample.db")
            self.assertEqual(result["preview_rows"], 5)
            self.assertEqual([table["name"] for table in result["tables"]], ["alpha", "beta"])
            alpha, beta = result["tables"]
            self.assertEqual(alpha["row_count"], 7)
            self.assertEqual(len(alpha["rows"]), 5)
            self.assertEqual(alpha["rows"][0], [7, "a7"])
            self.assertEqual(alpha["preview_order"], ["id"])
            self.assertEqual(beta["row_count"], 3)
            self.assertEqual(len(beta["rows"]), 3)
            self.assertEqual(beta["foreign_keys"][0]["from_column"], "alpha_id")
            self.assertEqual(beta["foreign_keys"][0]["to_table"], "alpha")

    def test_inspector_runs_read_only_query_and_reports_sqlite_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundled"
            source.mkdir()
            db_dir = root / "workspace" / "databases"
            backup_dir = root / "workspace" / "backups"
            db_dir.mkdir(parents=True)
            path = db_dir / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
                conn.executemany("INSERT INTO items(name) VALUES (?)", [("alpha",), ("beta",)])
            with patch.object(web, "DATA_DIR", source), patch.object(web, "DB_DIR", db_dir), \
                 patch.object(web, "BACKUP_DIR", backup_dir):
                result = web.inspect_query("sample.db", web.InspectQueryInput(
                    sql="SELECT id, name FROM items ORDER BY id"))
                self.assertEqual(result["columns"], ["id", "name"])
                self.assertEqual(result["rows"], [(1, "alpha"), (2, "beta")])
                with self.assertRaises(web.HTTPException) as bad:
                    web.inspect_query("sample.db", web.InspectQueryInput(sql="SELECT nope FROM items"))
                self.assertIn("SQLite error", bad.exception.detail)

    def test_inspector_rejects_writes_and_leaves_database_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bundled"
            source.mkdir()
            db_dir = root / "workspace" / "databases"
            backup_dir = root / "workspace" / "backups"
            db_dir.mkdir(parents=True)
            path = db_dir / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
                conn.execute("INSERT INTO items(name) VALUES ('alpha')")
            with patch.object(web, "DATA_DIR", source), patch.object(web, "DB_DIR", db_dir), \
                 patch.object(web, "BACKUP_DIR", backup_dir):
                with self.assertRaises(web.HTTPException):
                    web.inspect_query("sample.db", web.InspectQueryInput(
                        sql="UPDATE items SET name='changed' WHERE id=1"))
            with connect(path) as conn:
                self.assertEqual(conn.execute("SELECT name FROM items WHERE id=1").fetchone()[0], "alpha")


if __name__ == "__main__":
    unittest.main()
