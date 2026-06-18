"""Shared bge-small loader.

`bucket_search.py` and `modifier_search.py` originally each loaded
their own copy of `BAAI/bge-small-en-v1.5`. With `tag_search.py` added
that's three copies of the same ~30MB weights and three cold-start
loads on boot. This module owns one shared instance behind a lock.

Public API:
    get() -> (model, tokenizer, device)
    get_load_error() -> str | None  # None means "not yet attempted or
                                    # currently in transient retry state"

Behavior matches the pattern the call-sites already implemented locally:
- ModuleNotFoundError caches a hard failure (won't retry without pip install)
- ImportError / OSError leave load_error unset so the next call retries
  (handles boot-time circular-import races between torch and joblib/loky)
- Any other exception caches as a hard failure
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("promptchain.embed")

MODEL_ID = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

_lock = threading.RLock()
_state: dict[str, Any] = {
    "ready": False,
    "model": None,
    "tokenizer": None,
    "device": None,
    "load_error": None,
}


def get_load_error() -> str | None:
    return _state["load_error"]


def get() -> tuple[Any, Any, str] | None:
    """Load (or return cached) shared encoder. Returns None on failure;
    caller is expected to degrade gracefully (search returns empty list
    rather than raising)."""
    with _lock:
        if _state["ready"]:
            return _state["model"], _state["tokenizer"], _state["device"]
        if _state["load_error"]:
            return None
        try:
            import torch  # noqa: F401  (only checking availability here)
            from transformers import AutoTokenizer, AutoModel
        except ModuleNotFoundError as e:
            _state["load_error"] = f"missing dependency: {e}"
            logger.warning(
                "embed_model disabled: %s. Install with: pip install transformers",
                e,
            )
            return None
        except ImportError as e:
            # Partial-init / circular import — torch and its joblib/loky
            # transitive deps race against each other on cold ComfyUI
            # boot. Don't cache; the next request after the import cycle
            # resolves will succeed.
            logger.warning(
                "embed_model: deferred import (%s) — will retry on next request",
                e,
            )
            return None
        except ValueError as e:
            # joblib double-registration: when a prior import partially
            # initialized joblib (e.g. boot-time bucket_search.warmup
            # failed mid-import), the next transformers import re-registers
            # zlib and joblib raises ValueError. Treat as transient — the
            # process state is already broken, but the search caller will
            # degrade to alias-only matching and the request continues.
            logger.warning(
                "embed_model: deferred import (joblib state issue: %s) — "
                "search will degrade to alias-only this request",
                e,
            )
            return None
        try:
            # bge-small is ~30MB on GPU — negligible against image-gen VRAM
            # budgets, and a 5090 rebuilds the 14k-row tag index in ~30s
            # vs 5-10 min on CPU. Fall back to CPU when CUDA isn't there.
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
            model = AutoModel.from_pretrained(MODEL_ID).to(device).eval()
            _state["tokenizer"] = tokenizer
            _state["model"] = model
            _state["device"] = device
            _state["ready"] = True
            logger.info("embed_model: loaded %s on %s", MODEL_ID, device)
            return model, tokenizer, device
        except OSError as e:
            # HF download / disk error — transient (network/lock). Retry.
            logger.warning(
                "embed_model: deferred fetch (%s) — will retry on next request",
                e,
            )
            return None
        except Exception as e:
            _state["load_error"] = str(e)
            logger.exception("embed_model: load failed")
            return None


def embed(texts: list[str], batch_size: int = 32):
    """L2-normalized CLS-pooled embeddings for `texts`. Returns torch
    tensor [N, 384]. Returns None if the model isn't loaded.

    Note: bge-small uses CLS pooling (per the model card). E5 family
    requires mean pooling — if we ever switch models, callers may need
    their own embed function. Today all three consumers (bucket, modifier,
    tag) use bge-small, so this central embed is fine."""
    loaded = get()
    if loaded is None:
        return None
    model, tokenizer, device = loaded

    import torch
    import torch.nn.functional as F

    out_chunks = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        enc = tokenizer(
            chunk, padding=True, truncation=True, max_length=512,
            return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            out = model(**enc)
        cls = out.last_hidden_state[:, 0]
        out_chunks.append(F.normalize(cls, p=2, dim=1))
    return torch.cat(out_chunks, dim=0)
