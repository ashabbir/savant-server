"""Deterministic candidate selection and conservative cosine matching."""

import math

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

    def prepare(self, nodes: list[dict]) -> None:
        """Reuse durable model/text identities; encode missing candidate vectors."""
        from db.contemplate import ContemplateDB, fingerprint
        texts = sorted({node_text(node) for node in nodes if node["node_type"] != "domain"})
        identities = {text: fingerprint([self.model_identity, text]) for text in texts}
        cached = ContemplateDB.cached_vectors(list(identities.values()))
        self.cache.update({text: cached[key] for text, key in identities.items() if key in cached})
        missing = [text for text in texts if text not in self.cache]
        if not missing:
            return
        try:
            from context.embeddings import EmbeddingModel
            self.model = EmbeddingModel.get()
        except Exception as exc:
            for text in missing:
                self.cache[text] = f"embedding_unavailable: {exc}"
            return
        vectors = {}
        for offset in range(0, len(missing), 32):
            eligible = []
            for value in missing[offset:offset + 32]:
                try:
                    self._validate_length(value)
                    eligible.append(value)
                except ValueError as exc:
                    self.cache[value] = str(exc)
            if not eligible:
                continue
            try:
                encoded = list(self.model.embed(eligible))
                if len(encoded) != len(eligible):
                    raise ValueError("incomplete_embedding_batch")
                for value, vector in zip(eligible, encoded):
                    vector = vector.tolist() if hasattr(vector, "tolist") else list(vector)
                    cosine(vector, vector)
                    self.cache[value] = vector
                    vectors[identities[value]] = vector
            except Exception as exc:
                for value in eligible:
                    self.cache[value] = f"embedding_failed: {exc}"
        ContemplateDB.save_vectors(vectors)

    def _validate_length(self, text: str) -> None:
        if not text.strip():
            raise ValueError("empty_text")
        transformer = getattr(self.model, "_model", None)
        if transformer is None:
            return
        tokens = transformer.tokenizer(text, truncation=False)["input_ids"]
        if len(tokens) > transformer.max_seq_length:
            raise ValueError("input_too_long")

    def vector(self, node: dict, *, refresh: bool = False) -> list[float]:
        value = node_text(node)
        if value not in self.cache:
            if not refresh:
                raise ValueError("candidate_changed_since_preparation")
            if self.model is None:
                from context.embeddings import EmbeddingModel
                try:
                    self.model = EmbeddingModel.get()
                except Exception as exc:
                    raise ValueError(f"embedding_unavailable: {exc}") from exc
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
