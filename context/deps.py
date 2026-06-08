"""Dependency helpers for auto-installing ML dependencies on first use."""

import importlib.util
import subprocess
import sys
from typing import Iterable


def _have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _pip_install(pkgs: Iterable[str]) -> None:
    args = [sys.executable, "-m", "pip", "install", "--upgrade",
            "--break-system-packages", "--quiet"]
    args.extend(list(pkgs))
    subprocess.run(args, check=True)


def ensure_transformer_deps(auto_install: bool = True) -> None:
    """Ensure sentence-transformers stack is importable.

    Auto-installs if missing and auto_install=True.
    Raises RuntimeError with guidance on failure.
    """
    if _have_module("sentence_transformers"):
        # Check for huggingface_hub compatibility
        try:
            from packaging.version import Version
            from importlib import metadata as importlib_metadata
            try:
                st_ver = Version(importlib_metadata.version("sentence-transformers"))
            except Exception:
                st_ver = Version("0")
            try:
                hf_ver = Version(importlib_metadata.version("huggingface_hub"))
            except Exception:
                hf_ver = Version("0")
            if hf_ver >= Version("0.34.0") and st_ver < Version("5.0.0") and auto_install:
                _pip_install(["sentence-transformers>=5.0.0"])
        except Exception:
            pass
        return

    if not auto_install:
        raise RuntimeError(
            "sentence-transformers is required but not installed. "
            "Run: python -m pip install 'sentence-transformers>=2.2.2' 'torch>=2.0.0'"
        )

    try:
        _pip_install(["torch>=2.0.0"])
    except Exception as e:
        raise RuntimeError(f"Failed to install torch: {e}") from e

    try:
        _pip_install([
            "sentence-transformers>=5.0.0",
            "transformers>=4.24.0",
            "huggingface_hub>=0.20.0",
            "tokenizers", "safetensors",
            "numpy>=1.23.0", "nltk", "scikit-learn", "scipy",
        ])
    except Exception as e:
        raise RuntimeError(f"Failed to install sentence-transformers stack: {e}") from e

    if not _have_module("sentence_transformers"):
        raise RuntimeError(
            "sentence-transformers unavailable after installation. "
            "Check your Python interpreter."
        )
