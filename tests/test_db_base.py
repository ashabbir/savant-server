"""TDD tests for db/base.py shared utilities."""

import json
import os
import re
import unittest

BASE_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "base.py")
DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")


class TestBaseModule(unittest.TestCase):
    """Test the base module exports and behaviour."""

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(BASE_PATH))

    def test_exports_now(self):
        from db.base import _now
        ts = _now()
        self.assertIn("T", ts)
        self.assertIn("+", ts)  # timezone-aware

    def test_now_returns_utc(self):
        from db.base import _now
        ts = _now()
        self.assertTrue(ts.endswith("+00:00"), f"Expected UTC, got {ts}")

    def test_exports_row_to_dict(self):
        from db.base import _row_to_dict
        self.assertIsNone(_row_to_dict(None))

    def test_row_to_dict_simple(self):
        from db.base import _row_to_dict

        class FakeRow:
            def __init__(self, d): self._d = d
            def keys(self): return self._d.keys()
            def __iter__(self): return iter(self._d.items())
            def __getitem__(self, k): return self._d[k]

        row = FakeRow({"id": 1, "name": "test"})
        result = _row_to_dict(row)
        self.assertEqual(result, {"id": 1, "name": "test"})

    def test_row_to_dict_with_json_fields(self):
        from db.base import _row_to_dict

        class FakeRow:
            def __init__(self, d): self._d = d
            def keys(self): return self._d.keys()
            def __iter__(self): return iter(self._d.items())
            def __getitem__(self, k): return self._d[k]

        row = FakeRow({"id": 1, "metadata": '{"key":"val"}', "files": '["a.py"]'})
        result = _row_to_dict(row, json_fields={"metadata": {}, "files": []})
        self.assertEqual(result["metadata"], {"key": "val"})
        self.assertEqual(result["files"], ["a.py"])

    def test_row_to_dict_invalid_json_uses_default(self):
        from db.base import _row_to_dict

        class FakeRow:
            def __init__(self, d): self._d = d
            def keys(self): return self._d.keys()
            def __iter__(self): return iter(self._d.items())
            def __getitem__(self, k): return self._d[k]

        row = FakeRow({"id": 1, "metadata": "not-json"})
        result = _row_to_dict(row, json_fields={"metadata": {}})
        self.assertEqual(result["metadata"], {})

    def test_exports_rows_to_dicts(self):
        from db.base import _rows_to_dicts
        self.assertEqual(_rows_to_dicts([]), [])


class TestDBModulesUseBase(unittest.TestCase):
    """After refactoring, all DB modules should import from db.base."""

    MODULES_SIMPLE = ["workspaces.py", "tasks.py", "notes.py",
                      "merge_requests.py", "jira_tickets.py"]
    MODULES_JSON = ["notifications.py", "experiences.py", "knowledge_graph.py"]

    def _read(self, filename):
        with open(os.path.join(DB_DIR, filename)) as f:
            return f.read()

    def test_simple_modules_import_now_from_base(self):
        for mod in self.MODULES_SIMPLE:
            src = self._read(mod)
            self.assertIn("from db.base import", src,
                          f"{mod} should import from db.base")
            self.assertNotIn("def _now()", src,
                             f"{mod} should NOT define its own _now()")

    def test_json_modules_import_from_base(self):
        for mod in self.MODULES_JSON:
            src = self._read(mod)
            self.assertIn("from db.base import", src,
                          f"{mod} should import from db.base")
            self.assertNotIn("def _now()", src,
                             f"{mod} should NOT define its own _now()")

    def test_no_module_defines_duplicate_row_to_dict(self):
        """No module should have the full boilerplate _row_to_dict — only thin wrappers calling base."""
        for mod in self.MODULES_SIMPLE:
            src = self._read(mod)
            self.assertNotIn("def _row_to_dict(", src,
                             f"{mod} should NOT define its own _row_to_dict()")
        # JSON modules may have thin wrappers, but they must delegate to _base_row
        for mod in self.MODULES_JSON:
            src = self._read(mod)
            if "def _row_to_dict(" in src:
                self.assertIn("_base_row", src,
                              f"{mod} _row_to_dict must delegate to _base_row")

    def test_base_module_under_50_lines(self):
        with open(BASE_PATH) as f:
            lines = len(f.readlines())
        self.assertLess(lines, 50, f"base.py is {lines} lines — keep it lean")


if __name__ == "__main__":
    unittest.main()
