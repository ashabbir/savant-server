"""Embedding model loader and encoder.

Uses stsb-distilbert-base (768-dim) sentence transformer.
Auto-downloads model on first use to ~/.savant/models/.
"""

import logging
import os
from pathlib import Path
from typing import List, Sequence, Union

logger = logging.getLogger(__name__)

# Defaults
MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "stsb-distilbert-base")
MODEL_VERSION = os.getenv("EMBEDDING_VERSION", "v1")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
REPO_ID = os.getenv("EMBEDDING_REPO_ID", "sentence-transformers/stsb-distilbert-base")
REVISION = os.getenv("EMBEDDING_REVISION", "main")


def default_model_dir() -> Path:
    override = os.getenv("EMBEDDING_MODEL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".savant" / "models" / MODEL_NAME / MODEL_VERSION


def bundled_model_dir() -> Path:
    """Return the model directory bundled inside the app (extraResources)."""
    # Check env var set by Electron's main.js
    env_path = os.getenv("SAVANT_BUNDLED_MODEL_DIR")
    if env_path:
        p = Path(env_path)
        if p.exists() and (p / "config.json").exists():
            return p

    # Fallback: relative to this file (dev mode)
    dev_path = Path(__file__).parent.parent / "models" / MODEL_NAME / MODEL_VERSION
    if dev_path.exists() and (dev_path / "config.json").exists():
        return dev_path
    return None


def resolve_model_dir() -> Path:
    """Find the best available model directory (user > bundled > download)."""
    user_dir = default_model_dir()
    if user_dir.exists() and (user_dir / "config.json").exists():
        return user_dir
    bundled = bundled_model_dir()
    if bundled:
        return bundled
    return user_dir  # fallback — will trigger download


def download_model(dest: Path = None) -> Path:
    """Download model from HuggingFace to local directory."""
    from huggingface_hub import snapshot_download  # type: ignore

    target = dest or default_model_dir()
    target.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading embedding model {REPO_ID} to {target}...")
    snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        local_dir=str(target),
        local_dir_use_symlinks=False,
    )
    logger.info(f"Model downloaded to {target}")
    return target


class EmbeddingModel:
    """Singleton embedding model with lazy loading."""

    _instance = None

    def __init__(self, model_dir: Path):
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")

        from .deps import ensure_transformer_deps
        ensure_transformer_deps(auto_install=True)

        # Shim for older sentence_transformers importing deprecated HF APIs
        try:
            import huggingface_hub as _hf
            if not hasattr(_hf, "cached_download"):
                try:
                    from huggingface_hub import hf_hub_download
                    _hf.cached_download = hf_hub_download
                except Exception:
                    pass
        except Exception:
            pass

        from sentence_transformers import SentenceTransformer  # type: ignore
        import numpy as _np

        model_dir = Path(model_dir)
        if not model_dir.exists():
            raise RuntimeError(
                f"Embedding model directory not found: {model_dir}. "
                "It will be downloaded on first use."
            )

        logger.info(f"Loading embedding model from {model_dir} (CPU)")
        self._model = SentenceTransformer(str(model_dir), device="cpu")
        self._np = _np

    @classmethod
    def get(cls) -> "EmbeddingModel":
        if cls._instance is not None:
            return cls._instance

        model_dir = resolve_model_dir()

        # Auto-download if not present anywhere
        if not model_dir.exists() or not (model_dir / "config.json").exists():
            logger.info("Model not found locally or bundled, downloading...")
            model_dir = default_model_dir()
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            os.environ.pop("HF_HUB_OFFLINE", None)
            download_model(model_dir)
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_HUB_OFFLINE"] = "1"

        cls._instance = EmbeddingModel(model_dir)
        return cls._instance

    @classmethod
    def is_available(cls) -> bool:
        """Check if model is downloaded or bundled without loading it."""
        if cls._instance is not None:
            return True
        d = resolve_model_dir()
        return d.exists() and (d / "config.json").exists()

    @classmethod
    def is_loaded(cls) -> bool:
        """Check if model is loaded into memory."""
        return cls._instance is not None

    def embed(self, texts: Union[str, Sequence[str]]) -> "numpy.ndarray":
        """Embed text(s). Returns 2D array of shape (n, 768)."""
        if isinstance(texts, str):
            texts = [texts]
        embeddings = self._model.encode(
            list(texts), batch_size=32, convert_to_numpy=True,
            normalize_embeddings=False, show_progress_bar=False,
        )
        return embeddings.astype(self._np.float32)

    def embed_one(self, text: str) -> List[float]:
        return self.embed(text)[0].tolist()
