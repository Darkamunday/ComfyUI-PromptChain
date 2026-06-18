"""
Workflow template storage — capture and restore node subgraphs.

Two layers:
  System: data/templates/{id}.json — shipped presets for known models (read-only)
  User:   {user_dir}/PromptChain/templates/{id}.json — user overrides / new templates

User layer stores deltas for system templates (only changed fields).
Delete writes a tombstone ({hidden: true}) so system templates can be hidden.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

# Template IDs must start with alphanumeric so pathological IDs like
# "___" or "---" can't slip through.
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
from typing import Optional

from .api_utils import atomic_write_json

import folder_paths

_GRAPH_KEYS = {"nodes", "connections", "anchorConnections"}
_META_KEYS = {"name", "category", "order", "scope", "created_at"}
_CATEGORY_ORDER_FILE = "_category_order.json"


def _system_dir() -> Path:
    return Path(__file__).parent.parent / "data" / "templates"


def _user_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "templates"


def _ingest_dir(directory: Path) -> dict[str, dict]:
    """Read all JSON files in a directory, return templates keyed by id."""
    result = {}
    if not directory.is_dir():
        return result
    for path in directory.glob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        tid = data.get("id")
        if tid:
            result[tid] = data
    return result


def _find_in_dir(directory: Path, template_id: str) -> Optional[dict]:
    """Find a template by id field (not filename) in a directory."""
    if not directory.is_dir():
        return None
    for path in directory.glob("*.json"):
        data = _read_json(path)
        if data and data.get("id") == template_id:
            return data
    return None


def _merge_template(system: dict, user_overlay: dict) -> dict:
    """Merge system base with user overlay.

    Metadata fields overlay individually.
    Graph fields (nodes, connections, anchorConnections) replace as a block.
    """
    merged = dict(system)
    for key in _META_KEYS:
        if key in user_overlay:
            merged[key] = user_overlay[key]
    for key in _GRAPH_KEYS:
        if key in user_overlay:
            merged[key] = user_overlay[key]
    return merged


def _compute_delta(full: dict, system: dict) -> dict:
    """Return only the fields that differ from system.

    Graph keys are stored as a block if any differ (they're interdependent).
    """
    delta = {"id": full["id"]}

    for key in _META_KEYS:
        if key in full and full.get(key) != system.get(key):
            delta[key] = full[key]

    graph_changed = any(
        full.get(k) != system.get(k) for k in _GRAPH_KEYS
    )
    if graph_changed:
        for k in _GRAPH_KEYS:
            if k in full:
                delta[k] = full[k]

    return delta


# ── public API ─────────────────────────────────────────────────────


def list_templates(architecture: Optional[str] = None,
                   family: Optional[str] = None,
                   model_name: Optional[str] = None,
                   model_hash: Optional[str] = None,
                   include_hidden: bool = False) -> list[dict]:
    """List templates, merging system + user deltas, filtered by scope."""
    system = _ingest_dir(_system_dir())
    user = _ingest_dir(_user_dir())

    all_ids = set(system) | set(user)
    results = []
    for tid in all_ids:
        sys_tpl = system.get(tid)
        usr_tpl = user.get(tid)

        is_hidden = usr_tpl and usr_tpl.get("hidden")
        if is_hidden and not include_hidden:
            continue

        if is_hidden and sys_tpl:
            # tombstoned system template — show the system original
            tpl = dict(sys_tpl)
            tpl["_source"] = "system"
            tpl["_hidden"] = True
        elif sys_tpl and usr_tpl:
            tpl = _merge_template(sys_tpl, usr_tpl)
            tpl["_source"] = "overlay"
        elif usr_tpl:
            tpl = usr_tpl
            tpl["_source"] = "user"
        else:
            tpl = sys_tpl
            tpl["_source"] = "system"

        tpl["_has_system"] = sys_tpl is not None
        tpl["_has_user"] = usr_tpl is not None

        if architecture and not _matches_scope(tpl, architecture, family, model_name, model_hash):
            continue
        results.append(tpl)

    results.sort(key=lambda t: (
        t.get("order") if t.get("order") is not None else float('inf'),
        -t.get("created_at", 0),
    ))
    return results


def load(template_id: str) -> Optional[dict]:
    """Load a single template by id, merging system + user delta."""
    sys_tpl = _find_in_dir(_system_dir(), template_id)
    usr_tpl = _find_in_dir(_user_dir(), template_id)

    if usr_tpl and usr_tpl.get("hidden"):
        return None

    if sys_tpl and usr_tpl:
        return _merge_template(sys_tpl, usr_tpl)
    return usr_tpl or sys_tpl


def save(template: dict) -> str:
    """Save a template. Computes delta if system original exists."""
    tpl_dir = _user_dir()
    tpl_dir.mkdir(parents=True, exist_ok=True)

    if not template.get("id"):
        template["id"] = str(uuid.uuid4())

    tid = template["id"]
    if not _SAFE_ID_RE.match(tid):
        raise ValueError(f"invalid template id: {tid!r}")
    path = tpl_dir / f"{tid}.json"

    sys_tpl = _find_in_dir(_system_dir(), tid)
    if sys_tpl:
        delta = _compute_delta(template, sys_tpl)
        # only id remains — user saved identical to system
        if len(delta) <= 1:
            if path.exists():
                path.unlink()
            return tid
        data = delta
    else:
        data = template

    atomic_write_json(path, data)
    return tid


def delete(template_id: str) -> bool:
    """Delete or hide a template.

    System templates get a tombstone; user-only templates are removed.
    """
    if not _SAFE_ID_RE.match(template_id):
        raise ValueError(f"invalid template id: {template_id!r}")
    sys_tpl = _find_in_dir(_system_dir(), template_id)
    user_path = _user_dir() / f"{template_id}.json"

    if sys_tpl:
        _user_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_json(user_path, {"id": template_id, "hidden": True})
        return True

    if user_path.exists():
        user_path.unlink()
        return True
    return False


def reset(template_id: str) -> bool:
    """Remove user overlay so the system original shows through."""
    if not _SAFE_ID_RE.match(template_id):
        raise ValueError(f"invalid template id: {template_id!r}")
    path = _user_dir() / f"{template_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def save_order(ordered_ids: list[str]):
    """Persist user ordering by writing order field into overlays."""
    tpl_dir = _user_dir()
    tpl_dir.mkdir(parents=True, exist_ok=True)

    for idx, tid in enumerate(ordered_ids):
        user_path = tpl_dir / f"{tid}.json"
        existing = _read_json(user_path)

        if existing:
            existing["order"] = idx
            atomic_write_json(user_path, existing)
        else:
            # only create overlay if system template exists and order differs
            sys_tpl = _find_in_dir(_system_dir(), tid)
            if sys_tpl and sys_tpl.get("order") != idx:
                atomic_write_json(user_path, {"id": tid, "order": idx})


def update_metadata(template_id: str, updates: dict) -> bool:
    """Update metadata fields on a template (category, name, order, etc).

    Loads the merged template, applies updates, and re-saves as delta.
    """
    merged = load(template_id)
    if not merged:
        return False
    for key in _META_KEYS:
        if key in updates:
            merged[key] = updates[key]
    save(merged)
    return True


def reset_all() -> int:
    """Remove all user template overlays, restoring system defaults.

    Returns the number of files removed.
    """
    user_dir = _user_dir()
    if not user_dir.is_dir():
        return 0
    count = 0
    for path in user_dir.glob("*.json"):
        path.unlink()
        count += 1
    return count


def has_system_template(template_id: str) -> bool:
    return _find_in_dir(_system_dir(), template_id) is not None


def has_user_template(template_id: str) -> bool:
    return _find_in_dir(_user_dir(), template_id) is not None


def _matches_scope(template: dict, architecture: str,
                   family: Optional[str], model_name: Optional[str],
                   model_hash: Optional[str]) -> bool:
    scope = template.get("scope", {})
    scope_type = scope.get("type", "architecture")

    if scope_type == "version":
        return scope.get("model_hash") == model_hash
    if scope_type == "model":
        scope_name = scope.get("model_name") or scope.get("display_name")
        return scope_name == model_name and model_name is not None
    if scope_type == "family":
        return (scope.get("architecture") == architecture
                and scope.get("family") == family)
    # architecture scope — broadest match
    return scope.get("architecture") == architecture


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_json_list(path: Path) -> Optional[list]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else None
    except Exception:
        return None


# ── category ordering ─────────────────────────────────────────────


def load_category_order() -> list[str]:
    """Load category display order, merging system defaults with user overrides.

    User file replaces system entirely when present (same pattern as save_order).
    """
    user_order = _read_json_list(_user_dir() / _CATEGORY_ORDER_FILE)
    if user_order is not None:
        return user_order
    system_order = _read_json_list(_system_dir() / _CATEGORY_ORDER_FILE)
    return system_order or []


def save_category_order(ordered_categories: list[str]):
    """Persist user category ordering. Skips write if identical to system."""
    system_order = _read_json_list(_system_dir() / _CATEGORY_ORDER_FILE)
    if ordered_categories == system_order:
        # identical to system — remove user override
        user_path = _user_dir() / _CATEGORY_ORDER_FILE
        if user_path.exists():
            user_path.unlink()
        return

    tpl_dir = _user_dir()
    tpl_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(tpl_dir / _CATEGORY_ORDER_FILE, ordered_categories)
