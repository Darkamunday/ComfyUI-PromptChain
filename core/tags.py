"""
Tag autocomplete — two-layer CSV tag system with onboarding.

System CSVs ship with the node in data/tags/ (git-tracked, never modified).
User CSVs live in {comfyui_user}/PromptChain/tags/ (user's source of truth).

On first boot: system CSVs are copied to user dir, manifest records their hashes.
On subsequent boots: user dir is loaded. System dir is the reference for restore.

Per-file states:
  default  — user file matches system (safe to auto-update)
  modified — user file differs from system (don't overwrite, offer restore)
  updated  — system changed since onboarding, user hasn't modified (auto-update)
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Optional

import folder_paths

from . import config as global_config

MIN_RANKING_THRESHOLD = 100

_MANIFEST_FILE = "tags_manifest.json"


# ── paths ─────────────────────────────────────────────────────────


def _system_tags_dir() -> Path:
    return Path(__file__).parent.parent / "data" / "tags"


def _user_tags_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "tags"


# ── file hashing ──────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ── manifest ──────────────────────────────────────────────────────


def _manifest_path() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / _MANIFEST_FILE


def _load_manifest() -> dict:
    path = _manifest_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_manifest(manifest: dict):
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


# ── onboarding + sync ────────────────────────────────────────────


def sync_tag_files() -> dict[str, str]:
    """
    Ensure user tag dir is populated and up to date.
    Returns {filename: state} where state is default/modified/removed/custom.
    """
    system_dir = _system_tags_dir()
    user_dir = _user_tags_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    system_hashes = manifest.get("system_hashes", {})
    onboarded = global_config.is_onboarded()

    system_csvs = {f.name: f for f in system_dir.glob("*.csv")} if system_dir.is_dir() else {}

    states: dict[str, str] = {}

    for filename, system_path in system_csvs.items():
        user_path = user_dir / filename
        current_system_hash = _hash_file(system_path)

        if not onboarded:
            # First boot — copy all system files
            shutil.copy2(system_path, user_path)
            system_hashes[filename] = current_system_hash
            states[filename] = "default"
            continue

        if not user_path.exists():
            # User deleted it — respect that, don't re-copy
            system_hashes[filename] = current_system_hash
            states[filename] = "removed"
            continue

        stored_system_hash = system_hashes.get(filename)
        user_hash = _hash_file(user_path)

        system_changed = stored_system_hash != current_system_hash
        user_changed = user_hash != stored_system_hash

        if not user_changed and system_changed:
            # System updated, user hasn't touched it — auto-update
            shutil.copy2(system_path, user_path)
            system_hashes[filename] = current_system_hash
            states[filename] = "default"
        elif user_changed:
            # User modified — don't touch, but track new system hash
            if system_changed:
                system_hashes[filename] = current_system_hash
            states[filename] = "modified"
        else:
            states[filename] = "default"

    # User-only CSVs (not from system) — additional sources
    for f in user_dir.glob("*.csv"):
        if f.name not in states:
            states[f.name] = "custom"

    manifest["system_hashes"] = system_hashes
    _save_manifest(manifest)

    return states


def restore_file(filename: str) -> bool:
    """Restore a user file to the system default. Returns True if restored."""
    system_path = _system_tags_dir() / filename
    if not system_path.exists():
        return False
    user_dir = _user_tags_dir()
    user_path = user_dir / filename
    shutil.copy2(system_path, user_path)

    # Update manifest
    manifest = _load_manifest()
    system_hashes = manifest.get("system_hashes", {})
    system_hashes[filename] = _hash_file(system_path)
    manifest["system_hashes"] = system_hashes
    _save_manifest(manifest)
    return True


def get_file_states() -> dict[str, str]:
    """Get current state of each tag file without modifying anything."""
    system_dir = _system_tags_dir()
    user_dir = _user_tags_dir()
    manifest = _load_manifest()
    system_hashes = manifest.get("system_hashes", {})

    system_csvs = {f.name: f for f in system_dir.glob("*.csv")} if system_dir.is_dir() else {}
    states: dict[str, str] = {}

    for filename, system_path in system_csvs.items():
        user_path = user_dir / filename
        if not user_path.exists():
            states[filename] = "missing"
            continue
        stored_hash = system_hashes.get(filename)
        if not stored_hash:
            states[filename] = "default"
            continue
        user_hash = _hash_file(user_path)
        states[filename] = "modified" if user_hash != stored_hash else "default"

    for f in user_dir.glob("*.csv"):
        if f.name not in states:
            states[f.name] = "custom"

    return states


# ── tag store ─────────────────────────────────────────────────────


class TagStore:
    """In-memory tag storage loaded from user CSV files."""

    def __init__(self):
        self.sources: dict[str, dict] = {}
        self._loaded = False
        self._file_states: dict[str, str] = {}

    def load_all(self):
        if self._loaded:
            return
        # Sync system → user on first load
        self._file_states = sync_tag_files()

        user_dir = _user_tags_dir()
        if not user_dir.is_dir():
            self._loaded = True
            return

        for csv_path in sorted(user_dir.glob("*.csv")):
            self._load_source(csv_path)

        self._loaded = True
        print(f"[PromptChain] Loaded {len(self.sources)} tag source(s) from {user_dir}")

    def reload(self):
        """Force reload — re-syncs files and reloads all sources."""
        self.sources.clear()
        self._loaded = False
        self.load_all()

    def _load_source(self, csv_path: Path):
        source_name = csv_path.stem
        tags = []
        by_id: dict[int, dict] = {}

        try:
            with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    try:
                        tag = row.get("TAG", "").strip()
                        if not tag:
                            continue
                        tag_id = int(row.get("ID", 0))
                        category = row.get("CATEGORY", "general").strip()
                        ranking = int(row.get("RANKING", 0) or 0)
                        similar_str = row.get("SIMILAR", "").strip()
                        similar_ids = (
                            [int(x) for x in similar_str.split(",") if x.strip().isdigit()]
                            if similar_str else []
                        )
                        entry = {
                            "id": tag_id,
                            "tag": tag,
                            "category": category,
                            "ranking": ranking,
                            "similar": similar_ids,
                            "_search": tag.lower(),
                        }
                        tags.append(entry)
                        by_id[tag_id] = entry
                    except (ValueError, KeyError):
                        continue

            tags.sort(key=lambda x: -x["ranking"])
            self.sources[source_name] = {"tags": tags, "by_id": by_id, "count": len(tags)}
            state = self._file_states.get(csv_path.name, "")
            suffix = f" [{state}]" if state == "modified" else ""
            print(f"[PromptChain]   {source_name}: {len(tags):,} tags{suffix}")
        except Exception as e:
            print(f"[PromptChain]   Error loading {csv_path}: {e}")

    def list_sources(self) -> list[dict]:
        self.load_all()
        return [
            {"name": n, "count": d["count"], "state": self._file_states.get(f"{n}.csv", "default")}
            for n, d in self.sources.items()
        ]

    def search(self, source_name: str, query: str, limit: int = 20) -> list[dict]:
        self.load_all()
        if source_name not in self.sources:
            return []
        tags = self.sources[source_name]["tags"]
        if not query:
            return [_format(t) for t in tags[:limit]]

        query_lower = query.lower().replace(" ", "_")
        query_stripped = query_lower.rstrip("_")
        query_words = [w for w in query_stripped.replace("_", " ").split() if w]

        matches = []
        for tag in tags:
            ranking = tag["ranking"]
            if ranking > 0 and MIN_RANKING_THRESHOLD > 0 and ranking < MIN_RANKING_THRESHOLD:
                continue

            sk = tag["_search"]
            contiguous = query_lower in sk or query_stripped in sk
            multi = False
            if not contiguous and len(query_words) > 1:
                multi = all(w in sk for w in query_words)
            if not contiguous and not multi:
                continue

            if ranking > 0:
                sort_key = (0, -ranking, "")
            else:
                sort_key = (1, 0, sk)
            matches.append((sort_key, tag))

        matches.sort(key=lambda x: x[0])
        return [_format(m[1]) for m in matches[:limit]]

    def search_stacked(self, source_names: list[str], query: str, limit: int = 20) -> list[dict]:
        self.load_all()
        seen: set[str] = set()
        results: list[dict] = []
        for name in source_names:
            if name not in self.sources:
                continue
            for tag_data in self.search(name, query, limit * 2):
                key = tag_data["tag"].lower()
                if key not in seen:
                    seen.add(key)
                    tag_data["source"] = name
                    results.append(tag_data)
                    if len(results) >= limit:
                        return results
        return results

    def get_similar(self, source_name: str, tag_id: int | None = None,
                    tag_name: str | None = None, limit: int = 10) -> list[dict]:
        self.load_all()
        if source_name not in self.sources:
            return []
        source = self.sources[source_name]
        by_id = source["by_id"]
        tag = None
        if tag_id is not None:
            tag = by_id.get(tag_id)
        elif tag_name:
            needle = tag_name.lower().replace(" ", "_")
            for t in source["tags"]:
                if t["_search"] == needle:
                    tag = t
                    break
        if not tag:
            return []
        return [_format(by_id[sid]) for sid in tag.get("similar", [])[:limit] if sid in by_id]


def _format(tag: dict) -> dict:
    return {"id": tag["id"], "tag": tag["tag"], "category": tag["category"], "ranking": tag["ranking"]}


_store: Optional[TagStore] = None


def get_store() -> TagStore:
    global _store
    if _store is None:
        _store = TagStore()
    return _store
