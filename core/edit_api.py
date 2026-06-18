# Viewer "Inpaint"/"Edit" support: Inpaint Apply renders to ComfyUI's temp dir
# (PreviewImage), so discarded attempts never touch the output tree; Save
# finalizes the chosen render by copying it into the output dir under a
# SaveImage-style prefix and recording it with parent lineage — no re-render.
# Edit Save receives the airbrushed composite from the browser and persists it
# the same way, re-attaching the parent's embedded prompt/workflow chunks
# (canvas export strips them) so the result stays inpaint/upscale-able.

import asyncio
import io as _io
import json
import os
import re
import shutil
import time

import folder_paths
import node_helpers
import server
from aiohttp import web
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from .api_utils import error_response, parse_json
from .history_db import record_image

routes = server.PromptServer.instance.routes


def _format_prefix(prefix: str, fallback: str = "inpaint/inpaint") -> str:
    """Resolve SaveImage %date:...% tokens (the subset PromptChain emits)."""
    def date_token(match):
        fmt = match.group(1)
        now = time.localtime()
        out = fmt
        for token, value in [
            ("yyyy", time.strftime("%Y", now)), ("MM", time.strftime("%m", now)),
            ("dd", time.strftime("%d", now)), ("hh", time.strftime("%H", now)),
            ("mm", time.strftime("%M", now)), ("ss", time.strftime("%S", now)),
        ]:
            out = out.replace(token, value)
        return out
    return re.sub(r"%date:([^%]+)%", date_token, prefix or "").strip() or fallback


def _next_counter_name(directory: str, basename: str, ext: str) -> str:
    """SaveImage-style _00001_ counter suffix, scanning existing files."""
    pattern = re.compile(re.escape(basename) + r"_(\d{5})_" + re.escape(ext) + r"$")
    highest = 0
    try:
        for name in os.listdir(directory):
            m = pattern.match(name)
            if m:
                highest = max(highest, int(m.group(1)))
    except OSError:
        pass
    return f"{basename}_{highest + 1:05}_{ext}"


@routes.post("/promptchain/save-temp-image")
async def save_temp_image(request):
    data, err = await parse_json(request)
    if err:
        return err
    filename = (data.get("filename") or "").strip()
    subfolder = (data.get("subfolder") or "").strip()
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return error_response("invalid filename")
    temp_root = folder_paths.get_temp_directory()
    src = os.path.normpath(os.path.join(temp_root, subfolder, filename))
    if not src.startswith(os.path.normpath(temp_root)) or not os.path.isfile(src):
        return error_response("temp image not found", 404)

    prefix = _format_prefix(data.get("prefix") or "")
    rel_dir, basename = os.path.split(prefix)
    out_root = folder_paths.get_output_directory()
    out_dir = os.path.normpath(os.path.join(out_root, rel_dir))
    if not out_dir.startswith(os.path.normpath(out_root)):
        return error_response("invalid prefix")
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(filename)[1] or ".png"
    out_name = _next_counter_name(out_dir, basename, ext)
    shutil.copy2(src, os.path.join(out_dir, out_name))

    meta = {k: data.get(k) for k in (
        "prompt", "negative", "seed", "model", "steps", "cfg",
        "sampler", "scheduler", "denoise", "parent_filename", "regions",
    )}
    entry = record_image(
        filename=out_name,
        subfolder=rel_dir.replace("\\", "/"),
        source_type="output",
        workflow_id=data.get("workflow_id") or None,
        metadata=meta,
    )
    if entry is None:
        return error_response("saved file could not be recorded", 500)
    # The mask this render consumed must outlive the age sweep so the saved
    # output stays re-applicable (the parent ref is pinned in record_image).
    from . import inpaint_files
    inpaint_files.pin_value(data.get("mask_filename") or "")
    return web.json_response(entry)


def _save_edit_png(raw: bytes, parent_path: str | None, out_path: str) -> None:
    """Write the browser composite, re-attaching the parent's prompt/workflow
    PNG chunks — the image WAS rendered with that recipe, then hand-painted,
    and without them the result can't anchor a follow-up inpaint/upscale."""
    img = node_helpers.pillow(Image.open, _io.BytesIO(raw))
    pnginfo = PngInfo()
    if parent_path and os.path.isfile(parent_path):
        try:
            parent = node_helpers.pillow(Image.open, parent_path)
            chunks = dict(parent.info or {})
            chunks.update(getattr(parent, "text", None) or {})
            for key in ("prompt", "workflow"):
                if isinstance(chunks.get(key), str) and chunks[key]:
                    pnginfo.add_text(key, chunks[key])
        except OSError:
            pass
    img.save(out_path, pnginfo=pnginfo)


@routes.post("/promptchain/save-edited-image")
async def save_edited_image(request):
    form = await request.post()
    upload = form.get("image")
    if upload is None or not getattr(upload, "file", None):
        return error_response("missing image upload")
    raw = upload.file.read()
    if not raw:
        return error_response("empty image upload")

    prefix = _format_prefix(str(form.get("prefix") or ""), fallback="edit/edit")
    rel_dir, basename = os.path.split(prefix)
    out_root = folder_paths.get_output_directory()
    out_dir = os.path.normpath(os.path.join(out_root, rel_dir))
    if not out_dir.startswith(os.path.normpath(out_root)):
        return error_response("invalid prefix")
    os.makedirs(out_dir, exist_ok=True)
    out_name = _next_counter_name(out_dir, basename, ".png")

    # The parent may be subfolder-scoped (e.g. "promptchain_inpaint/foo.png"), so
    # allow a relative subpath but keep it contained within the input dir — a
    # blanket slash-reject silently drops the parent's prompt/workflow chunks.
    parent_ref = str(form.get("parent_filename") or "").strip()
    parent_path = None
    if parent_ref and ".." not in parent_ref:
        input_root = os.path.normpath(folder_paths.get_input_directory())
        cand = os.path.normpath(os.path.join(input_root, parent_ref))
        if cand.startswith(input_root + os.sep):
            parent_path = cand
    try:
        await asyncio.to_thread(_save_edit_png, raw, parent_path, os.path.join(out_dir, out_name))
    except (OSError, ValueError) as e:
        return error_response(f"could not save image: {e}", 500)

    entry = record_image(
        filename=out_name,
        subfolder=rel_dir.replace("\\", "/"),
        source_type="output",
        workflow_id=str(form.get("workflow_id") or "") or None,
        metadata={"parent_filename": parent_ref or None, "prompt": ""},
    )
    if entry is None:
        return error_response("saved file could not be recorded", 500)
    return web.json_response(entry)


# ── layer-stack persistence ("don't-flatten = keep your PSD") ──
# A sidecar layered document keyed by the SAVED image's content hash: a doc.json
# manifest + one PNG per layer bitmap + per mask, under {user}/PromptChain/
# edit-docs/<hash>/. Re-opening Edit on that image restores the stack. The flat
# PNG saved by save-edited-image stays the source of truth; this is a convenience
# cache, capped by an LRU size budget. Content-addressed → free dedup.

_EDIT_DOC_BUDGET_BYTES = 2 * 1024 ** 3
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PLANE_RE = re.compile(r"L\d+(\.mask)?\.png")


def _edit_docs_root() -> str:
    return os.path.join(folder_paths.get_user_directory(), "PromptChain", "edit-docs")


def _rmtree_robust(path: str, tries: int = 6) -> None:
    """rmtree, retried briefly. On Windows a file that's momentarily open (e.g. a
    concurrent list-read counting layers) can't be deleted, and rmtree's
    ignore_errors SILENTLY skips it — leaving a stale doc.json behind. The lock
    is transient, so a few short retries clear it."""
    for _ in range(tries):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.05)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _evict_edit_docs(keep_hash: str) -> None:
    """LRU by doc.json mtime until under the size budget; never evict keep_hash."""
    root = _edit_docs_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    docs, total = [], 0
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        size = _dir_size(path)
        total += size
        manifest = os.path.join(path, "doc.json")
        try:
            mtime = os.path.getmtime(manifest if os.path.isfile(manifest) else path)
        except OSError:
            mtime = 0
        docs.append((mtime, size, name, path))
    if total <= _EDIT_DOC_BUDGET_BYTES:
        return
    docs.sort()  # oldest first
    for _mtime, size, name, path in docs:
        if total <= _EDIT_DOC_BUDGET_BYTES:
            break
        if name == keep_hash:
            continue
        _rmtree_robust(path)
        total -= size


@routes.post("/promptchain/edit-doc/{hash}")
async def save_edit_doc(request):
    h = request.match_info.get("hash", "")
    if not _HASH_RE.fullmatch(h):
        return error_response("invalid hash")
    form = await request.post()
    manifest = form.get("manifest")
    if manifest is None:
        return error_response("missing manifest")
    manifest_text = manifest if isinstance(manifest, str) else manifest.file.read().decode("utf-8")
    planes = {}
    for key, val in form.items():
        if key == "manifest" or not _PLANE_RE.fullmatch(key):
            continue
        f = getattr(val, "file", None)
        if f is not None:
            planes[key] = f.read()

    def _write():
        doc_dir = os.path.join(_edit_docs_root(), h)
        # rewrite from scratch so stale planes from a prior save don't linger
        _rmtree_robust(doc_dir)
        os.makedirs(doc_dir, exist_ok=True)
        with open(os.path.join(doc_dir, "doc.json"), "w", encoding="utf-8") as fp:
            fp.write(manifest_text)
        for name, data in planes.items():
            with open(os.path.join(doc_dir, name), "wb") as fp:
                fp.write(data)
        _evict_edit_docs(h)

    try:
        await asyncio.to_thread(_write)
    except OSError as e:
        return error_response(f"could not save edit doc: {e}", 500)
    return web.json_response({"ok": True})


@routes.get("/promptchain/edit-doc/{hash}")
async def get_edit_doc(request):
    h = request.match_info.get("hash", "")
    if not _HASH_RE.fullmatch(h):
        return error_response("invalid hash")
    path = os.path.join(_edit_docs_root(), h, "doc.json")
    if not os.path.isfile(path):
        return web.json_response({"exists": False}, status=404)
    try:
        os.utime(path, None)  # LRU touch — reopened docs survive eviction
    except OSError:
        pass
    return web.FileResponse(path)


@routes.get("/promptchain/edit-doc/{hash}/{file}")
async def get_edit_doc_file(request):
    h = request.match_info.get("hash", "")
    fname = request.match_info.get("file", "")
    if not _HASH_RE.fullmatch(h) or not (fname == "doc.json" or _PLANE_RE.fullmatch(fname)):
        return error_response("invalid request")
    root = os.path.normpath(_edit_docs_root())
    path = os.path.normpath(os.path.join(root, h, fname))
    if not path.startswith(root) or not os.path.isfile(path):
        return web.Response(status=404)
    return web.FileResponse(path)


@routes.get("/promptchain/edit-docs")
async def list_edit_docs(request):
    """Which hashes have a saved layer stack (→ a "layers" badge in lineage/the
    gallery), mapped to their layer count for the tooltip. One listdir + a small
    JSON read per doc; the frontend caches the result."""
    root = _edit_docs_root()

    def _scan():
        out = {}
        try:
            names = os.listdir(root)
        except OSError:
            return out
        for name in names:
            if not _HASH_RE.fullmatch(name):
                continue
            manifest = os.path.join(root, name, "doc.json")
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, "r", encoding="utf-8") as fp:
                    out[name] = len(json.load(fp).get("layers") or [])
            except (OSError, ValueError):
                out[name] = 0
        return out

    return web.json_response({"docs": await asyncio.to_thread(_scan)})


@routes.delete("/promptchain/edit-doc/{hash}")
async def delete_edit_doc(request):
    # Flatten saves to the SAME hash (pixels unchanged), so a flattened save must
    # actively remove any prior layer stack — else the file would still restore
    # its old layers. Idempotent: a missing dir is fine.
    h = request.match_info.get("hash", "")
    if not _HASH_RE.fullmatch(h):
        return error_response("invalid hash")
    await asyncio.to_thread(_rmtree_robust, os.path.join(_edit_docs_root(), h))
    return web.json_response({"ok": True})
