"""
Prompt preset storage — reusable prompt snippets scoped by model classification.

Two layers:
  System: data/prompts/**/*.json — shipped preset packs (read-only)
  User:   {user_dir}/PromptChain/prompts/**/*.json — user-created presets

Each file contains a list of prompts. Files may be organized into subdirectories.
All JSON files are discovered recursively and merged. User prompts with the same `id` override
system prompts. Prompts are filtered by scope against the current model's
architecture, family, version, or exact hash.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

import folder_paths

_write_lock = threading.Lock()


def _system_dir() -> Path:
    return Path(__file__).parent.parent / "data" / "prompts"


def _user_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "prompts"


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ingest_dir(directory: Path) -> dict[str, dict]:
    """Read all JSON files in a directory, return prompts keyed by id."""
    result = {}
    if not directory.is_dir():
        return result
    for path in directory.rglob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        for prompt in data.get("prompts", []):
            pid = prompt.get("id")
            if pid:
                prompt["_source_file"] = path.name
                result[pid] = prompt
    return result


def list_prompts(architecture: Optional[str] = None,
                 family: Optional[str] = None,
                 model_name: Optional[str] = None,
                 model_hash: Optional[str] = None) -> list[dict]:
    """List prompts, merging system + user (user overrides by id), filtered by scope."""
    system = _ingest_dir(_system_dir())
    user = _ingest_dir(_user_dir())

    # user overrides system on same id
    merged = {**system, **user}

    results = []
    for prompt in merged.values():
        clean = {k: v for k, v in prompt.items() if not k.startswith("_")}
        if architecture and not _matches_scope(clean, architecture, family, model_name, model_hash):
            continue
        results.append(clean)

    results.sort(key=lambda p: (p.get("category", ""), p.get("name", "")))
    return results


def save(prompt: dict) -> str:
    """Save a user prompt. Appends to the user's custom.json file."""
    user_dir = _user_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    if not prompt.get("id"):
        prompt["id"] = str(uuid.uuid4())

    custom_path = user_dir / "custom.json"
    with _write_lock:
        data = _read_json(custom_path) or {"name": "Custom Prompts", "prompts": []}

        # replace existing prompt with same id, or append
        prompts = data.get("prompts", [])
        replaced = False
        for i, p in enumerate(prompts):
            if p.get("id") == prompt["id"]:
                prompts[i] = prompt
                replaced = True
                break
        if not replaced:
            prompts.append(prompt)

        data["prompts"] = prompts
        with open(custom_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return prompt["id"]


def delete(prompt_id: str) -> bool:
    """Delete a user prompt by id. Scans all user files."""
    user_dir = _user_dir()
    if not user_dir.is_dir():
        return False

    with _write_lock:
        for path in user_dir.rglob("*.json"):
            data = _read_json(path)
            if not data:
                continue
            prompts = data.get("prompts", [])
            original_len = len(prompts)
            prompts = [p for p in prompts if p.get("id") != prompt_id]
            if len(prompts) < original_len:
                data["prompts"] = prompts
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return True
    return False


def _matches_scope(prompt: dict, architecture: str,
                   family: Optional[str], model_name: Optional[str],
                   model_hash: Optional[str]) -> bool:
    scope = prompt.get("scope")
    if not scope:
        return True  # no scope = global prompt

    scope_type = scope.get("type", "global")

    if scope_type == "global":
        return True
    if scope_type == "version":
        return scope.get("model_hash") == model_hash
    if scope_type == "model":
        scope_name = scope.get("model_name") or scope.get("display_name")
        return scope_name == model_name and model_name is not None
    if scope_type == "family":
        return (scope.get("architecture") == architecture
                and scope.get("family") == family)
    if scope_type == "architecture":
        return scope.get("architecture") == architecture
    return True
