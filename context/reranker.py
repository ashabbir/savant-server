"""Dedicated Cross-Encoder reranker for high-precision code and context search."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional

logger = logging.getLogger("savant.context.reranker")

RERANKER_NAME = "bge-reranker-base"
RERANKER_VERSION = "v1"
RERANKER_REPO_ID = os.getenv("RERANKER_REPO_ID", "BAAI/bge-reranker-base")
RERANKER_REVISION = os.getenv("RERANKER_REVISION", "main")


def default_model_dir() -> Path:
    override = os.getenv("RERANKER_MODEL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".savant" / "models" / RERANKER_NAME / RERANKER_VERSION


def bundled_model_dir() -> Optional[Path]:
    """Return model directory bundled inside the app or repository."""
    env_path = os.getenv("SAVANT_BUNDLED_RERANKER_DIR")
    if env_path:
        p = Path(env_path)
        if p.exists() and (p / "config.json").exists():
            return p

    dev_path = Path(__file__).parent.parent / "models" / RERANKER_NAME / RERANKER_VERSION
    if dev_path.exists() and (dev_path / "config.json").exists():
        return dev_path
    return None


def resolve_model_dir() -> Path:
    """Find the best available model directory (bundled > user > download target)."""
    bundled = bundled_model_dir()
    if bundled:
        return bundled
    user_dir = default_model_dir()
    if user_dir.exists() and (user_dir / "config.json").exists():
        return user_dir
    return user_dir


def download_model(dest: Optional[Path] = None) -> Path:
    """Download reranker model from Hugging Face."""
    from huggingface_hub import snapshot_download

    target = dest or default_model_dir()
    target.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading reranker model %s to %s...", RERANKER_REPO_ID, target)
    snapshot_download(
        repo_id=RERANKER_REPO_ID,
        revision=RERANKER_REVISION,
        local_dir=str(target),
        allow_patterns=[
            "config.json",
            "model.safetensors",
            "tokenizer*",
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
        ],
        local_dir_use_symlinks=False,
    )
    logger.info("Reranker model downloaded to %s", target)
    return target


class RerankerModel:
    """Singleton Cross-Encoder reranker model with lazy loading."""

    _instance: Optional[RerankerModel] = None
    _lock = Lock()

    def __init__(self, model_dir: Path):
        try:
            import torch
            num_threads = max(1, min(2, (os.cpu_count() or 4) // 2))
            torch.set_num_threads(num_threads)
            if hasattr(torch, "set_num_interop_threads"):
                torch.set_num_interop_threads(1)
        except Exception:
            pass

        from sentence_transformers import CrossEncoder

        logger.info("Loading Cross-Encoder reranker from %s (CPU)", model_dir)
        self._model = CrossEncoder(str(model_dir), device="cpu", max_length=512)

    @classmethod
    def get(cls) -> Optional[RerankerModel]:
        """Get or initialize the singleton reranker model."""
        if os.getenv("SAVANT_ENABLE_RERANKER", "1").lower() not in ("1", "true", "yes"):
            return None

        if cls._instance is not None:
            return cls._instance

        with cls._lock:
            if cls._instance is not None:
                return cls._instance
            try:
                model_dir = resolve_model_dir()
                if not model_dir.exists() or not (model_dir / "config.json").exists():
                    logger.info("Reranker not found locally, downloading...")
                    model_dir = download_model(model_dir)
                cls._instance = RerankerModel(model_dir)
            except Exception as exc:
                logger.warning("Failed to initialize RerankerModel: %s. Reranking will be bypassed.", exc)
                return None
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (primarily for testing)."""
        with cls._lock:
            cls._instance = None

    def rerank(
        self,
        query: str,
        candidates: List[dict[str, Any]],
        text_key: str = "content",
        top_k: int = 10,
    ) -> List[dict[str, Any]]:
        """Score candidate chunks against query and return top_k sorted by relevance."""
        if not candidates or not query:
            return candidates[:top_k]

        pairs = [[query, str(c.get(text_key) or "")] for c in candidates]
        try:
            scores = self._model.predict(pairs, batch_size=16, show_progress_bar=False)
            scored = []
            for score, cand in zip(scores, candidates):
                entry = dict(cand)
                entry["rerank_score"] = round(float(score), 4)
                scored.append(entry)
            scored.sort(key=lambda x: x["rerank_score"], reverse=True)
            return scored[:top_k]
        except Exception as exc:
            logger.warning("Reranking prediction failed: %s. Returning unranked candidates.", exc)
            return candidates[:top_k]
