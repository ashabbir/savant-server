"""AbilityValidator — validates include references and detects circular include cycles."""

from __future__ import annotations

from typing import Set

from .ability_index import AbilityIndex


class AbilityValidator:
    """Validates schema requirements and structural dependencies across ability blocks."""

    def __init__(self, index: AbilityIndex) -> None:
        self.index = index

    def check_cycles(self) -> None:
        visited: Set[str] = set()
        stack: Set[str] = set()

        def dfs(node: str) -> None:
            if node in stack:
                raise RuntimeError(f"Circular include detected at '{node}'")
            if node in visited:
                return
            visited.add(node)
            stack.add(node)
            for child in self.index.include_edges.get(node, []):
                if child not in self.index.blocks_by_id:
                    continue
                dfs(child)
            stack.remove(node)

        for nid in list(self.index.blocks_by_id.keys()):
            dfs(nid)

    def validate_includes(self, raise_on_error: bool = True) -> bool:
        try:
            self.check_cycles()
            for bid, edges in self.index.include_edges.items():
                for inc in edges:
                    if inc not in self.index.blocks_by_id:
                        raise RuntimeError(f"Unknown include '{inc}' referenced by '{bid}'")
            return True
        except Exception:
            if raise_on_error:
                raise
            return False

    def validate_all(self) -> None:
        self.validate_includes(raise_on_error=True)
