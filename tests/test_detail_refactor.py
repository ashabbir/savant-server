"""TDD tests for detail.html refactoring — renderMetaTab decomposition."""
import os
import re
import unittest

DETAIL_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "detail.html")

class TestRenderMetaTabDecomposition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DETAIL_PATH, "r") as f:
            cls.src = f.read()

    def test_renderMetaTab_exists(self):
        self.assertIn("function renderMetaTab", self.src)

    def test_renderMetaTab_under_60_lines(self):
        """After refactoring, renderMetaTab should be a thin orchestrator < 60 lines."""
        match = re.search(r'function renderMetaTab\b.*?\{', self.src)
        self.assertIsNotNone(match)
        start = self.src[:match.start()].count('\n')
        # Find matching closing brace
        depth = 0
        for i, ch in enumerate(self.src[match.start():]):
            if ch == '{': depth += 1
            elif ch == '}': depth -= 1
            if depth == 0:
                end = self.src[:match.start() + i].count('\n')
                break
        self.assertLess(end - start, 60, f"renderMetaTab is still {end - start} lines — should be < 60")

    def test_has_renderSessionInfoCard(self):
        self.assertIn("function _renderSessionInfoCard", self.src)

    def test_has_renderStatsCard(self):
        self.assertIn("function _renderStatsCard", self.src)

    def test_has_renderMcpServersCard(self):
        self.assertIn("function _renderMcpServersCard", self.src)

    def test_has_renderSessionFilesCard(self):
        self.assertIn("function _renderSessionFilesCard", self.src)

    def test_renderMetaTab_calls_helpers(self):
        """renderMetaTab should call the extracted helpers."""
        match = re.search(r'function renderMetaTab\b[^}]*?\{([\s\S]*?)(?=\nfunction |\n// ──)', self.src)
        if not match:
            # Fallback: find the function body
            start_idx = self.src.index('function renderMetaTab')
            body = self.src[start_idx:start_idx + 3000]
        else:
            body = match.group(1)
        self.assertIn("_renderSessionInfoCard", body)
        self.assertIn("_renderStatsCard", body)

    def test_no_function_over_100_lines(self):
        """No render* function should exceed 100 lines."""
        for match in re.finditer(r'function (_render\w+)\b.*?\{', self.src):
            name = match.group(1)
            depth = 0
            for i, ch in enumerate(self.src[match.start():]):
                if ch == '{': depth += 1
                elif ch == '}': depth -= 1
                if depth == 0:
                    start_line = self.src[:match.start()].count('\n')
                    end_line = self.src[:match.start() + i].count('\n')
                    lines = end_line - start_line
                    self.assertLess(lines, 100, f"{name} is {lines} lines — should be < 100")
                    break


class TestDetailGlobalStateReduction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DETAIL_PATH, "r") as f:
            cls.src = f.read()

    def test_session_state_object_exists(self):
        """Global state should be consolidated into a _state or sessionState object."""
        self.assertTrue(
            "const _state" in self.src or "const sessionState" in self.src or "window.sessionData" in self.src,
            "Expected a consolidated state object"
        )


if __name__ == "__main__":
    unittest.main()
