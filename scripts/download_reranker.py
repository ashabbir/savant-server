#!/usr/bin/env python3
"""CLI utility to download and verify the local BGE reranker model."""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from context.reranker import (
    RERANKER_NAME,
    RERANKER_VERSION,
    RerankerModel,
    download_model,
    resolve_model_dir,
)


def main():
    print(f"Target model: {RERANKER_NAME}:{RERANKER_VERSION}")
    model_dir = resolve_model_dir()
    print(f"Resolved directory: {model_dir}")

    if not model_dir.exists() or not (model_dir / "config.json").exists():
        print("Model weights missing. Downloading...")
        download_model(model_dir)
    else:
        print("Model weights already present.")

    print("Verifying model loading...")
    model = RerankerModel.get()
    if model is None:
        print("ERROR: Failed to load RerankerModel.")
        sys.exit(1)

    test_pairs = [
        ("postgresql database pool", "def get_db_connection(): return pool.acquire()"),
        ("postgresql database pool", "def make_cake(): return oven.bake()"),
    ]
    scores = model._model.predict(test_pairs)
    print("Verification test passed. Test scores:", scores)
    print("Reranker is ready for local and Docker operation.")


if __name__ == "__main__":
    main()
