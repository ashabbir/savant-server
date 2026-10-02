#!/usr/bin/env python3
"""CLI utility to download and verify the local stsb-distilbert embedding model."""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from context.embeddings import (
    MODEL_NAME,
    MODEL_VERSION,
    EmbeddingModel,
    download_model,
    resolve_model_dir,
)


def main():
    print(f"Target model: {MODEL_NAME}:{MODEL_VERSION}")
    model_dir = resolve_model_dir()
    print(f"Resolved directory: {model_dir}")

    if not model_dir.exists() or not (model_dir / "config.json").exists():
        print("Model weights missing. Downloading...")
        download_model(model_dir)
    else:
        print("Model weights already present.")

    print("Verifying model loading...")
    model = EmbeddingModel.get()
    if model is None:
        print("ERROR: Failed to load EmbeddingModel.")
        sys.exit(1)

    vec = model.embed_one("def test(): pass")
    print(f"Verification test passed. Generated {len(vec)}-dimensional embedding vector.")
    print("Embedding model is ready for local and Docker operation.")


if __name__ == "__main__":
    main()
