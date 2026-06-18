import asyncio

from aiohttp import web
import server

from .api_utils import parse_json
from .compiler import (
    WILDCARD_EXTENSIONS,
    add_wildcard_path,
    get_wildcard_info,
    get_wildcard_paths,
    parse_wildcard_file,
    remove_wildcard_path,
    resolve_wildcard_name,
)

routes = server.PromptServer.instance.routes


# ── wildcard check + content ─────────────────────────────────────

@routes.get("/promptchain/wildcard")
async def _api_check_wildcard(request):
    name = request.query.get("name", "").strip()
    if not name:
        return web.json_response({"exists": False, "name": "", "folder": "wildcards"})

    found_path, resolved_key = resolve_wildcard_name(name)

    if found_path is None:
        return web.json_response({"exists": False, "name": name, "folder": "wildcards", "error": "not_found"})

    count, sections = get_wildcard_info(found_path)
    # re-parse with key for the specific count when a key was resolved
    if resolved_key:
        options = parse_wildcard_file(found_path, resolved_key)
        count = len(options)
    else:
        options = None

    error = "parse_error" if count == 0 else None

    result = {
        "exists": count > 0,
        "name": name,
        "folder": "wildcards",
        "count": count,
        "format": found_path.suffix.lstrip("."),
        "sections": sections,
        "error": error,
    }

    # include full options list when requested
    if request.query.get("options") == "true":
        if options is None:
            options = parse_wildcard_file(found_path, resolved_key)
        result["options"] = options

    return web.json_response(result)


@routes.get("/promptchain/wildcard/content")
async def _api_wildcard_content(request):
    name = request.query.get("name", "").strip()
    if not name:
        return web.json_response({"error": "missing name"}, status=400)

    found_path, _resolved_key = resolve_wildcard_name(name)
    if found_path is None:
        return web.json_response({"error": "not_found"}, status=404)

    try:
        content = found_path.read_text(encoding="utf-8")
    except Exception:
        return web.json_response({"error": "read_error"}, status=500)

    return web.json_response({
        "content": content,
        "format": found_path.suffix.lstrip("."),
        "filename": found_path.name,
        "key": _resolved_key,
    })


@routes.post("/promptchain/wildcard/content")
async def _api_wildcard_content_write(request):
    data, err = await parse_json(request)
    if err: return err
    name = data.get("name", "").strip()
    content = data.get("content")
    if not name or content is None:
        return web.json_response({"error": "missing name or content"}, status=400)

    found_path, _resolved_key = resolve_wildcard_name(name)
    if found_path is None:
        return web.json_response({"error": "not_found"}, status=404)

    # Defense in depth: verify the resolved path lives under a configured
    # wildcard root before writing.  resolve_wildcard_name already rejects
    # `..`, but an independent check means a future regression in that
    # helper cannot turn into an arbitrary file write.
    resolved = found_path.resolve()
    if not any(resolved.is_relative_to(root) for root in get_wildcard_paths()):
        return web.json_response({"error": "path outside wildcard roots"}, status=400)

    try:
        found_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"write_error: {e}"}, status=500)

    # invalidate wildcard list cache so changes are picked up
    global _wildcard_list_cache, _wildcard_list_mtime
    _wildcard_list_cache = None
    _wildcard_list_mtime = 0.0

    return web.json_response({"status": "ok", "filename": found_path.name})


# ── wildcard list with dir-level mtime caching ───────────────────

_wildcard_list_cache = None
_wildcard_list_mtime = 0.0
_wildcard_list_lock = asyncio.Lock()


def _get_dirs_mtime() -> float:
    # Stat directories only, not every file — staleness check runs on every
    # list request and rglob over a large wildcard tree is too slow.
    newest = 0.0
    for base in get_wildcard_paths():
        if not base.is_dir():
            continue
        try:
            newest = max(newest, base.stat().st_mtime)
            for item in base.iterdir():
                if item.is_dir():
                    newest = max(newest, item.stat().st_mtime)
        except OSError:
            pass
    return newest


def _build_wildcard_entry(path, base):
    ext = path.suffix.lower()
    if ext not in WILDCARD_EXTENSIONS:
        return None
    rel = path.relative_to(base)
    name = str(rel.with_suffix("")).replace("\\", "/")
    count, sections = get_wildcard_info(path)
    return {
        "name": name,
        "format": ext.lstrip("."),
        "count": count,
        "sections": sections,
    }


@routes.get("/promptchain/wildcard/list")
async def _api_list_wildcards(request):
    global _wildcard_list_cache, _wildcard_list_mtime

    async with _wildcard_list_lock:
        current_mtime = _get_dirs_mtime()
        if _wildcard_list_cache is not None and current_mtime <= _wildcard_list_mtime:
            return web.json_response({"wildcards": _wildcard_list_cache})

        wildcards = []
        seen_names = set()

        for base in get_wildcard_paths():
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                entry = _build_wildcard_entry(path, base)
                if entry is None or entry["name"] in seen_names:
                    continue
                seen_names.add(entry["name"])
                wildcards.append(entry)

        _wildcard_list_cache = wildcards
        _wildcard_list_mtime = current_mtime
        return web.json_response({"wildcards": wildcards})


# ── wildcard path management ─────────────────────────────────────

@routes.post("/promptchain/wildcard/paths")
async def _api_wildcard_paths(request):
    data, err = await parse_json(request)
    if err: return err
    action = data.get("action")
    path = data.get("path", "")
    if action == "add" and path:
        from pathlib import Path as P
        resolved = P(path).resolve()
        if not resolved.is_dir():
            return web.json_response({"error": "path is not a directory"}, status=400)
        add_wildcard_path(path)
    elif action == "remove" and path:
        remove_wildcard_path(path)
    paths = [str(p) for p in get_wildcard_paths()]
    return web.json_response({"status": "ok", "paths": paths})


@routes.get("/promptchain/wildcard/paths")
async def _api_get_wildcard_paths(request):
    paths = [str(p) for p in get_wildcard_paths()]
    return web.json_response({"paths": paths})
