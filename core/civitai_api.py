# CivitAI API — model search, download management, and API key
# configuration endpoints.

import asyncio
import os
import threading
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web
import server

from . import civitai
from . import config as promptchain_config
from . import model_settings
from .api_utils import error_response, parse_json
from .shared import send_ws

routes = server.PromptServer.instance.routes


# ── offline download test mode ────────────────────────────────────
# Enabled only by the env var (set by the test launcher, never in code), so a
# real install can't accidentally be in test mode. When on, the download route
# redirects model-file fetches to the test-mirror route below, which serves
# fixtures from PROMPTCHAIN_FIXTURES_DIR — no internet, no second process.

def _test_downloads_on() -> bool:
    return os.environ.get("PROMPTCHAIN_TEST_DOWNLOADS", "").strip().lower() not in ("", "0", "false")


def _fixtures_dir() -> Path:
    return Path(os.environ.get("PROMPTCHAIN_FIXTURES_DIR", "").strip() or ".")


@routes.get("/promptchain/test-mirror/{name}")
async def _api_test_mirror(request):
    """Serve a fixture file by name (test mode only). Fault injection by name:
    `__fail` -> 500, `__stall` -> hang. Missing fixture -> 404 so the download
    fails cleanly. PROMPTCHAIN_MIRROR_THROTTLE (MB/s) animates the progress bar."""
    if not _test_downloads_on():
        return web.Response(status=404, text="test mode off")
    name = os.path.basename(request.match_info["name"])
    if "__fail" in name:
        return web.Response(status=500, text="injected failure")
    path = _fixtures_dir() / name
    if not path.is_file():
        return web.Response(status=404, text=f"fixture not found: {name}")

    total = path.stat().st_size
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "application/octet-stream",
        "Content-Length": str(total),
    })
    await resp.prepare(request)
    if "__stall" in name:
        await asyncio.sleep(600)
        return resp

    try:
        bps = int(float(os.environ.get("PROMPTCHAIN_MIRROR_THROTTLE", "0")) * 1024 * 1024)
    except ValueError:
        bps = 0
    sent = 0
    start = time.monotonic()
    with open(path, "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            try:
                await resp.write(buf)
            except (ConnectionResetError, ConnectionError):
                break
            sent += len(buf)
            if bps:
                delay = start + sent / bps - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
    await resp.write_eof()
    return resp


@routes.get("/promptchain/civitai/search")
async def _api_civitai_search(request):
    query = request.query.get("q", "").strip()
    if not query:
        return error_response("missing q parameter")
    try:
        limit = min(int(request.query.get("limit", "10")), 20)
    except (ValueError, TypeError):
        limit = 10
    cursor = request.query.get("cursor", "")
    data = await civitai.search_models(query, limit=limit, cursor=cursor)
    return web.json_response(data)


def _installed_version_ids_for_model(model_id: int) -> list[int]:
    """Walk the in-memory config index once, collecting civitai_version_ids
    whose config points at `model_id`.  The index is already deduped by
    config identity so we skip configs we've already counted."""
    seen_configs: set[int] = set()
    vids: list[int] = []
    index = model_settings._get_config_index()
    for cfg in index.get("by_quick_hash", {}).values():
        if id(cfg) in seen_configs:
            continue
        seen_configs.add(id(cfg))
        try:
            mid = cfg.get("civitai_model_id")
            if mid is None or int(mid) != model_id:
                continue
            vid = cfg.get("civitai_version_id")
            if vid is not None:
                vids.append(int(vid))
        except (TypeError, ValueError):
            continue
    return vids


@routes.get("/promptchain/civitai/model-versions")
async def _api_civitai_model_versions(request):
    """List all versions of a CivitAI model plus which civitai_version_ids
    the user already has installed locally.  `?force=1` bypasses the
    disk-backed TTL cache so the user can refresh on demand."""
    try:
        model_id = int(request.query.get("model_id", "").strip())
    except (ValueError, TypeError):
        return error_response("missing or invalid model_id")

    force = request.query.get("force", "").strip() in ("1", "true")
    versions = await civitai.fetch_model_versions(model_id, force=force)
    entry = civitai.get_cached_versions_entry(model_id)

    return web.json_response({
        "versions": versions,
        "installed_version_ids": _installed_version_ids_for_model(model_id),
        "fetched_at": entry.get("fetched_at") if entry else None,
        "deleted": bool(entry.get("deleted")) if entry else False,
    })


@routes.post("/promptchain/civitai/file-size")
async def _api_civitai_file_size(request):
    """Best-effort byte size for a download URL. Catalog presets carry no size,
    so the download modal probes Content-Length to display it before download."""
    data, err = await parse_json(request)
    if err:
        return err
    url = (data.get("url") or "").strip()
    size = await asyncio.to_thread(civitai.probe_file_size, url) if url else 0
    return web.json_response({"size_bytes": size})


@routes.post("/promptchain/civitai/download")
async def _api_civitai_download(request):
    """Start downloading a model file. Supports CivitAI and HuggingFace URLs."""
    data, err = await parse_json(request)
    if err: return err
    url = data.get("url", "").strip()
    filename = data.get("filename", "").strip()
    folder_type = data.get("folder", "checkpoints").strip()
    # Optional — when present, the versions-cache entry for this model
    # is invalidated on successful download so the next panel open sees
    # the newly-installed version instead of continuing to advertise it.
    civitai_model_id = data.get("civitai_model_id")

    # Test mode: pull the file from our own mirror route (keyed by filename)
    # instead of the real CDN, so we only need the filename — the real url may
    # be absent (e.g. catalog entries with no download source). request.host
    # gives whatever host:port the client reached us on, so no port to
    # configure; a localhost url also skips the civitai.com key requirement.
    if _test_downloads_on():
        if not filename:
            return error_response("missing filename")
        safe = urllib.parse.quote(filename.rsplit("/", 1)[-1])
        url = f"http://{request.host}/promptchain/test-mirror/{safe}"
    elif not url or not filename:
        return error_response("missing url or filename")

    state = civitai.get_download_state()
    if state["active"]:
        return error_response("download already in progress", 409)

    def on_progress(downloaded, total):
        progress = (downloaded / total * 100) if total else 0
        send_ws("promptchain_download_progress", {
            "filename": filename,
            "downloaded": downloaded,
            "total": total,
            "progress": round(progress, 1),
        })

    def run_download():
        try:
            civitai.download_model(url, filename, on_progress=on_progress, folder_type=folder_type)
            if civitai_model_id is not None:
                try:
                    civitai.invalidate_versions_cache(int(civitai_model_id))
                except (TypeError, ValueError):
                    pass
            send_ws("promptchain_download_done", {"filename": filename, "status": "completed"})
        except Exception as e:
            send_ws("promptchain_download_done", {"filename": filename, "status": "failed", "error": str(e)})

    threading.Thread(target=run_download, daemon=True).start()
    return web.json_response({"status": "started", "filename": filename})


@routes.get("/promptchain/civitai/download-status")
async def _api_download_status(request):
    return web.json_response(civitai.get_download_state())


@routes.post("/promptchain/civitai/download-cancel")
async def _api_download_cancel(request):
    civitai.cancel_download()
    return web.json_response({"status": "ok"})


@routes.post("/promptchain/civitai/api-key")
async def _api_civitai_set_key(request):
    """Save CivitAI API key to config."""
    data, err = await parse_json(request)
    if err: return err
    key = data.get("key", "").strip()
    config = promptchain_config.load()
    config["civitai_api_key"] = key
    promptchain_config.save(config)
    return web.json_response({"status": "ok"})


@routes.get("/promptchain/civitai/api-key")
async def _api_civitai_get_key(request):
    """Check if CivitAI API key is configured."""
    key = civitai._get_api_key()
    return web.json_response({"has_key": bool(key)})


@routes.post("/promptchain/civitai/validate-key")
async def _api_civitai_validate_key(request):
    """Validate a CivitAI API key by hitting /api/v1/me.
    If no key in payload, validates the stored/env key instead."""
    data, err = await parse_json(request)
    if err: return err
    key = data.get("key", "").strip()
    if not key:
        key = civitai._get_api_key() or ""
    if not key:
        return web.json_response({"valid": False, "error": "No key provided"})
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {key}"}
            async with session.get("https://civitai.com/api/v1/me", headers=headers, timeout=civitai._TIMEOUT) as resp:
                if resp.status == 200:
                    user = await resp.json()
                    return web.json_response({"valid": True, "username": user.get("username", "")})
                return web.json_response({"valid": False, "error": "Invalid API key"})
    except Exception as e:
        return web.json_response({"valid": False, "error": f"Connection failed: {e}"})
