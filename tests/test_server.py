import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import connect, init_db, load_env_file, html_inspection


class DatabaseTests(unittest.TestCase):
    def test_init_db_creates_expected_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            init_db(path)
            with connect(path) as db:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"cases", "responses", "evaluations"}.issubset(names))

    def test_init_db_adds_evidence_column_to_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with connect(path) as db:
                db.execute(
                    """CREATE TABLE evaluations (
                        id INTEGER PRIMARY KEY,
                        response_id INTEGER NOT NULL,
                        evaluator_model TEXT NOT NULL,
                        scores_json TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
            init_db(path)
            with connect(path) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
            self.assertIn("evidence_json", columns)


class HTMLTests(unittest.TestCase):
    def test_nested_tables_and_noncontent(self):
        inspection = html_inspection('<head><style>CSS noise</style><title>Hidden title</title></head>'
                                     '<h1>Title</h1><table><tr><td><ol><li>Step</li></ol>'
                                     '<table><tr><td>Nested</td></tr></table>Tail</td></tr></table>'
                                     '<p>Outside</p><script>secret()</script>')
        blocks = {block['text']: block['in_table'] for block in inspection['blocks']}
        self.assertEqual(blocks, {'Title': False, 'Step': True, 'Nested': True,
                                  'Tail': True, 'Outside': False})
        self.assertEqual(inspection['table_count'], 2)
        self.assertFalse(inspection['rendered'])

class EnvironmentTests(unittest.TestCase):
    def test_load_env_file_reads_quoted_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('JEV_TEST_KEY="secret-value"\n', encoding="utf-8")
            os.environ.pop("JEV_TEST_KEY", None)
            try:
                load_env_file(path)
                self.assertEqual(os.environ["JEV_TEST_KEY"], "secret-value")
            finally:
                os.environ.pop("JEV_TEST_KEY", None)

    def test_load_env_file_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("JEV_TEST_KEY=file-value\n", encoding="utf-8")
            os.environ["JEV_TEST_KEY"] = "shell-value"
            try:
                load_env_file(path)
                self.assertEqual(os.environ["JEV_TEST_KEY"], "shell-value")
            finally:
                os.environ.pop("JEV_TEST_KEY", None)


if __name__ == "__main__":
    unittest.main()
