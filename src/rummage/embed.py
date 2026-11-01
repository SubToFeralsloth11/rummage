"""Local sentence embeddings (bge-small, ONNX on CPU). Downloaded once, then fully offline."""

from __future__ import annotations

import os
import threading

import numpy as np

from . import config

_model = None
_lock = threading.Lock()


def get_model():
    """Load the model, offline from the local cache if possible. Thread-safe."""
    global _model
    with _lock:
        if _model is None:
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            from fastembed import TextEmbedding

            config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            kw = dict(cache_dir=str(config.MODELS_DIR), threads=config.EMBED_THREADS)
            if any(config.MODELS_DIR.iterdir()):
                try:
                    _model = TextEmbedding(config.EMBED_MODEL, local_files_only=True, **kw)
                except Exception:
                    pass
            if _model is None:
                _model = TextEmbedding(config.EMBED_MODEL, **kw)  # first run: download (~70 MB)
        return _model


def is_loaded() -> bool:
    return _model is not None


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    norms[norms == 0] = 1
    return m / norms


def embed_passages(texts: list[str]) -> np.ndarray:
    return _normalize(np.stack(list(get_model().passage_embed(texts, batch_size=32))))


def embed_query(text: str) -> np.ndarray:
    return _normalize(next(iter(get_model().query_embed(text))))
