"""
Model fingerprinting — stable identity for checkpoint files.

Computes a quick hash from model file contents (header + tensor data)
that survives renames, moves, and copies. Detects architecture from
file headers. Provides lookup in both directions: hash→file, file→hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
from pathlib import Path
from typing import Optional

import logging

import folder_paths

logger = logging.getLogger("promptchain.fingerprint")

_HASHABLE_EXTENSIONS = {".safetensors", ".gguf"}
_MODEL_FOLDER_KEYS = ("checkpoints", "diffusion_models", "unet")
# Optional folders registered by third-party nodes (e.g. ComfyUI-GGUF)
_OPTIONAL_FOLDER_KEYS = ("unet_gguf",)

ARCHITECTURE_PATTERNS = [
    ("__index_timestep_zero__", "qwen_edit"),
    ("vae.conv1.", "qwen_edit"),
    ("time_text_embed.timestep_embedder", "qwen_image"),
    ("context_refiner.", "zimage"),
    ("layers.0.mlp.linear_fc2.weight", "ernie"),
    ("double_blocks.", "flux"),
    ("joint_blocks.", "sd3"),
    ("cond_stage_model.transformer.", "sd15"),
    ("conditioner.embedders.", "sdxl"),
]

_FLUX2_MARKERS = ("flux2", "flux.2", "flux 2", "flux-2")


# ── hashing ───────────────────────────────────────────────────────


def compute_quick_hash(filepath: str) -> Optional[str]:
    """SHA256 fingerprint from header + sampled tensor data. ~1ms per file."""
    try:
        with open(filepath, "rb") as f:
            first_8 = f.read(8)
            if len(first_8) < 8:
                return None
            if first_8[:4] == b"GGUF":
                return _hash_gguf(filepath)
            return _hash_safetensors(f, first_8, filepath)
    except Exception:
        logger.debug("quick hash failed for %s", filepath, exc_info=True)
        return None


_SAMPLE_SIZE = 512 * 1024  # 512KB per sample point
_FULL_HASH_BLOCK = 1024 * 1024  # 1MB read chunks for full-file hash


def compute_full_sha256(filepath: str) -> Optional[str]:
    """Full-file SHA256. Slow (reads entire file) but matches CivitAI's SHA256/AutoV2.
    Returns full 64-char hex digest; slice [:10] for AutoV2."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(_FULL_HASH_BLOCK), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        logger.debug("full SHA256 failed for %s", filepath, exc_info=True)
        return None


def _hash_safetensors(f, first_8: bytes, filepath: str) -> Optional[str]:
    header_size = struct.unpack("<Q", first_8)[0]
    if header_size > 50 * 1024 * 1024:
        return None
    header_json = f.read(header_size)
    file_size = os.path.getsize(filepath)
    tensor_start = 8 + header_size
    tensor_size = file_size - tensor_start

    h = hashlib.sha256()
    h.update(first_8)
    h.update(header_json)

    # Sample from start, middle, and end of tensor data to catch
    # merge-family models that share identical early weights
    h.update(f.read(_SAMPLE_SIZE))
    if tensor_size > _SAMPLE_SIZE * 2:
        f.seek(tensor_start + tensor_size // 2)
        h.update(f.read(_SAMPLE_SIZE))
    if tensor_size > _SAMPLE_SIZE * 3:
        f.seek(max(tensor_start, file_size - _SAMPLE_SIZE))
        h.update(f.read(_SAMPLE_SIZE))

    return h.hexdigest()[:16]


def _hash_gguf(filepath: str) -> Optional[str]:
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            header = f.read(24)
            if len(header) < 24 or header[:4] != b"GGUF":
                return None

            h = hashlib.sha256()
            h.update(header)

            # Sample from start, middle, end of tensor data — same strategy
            # as safetensors. GGUF metadata is variable-length but typically
            # <1MB, so 1MB offset safely clears it.
            data_start = 1024 * 1024
            data_size = file_size - data_start
            if data_size <= 0:
                return None

            f.seek(data_start)
            h.update(f.read(_SAMPLE_SIZE))

            if data_size > _SAMPLE_SIZE * 2:
                f.seek(data_start + data_size // 2)
                h.update(f.read(_SAMPLE_SIZE))

            if data_size > _SAMPLE_SIZE * 3:
                f.seek(max(data_start, file_size - _SAMPLE_SIZE))
                h.update(f.read(_SAMPLE_SIZE))

            return h.hexdigest()[:16]
    except Exception:
        return None


# ── architecture detection ────────────────────────────────────────


def detect_architecture(filepath: str) -> str:
    """Detect model architecture from file header. Returns arch string or 'unknown'."""
    try:
        with open(filepath, "rb") as f:
            magic = f.read(4)
        if magic == b"GGUF":
            return _detect_gguf_arch(filepath)
    except Exception:
        return "unknown"
    return _detect_safetensor_arch(filepath)


def _detect_safetensor_arch(filepath: str) -> str:
    header = _read_safetensor_header(filepath)
    if not header:
        return "unknown"
    all_names = [k for k in header.keys() if k != "__metadata__"]
    # Ideogram 4's marker tensor sits ~200 keys deep in the fp8 checkpoints (leading
    # keys are .weight_scale quant tensors), past the first-50 window below — so check
    # the full key set, mirroring ComfyUI's detect_unet_config.
    if any("embed_image_indicator." in k for k in all_names):
        return "ideogram"
    names_str = " ".join(all_names[:50])
    for pattern, arch in ARCHITECTURE_PATTERNS:
        if pattern in names_str:
            if arch == "flux":
                return _refine_flux_version(header, filepath)
            return arch
    return "unknown"


def _refine_flux_version(header: dict, filepath: str) -> str:
    """Distinguish flux vs flux2 from metadata and filename."""
    metadata = header.get("__metadata__", {})
    spec_arch = metadata.get("modelspec.architecture", "").lower()
    filename_lower = os.path.basename(filepath).lower()
    for marker in _FLUX2_MARKERS:
        if marker in spec_arch or marker in filename_lower:
            return "flux2"
    return "flux"


def _detect_gguf_arch(filepath: str) -> str:
    metadata = _read_gguf_metadata(filepath)
    if not metadata:
        return "unknown"
    arch = metadata.get("general.architecture", "")
    if not arch:
        return "unknown"
    arch_lower = arch.lower()
    if "flux" in arch_lower:
        name = metadata.get("general.name", "").lower()
        filename_lower = os.path.basename(filepath).lower()
        for marker in _FLUX2_MARKERS:
            if marker in name or marker in filename_lower:
                return "flux2"
        return "flux"
    return arch_lower


def _read_safetensor_header(filepath: str) -> Optional[dict]:
    try:
        with open(filepath, "rb") as f:
            first_8 = f.read(8)
            if len(first_8) < 8:
                return None
            header_size = struct.unpack("<Q", first_8)[0]
            if header_size > 50 * 1024 * 1024:
                return None
            return json.loads(f.read(header_size))
    except Exception:
        return None


def _read_gguf_metadata(filepath: str) -> Optional[dict]:
    """Read GGUF metadata keys. Minimal parser — stops after architecture + name."""
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            magic = f.read(4)
            if magic != b"GGUF":
                return None
            _version = struct.unpack("<I", f.read(4))[0]
            _tensor_count = struct.unpack("<Q", f.read(8))[0]
            kv_count = struct.unpack("<Q", f.read(8))[0]

            metadata: dict[str, object] = {}
            target_keys = {"general.architecture", "general.name"}
            for _ in range(min(kv_count, 64)):
                key = _read_gguf_string(f)
                if key is None:
                    break
                value_type = struct.unpack("<I", f.read(4))[0]
                value = _read_gguf_value(f, value_type, file_size=file_size)
                if key and value is not None:
                    metadata[key] = value
                if target_keys.issubset(metadata.keys()):
                    break
            return metadata
    except Exception:
        return None


def _read_gguf_string(f) -> Optional[str]:
    try:
        length = struct.unpack("<Q", f.read(8))[0]
        if length > 1024 * 1024:
            return None
        return f.read(length).decode("utf-8", errors="replace")
    except Exception:
        return None


_GGUF_TYPE_READERS = {
    0: lambda f: struct.unpack("<B", f.read(1))[0],    # uint8
    1: lambda f: struct.unpack("<b", f.read(1))[0],    # int8
    2: lambda f: struct.unpack("<H", f.read(2))[0],    # uint16
    3: lambda f: struct.unpack("<h", f.read(2))[0],    # int16
    4: lambda f: struct.unpack("<I", f.read(4))[0],    # uint32
    5: lambda f: struct.unpack("<i", f.read(4))[0],    # int32
    6: lambda f: struct.unpack("<f", f.read(4))[0],    # float32
    7: lambda f: struct.unpack("<?", f.read(1))[0],    # bool
    8: lambda f: _read_gguf_string(f),                  # string
    10: lambda f: struct.unpack("<Q", f.read(8))[0],   # uint64
    11: lambda f: struct.unpack("<q", f.read(8))[0],   # int64
    12: lambda f: struct.unpack("<d", f.read(8))[0],   # float64
}


# Fixed-width GGUF element sizes for bounds validation.  String and
# array element types are handled separately (variable width).
_GGUF_ELEM_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _read_gguf_value(f, value_type: int, file_size: Optional[int] = None):
    reader = _GGUF_TYPE_READERS.get(value_type)
    if reader:
        return reader(f)
    if value_type == 9:  # array
        elem_type = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<Q", f.read(8))[0]
        elem_reader = _GGUF_TYPE_READERS.get(elem_type)
        if not elem_reader or count > 10000:
            return None
        # Bounds check: a corrupted GGUF could claim a count that would
        # require reading past EOF.  struct.unpack would raise, but only
        # after each element — cap up front for clean rejection.
        elem_size = _GGUF_ELEM_SIZE.get(elem_type)
        if elem_size is not None and file_size is not None:
            required = count * elem_size
            remaining = file_size - f.tell()
            if required > remaining:
                return None
        return [elem_reader(f) for _ in range(count)]
    return None


# ── registry ──────────────────────────────────────────────────────

_lock = threading.Lock()
# single registry dict for atomic swap — readers snapshot the reference under lock
_registry: dict = {
    "by_hash": {},       # hash → {filepath, filename, architecture}
    "by_filename": {},   # lowercase filename → hash
    "by_path": {},       # filepath → hash (scan dedup cache)
    "scanned": False,
}


def _get_cache_dir() -> Path:
    return Path(__file__).parent.parent / "cache"


def _get_index_path() -> Path:
    return _get_cache_dir() / "hash_index.json"


def _get_model_folders() -> list[str]:
    dirs: list[str] = []
    for key in _MODEL_FOLDER_KEYS:
        try:
            dirs.extend(folder_paths.get_folder_paths(key))
        except KeyError:
            pass
    for key in _OPTIONAL_FOLDER_KEYS:
        try:
            dirs.extend(folder_paths.get_folder_paths(key))
        except KeyError:
            pass
    return [d for d in dirs if os.path.isdir(d)]


def _walk_model_files(folder: str) -> list[tuple[str, str]]:
    """Return [(filepath, filename)] for hashable files in folder tree."""
    results = []
    for root, _dirs, files in os.walk(folder):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in _HASHABLE_EXTENSIONS:
                results.append((os.path.join(root, fname), fname))
    return results


def scan_models() -> int:
    """Scan all model folders, hash new files, rebuild registry. Returns model count."""
    global _registry

    # Load existing index as hash cache (avoids re-hashing known files)
    cached_by_path: dict[str, tuple[str, dict]] = {}
    index = _load_index()
    if index:
        for h, info in index.get("by_hash", {}).items():
            cached_by_path[info.get("filepath", "")] = (h, info)

    new_by_hash: dict[str, dict] = {}
    new_by_filename: dict[str, str] = {}
    new_by_path: dict[str, str] = {}
    hashed_count = 0

    for folder in _get_model_folders():
        for filepath, filename in _walk_model_files(folder):
            # Fast path: reuse cached hash if file hasn't changed
            cached = cached_by_path.get(filepath)
            if cached:
                h, info = cached
                try:
                    st = os.stat(filepath)
                    cached_mtime = info.get("mtime", 0)
                    cached_size = info.get("size", -1)
                    if st.st_mtime == cached_mtime and st.st_size == cached_size:
                        # Re-detect a previously-unknown arch (cheap header read, no
                        # re-hash) so fingerprint improvements reclassify old files
                        # without a full cache wipe.
                        if info.get("architecture") == "unknown":
                            info = {**info, "architecture": detect_architecture(filepath)}
                        new_by_hash[h] = info
                        new_by_filename[filename.lower()] = h
                        new_by_path[filepath] = h
                        continue
                except OSError:
                    pass
                # File changed — fall through to re-hash

            # Slow path: hash the file
            h = compute_quick_hash(filepath)
            if not h:
                continue
            arch = detect_architecture(filepath)
            try:
                st = os.stat(filepath)
                mtime, size = st.st_mtime, st.st_size
            except OSError:
                mtime, size = 0, 0
            entry = {
                "filepath": filepath,
                "filename": filename,
                "architecture": arch,
                "mtime": mtime,
                "size": size,
            }
            new_by_hash[h] = entry
            new_by_filename[filename.lower()] = h
            new_by_path[filepath] = h
            hashed_count += 1

    with _lock:
        _registry = {
            "by_hash": new_by_hash,
            "by_filename": new_by_filename,
            "by_path": new_by_path,
            "scanned": True,
        }

    _save_index()

    total = len(new_by_hash)
    if hashed_count:
        print(f"[PromptChain] Scanned {total} model(s), hashed {hashed_count} new file(s)")
    else:
        print(f"[PromptChain] Loaded {total} model(s) from index")
    return total


def _ensure_scanned():
    with _lock:
        scanned = _registry["scanned"]
    if not scanned:
        scan_models()


# ── index persistence ─────────────────────────────────────────────


def _load_index() -> Optional[dict]:
    path = _get_index_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_index():
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        reg = _registry
        # snapshot inner dicts under lock — get_model_identity() can mutate them concurrently
        by_hash_copy = {h: dict(info) for h, info in reg["by_hash"].items()}
        by_path_copy = dict(reg["by_path"])
    index = {
        "by_hash": by_hash_copy,
        "by_path": by_path_copy,
        "scanned_at": int(time.time()),
    }

    try:
        from .api_utils import atomic_write_json
        atomic_write_json(_get_index_path(), index)
    except Exception as e:
        print(f"[PromptChain] Failed to save hash index: {e}")


# ── public API ────────────────────────────────────────────────────


def get_model_identity(filepath: str) -> Optional[dict]:
    """
    Get identity for a model file. Checks registry first, hashes on demand if needed.
    Returns {hash, filepath, filename, architecture} or None.
    """
    _ensure_scanned()

    filename = os.path.basename(filepath)

    # snapshot registry once for consistent reads
    with _lock:
        reg = _registry

    # Try exact path first (disambiguates same-name files in different dirs)
    h = reg["by_path"].get(filepath)
    if h and h in reg["by_hash"]:
        return _public(reg["by_hash"][h], hash=h)

    # Try filename lookup (covers path changes / renames within same dir)
    h = reg["by_filename"].get(filename.lower())
    if h and h in reg["by_hash"]:
        return _public(reg["by_hash"][h], hash=h)

    # Not in registry — hash on demand
    if not os.path.isfile(filepath):
        return None
    h = compute_quick_hash(filepath)
    if not h:
        return None

    # Check if hash is known (file was renamed)
    if h in reg["by_hash"]:
        return _public(reg["by_hash"][h], hash=h)

    # Truly new file — detect arch and register (mutates current registry in-place)
    arch = detect_architecture(filepath)
    try:
        st = os.stat(filepath)
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = 0, 0
    entry = {"filepath": filepath, "filename": filename, "architecture": arch,
             "mtime": mtime, "size": size}
    with _lock:
        _registry["by_hash"][h] = entry
        _registry["by_filename"][filename.lower()] = h
        _registry["by_path"][filepath] = h
    _save_index()
    return _public(entry, hash=h)


_INTERNAL_KEYS = {"mtime", "size"}


def _public(info: dict, **extra) -> dict:
    """Strip internal cache fields from a registry entry."""
    return {k: v for k, v in {**info, **extra}.items() if k not in _INTERNAL_KEYS}


def find_by_hash(model_hash: str) -> Optional[dict]:
    """Find model info by hash. Returns {hash, filepath, filename, architecture} or None."""
    _ensure_scanned()
    with _lock:
        reg = _registry
    info = reg["by_hash"].get(model_hash)
    if info:
        return _public(info, hash=model_hash)
    return None


def find_by_filename(filename: str) -> Optional[dict]:
    """Find model info by filename (case-insensitive). Returns {hash, filepath, filename, architecture} or None."""
    _ensure_scanned()
    normalized = os.path.basename(filename).lower()
    with _lock:
        reg = _registry
    h = reg["by_filename"].get(normalized)
    if h and h in reg["by_hash"]:
        return _public(reg["by_hash"][h], hash=h)
    return None


def list_models() -> list[dict]:
    """All known models. Returns [{hash, filepath, filename, architecture}, ...]."""
    _ensure_scanned()
    with _lock:
        # snapshot under lock — get_model_identity() can mutate by_hash concurrently
        items = list(_registry["by_hash"].items())
    return [_public(info, hash=h) for h, info in items]
