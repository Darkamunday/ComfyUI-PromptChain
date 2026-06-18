# Browse API — folder listing with metadata for the asset browser sidebar.
# Supports output, input, and workflows scopes with path security.

import json
import os
import struct
from pathlib import Path

import logging

import folder_paths
import server
from aiohttp import web

from .api_utils import error_response, ok_response, parse_json

logger = logging.getLogger("promptchain.browse_api")

routes = server.PromptServer.instance.routes

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}
_WORKFLOW_EXT = ".json"
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB


def _get_scope_root(scope: str) -> Path | None:
    if scope == "output":
        return Path(folder_paths.get_output_directory())
    if scope == "input":
        return Path(folder_paths.get_input_directory())
    if scope == "workflows":
        user_dir = folder_paths.get_user_directory()
        root = Path(user_dir) / "default" / "workflows"
        # fresh installs lack this dir until the first workflow save — a new
        # user's Workflows tab must list empty, not 400
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return root
    return None


def _validate_path(root: Path, rel_path: str) -> Path | None:
    """Resolve a relative path within root, rejecting traversal and symlink escapes."""
    if not rel_path:
        return root
    # reject raw ".." components before resolving
    for part in Path(rel_path).parts:
        if part == "..":
            return None
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    # reject symlinks anywhere in the traversal chain
    check = root
    for part in Path(rel_path).parts:
        check = check / part
        if check.is_symlink():
            return None
    return target


def _item_type(path: Path) -> str:
    if path.is_dir():
        return "folder"
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == _WORKFLOW_EXT:
        return "workflow"
    return "file"


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Extract image dimensions from binary headers without loading the full image."""
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            if ext == ".png":
                header = f.read(24)
                if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
                    w, h = struct.unpack(">II", header[16:24])
                    return w, h
            elif ext in (".jpg", ".jpeg"):
                f.read(2)  # SOI
                while True:
                    marker = f.read(2)
                    if len(marker) < 2:
                        break
                    if marker[0] != 0xFF:
                        break
                    if marker[1] in (0xC0, 0xC1, 0xC2):
                        f.read(3)  # length + precision
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    length = struct.unpack(">H", f.read(2))[0]
                    f.seek(length - 2, 1)
            elif ext == ".webp":
                header = f.read(30)
                if len(header) >= 30 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                    if header[12:16] == b"VP8 ":
                        w = (header[26] | (header[27] << 8)) & 0x3FFF
                        h = (header[28] | (header[29] << 8)) & 0x3FFF
                        return w, h
                    elif header[12:16] == b"VP8L":
                        bits = struct.unpack("<I", header[21:25])[0]
                        w = (bits & 0x3FFF) + 1
                        h = ((bits >> 14) & 0x3FFF) + 1
                        return w, h
            elif ext == ".gif":
                header = f.read(10)
                if len(header) >= 10:
                    w, h = struct.unpack("<HH", header[6:10])
                    return w, h
    except Exception:
        logger.debug("image dimensions failed for %s", path, exc_info=True)
    return None, None


def _child_count(path: Path) -> int:
    try:
        return sum(1 for p in path.iterdir() if not p.name.startswith("."))
    except Exception:
        return 0


def _build_item(path: Path, scope: str, root: Path,
                wf_thumbs: dict[str, str] | None = None,
                wf_id: str | None = None) -> dict:
    """Build a listing item. If wf_thumbs is provided, use the prebuilt
    {workflow_id: hash} map instead of querying the DB per file — avoids
    the N+1 query on folder listings.  wf_id, when provided, skips a
    second JSON parse of the same workflow file."""
    rel = str(path.relative_to(root)).replace("\\", "/")
    item_type = _item_type(path)
    item = {
        "path": rel,
        "name": path.name,
        "type": item_type,
        "size": path.stat().st_size if path.is_file() else 0,
        "modified": int(path.stat().st_mtime),
    }
    if item_type == "folder":
        item["childCount"] = _child_count(path)
    elif item_type == "image":
        item["extension"] = path.suffix.lower().lstrip(".")
        w, h = _image_dimensions(path)
        if w and h:
            item["width"] = w
            item["height"] = h
        # look up DB hash so the image viewer can load by hash
        if scope == "output":
            try:
                from .history_db import find_image_by_path
                subfolder = str(path.parent.relative_to(root)).replace("\\", "/")
                if subfolder == ".":
                    subfolder = ""
                db_row = find_image_by_path(path.name, subfolder)
                if db_row:
                    item["hash"] = db_row["hash"]
            except Exception:
                pass
    elif item_type == "video":
        item["extension"] = path.suffix.lower().lstrip(".")
    elif item_type == "workflow":
        item["extension"] = "json"
        if wf_thumbs is not None:
            # wf_id passed in by listing loop to avoid re-parsing this
            # file's JSON.  If the caller didn't precompute it, fall
            # back to the single-file helper (still one parse).
            effective_id = wf_id if wf_id is not None else _read_workflow_id(path)
            item["thumbnailHash"] = wf_thumbs.get(effective_id) if effective_id else None
        else:
            item["thumbnailHash"] = _workflow_latest_image_hash(path)
    return item


_MAX_WORKFLOW_JSON_BYTES = 10 * 1024 * 1024  # 10 MiB cap on per-file parse


def _read_workflow_id(wf_path: Path) -> str | None:
    """Extract just the workflow UUID from a JSON file without loading everything."""
    try:
        if wf_path.stat().st_size > _MAX_WORKFLOW_JSON_BYTES:
            return None
        data = json.loads(wf_path.read_text(encoding="utf-8"))
        return data.get("id") or None
    except Exception:
        return None


def _workflow_latest_image_hash(wf_path: Path) -> str | None:
    """Latest image hash for a single workflow. Kept for single-item endpoints;
    listing uses _batch_workflow_thumbnails to avoid N+1 queries."""
    wf_id = _read_workflow_id(wf_path)
    if not wf_id:
        return None
    try:
        from .history_db import _get_conn
        conn = _get_conn()
        row = conn.execute("""
            SELECT i.hash FROM images i
            JOIN image_workflows iw ON i.hash = iw.hash
            WHERE iw.workflow_id = ?
            ORDER BY i.created_at DESC LIMIT 1
        """, (wf_id,)).fetchone()
        return row["hash"] if row else None
    except Exception:
        return None


def _batch_workflow_thumbnails(wf_ids: list[str]) -> dict[str, str]:
    """Single query mapping {workflow_id: latest_image_hash} for many workflows."""
    if not wf_ids:
        return {}
    try:
        from .history_db import _get_conn
        conn = _get_conn()
        placeholders = ",".join("?" * len(wf_ids))
        rows = conn.execute(f"""
            SELECT iw.workflow_id, i.hash
            FROM images i
            JOIN image_workflows iw ON i.hash = iw.hash
            WHERE iw.workflow_id IN ({placeholders})
            AND i.created_at = (
                SELECT MAX(i2.created_at) FROM images i2
                JOIN image_workflows iw2 ON i2.hash = iw2.hash
                WHERE iw2.workflow_id = iw.workflow_id
            )
        """, wf_ids).fetchall()
        return {row["workflow_id"]: row["hash"] for row in rows}
    except Exception:
        return {}


def _favorites_for(scope: str) -> set[str]:
    try:
        from .history_db import get_favorites
        return get_favorites(scope)
    except Exception:
        return set()


def _mark_favorites(items: list[dict], favs: set[str]):
    if not favs:
        return
    for item in items:
        if item["path"] in favs:
            item["favorite"] = True


def _sort_items(items: list[dict], sort: str, direction: str) -> list[dict]:
    folders = [i for i in items if i["type"] == "folder"]
    files = [i for i in items if i["type"] != "folder"]

    reverse = direction == "desc"
    key_map = {
        "name": lambda x: x["name"].lower(),
        "modified": lambda x: x.get("modified", 0),
        "size": lambda x: x.get("size", 0),
        "type": lambda x: x.get("extension", x["type"]),
    }
    key_fn = key_map.get(sort, key_map["name"])
    folders.sort(key=key_fn, reverse=reverse)
    files.sort(key=key_fn, reverse=reverse)
    return folders + files


@routes.get("/promptchain/browse")
async def _api_browse(request):
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")
    sort = request.query.get("sort", "name")
    direction = request.query.get("direction", "asc")

    root = _get_scope_root(scope)
    if not root or not root.is_dir():
        return error_response(f"invalid scope: {scope}")

    target = _validate_path(root, rel_path)
    if not target or not target.is_dir():
        return error_response("invalid path")

    items = []
    try:
        entries = [e for e in target.iterdir() if not e.name.startswith(".")]
        # Parse each workflow JSON exactly once — build a {path: wf_id}
        # map, use its values for the batched thumbnail query, and pass
        # the map through to _build_item so per-entry lookup is O(1).
        entry_wf_ids = {}
        for e in entries:
            if _item_type(e) == "workflow":
                wid = _read_workflow_id(e)
                if wid:
                    entry_wf_ids[e] = wid
        wf_thumbs = _batch_workflow_thumbnails(list(entry_wf_ids.values())) if entry_wf_ids else {}
        for entry in entries:
            items.append(_build_item(entry, scope, root, wf_thumbs=wf_thumbs, wf_id=entry_wf_ids.get(entry)))
    except PermissionError:
        return error_response("permission denied", 403)

    _mark_favorites(items, _favorites_for(scope))
    items = _sort_items(items, sort, direction)

    # breadcrumb path segments
    path_segments = rel_path.split("/") if rel_path else []

    return web.json_response({
        "scope": scope,
        "path": path_segments,
        "root": str(root.resolve()).replace("\\", "/"),
        "items": items,
    })


def _scan_recent_files(target: Path, root: Path) -> list[tuple[int, str, Path]]:
    """Collect (mtime, rel_path, abs_path) for every file under target,
    skipping dot-prefixed entries and symlinks.  DirEntry.stat() is filled
    during enumeration on Windows, so this stays cheap on large trees."""
    results = []
    stack = [target]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            mtime = int(entry.stat(follow_symlinks=False).st_mtime)
                            rel = str(Path(entry.path).relative_to(root)).replace("\\", "/")
                            results.append((mtime, rel, Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            continue
    return results


@routes.get("/promptchain/browse/recent")
async def _api_browse_recent(request):
    """Flat newest-first listing of every file under a subtree, cursor-paginated.
    Powers the sidebar's recent-feed mode."""
    scope = request.query.get("scope", "output")
    if scope not in ("input", "output", "workflows"):
        return error_response(f"invalid scope: {scope}")
    rel_path = request.query.get("path", "")
    try:
        limit = max(1, min(int(request.query.get("limit", "60")), 200))
    except ValueError:
        limit = 60
    cursor_m = request.query.get("cursor_m")
    cursor_p = request.query.get("cursor_p", "")
    query = request.query.get("q", "").strip().lower()
    starred = request.query.get("starred") == "1"

    root = _get_scope_root(scope)
    if not root or not root.is_dir():
        return error_response(f"invalid scope: {scope}")

    target = _validate_path(root, rel_path)
    if not target or not target.is_dir():
        return error_response("invalid path")

    import asyncio
    entries = await asyncio.to_thread(_scan_recent_files, target, root)
    # filter before sort/cursor so pagination stays consistent within the
    # result set; matching the rel path makes folder names searchable too
    favs = _favorites_for(scope)
    if starred:
        entries = [e for e in entries if e[1] in favs]
    if query:
        entries = [e for e in entries if query in e[1].lower()]
    # path tiebreak keeps the cursor deterministic when a render burst
    # lands several files in the same second
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)

    if cursor_m is not None:
        try:
            cm = int(cursor_m)
        except ValueError:
            return error_response("invalid cursor")
        remaining = [e for e in entries if e[0] < cm or (e[0] == cm and e[1] < cursor_p)]
    else:
        remaining = entries

    page = remaining[:limit]
    # batch the workflow-thumbnail lookup for the page, same as the folder
    # listing does — _build_item would otherwise re-parse + query per file
    page_wf_ids = {}
    for _, _, abs_path in page:
        if _item_type(abs_path) == "workflow":
            wid = _read_workflow_id(abs_path)
            if wid:
                page_wf_ids[abs_path] = wid
    wf_thumbs = _batch_workflow_thumbnails(list(page_wf_ids.values())) if page_wf_ids else {}
    items = [
        _build_item(abs_path, scope, root, wf_thumbs=wf_thumbs, wf_id=page_wf_ids.get(abs_path))
        for _, _, abs_path in page
    ]
    _mark_favorites(items, favs)

    next_cursor = None
    if len(remaining) > limit and page:
        next_cursor = {"m": page[-1][0], "p": page[-1][1]}

    return web.json_response({
        "scope": scope,
        "path": rel_path.split("/") if rel_path else [],
        "root": str(root.resolve()).replace("\\", "/"),
        "items": items,
        "total": len(entries),
        "nextCursor": next_cursor,
    })


def _dhash64(im) -> int:
    """64-bit difference hash: 9×8 grayscale, each bit = left pixel brighter
    than its right neighbor. Robust to re-encodes and mild post-processing."""
    g = im.convert("L").resize((9, 8))
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def _compute_phash(scope: str, rel_path: str, abs_path: Path, stat) -> int | None:
    """Hash from the cached browse thumb when present (≈5ms) — falls back to
    decoding the original."""
    from PIL import Image
    thumb = _thumb_cache_path(scope, rel_path, stat)
    src = thumb if thumb.is_file() else abs_path
    try:
        with Image.open(src) as im:
            return _dhash64(im)
    except Exception:
        logger.debug("phash failed for %s", src, exc_info=True)
        return None


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@routes.get("/promptchain/browse/duplicates")
async def _api_browse_duplicates(request):
    """Cluster visually near-identical images under a subtree.

    Hashes are cached in the DB; first run over a large library decodes
    every image once (threaded, progress over the websocket as
    promptchain.dedup.progress). Clustering: identical hashes group
    directly, then 8×8-bit LSH bands propose candidate pairs — pigeonhole
    guarantees no missed pair up to Hamming distance 7."""
    scope = request.query.get("scope", "output")
    if scope not in ("input", "output"):
        return error_response(f"invalid scope: {scope}")
    rel_path = request.query.get("path", "")
    try:
        threshold = max(0, min(int(request.query.get("threshold", "5")), 7))
    except ValueError:
        threshold = 5

    root = _get_scope_root(scope)
    if not root or not root.is_dir():
        return error_response(f"invalid scope: {scope}")
    target = _validate_path(root, rel_path)
    if not target or not target.is_dir():
        return error_response("invalid path")

    import asyncio
    from .history_db import get_phashes, upsert_phashes
    from .shared import send_ws

    entries = await asyncio.to_thread(_scan_recent_files, target, root)
    images = [(rel, p) for _, rel, p in entries if p.suffix.lower() in _IMAGE_EXTS]
    total = len(images)

    cached = get_phashes(scope)
    hashes: dict[str, int] = {}
    todo = []
    for rel, p in images:
        try:
            st = p.stat()
        except OSError:
            continue
        c = cached.get(rel)
        if c and c[0] == st.st_mtime_ns and c[1] == st.st_size:
            hashes[rel] = c[2]
        else:
            todo.append((rel, p, st))

    if todo:
        def hash_all():
            from concurrent.futures import ThreadPoolExecutor
            done = total - len(todo)
            out = []
            def one(job):
                rel, p, st = job
                return rel, st, _compute_phash(scope, rel, p, st)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for rel, st, h in pool.map(one, todo):
                    done += 1
                    if h is not None:
                        out.append((rel, st.st_mtime_ns, st.st_size, h))
                    if done % 100 == 0 or done == total:
                        send_ws("promptchain.dedup.progress", {"done": done, "total": total})
            return out
        computed = await asyncio.to_thread(hash_all)
        upsert_phashes(scope, computed)
        for rel, _, _, h in computed:
            hashes[rel] = h

    # --- cluster ---
    # group identical hashes first so a pile of exact re-renders can't blow
    # up the pairwise stage
    by_hash: dict[int, list[str]] = {}
    for rel, h in hashes.items():
        by_hash.setdefault(h, []).append(rel)
    unique_hashes = list(by_hash.keys())

    parent = list(range(len(unique_hashes)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    if threshold > 0:
        bands: dict[tuple[int, int], list[int]] = {}
        for idx, h in enumerate(unique_hashes):
            for band in range(8):
                bands.setdefault((band, (h >> (band * 8)) & 0xFF), []).append(idx)
        seen_pairs = set()
        for bucket in bands.values():
            if len(bucket) < 2 or len(bucket) > 500:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    a, b = bucket[i], bucket[j]
                    key = (a, b) if a < b else (b, a)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    if _hamming(unique_hashes[a], unique_hashes[b]) <= threshold:
                        union(a, b)

    groups: dict[int, list[str]] = {}
    for idx, h in enumerate(unique_hashes):
        groups.setdefault(find(idx), []).extend(by_hash[h])

    favs = _favorites_for(scope)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        items = []
        for rel in members:
            p = _validate_path(root, rel)
            if not p or not p.is_file():
                continue
            item = _build_item(p, scope, root)
            if rel in favs:
                item["favorite"] = True
            items.append(item)
        if len(items) < 2:
            continue
        # suggested keeper: the largest file (highest quality original)
        keeper = max(items, key=lambda i: i.get("size", 0))
        keeper["keep"] = True
        items.sort(key=lambda i: (not i.get("keep"), -(i.get("size", 0))))
        clusters.append({"items": items})
    clusters.sort(key=lambda c: len(c["items"]), reverse=True)

    return web.json_response({
        "clusters": clusters,
        "totalImages": total,
        "duplicateCount": sum(len(c["items"]) - 1 for c in clusters),
    })


@routes.get("/promptchain/browse/item")
async def _api_browse_item(request):
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")

    root = _get_scope_root(scope)
    if not root or not root.is_dir():
        return error_response(f"invalid scope: {scope}")

    target = _validate_path(root, rel_path)
    if not target or not target.exists():
        return error_response("not found", 404)

    item = _build_item(target, scope, root)
    _mark_favorites([item], _favorites_for(scope))
    return web.json_response(item)


@routes.get("/promptchain/browse/workflow-by-id")
async def _api_browse_workflow_by_id(request):
    """Find a workflow file by UUID and return its item data (with fresh thumbnailHash)."""
    wf_id = request.query.get("id", "")
    if not wf_id:
        return error_response("missing id")

    root = _get_scope_root("workflows")
    if not root or not root.is_dir():
        return error_response("invalid scope")

    rel_dir = request.query.get("path", "")
    target = _validate_path(root, rel_dir)
    if not target or not target.is_dir():
        return error_response("invalid path")

    for entry in target.iterdir():
        if entry.suffix.lower() != _WORKFLOW_EXT or entry.is_dir():
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            if data.get("id") == wf_id:
                return web.json_response(_build_item(entry, "workflows", root))
        except Exception:
            continue

    return error_response("not found", 404)


@routes.get("/promptchain/browse/mtime")
async def _api_browse_mtime(request):
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")

    root = _get_scope_root(scope)
    if not root or not root.is_dir():
        return error_response(f"invalid scope: {scope}")

    target = _validate_path(root, rel_path)
    if not target or not target.is_dir():
        return error_response("invalid path")

    return web.json_response({"mtime": int(target.stat().st_mtime)})


_THUMB_MAX_EDGE = 512


def _thumb_cache_path(scope: str, rel_path: str, stat) -> Path:
    import hashlib
    from .history_db import get_data_dir
    # mtime+size in the key: an edited file gets a fresh cache entry, the
    # stale one is just an orphan on disk
    key = hashlib.sha1(f"{scope}|{rel_path}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()
    return get_data_dir() / "browse_thumbs" / f"{key}.webp"


def _generate_thumb(src: Path, dst: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.thumbnail((_THUMB_MAX_EDGE, _THUMB_MAX_EDGE))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if im.mode == "P" else "RGB")
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            im.save(tmp, "WEBP", quality=80, method=4)
        os.replace(tmp, dst)
        return True
    except Exception:
        logger.debug("thumbnail generation failed for %s", src, exc_info=True)
        return False


@routes.get("/promptchain/browse/preview")
async def _api_browse_preview(request):
    """Serve an image file from any scope for thumbnail display.

    thumb=1 returns a disk-cached ≤512px webp instead of the original —
    grid tiles were decoding multi-MB full-resolution renders, which locked
    up the browser once the feed accumulated a few pages."""
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    target = _validate_path(root, rel_path)
    if not target or not target.is_file():
        return error_response("not found", 404)

    if request.query.get("thumb") == "1" and _item_type(target) == "image":
        try:
            cache = _thumb_cache_path(scope, rel_path, target.stat())
            if not cache.is_file():
                import asyncio
                if not await asyncio.to_thread(_generate_thumb, target, cache):
                    cache = None
            if cache and cache.is_file():
                return web.FileResponse(cache, headers={"Cache-Control": "public, max-age=3600"})
        except Exception:
            logger.debug("thumb lookup failed for %s", target, exc_info=True)
        # fall through to the original on any failure

    return web.FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


@routes.get("/promptchain/browse/meta")
async def _api_browse_meta(request):
    """Extract metadata from an image file. Caches to DB on first read."""
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    target = _validate_path(root, rel_path)
    if not target or not target.is_file():
        return error_response("not found", 404)

    # check DB first — if already hashed and stored, return cached meta
    try:
        import asyncio
        from .history_db import compute_hash, get_image_meta, _get_conn, _write_lock
        import time

        # SHA256 on a multi-MB file blocks the event loop for tens of ms
        # if we call it directly; push to a thread so other requests
        # stay responsive while a hash runs.
        image_hash = await asyncio.to_thread(compute_hash, target)

        existing = get_image_meta(image_hash)
        if existing:
            return web.json_response(existing)

        # not in DB — extract from file and store
        meta = _extract_file_meta(target)
        meta["hash"] = image_hash

        # derive subfolder relative to scope root
        subfolder = str(target.parent.relative_to(root)).replace("\\", "/")
        if subfolder == ".":
            subfolder = ""

        # map scope to source_type
        source_type = "input" if scope == "input" else "output"

        now = int(time.time())
        with _write_lock:
            conn = _get_conn()
            conn.execute("""
                INSERT OR IGNORE INTO images
                    (hash, filename, subfolder, source_type, width, height, format, file_size, created_at,
                     prompt, negative, seed, model, steps, cfg, sampler, denoise)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                image_hash, target.name, subfolder, source_type,
                meta.get("width"), meta.get("height"),
                target.suffix.lower().lstrip("."),
                meta.get("file_size"), now,
                meta.get("prompt"), meta.get("negative"),
                meta.get("seed"), meta.get("model"),
                meta.get("steps"), meta.get("cfg"),
                meta.get("sampler"), meta.get("denoise"),
            ))
            conn.commit()

        return web.json_response(meta)
    except Exception:
        # fallback: just extract without caching
        meta = _extract_file_meta(target)
        return web.json_response(meta)


def _extract_file_meta(path: Path) -> dict:
    """Read metadata from image file — PNG text chunks or EXIF."""
    stat = path.stat()
    meta = {
        "filename": path.name,
        "file_size": stat.st_size,
        "created_at": int(stat.st_mtime),
    }

    w, h = _image_dimensions(path)
    if w and h:
        meta["width"] = w
        meta["height"] = h

    ext = path.suffix.lower()
    if ext == ".png":
        _extract_png_meta(path, meta)
    elif ext in (".jpg", ".jpeg"):
        _extract_exif_meta(path, meta)

    return meta


def _extract_png_meta(path: Path, meta: dict):
    """Read PNG tEXt/iTXt chunks for embedded generation parameters."""
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return

            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                length = struct.unpack(">I", chunk_hdr[:4])[0]
                chunk_type = chunk_hdr[4:8]

                if chunk_type in (b"tEXt", b"iTXt"):
                    data = f.read(length)
                    f.read(4)  # CRC
                    if chunk_type == b"tEXt":
                        sep = data.find(b"\x00")
                        if sep >= 0:
                            key = data[:sep].decode("latin-1", errors="replace")
                            val = data[sep + 1:].decode("utf-8", errors="replace")
                            _parse_png_text_field(key, val, meta)
                    elif chunk_type == b"iTXt":
                        sep = data.find(b"\x00")
                        if sep >= 0:
                            key = data[:sep].decode("utf-8", errors="replace")
                            # iTXt: key\0 compression_flag\0 compression_method\0 lang\0 translated_key\0 text
                            rest = data[sep + 1:]
                            parts = rest.split(b"\x00", 4)
                            if len(parts) >= 5:
                                val = parts[4].decode("utf-8", errors="replace")
                            elif len(parts) >= 1:
                                val = parts[-1].decode("utf-8", errors="replace")
                            else:
                                val = ""
                            _parse_png_text_field(key, val, meta)
                elif chunk_type == b"IEND":
                    break
                else:
                    f.seek(length + 4, 1)  # skip data + CRC
    except Exception:
        pass


def _parse_png_text_field(key: str, val: str, meta: dict):
    """Parse a single PNG text field into metadata."""
    if key == "parameters":
        # A1111-style: first line(s) = positive, after "Negative prompt:" = negative, after "Steps:" = settings
        _parse_a1111_parameters(val, meta)
    elif key == "prompt":
        # ComfyUI workflow prompt JSON — try to extract KSampler settings
        try:
            prompt_data = json.loads(val)
            _parse_comfyui_prompt(prompt_data, meta)
        except Exception:
            pass
    elif key == "workflow":
        meta["_has_workflow"] = True


def _parse_a1111_parameters(text: str, meta: dict):
    """Parse A1111 'parameters' text chunk."""
    lines = text.strip().split("\n")
    neg_idx = -1
    settings_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("Negative prompt:"):
            neg_idx = i
        if line.startswith("Steps:"):
            settings_idx = i

    # positive prompt
    end = neg_idx if neg_idx >= 0 else (settings_idx if settings_idx >= 0 else len(lines))
    meta["prompt"] = "\n".join(lines[:end]).strip()

    # negative prompt
    if neg_idx >= 0:
        neg_end = settings_idx if settings_idx >= 0 else len(lines)
        neg_text = "\n".join(lines[neg_idx:neg_end])
        if neg_text.startswith("Negative prompt:"):
            neg_text = neg_text[len("Negative prompt:"):].strip()
        meta["negative"] = neg_text

    # settings line
    if settings_idx >= 0:
        settings_line = lines[settings_idx]
        for part in settings_line.split(","):
            part = part.strip()
            if ":" in part:
                k, v = part.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "steps": meta["steps"] = int(v) if v.isdigit() else v
                elif k == "sampler": meta["sampler"] = v
                elif k == "cfg scale": meta["cfg"] = float(v) if v.replace(".", "").isdigit() else v
                elif k == "seed": meta["seed"] = int(v) if v.isdigit() else v
                elif k == "model": meta["model"] = v
                elif k == "model hash": meta["model_hash"] = v
                elif k == "denoising strength":
                    try: meta["denoise"] = float(v)
                    except ValueError: pass
                elif k == "size":
                    # "512x768"
                    if "x" in v:
                        parts = v.split("x")
                        if len(parts) == 2:
                            try:
                                meta["width"] = int(parts[0])
                                meta["height"] = int(parts[1])
                            except ValueError:
                                pass


def _parse_comfyui_prompt(prompt: dict, meta: dict):
    """Extract generation settings from ComfyUI prompt JSON."""
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "")
        inputs = node.get("inputs", {})

        # KSampler variants
        if "KSampler" in class_type or "sampler" in class_type.lower():
            if "seed" in inputs and meta.get("seed") is None:
                seed = inputs["seed"]
                if isinstance(seed, (int, float)):
                    meta["seed"] = int(seed)
            if "steps" in inputs and meta.get("steps") is None:
                meta["steps"] = inputs["steps"]
            if "cfg" in inputs and meta.get("cfg") is None:
                meta["cfg"] = inputs["cfg"]
            if "sampler_name" in inputs and meta.get("sampler") is None:
                meta["sampler"] = inputs["sampler_name"]
            if "denoise" in inputs and meta.get("denoise") is None:
                meta["denoise"] = inputs["denoise"]

        # checkpoint loader
        if "CheckpointLoader" in class_type or "checkpoint" in class_type.lower():
            ckpt = inputs.get("ckpt_name") or inputs.get("model_name")
            if ckpt and meta.get("model") is None:
                meta["model"] = ckpt

        # CLIP Text Encode
        if class_type in ("CLIPTextEncode", "CLIPTextEncodeSDXL"):
            text = inputs.get("text", "")
            if isinstance(text, str) and len(text) > 5:
                # heuristic: shorter text or text with "negative" in node title is negative
                if meta.get("prompt") is None:
                    meta["prompt"] = text
                elif meta.get("negative") is None:
                    meta["negative"] = text


def _extract_exif_meta(path: Path, meta: dict):
    """Read basic EXIF metadata from JPEG files."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return
            # look for EXIF APP1 marker
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    break
                if marker[1] == 0xE1:  # APP1 (EXIF)
                    length = struct.unpack(">H", f.read(2))[0]
                    data = f.read(length - 2)
                    # check for UserComment or ImageDescription containing parameters
                    text = data.decode("utf-8", errors="replace")
                    if "parameters" in text.lower() or "steps:" in text.lower():
                        # try to find A1111-style params embedded in EXIF
                        idx = text.find("Steps:")
                        if idx > 0:
                            _parse_a1111_parameters(text[:idx + 200], meta)
                    break
                elif marker[1] == 0xDA:  # SOS — end of headers
                    break
                else:
                    length = struct.unpack(">H", f.read(2))[0]
                    f.seek(length - 2, 1)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# File Operations
# ---------------------------------------------------------------------------

@routes.post("/promptchain/browse/favorite")
async def _api_browse_favorite(request):
    """Star or unstar a file/folder."""
    data, err = await parse_json(request)
    if err: return err
    scope = data.get("scope", "output")
    rel_path = data.get("path", "")
    on = bool(data.get("on", True))

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    target = _validate_path(root, rel_path)
    if not target or not target.exists():
        return error_response("not found", 404)

    from .history_db import set_favorite
    set_favorite(scope, rel_path, on)
    return ok_response({"path": rel_path, "favorite": on})


@routes.post("/promptchain/browse/mkdir")
async def _api_browse_mkdir(request):
    """Create a new folder."""
    data, err = await parse_json(request)
    if err: return err
    scope = data.get("scope", "output")
    rel_path = data.get("path", "")
    name = data.get("name", "").strip()

    if not name or "/" in name or "\\" in name or name.startswith("."):
        return error_response("invalid folder name")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    parent = _validate_path(root, rel_path)
    if not parent or not parent.is_dir():
        return error_response("invalid path")

    target = parent / name
    if target.exists():
        return error_response("already exists", 409)

    try:
        target.mkdir(parents=False)
    except OSError:
        logger.warning("mkdir failed for %s", target, exc_info=True)
        return error_response("mkdir failed", 500)

    return ok_response({"path": str(target.relative_to(root)).replace("\\", "/")})


@routes.post("/promptchain/browse/rename")
async def _api_browse_rename(request):
    """Rename a file or folder."""
    data, err = await parse_json(request)
    if err: return err
    scope = data.get("scope", "output")
    rel_path = data.get("path", "")
    new_name = data.get("name", "").strip()

    if not new_name or "/" in new_name or "\\" in new_name or new_name.startswith("."):
        return error_response("invalid name")
    if not rel_path:
        return error_response("cannot rename root")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    target = _validate_path(root, rel_path)
    if not target or not target.exists():
        return error_response("not found", 404)

    dest = target.parent / new_name
    if dest.exists():
        return error_response("already exists", 409)

    try:
        target.rename(dest)
    except OSError:
        logger.warning("rename failed for %s -> %s", target, dest, exc_info=True)
        return error_response("rename failed", 500)

    new_rel = str(dest.relative_to(root)).replace("\\", "/")
    try:
        from .history_db import move_favorites
        move_favorites(scope, rel_path, scope, new_rel)
    except Exception:
        logger.debug("favorite re-key failed on rename", exc_info=True)

    return ok_response({"path": new_rel})


@routes.post("/promptchain/browse/delete")
async def _api_browse_delete(request):
    """Delete files or folders."""
    data, err = await parse_json(request)
    if err: return err
    scope = data.get("scope", "output")
    paths = data.get("paths", [])

    if not paths or not isinstance(paths, list):
        return error_response("no paths provided")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    import shutil
    deleted = []
    errors = []
    for rel in paths:
        target = _validate_path(root, rel)
        if not target or not target.exists():
            errors.append({"path": rel, "error": "not found"})
            continue
        # safety: never delete the scope root itself
        if target.resolve() == root.resolve():
            errors.append({"path": rel, "error": "cannot delete root"})
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted.append(rel)
        except OSError as e:
            errors.append({"path": rel, "error": str(e)})

    if deleted:
        try:
            from .history_db import remove_favorites
            remove_favorites(scope, deleted)
        except Exception:
            logger.debug("favorite prune failed on delete", exc_info=True)

    return web.json_response({"deleted": deleted, "errors": errors})


@routes.post("/promptchain/browse/paste")
async def _api_browse_paste(request):
    """Move or copy files/folders into a destination folder."""
    data, err = await parse_json(request)
    if err: return err
    src_scope = data.get("srcScope", "output")
    dst_scope = data.get("dstScope", "output")
    dst_path = data.get("dstPath", "")
    paths = data.get("paths", [])
    op = data.get("op", "copy")  # "copy" or "cut"
    conflict_res = data.get("conflictResolution", None)  # None | "replace" | "skip" | "rename"

    if not paths or not isinstance(paths, list):
        return error_response("no paths provided")

    src_root = _get_scope_root(src_scope)
    dst_root = _get_scope_root(dst_scope)
    if not src_root or not dst_root:
        return error_response("invalid scope")

    dst_dir = _validate_path(dst_root, dst_path)
    if not dst_dir or not dst_dir.is_dir():
        return error_response("invalid destination")

    # detect conflicts when no resolution strategy provided
    if conflict_res is None:
        conflicts = []
        for rel in paths:
            src = _validate_path(src_root, rel)
            if not src or not src.exists():
                continue
            dest = dst_dir / src.name
            if dest.exists():
                conflicts.append(src.name)
        if conflicts:
            return web.json_response({
                "conflicts": conflicts,
                "total": len(paths),
                "conflictCount": len(conflicts),
            })
        conflict_res = "rename"

    import shutil
    pasted = []
    # source-relative twins of `pasted` — the client removes moved items
    # from the source view by these (dest paths never match source items)
    pasted_sources = []
    errors = []
    for rel in paths:
        src = _validate_path(src_root, rel)
        if not src or not src.exists():
            errors.append({"path": rel, "error": "not found"})
            continue
        if src.is_symlink():
            errors.append({"path": rel, "error": "symlinks not supported"})
            continue
        dest = dst_dir / src.name
        if dest.exists():
            if conflict_res == "skip":
                continue
            elif conflict_res == "replace":
                try:
                    if dest.is_dir():
                        shutil.rmtree(str(dest))
                    else:
                        dest.unlink()
                except OSError as e:
                    errors.append({"path": rel, "error": str(e)})
                    continue
            else:  # "rename"
                stem, ext = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = dst_dir / f"{stem} ({n}){ext}"
                    n += 1
        try:
            if op == "cut":
                shutil.move(str(src), str(dest))
            else:
                if src.is_dir():
                    shutil.copytree(str(src), str(dest))
                else:
                    shutil.copy2(str(src), str(dest))
            # assign new UUID to copied workflow files to prevent duplication
            if op == "copy" and dest.suffix.lower() == ".json":
                _dedup_workflow_uuid(dest)
            dest_rel = str(dest.relative_to(dst_root)).replace("\\", "/")
            pasted.append(dest_rel)
            pasted_sources.append(rel)
            if op == "cut":
                try:
                    from .history_db import move_favorites
                    move_favorites(src_scope, rel, dst_scope, dest_rel)
                except Exception:
                    logger.debug("favorite re-key failed on move", exc_info=True)
        except OSError as e:
            errors.append({"path": rel, "error": str(e)})

    return web.json_response({"pasted": pasted, "pastedSources": pasted_sources, "errors": errors})


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1048576:
        return f"{b / 1024:.1f} KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1073741824:.2f} GB"


@routes.get("/promptchain/browse/properties")
async def _api_browse_properties(request):
    """Return detailed properties for a single file or folder."""
    scope = request.query.get("scope", "output")
    rel_path = request.query.get("path", "")

    root = _get_scope_root(scope)
    if not root:
        return error_response("invalid scope")

    target = _validate_path(root, rel_path)
    if not target or not target.exists():
        return error_response("not found", 404)

    stat = target.stat()
    item_type = _item_type(target)
    props = {
        "name": target.name,
        "type": item_type,
        "path": rel_path,
        "fullPath": str(target),
        "created": int(stat.st_ctime),
        "modified": int(stat.st_mtime),
    }

    if target.is_file():
        props["size"] = stat.st_size
        props["sizeFormatted"] = _fmt_size(stat.st_size)

    if item_type == "folder":
        props["childCount"] = _child_count(target)
    elif item_type == "image":
        w, h = _image_dimensions(target)
        if w and h:
            props["width"] = w
            props["height"] = h

    return web.json_response(props)


@routes.post("/promptchain/browse/upload")
async def _api_browse_upload(request):
    """Upload files from external drag-drop into a scope/path."""
    reader = await request.multipart()
    scope = None
    rel_path = ""
    uploaded = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "scope":
            scope = (await part.text()).strip()
        elif part.name == "path":
            rel_path = (await part.text()).strip()
        elif part.name == "files":
            filename = part.filename
            if not filename or filename.startswith("."):
                continue
            root = _get_scope_root(scope or "input")
            if not root:
                continue
            # support subdirectory in filename (from folder drops)
            file_rel = filename.replace("\\", "/")
            dst = _validate_path(root, rel_path)
            if not dst or not dst.is_dir():
                continue
            dest = dst / file_rel
            # reject filenames that escape the scope root via traversal
            try:
                dest.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            # auto-rename on conflict
            if dest.exists():
                stem, ext = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = dest.parent / f"{stem} ({n}){ext}"
                    n += 1
            # Write through a sibling temp file so the final path never
            # contains a partially-written or oversize payload.  The
            # previous approach wrote direct-to-dest then unlinked on
            # size violation — if unlink failed, the bad file persisted.
            tmp = dest.with_name(dest.name + ".part")
            bytes_written = 0
            oversize = False
            try:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(8192)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > _MAX_UPLOAD_BYTES:
                            oversize = True
                            break
                        f.write(chunk)
                if oversize:
                    tmp.unlink(missing_ok=True)
                    return error_response("upload exceeds size limit", 413)
                os.replace(tmp, dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            uploaded.append(str(dest.relative_to(root)).replace("\\", "/"))

    if not uploaded:
        return error_response("no files uploaded")
    return web.json_response({"uploaded": uploaded})


def _dedup_workflow_uuid(path: Path):
    """Replace workflow UUID with a fresh one and clone image associations."""
    import uuid as _uuid
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict) and data.get("id"):
            old_id = data["id"]
            new_id = str(_uuid.uuid4())
            data["id"] = new_id
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # clone image associations from old workflow to new
            try:
                from .history_db import clone_and_register_workflow_atomic
                clone_and_register_workflow_atomic(old_id, new_id, str(path))
            except Exception:
                pass
    except Exception:
        pass
