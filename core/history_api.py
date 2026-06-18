# History API — image history, workflow tracking, thumbnail serving,
# lineage queries, orphan detection, and image prompt extraction.

import asyncio
import logging
import os

from aiohttp import web
import server

from .api_utils import error_response, ok_response, parse_json, validate_content_path
from .shared import HASH_RE

logger = logging.getLogger("promptchain.history_api")

routes = server.PromptServer.instance.routes


# ── image prompt extraction ──────────────────────────────────────

@routes.post("/promptchain/extract-image-prompts")
async def _api_extract_image_prompts(request):
    """Extract pos/neg prompts from a ComfyUI-generated PNG's metadata."""
    from .load_image_prompts import extract_prompts_from_file

    data, err = await parse_json(request)
    if err: return err
    image_path = data.get("image_path", "")

    if not image_path:
        return error_response("missing image_path")

    # Reject path-traversal attempts before attempting any resolution.
    # validate_content_path is the final authority, but an up-front check
    # keeps a single bug in folder_paths.get_annotated_filepath from
    # turning into an arbitrary read.
    if ".." in image_path.replace("\\", "/").split("/"):
        return error_response("invalid image_path", 400)

    if not os.path.isabs(image_path):
        try:
            import folder_paths as fp
            image_path = fp.get_annotated_filepath(image_path)
        except Exception:
            logger.debug("failed to resolve image path %s", image_path, exc_info=True)

    image_path = validate_content_path(image_path)
    if not image_path or not os.path.isfile(image_path):
        return error_response("file not found or not in allowed directory", 403)

    try:
        positive, negative = extract_prompts_from_file(image_path)
        return web.json_response({"positive": positive, "negative": negative})
    except Exception:
        logger.warning("prompt extraction failed for %s", image_path, exc_info=True)
        return error_response("prompt extraction failed", 500)


# 100 MB is well past any legitimate PNG/JPEG workflow image while still
# bounding memory for the in-RAM PIL.Image.open path.
_MAX_EXTRACT_BYTES = 100 * 1024 * 1024


@routes.post("/promptchain/extract-prompts-upload")
async def _api_extract_prompts_upload(request):
    """Extract pos/neg prompts from an uploaded image's embedded ComfyUI metadata."""
    from .load_image_prompts import extract_prompts_from_bytes

    reader = await request.multipart()
    raw = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name != "file":
            continue
        buf = bytearray()
        while True:
            chunk = await part.read_chunk(64 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_EXTRACT_BYTES:
                return error_response("file too large", 413)
        raw = bytes(buf)
        break

    if not raw:
        return error_response("missing file")

    try:
        positive, negative = extract_prompts_from_bytes(raw)
        return web.json_response({"positive": positive, "negative": negative})
    except Exception:
        logger.warning("uploaded prompt extraction failed", exc_info=True)
        return error_response("prompt extraction failed", 500)


# ── workflow management ──────────────────────────────────────────

@routes.post("/promptchain/workflow-check")
async def _api_workflow_check(request):
    data, err = await parse_json(request)
    if err: return err
    workflow_id = data.get("workflow_id", "")
    filepath = data.get("filepath", "")
    if not workflow_id or not filepath:
        return error_response("missing workflow_id or filepath")

    from .history_db import try_register_workflow_atomic, clone_and_register_workflow_atomic, register_workflow

    # resolve path
    user_dir = os.path.join(server.PromptServer.instance.web_root or "", "user", "default")
    try:
        import folder_paths as fp
        user_dir = fp.get_user_directory()
    except Exception:
        logger.debug("failed to resolve user directory", exc_info=True)
    if not os.path.isabs(filepath):
        filepath = os.path.normpath(os.path.join(user_dir, filepath))

    registered, existing_path = try_register_workflow_atomic(workflow_id, filepath)

    if registered:
        return ok_response({"action": "ok", "workflow_id": workflow_id})

    # different path — check if old file still exists
    if not os.path.exists(existing_path):
        register_workflow(workflow_id, filepath)
        return ok_response({"action": "update_path", "workflow_id": workflow_id})

    # both files exist — duplicate. generate new UUID, clone attachments
    import uuid as _uuid
    new_uuid = str(_uuid.uuid4())
    success, cloned = clone_and_register_workflow_atomic(workflow_id, new_uuid, filepath)
    if not success:
        return error_response("clone failed", 500)

    return ok_response({
        "action": "clone",
        "workflow_id": workflow_id,
        "new_uuid": new_uuid,
        "cloned_count": cloned,
    })


@routes.post("/promptchain/client-error")
async def _api_client_error(request):
    """Relay frontend failures into the server log — browser consoles are
    invisible during remote debugging, and graph-build errors otherwise die
    client-side with no trace here."""
    data, err = await parse_json(request)
    if err: return err
    where = str(data.get("where", ""))[:200]
    message = str(data.get("message", ""))[:2000]
    stack = str(data.get("stack", ""))[:8000]
    logger.error("CLIENT ERROR [%s]: %s\n%s", where, message, stack)
    return ok_response({})


@routes.post("/promptchain/workflow-clone")
async def _api_workflow_clone(request):
    """Clone image associations from one workflow UUID to another."""
    data, err = await parse_json(request)
    if err: return err
    from_id = data.get("from_id", "")
    to_id = data.get("to_id", "")
    filepath = data.get("filepath", "")
    if not from_id or not to_id:
        return error_response("missing from_id or to_id")

    from .history_db import clone_and_register_workflow_atomic
    success, cloned = clone_and_register_workflow_atomic(from_id, to_id, filepath)
    if not success:
        return error_response("clone failed", 500)

    return ok_response({"cloned_count": cloned})


# ── generation recording ─────────────────────────────────────────

@routes.post("/promptchain/generation/{workflow_id}")
async def _api_record_generation(request):
    workflow_id = request.match_info.get("workflow_id", "")
    if not workflow_id:
        return error_response("missing workflow_id")
    data, err = await parse_json(request)
    if err: return err
    filename = data.get("filename", "")
    if not filename:
        return error_response("missing filename")

    from .history_db import record_image
    from .thumbs import get_or_create_thumbnail
    result = record_image(
        filename=filename,
        subfolder=data.get("subfolder", ""),
        source_type=data.get("source_type", "output"),
        workflow_id=workflow_id,
        metadata={
            "prompt": data.get("prompt"),
            "negative": data.get("negative"),
            "seed": data.get("seed"),
            "model": data.get("model"),
            "steps": data.get("steps"),
            "cfg": data.get("cfg"),
            "sampler": data.get("sampler"),
            "scheduler": data.get("scheduler"),
            "parent_filename": data.get("parent_filename"),
            "denoise": data.get("denoise"),
            "regions": data.get("regions"),
        },
    )
    if result is None:
        return error_response("file not found", 404)
    # generate thumbnail immediately while source file exists.  PIL
    # resize is CPU-bound and synchronous, so push it off the event loop.
    if result.get("hash"):
        import asyncio
        await asyncio.to_thread(get_or_create_thumbnail, result["hash"])
    return web.json_response(result)


# ── image/thumbnail serving ──────────────────────────────────────

@routes.get("/promptchain/thumb/{hash}")
async def _api_serve_thumb(request):
    image_hash = request.match_info.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")

    from .thumbs import get_or_create_thumbnail
    import asyncio
    thumb_path = await asyncio.to_thread(get_or_create_thumbnail, image_hash)
    if not thumb_path:
        # no-store so a transient miss (thumb requested a beat before the record
        # commits it) is never cached by the browser — otherwise the <img> stays
        # broken even after the thumb lands. The gallery also retries on error.
        return web.Response(status=404, headers={"Cache-Control": "no-store"})

    return web.FileResponse(thumb_path, headers={
        "Content-Type": "image/webp",
        "Cache-Control": "public, max-age=86400",
    })


@routes.get("/promptchain/image/{hash}")
async def _api_serve_image(request):
    image_hash = request.match_info.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")

    from .history_db import resolve_image_path
    file_path = resolve_image_path(image_hash)
    if not file_path or not file_path.is_file():
        return error_response("not found", 404)

    import mimetypes as mt
    mime, _ = mt.guess_type(str(file_path))
    return web.FileResponse(file_path, headers={
        "Content-Type": mime or "application/octet-stream",
        "Cache-Control": "public, max-age=31536000",
    })


# ── workflow image queries ───────────────────────────────────────

@routes.get("/promptchain/workflow/{workflow_id}")
async def _api_workflow_images(request):
    workflow_id = request.match_info.get("workflow_id", "")
    if not workflow_id:
        return error_response("missing workflow_id")

    try:
        limit = int(request.query.get("limit", "100"))
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return error_response("invalid limit/offset")

    from .history_db import get_workflow_images, get_workflow_image_count
    images = get_workflow_images(workflow_id, limit=limit, offset=offset)
    total = get_workflow_image_count(workflow_id)
    return web.json_response({"images": images, "total": total})


@routes.get("/promptchain/count/{workflow_id}")
async def _api_workflow_count(request):
    workflow_id = request.match_info.get("workflow_id", "")
    if not workflow_id:
        return error_response("missing workflow_id")
    from .history_db import get_workflow_image_count
    return web.json_response({"count": get_workflow_image_count(workflow_id)})


@routes.post("/promptchain/workflow/{workflow_id}/clear")
async def _api_clear_workflow(request):
    workflow_id = request.match_info.get("workflow_id", "")
    if not workflow_id:
        return error_response("missing workflow_id")
    if request.content_length:
        data, err = await parse_json(request)
        if err: return err
    else:
        data = {}
    hashes = data.get("hashes", None)
    from .history_db import detach_workflow_images, detach_images
    if hashes and isinstance(hashes, list):
        count = detach_images(workflow_id, hashes)
    else:
        count = detach_workflow_images(workflow_id)
    return web.json_response({"cleared": count})


@routes.post("/promptchain/image-delete")
async def _api_image_delete(request):
    """Permanently delete an image by content hash. The same bytes can live at
    several paths at once (output/input/temp + a cached copy + the
    promptchain_source_<hash12> staging copies LoadImage uses for lineage), and
    resolve_image_path heals a stale record onto any surviving copy — so deleting
    one file let the image reappear on the next read. delete_image purges every
    replica and tombstones the record so heal-at-read can't resurrect it. Returns
    the primary scope + relative path so callers can broadcast the removal."""
    data, err = await parse_json(request)
    if err: return err
    image_hash = data.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")

    from .history_db import delete_image, get_image_meta
    # read meta BEFORE deleting — get_image_meta filters tombstoned rows, so the
    # broadcast path has to be captured while the record is still live
    meta = get_image_meta(image_hash) or {}
    result = delete_image(image_hash)
    if not result.get("deleted"):
        return web.json_response(result, status=404)

    sub = meta.get("subfolder") or ""
    fn = meta.get("filename") or ""
    return web.json_response({
        "deleted": True,
        "scope": meta.get("source_type") or "output",
        "path": (f"{sub}/{fn}" if sub else fn),
    })


# ── lineage + metadata ───────────────────────────────────────────

@routes.get("/promptchain/lineage/{hash}")
async def _api_lineage(request):
    image_hash = request.match_info.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")

    from .history_db import get_lineage
    data = get_lineage(image_hash)
    if not data["image"]:
        return error_response("not found", 404)
    return web.json_response(data)


@routes.get("/promptchain/image-meta/{hash}")
async def _api_image_meta(request):
    image_hash = request.match_info.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")

    from .history_db import get_image_meta
    meta = get_image_meta(image_hash)
    if not meta:
        return error_response("not found", 404)
    return web.json_response(meta)


@routes.post("/promptchain/check-orphans")
async def _api_check_orphans(request):
    data, err = await parse_json(request)
    if err: return err
    hashes = data.get("hashes", [])
    if not hashes or not isinstance(hashes, list):
        return web.json_response({"orphaned": []})
    from .history_db import check_orphans
    orphaned = check_orphans(hashes)
    return web.json_response({"orphaned": orphaned})


@routes.post("/promptchain/reattach")
async def _api_reattach_image(request):
    """Re-attach an orphaned image record to a user-supplied file. The record's
    key is the sha256 of its content, so the upload is accepted only when its
    digest matches — picking the wrong file cannot corrupt lineage. The
    verified bytes are staged as a content-addressed input copy (same
    convention image-workflow uses) and the record re-pointed there."""
    import hashlib
    import re

    # Stream the multipart body with a hard byte cap rather than buffering the
    # whole upload via request.post() — a re-attach only needs to digest-match
    # an existing record, so an oversize payload is never legitimate.
    reader = await request.multipart()
    image_hash = ""
    raw = None
    upload_name = ""
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "hash":
            image_hash = (await part.text()).strip()
        elif part.name == "image":
            upload_name = part.filename or ""
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > _MAX_EXTRACT_BYTES:
                    return error_response("file too large", 413)
            raw = bytes(buf)

    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")
    if not raw:
        return error_response("missing image upload")

    from .history_db import get_image_meta, reattach_record
    meta = get_image_meta(image_hash)
    if not meta:
        return error_response("not found", 404)

    digest = await asyncio.to_thread(lambda: hashlib.sha256(raw).hexdigest())
    if digest != image_hash:
        return error_response("file content does not match this image", 409)

    ext = os.path.splitext(upload_name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        fmt = (meta.get("format") or "").lower()
        ext = ".jpg" if fmt == "jpeg" else f".{fmt}" if fmt else ".png"

    import folder_paths
    name = f"promptchain_source_{image_hash[:12]}{ext}"
    dest = os.path.join(folder_paths.get_input_directory(), name)
    try:
        await asyncio.to_thread(_write_bytes, dest, raw)
    except OSError as e:
        return error_response(f"could not stage file: {e}", 500)
    reattach_record(image_hash, name, "", "input")
    return web.json_response(get_image_meta(image_hash))


def _write_bytes(dest: str, raw: bytes) -> None:
    with open(dest, "wb") as f:
        f.write(raw)
