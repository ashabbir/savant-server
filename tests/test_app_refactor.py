"""TDD tests for app.py refactoring — god function decomposition & error handling."""

import os
import re
import unittest

APP_PATH = os.path.join(os.path.dirname(__file__), "..", "app.py")


class TestWorkspacesContextDecomposition(unittest.TestCase):
    """api_workspaces_context should be broken into helper functions."""

    @classmethod
    def setUpClass(cls):
        with open(APP_PATH, "r") as f:
            cls.src = f.read()

    def test_has_collect_workspace_sessions(self):
        self.assertIn("def _collect_workspace_sessions(", self.src)

    def test_has_collect_session_artifacts(self):
        self.assertIn("def _collect_session_artifacts(", self.src)

    def test_has_build_union_prompt(self):
        self.assertIn("def _build_union_prompt(", self.src)

    def test_has_format_session_detail(self):
        self.assertIn("def _format_session_detail(", self.src)

    def test_route_function_under_30_lines(self):
        """The route handler should be a thin orchestrator."""
        match = re.search(r'def api_workspaces_context\b.*?\{', self.src)
        # Python doesn't use {}, find via indentation
        lines = self.src.split('\n')
        start = None
        for i, line in enumerate(lines):
            if 'def api_workspaces_context(' in line:
                start = i
                break
        self.assertIsNotNone(start, "api_workspaces_context not found")
        # Find end of function (next def at same or lesser indent)
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line.startswith(' ') and not line.startswith('\t'):
                break
            if re.match(r'^def ', line):
                break
            end += 1
        func_lines = end - start
        self.assertLess(func_lines, 30,
                        f"api_workspaces_context is {func_lines} lines — should be thin orchestrator < 30")


class TestConversationParserDecomposition(unittest.TestCase):
    """api_session_conversation should be broken into helper functions."""

    @classmethod
    def setUpClass(cls):
        with open(APP_PATH, "r") as f:
            cls.src = f.read()

    def test_has_parse_conversation_events(self):
        self.assertIn("def _parse_conversation_events(", self.src)

    def test_has_process_assistant_message(self):
        self.assertIn("def _process_assistant_message(", self.src)

    def test_has_process_tool_execution(self):
        self.assertIn("def _process_tool_execution(", self.src)

    def test_has_finalize_stats(self):
        self.assertIn("def _finalize_conversation_stats(", self.src)

    def test_has_new_conversation_stats(self):
        self.assertIn("def _new_conversation_stats(", self.src)

    def test_route_function_under_25_lines(self):
        """The route handler should be a thin orchestrator."""
        lines = self.src.split('\n')
        start = None
        for i, line in enumerate(lines):
            if 'def api_session_conversation(' in line:
                start = i
                break
        self.assertIsNotNone(start)
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line.startswith(' ') and not line.startswith('\t'):
                break
            if re.match(r'^def ', line):
                break
            end += 1
        func_lines = end - start
        self.assertLess(func_lines, 25,
                        f"api_session_conversation is {func_lines} lines — should be < 25")


class TestErrorHandling(unittest.TestCase):
    """No bare except: in the codebase."""

    @classmethod
    def setUpClass(cls):
        with open(APP_PATH, "r") as f:
            cls.src = f.read()

    def test_no_bare_except(self):
        """All except clauses must specify an exception type."""
        for i, line in enumerate(self.src.split('\n'), 1):
            stripped = line.strip()
            if stripped == 'except:' or stripped == 'except :':
                self.fail(f"Bare except: found at line {i}: {stripped}")


if __name__ == "__main__":
    unittest.main()
