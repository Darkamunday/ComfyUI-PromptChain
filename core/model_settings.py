"""
Per-model settings storage — keyed by fingerprint hash.

Three layers:
  System:     data/models/{readable_name}.json — shipped presets (read-only)
  Discovered: cache/models/{readable_name}.json — auto-detected via CivitAI
  User:       {user_dir}/PromptChain/models/{hash}.json — user overrides

Each config embeds quick_hash and sha256 in its files array. Lookups go
through a hash-based config index, not filenames.

User overrides discovered overrides system. On save, only the delta
from the base config is stored so updates still propagate.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import folder_paths

from . import fingerprint
from .api_utils import atomic_write_json

logger = logging.getLogger("promptchain.model_settings")

_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def _ascii_safe(text: str) -> str:
    # Windows stdout is often cp1252; emoji/non-latin chars in log args
    # raise UnicodeEncodeError inside the handler. Strip to ASCII for logs.
    return str(text).encode("ascii", "replace").decode("ascii")


def _system_dir() -> Path:
    return Path(__file__).parent.parent / "data" / "models"


def _discovered_dir() -> Path:
    return Path(__file__).parent.parent / "cache" / "models"


def _user_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "models"


# ── config index: hash → config data ─────────────────────────────
# Built lazily from system + discovered configs. Maps quick_hash,
# sha256, and filename to config data for O(1) lookups. Replaces the
# old filename-based reverse index.

_config_index: Optional[dict] = None


def _get_config_index() -> dict:
    global _config_index
    if _config_index is not None:
        return _config_index
    _config_index = _build_config_index()
    return _config_index


def invalidate_config_index():
    """Force rebuild on next lookup (e.g. after adding/removing configs)."""
    global _config_index
    _config_index = None


def _build_config_index() -> dict:
    """Scan discovered + system configs, build hash and filename indexes.

    Discovered is scanned first so curated system configs take priority
    when both exist for the same hash.
    """
    index = {
        "by_quick_hash": {},  # quick_hash → config dict
        "by_sha256": {},      # sha256 → config dict
        "by_filename": {},    # filename.lower() → config dict
    }
    # Discovered first, then system — system overwrites, giving curated
    # configs priority over auto-detected ones.
    for directory in (_discovered_dir(), _system_dir()):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.name.startswith("_"):
                continue
            data = _read_json(path)
            if not data:
                continue

            # Legacy hash-named configs without files array:
            # treat stem as quick_hash
            if "files" not in data and _HASH_RE.match(path.stem):
                index["by_quick_hash"][path.stem] = data
                continue

            if "files" not in data:
                continue

            _index_file_entries(index, data)

    return index


def _index_file_entries(index: dict, config: dict):
    """Add a config's file entries to the index by hash and filename."""
    for entry in config.get("files", []):
        _index_single_entry(index, entry, config)
        for variant in entry.get("variants", []):
            _index_single_entry(index, variant, config)


def _index_single_entry(index: dict, entry: dict, config: dict):
    qh = entry.get("quick_hash")
    if qh:
        index["by_quick_hash"][qh] = config
    sha = entry.get("sha256")
    if sha:
        index["by_sha256"][sha] = config
    fname = entry.get("filename")
    if fname:
        key = fname.lower()
        existing = index["by_filename"].get(key)
        # Two configs claiming the same filename is ambiguous; keep first
        # (deterministic by scan order) and surface the conflict so the
        # user can see why a particular config won.
        if existing is not None and existing is not config:
            logger.debug(
                "filename collision %s: kept %s, ignored %s",
                fname,
                _ascii_safe(existing.get("display_name", "?")),
                _ascii_safe(config.get("display_name", "?")),
            )
            return
        index["by_filename"][key] = config


# Fields that are specific to a single version of a model — must come
# from the new recognition, never inherited from a sibling config.
# Everything else (model_name, trigger, negative, prompt_style, nodes
# ranges / options, tag_sources, notes, etc.) counts as curated
# model-level data worth inheriting.
VERSION_SPECIFIC_KEYS = frozenset({
    "version",
    "release_date",
    "civitai_version_id",
    "download_url",
    "files",
    "description",   # CivitAI returns a per-version description
    "nsfw_level",    # can differ between versions
})


def find_sibling_config(civitai_model_id, exclude_quick_hash: Optional[str] = None) -> Optional[dict]:
    """Return the oldest existing config sharing this civitai_model_id.

    Used during recognition of a new version so it can inherit the
    sibling's curated fields (model_name, trigger words, node ranges,
    etc.) instead of saving a bare-bones CivitAI dump.  Oldest wins
    because older configs reflect the page before the creator started
    embellishing it, and curated system presets generally have their
    release_date already set.
    """
    if civitai_model_id is None:
        return None
    try:
        target = int(civitai_model_id)
    except (TypeError, ValueError):
        return None
    best_date = "9999-99-99"
    best: Optional[dict] = None
    seen: set[int] = set()
    index = _get_config_index()
    for cfg in index.get("by_quick_hash", {}).values():
        if id(cfg) in seen:
            continue
        seen.add(id(cfg))
        try:
            if int(cfg.get("civitai_model_id") or 0) != target:
                continue
        except (TypeError, ValueError):
            continue
        if exclude_quick_hash:
            files = cfg.get("files") or []
            if any(f.get("quick_hash") == exclude_quick_hash for f in files):
                continue
            # Legacy hash-named configs: the filename stem is the hash.
            # _build_config_index indexes them without a files array so
            # we can't check that path; fall back to sniffing the index
            # key that holds this config.
        date = cfg.get("release_date") or "9999-99-99"
        if date < best_date:
            best_date = date
            best = cfg
    return best


def find_curated_hash_sibling(config: dict) -> Optional[dict]:
    """Find a curated config sharing a file hash with this one.

    CivitAI never returns a 'family', and a CivitAI dump that is byte-identical
    to a curated preset often can't match it by civitai_model_id (HF-sourced
    presets carry none). Matching on the exact file hash lets the discovered
    config inherit the curated identity fields — family above all. A shared
    quick_hash/sha256 means identical file bytes, so it's the same model.
    Only returns a candidate that actually has 'family' (i.e. a curated source),
    never another bare auto-discovered config.
    """
    index = _get_config_index()

    def _candidates(entry):
        for bucket, key in (("by_quick_hash", entry.get("quick_hash")),
                            ("by_sha256", entry.get("sha256"))):
            if key:
                yield index[bucket].get(key)

    for entry in config.get("files", []):
        sources = [entry] + entry.get("variants", [])
        for src in sources:
            for cand in _candidates(src):
                if cand is not None and cand is not config and cand.get("family"):
                    return cand
    return None


def inherit_from_sibling(new_config: dict, sibling: dict) -> dict:
    """Merge a sibling config's curated fields into a freshly-built
    version config.  New wins for version-specific keys; sibling wins
    for everything else.  display_name regenerates from the canonical
    model_name plus the new version's label so it stays consistent."""
    merged = dict(new_config)
    for key, value in sibling.items():
        if key in VERSION_SPECIFIC_KEYS:
            continue
        if key.startswith("_"):
            continue
        merged[key] = value
    name = merged.get("model_name") or merged.get("display_name", "")
    version = merged.get("version", "")
    if name:
        merged["display_name"] = f"{name} - {version}" if version else name
    return merged


# Back-compat alias: previous code called find_canonical_model_name
# before we broadened the inheritance to whole configs.
def find_canonical_model_name(civitai_model_id) -> Optional[str]:
    sib = find_sibling_config(civitai_model_id)
    if not sib:
        return None
    return sib.get("model_name") or sib.get("display_name")


def normalize_civitai_model_names():
    """Heal discovered configs on boot: for each one that shares a
    civitai_model_id with a sibling (curated system config, or older
    auto-discovered config), re-inherit the sibling's curated fields.

    Runs after scan_models so the config index is primed.  Only touches
    files in _discovered_dir() — system configs stay read-only.  Fixes
    both the model_name inconsistency (CivitAI page rename drift) and
    the missing curated fields (trigger words, negative prompt, node
    ranges, tag_sources, prompt_style) on versions that were recognized
    before sibling-inheritance shipped.  Idempotent — no writes when
    the config is already aligned with its sibling.
    """
    discovered = _discovered_dir()
    if not discovered.is_dir():
        return

    changed_any = False
    for path in discovered.glob("*.json"):
        cfg = _read_json(path)
        if not cfg:
            continue

        # Sibling lookup uses the full index so a curated system
        # preset for an older version counts as the canonical source
        # for a freshly-downloaded new version.
        sibling = None
        mid = cfg.get("civitai_model_id")
        if mid is not None:
            my_hash = None
            for f in cfg.get("files", []):
                qh = f.get("quick_hash")
                if qh:
                    my_hash = qh
                    break
            sibling = find_sibling_config(mid, exclude_quick_hash=my_hash)
        # No civitai sibling (or none at all): inherit from a hash-identical
        # curated preset instead, so 'family' lands on CivitAI dumps of models
        # that ship a curated config without a civitai_model_id (e.g. Z-Image).
        if not sibling or sibling is cfg:
            sibling = find_curated_hash_sibling(cfg)
        if not sibling or sibling is cfg:
            continue

        merged = inherit_from_sibling(cfg, sibling)
        if merged == cfg:
            continue

        print(f"[PromptChain] inheriting sibling config for civitai model {mid}: "
              f"{_ascii_safe(path.name)} "
              f"(name '{_ascii_safe(cfg.get('model_name','?'))}' -> "
              f"'{_ascii_safe(merged.get('model_name','?'))}')")
        atomic_write_json(path, merged)
        changed_any = True

    if changed_any:
        invalidate_config_index()


def find_config_by_hash(model_hash: str) -> Optional[dict]:
    """Look up a config by quick_hash, checking system + discovered configs."""
    return _get_config_index()["by_quick_hash"].get(model_hash)


def find_config_by_sha256(sha256: str) -> Optional[dict]:
    """Look up a config by full SHA256, checking system + discovered configs."""
    return _get_config_index()["by_sha256"].get(sha256)


def find_config_by_filename(filename: str) -> Optional[dict]:
    """Look up a config by model filename."""
    return _get_config_index()["by_filename"].get(filename.lower())


def _get_primary_hash(config: dict) -> Optional[str]:
    """Get the quick_hash of the primary file (first diffusion_models/unet/checkpoints entry)."""
    for entry in config.get("files", []):
        if entry.get("folder") not in ("diffusion_models", "unet", "checkpoints"):
            continue
        qh = entry.get("quick_hash")
        if qh:
            return qh
        for v in entry.get("variants", []):
            qh = v.get("quick_hash")
            if qh:
                return qh
    return None


def _file_installed(file_entry: dict, folder_type: str) -> bool:
    """Check if a file is installed — prefer hash match, fall back to filename+size.

    Size check guards against truncated downloads that landed at the final
    path without a .part suffix: without it, a partial file reads as
    installed and its catalog entry vanishes, making resume impossible.
    """
    qh = file_entry.get("quick_hash")
    if qh and fingerprint.find_by_hash(qh):
        return True
    fname = file_entry.get("filename")
    if fname:
        return _file_exists_in_folder(fname, folder_type, file_entry.get("size_bytes", 0))
    return False


def load(model_hash: str) -> Optional[dict]:
    """Load settings for a model hash. User overrides system."""
    user_config = _read_json(_user_dir() / f"{model_hash}.json")

    # Config index handles both hash-named and readable-named configs,
    # system and discovered, in one lookup.
    base_config = _find_base_config(model_hash)

    if not user_config and not base_config:
        return None

    if not base_config:
        return user_config

    if not user_config:
        return base_config

    # Merge: base (system+discovered), user overlay.
    # For 'nodes', merge per-node-type so base nodes the user
    # hasn't touched still appear.
    merged = {**base_config, **user_config}
    base_nodes = base_config.get("nodes", {})
    user_nodes = user_config.get("nodes", {})
    merged_nodes = {**base_nodes}
    for node_type, widgets in user_nodes.items():
        merged_nodes[node_type] = {**merged_nodes.get(node_type, {}), **widgets}
    merged["nodes"] = merged_nodes
    return merged


def _find_base_config(model_hash: str) -> Optional[dict]:
    """Find the base config (system or discovered) for a model hash.

    Checks the config index by quick_hash first, then falls back to
    filename matching via the fingerprint index. For multi-file models,
    verifies all required files are installed before returning.
    """
    config = find_config_by_hash(model_hash)
    if not config:
        # Fallback: configs without quick_hash (e.g. HuggingFace-only models)
        # can still be matched by filename from the fingerprint scan
        fp_info = fingerprint.find_by_hash(model_hash)
        if fp_info:
            config = find_config_by_filename(fp_info["filename"])
    if config:
        if "files" in config and not _all_files_installed(config["files"]):
            import logging
            logging.getLogger("promptchain.model_settings").info(
                "config %r found but files incomplete; hiding until install completes",
                config.get("display_name") or config.get("_hash", "?"),
            )
            return None
        return config
    return None


def load_bulk(hashes: list[str]) -> dict[str, dict]:
    """Load settings for multiple hashes. Returns {hash: config} (omits misses)."""
    result = {}
    for h in hashes:
        config = load(h)
        if config:
            result[h] = config
    return result


def save(model_hash: str, config: dict):
    """Save user settings — stores only the delta from system defaults."""
    config = {k: v for k, v in config.items() if not k.startswith("_")}

    user_dir = _user_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    system_config = find_config_by_hash(model_hash)
    delta = _compute_delta(config, system_config) if system_config else config

    if not delta and system_config:
        path = user_dir / f"{model_hash}.json"
        if path.exists():
            path.unlink()
        return

    path = user_dir / f"{model_hash}.json"
    atomic_write_json(path, delta)


def save_discovered(model_hash: str, config: dict,
                     sha256: Optional[str] = None):
    """Save auto-discovered settings (CivitAI recognition) to cache.

    Generates a readable filename from model metadata. Embeds quick_hash
    and sha256 into the files array so the config is self-identifying.
    """
    config = {k: v for k, v in config.items() if not k.startswith("_")}

    # Ensure files array with hashes exists
    if "files" not in config:
        fp_info = fingerprint.find_by_hash(model_hash)
        config["files"] = [{
            "label": "Checkpoint",
            "folder": "checkpoints",
            "filename": fp_info["filename"] if fp_info else None,
            "quick_hash": model_hash,
            "sha256": sha256,
        }]
    else:
        _backfill_hashes(config["files"], model_hash, sha256)

    slug = _make_config_slug(config, model_hash)

    discovered_dir = _discovered_dir()
    discovered_dir.mkdir(parents=True, exist_ok=True)
    path = discovered_dir / f"{slug}.json"
    atomic_write_json(path, config)
    invalidate_config_index()


def _make_config_slug(config: dict, fallback: str) -> str:
    """Generate a readable filename slug from model metadata."""
    name = config.get("model_name", "")
    version = config.get("version", "")
    if name:
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if version:
            slug += "_" + re.sub(r"[^a-z0-9]+", "_", version.lower()).strip("_")
        return slug[:60]
    return fallback


def _backfill_hashes(files: list[dict], quick_hash: str,
                     sha256: Optional[str]):
    """Add quick_hash/sha256 to the first primary file entry that lacks them."""
    for entry in files:
        if entry.get("folder") not in ("diffusion_models", "unet", "checkpoints"):
            continue
        if entry.get("variants"):
            for v in entry["variants"]:
                if not v.get("quick_hash"):
                    v["quick_hash"] = quick_hash
                    if sha256:
                        v["sha256"] = sha256
                    return
        else:
            if not entry.get("quick_hash"):
                entry["quick_hash"] = quick_hash
                if sha256:
                    entry["sha256"] = sha256
            return


def delete(model_hash: str) -> bool:
    """Delete user settings for a model hash. Returns True if deleted."""
    path = _user_dir() / f"{model_hash}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def has_user_config(model_hash: str) -> bool:
    return (_user_dir() / f"{model_hash}.json").exists()


def has_system_config(model_hash: str) -> bool:
    return find_config_by_hash(model_hash) is not None


def _all_config_dirs() -> tuple[Path, ...]:
    return (_system_dir(), _discovered_dir(), _user_dir())


def list_model_names() -> list[str]:
    """Return sorted unique model names across all model configs."""
    names = set()
    for directory in _all_config_dirs():
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.name.startswith("_"):
                continue
            data = _read_json(path)
            name = data.get("model_name") or data.get("display_name") if data else None
            if name:
                names.add(name)
    return sorted(names)


def list_versions(model_name: str) -> list[str]:
    """Return sorted unique version strings for a given model name."""
    versions = set()
    for directory in _all_config_dirs():
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.name.startswith("_"):
                continue
            data = _read_json(path)
            if not data:
                continue
            name = data.get("model_name") or data.get("display_name")
            if name == model_name and data.get("version"):
                versions.add(data["version"])
    return sorted(versions)


def list_version_details(model_name: str) -> list[dict]:
    """Version objects with hash, filename, and release_date for a model name.

    Sorted by release_date descending (empty dates last), then version descending.
    """
    seen_hashes: set[str] = set()
    versions: list[dict] = []

    # Scan config index (covers system + discovered with any naming scheme)
    for _qh, data in _get_config_index()["by_quick_hash"].items():
        name = data.get("model_name") or data.get("display_name")
        if name != model_name or not data.get("version"):
            continue
        primary_hash = _get_primary_hash(data) or _qh
        if primary_hash in seen_hashes:
            continue
        fp_info = fingerprint.find_by_hash(primary_hash)
        if not fp_info:
            continue
        seen_hashes.add(primary_hash)
        versions.append({
            "version": data["version"],
            "hash": primary_hash,
            "filename": fp_info["filename"],
            "release_date": data.get("release_date", ""),
        })

    # Also check user overrides (still hash-named)
    user_dir = _user_dir()
    if user_dir.is_dir():
        for path in user_dir.glob("*.json"):
            if path.name.startswith("_"):
                continue
            model_hash = path.stem
            if model_hash in seen_hashes:
                continue
            data = load(model_hash)
            if not data:
                continue
            name = data.get("model_name") or data.get("display_name")
            if name != model_name or not data.get("version"):
                continue
            fp_info = fingerprint.find_by_hash(model_hash)
            if not fp_info:
                continue
            seen_hashes.add(model_hash)
            versions.append({
                "version": data["version"],
                "hash": model_hash,
                "filename": fp_info["filename"],
                "release_date": data.get("release_date", ""),
            })

    def sort_key(v):
        return (1 if v["release_date"] else 0, v["release_date"], v["version"])

    versions.sort(key=sort_key, reverse=True)
    return versions


def _file_exists_in_folder(filename: str, folder_type: str, min_size: int = 0) -> bool:
    """True if filename exists in folder_type and is at least min_size bytes.

    A truncated download left at its final path (no .part suffix) would
    otherwise pass mere-existence checks and make its catalog entry
    disappear, blocking the user from resuming it.
    """
    try:
        for folder in folder_paths.get_folder_paths(folder_type):
            path = os.path.join(folder, filename)
            if not os.path.isfile(path):
                continue
            if min_size and os.path.getsize(path) < min_size:
                continue
            return True
    except Exception:
        pass
    return False


def _all_files_installed(files: list[dict]) -> bool:
    """True when every file entry has at least one present file on disk.

    Checks by hash first (via fingerprint registry), falls back to filename
    for files not in the fingerprint scan (text encoders, VAEs). Variant
    entries need at least one variant present.
    """
    for entry in files:
        folder_type = entry.get("folder", "")
        if not folder_type:
            continue

        variants = entry.get("variants")
        if variants:
            if not any(_file_installed(v, folder_type) for v in variants):
                return False
        elif entry.get("filename") or entry.get("quick_hash"):
            if not _file_installed(entry, folder_type):
                return False
    return True


def _base_version_key(version: str) -> tuple:
    """Parse version string into a comparable tuple, ignoring precision suffixes.

    '16.0 fp16' → (16, 0), '14.1' → (14, 1), 'v2' → (2,)
    """
    base = re.split(r"\s+", version)[0]
    parts = re.findall(r"\d+", base)
    return tuple(int(p) for p in parts) if parts else (0,)


def _collect_uninstalled_presets() -> list[dict]:
    """Collect system presets whose required files aren't all on disk.

    Mirrors load()'s completeness check so partially-installed configs
    (primary present, supplemental files missing) surface as downloadable
    rather than disappearing between load() hiding them and the catalog
    treating them as installed.
    """
    entries = []
    seen = set()

    system_dir = _system_dir()
    if not system_dir.is_dir():
        return entries

    for path in system_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        data = _read_json(path)
        if not data or "files" not in data:
            continue

        if _all_files_installed(data["files"]):
            continue

        primary_hash = _get_primary_hash(data)

        dedup_key = (data.get("display_name", ""), data.get("version", ""))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        entry = {
            "hash": primary_hash or path.stem,
            "display_name": data.get("display_name", ""),
            "model_name": data.get("model_name", ""),
            "version": data.get("version", ""),
            "architecture": data.get("architecture", ""),
            "family": data.get("family", ""),
            "description": data.get("description", ""),
            "files": data["files"],
        }
        if data.get("civitai_model_id"):
            entry["civitai_model_id"] = data["civitai_model_id"]
        if data.get("thumbnail"):
            entry["thumbnail"] = data["thumbnail"]
        entries.append(entry)

    return entries


def list_catalog() -> list[dict]:
    """Return system presets whose required files aren't all on disk yet.

    Every uninstalled version newer than the user's newest installed one is
    returned (flat, one entry per version/precision) so the picker can group
    them under a single model_name with a version submenu. Versions at or
    below what's installed are dropped.
    """
    entries = _collect_uninstalled_presets()

    # Build installed version map: model_name → highest base version.
    # A truncated file landing at its final path gets fingerprinted and
    # would otherwise resolve to its config via filename, bumping
    # installed_max and hiding the model's own catalog entry behind the
    # "already have this version" dedup. Only count configs whose
    # required files are all present at full size.
    installed_max: dict[str, tuple] = {}
    config_idx = _get_config_index()
    for m in fingerprint.list_models():
        cfg = config_idx["by_quick_hash"].get(m["hash"])
        if not cfg:
            cfg = config_idx["by_filename"].get(m.get("filename", "").lower())
        if not cfg:
            continue
        if cfg.get("files") and not _all_files_installed(cfg["files"]):
            continue
        name = cfg.get("model_name") or cfg.get("display_name") or ""
        ver = _base_version_key(cfg.get("version", ""))
        if name and ver > installed_max.get(name, ()):
            installed_max[name] = ver

    # Group by model_name, keep only the latest version per model
    by_model: dict[str, list[dict]] = {}
    for entry in entries:
        name = entry.get("model_name") or entry.get("display_name") or ""
        by_model.setdefault(name, []).append(entry)

    catalog = []
    for name, group in by_model.items():
        imax = installed_max.get(name, ())
        # Keep every uninstalled version newer than the user's newest install;
        # the picker groups these by model_name into a version submenu. Drop
        # versions at or below what's already installed.
        for entry in group:
            if _base_version_key(entry.get("version", "")) > imax:
                catalog.append(entry)

    catalog.sort(key=lambda c: c.get("display_name", ""))
    return catalog


def list_older_versions(model_name: str) -> list[dict]:
    """Return all uninstalled versions of a model.

    Used by the model panel's "Older Versions" dropdown. The main catalog
    only shows the latest version; this provides access to older ones.
    """
    entries = _collect_uninstalled_presets()
    matching = [e for e in entries if (e.get("model_name") or e.get("display_name")) == model_name]
    matching.sort(key=lambda e: _base_version_key(e.get("version", "")), reverse=True)
    return matching


def is_catalog_filename(filename: str) -> bool:
    """Check if a filename appears in any system/discovered config's files array."""
    return filename.lower() in _get_config_index()["by_filename"]


def is_primary_model_file(filename: str) -> bool:
    """True if this filename is the primary file for its config.

    Multi-file models list several component files (dual experts, text
    encoders, VAEs, LoRAs). The primary file is the first diffusion_models
    or unet entry. Only it should appear in the picker — secondaries are
    hidden to avoid duplicate entries.

    Returns True for files not in any config (standalone models).
    """
    config = find_config_by_filename(filename)
    if not config or "files" not in config:
        return True

    lower = filename.lower()
    for entry in config["files"]:
        if entry.get("folder") not in ("diffusion_models", "unet"):
            continue
        flat = entry.get("filename")
        if flat:
            return flat.lower() == lower
        for v in entry.get("variants", []):
            if v.get("filename", "").lower() == lower:
                return True
        return False
    return True


def _compute_delta(config: dict, system: dict) -> dict:
    """Return only the keys/values in config that differ from system.

    For the 'nodes' key, diffs per-node-type, then per-widget within each.
    """
    delta = {}

    for key, value in config.items():
        if key == "nodes":
            continue
        if key not in system or system[key] != value:
            delta[key] = value

    # Deep diff for nodes — only store changed widgets per node type
    config_nodes = config.get("nodes", {})
    system_nodes = system.get("nodes", {})

    if config_nodes:
        delta_nodes = {}
        for node_type, widgets in config_nodes.items():
            if node_type not in system_nodes:
                # Entirely new node type — keep all
                delta_nodes[node_type] = widgets
                continue
            sys_widgets = system_nodes[node_type]
            changed = {}
            for wk, wv in widgets.items():
                if wk not in sys_widgets or sys_widgets[wk] != wv:
                    changed[wk] = wv
            if changed:
                delta_nodes[node_type] = changed
        if delta_nodes:
            delta["nodes"] = delta_nodes

    return delta


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("failed to read config %s", path, exc_info=True)
        return None
