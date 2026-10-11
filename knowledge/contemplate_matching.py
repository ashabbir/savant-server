"""Deterministic candidate selection and conservative cosine matching."""

import math
from collections import defaultdict

from knowledge.contemplate_report import json_value


def normalise(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def text_guard(left: dict, right: dict) -> bool:
    title = normalise(left.get("title"))
    content = normalise(left.get("content"))
    return bool((title and title == normalise(right.get("title"))) or
                (content and content == normalise(right.get("content"))))


def node_text(node: dict) -> str:
    return normalise(node.get("title")) + "\n\n" + normalise(node.get("content"))


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("invalid_vector")
    if not all(math.isfinite(float(item)) for item in (*left, *right)):
        raise ValueError("invalid_vector")
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    if not left_norm or not right_norm or not math.isfinite(left_norm) or not math.isfinite(right_norm):
        raise ValueError("invalid_vector")
    return max(-1.0, min(1.0, sum((float(a) / left_norm) * (float(b) / right_norm) for a, b in zip(left, right))))


def workspaces(metadata: dict) -> set[str]:
    values = metadata.get("workspaces")
    return {item for item in values if isinstance(item, str) and item} if isinstance(values, list) else set()


class CandidateIndex:
    """Text-guard indexes avoid an all-pairs similarity matrix."""

    def __init__(self) -> None:
        self.titles = defaultdict(set)
        self.contents = defaultdict(set)

    def add(self, node: dict) -> None:
        for field, index in (("title", self.titles), ("content", self.contents)):
            value = normalise(node.get(field))
            if value:
                index[(node["node_type"], value)].add(node["node_id"])

    def remove(self, node: dict) -> None:
        for field, index in (("title", self.titles), ("content", self.contents)):
            index[(node["node_type"], normalise(node.get(field)))].discard(node["node_id"])

    def candidates(self, node: dict) -> set[str]:
        return (self.titles.get((node["node_type"], normalise(node.get("title"))), set()) |
                self.contents.get((node["node_type"], normalise(node.get("content"))), set())) - {node["node_id"]}


def root_domains(node_id: str, nodes: dict, adjacency: dict) -> set[str]:
    """Match KnowledgeGraphDB's undirected, five-hop domain scope."""
    visited = {node_id}
    frontier = {node_id}
    domains = set()
    for _ in range(5):
        following = {neighbor for item in frontier for neighbor in adjacency.get(item, ())} - visited
        domains.update(item for item in following if nodes.get(item, {}).get("node_type") == "domain")
        visited.update(following)
        frontier = following
        if not frontier:
            break
    return domains


class SimilarityMatcher:
    def __init__(self) -> None:
        from context.embeddings import MODEL_NAME, MODEL_VERSION, REPO_ID, REVISION, EMBEDDING_DIM
        self.model_identity = {"name": MODEL_NAME, "version": MODEL_VERSION, "repository": REPO_ID,
                               "revision": REVISION, "dimensions": EMBEDDING_DIM}
        self.cache = {}
        self.model = None
        self.error = None

    def prepare(self, nodes: list[dict]) -> None:
        """Encode only possible strict-guard duplicates, outside graph write locks."""
        index = CandidateIndex()
        for node in nodes:
            if node["node_type"] != "domain":
                index.add(node)
        candidates = [node for node in nodes if node["node_type"] != "domain" and index.candidates(node)]
        if not candidates:
            return
        try:
            from context.embeddings import EmbeddingModel
            self.model = EmbeddingModel.get()
        except Exception as exc:
            self.error = f"embedding_unavailable: {exc}"
            return
        texts = sorted({node_text(node) for node in candidates})
        for offset in range(0, len(texts), 32):
            eligible = []
            for value in texts[offset:offset + 32]:
                try:
                    self._validate_length(value)
                    eligible.append(value)
                except ValueError as exc:
                    self.cache[value] = str(exc)
            if eligible:
                try:
                    for value, vector in zip(eligible, self.model.embed(eligible)):
                        self.cache[value] = vector.tolist()
                except Exception as exc:
                    for value in eligible:
                        self.cache[value] = f"embedding_failed: {exc}"

    def _validate_length(self, text: str) -> None:
        if not text.strip():
            raise ValueError("empty_text")
        transformer = self.model._model
        tokens = transformer.tokenizer(text, truncation=False)["input_ids"]
        if len(tokens) > transformer.max_seq_length:
            raise ValueError("input_too_long")

    def vector(self, node: dict, *, refresh: bool = False) -> list[float]:
        value = node_text(node)
        if self.error:
            raise ValueError(self.error)
        if value not in self.cache:
            if not refresh or self.model is None:
                raise ValueError("candidate_changed_since_preparation")
            self._validate_length(value)
            try:
                self.cache[value] = self.model.embed_one(value)
            except Exception as exc:
                raise ValueError(f"embedding_failed: {exc}") from exc
        result = self.cache[value]
        if isinstance(result, str):
            raise ValueError(result)
        return result

    def score(self, left: dict, right: dict, *, refresh: bool = False) -> float:
        return cosine(self.vector(left, refresh=refresh), self.vector(right))


def stable_order(node: dict) -> tuple:
    return (node["status"] != "committed", json_value(node["created_at"]), node["node_id"])
