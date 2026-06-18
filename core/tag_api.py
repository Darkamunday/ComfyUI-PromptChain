# Tag, Onboarding, and Iterate API — tag autocomplete search,
# onboarding status, and iteration state management endpoints.

from aiohttp import web
import server

from .api_utils import error_response, ok_response, parse_json

from .tags import get_store as get_tag_store, get_file_states as get_tag_file_states, restore_file as restore_tag_file
from . import config as promptchain_config
from .iterate_state import (
    reset_iterate_state,
    advance_iterate_state,
    set_iterate_state,
    set_subordinate_nodes,
)

routes = server.PromptServer.instance.routes


# ── onboarding endpoints ─────────────────────────────────────────

@routes.get("/promptchain/onboarding/status")
async def _api_onboarding_status(request):
    return web.json_response({"onboarded": promptchain_config.is_onboarded()})


@routes.post("/promptchain/onboarding/complete")
async def _api_onboarding_complete(request):
    promptchain_config.set_onboarded()
    get_tag_store().reload()
    return ok_response()


@routes.get("/promptchain/ai-setup/dismissed")
async def _api_ai_setup_dismissed(request):
    return web.json_response({"dismissed": promptchain_config.is_ai_setup_dismissed()})


@routes.post("/promptchain/ai-setup/dismiss")
async def _api_ai_setup_dismiss(request):
    promptchain_config.set_ai_setup_dismissed()
    return ok_response()


# ── iterate endpoints ────────────────────────────────────────────

@routes.post("/promptchain/iterate/reset")
async def _api_reset(request):
    data, err = await parse_json(request)
    if err: return err
    hash_key = data.get("content_hash")
    reset_iterate_state(hash_key)  # None clears all
    return ok_response()


@routes.post("/promptchain/iterate/advance")
async def _api_advance(request):
    data, err = await parse_json(request)
    if err: return err
    hash_key = data.get("content_hash")
    if not hash_key:
        return error_response("missing content_hash")
    result = advance_iterate_state(hash_key)
    if result is None:
        return error_response("unknown hash", 404)
    new_index, new_cycle, wrapped, prev_index, prev_cycle = result
    return web.json_response({
        "new_index": new_index,
        "new_cycle": new_cycle,
        "wrapped": wrapped,
        "prev_index": prev_index,
        "prev_cycle": prev_cycle,
    })


@routes.post("/promptchain/iterate/set-state")
async def _api_set_state(request):
    data, err = await parse_json(request)
    if err: return err
    hash_key = data.get("content_hash")
    index = data.get("index", 0)
    cycle = data.get("cycle", 1)
    if not hash_key:
        return error_response("missing content_hash")
    set_iterate_state(hash_key, index, cycle)
    return ok_response()


@routes.post("/promptchain/iterate/set-subordinates")
async def _api_set_subordinates(request):
    data, err = await parse_json(request)
    if err: return err
    node_ids = data.get("node_ids", [])
    set_subordinate_nodes(node_ids)
    return ok_response({"count": len(node_ids)})


# ── tag autocomplete endpoints ───────────────────────────────────

@routes.get("/promptchain/tags/sources")
async def _api_tag_sources(request):
    store = get_tag_store()
    return web.json_response({"sources": store.list_sources()})


@routes.get("/promptchain/tags/search")
async def _api_tag_search(request):
    source = request.query.get("source", "")
    query = request.query.get("q", "")
    if not source:
        return error_response("source parameter required")
    try:
        limit = min(int(request.query.get("limit", 20)), 100)
    except ValueError:
        limit = 20
    store = get_tag_store()
    results = store.search(source, query, limit)
    for r in results:
        r["source"] = source
    return web.json_response(results)


@routes.get("/promptchain/tags/search-stacked")
async def _api_tag_search_stacked(request):
    sources_param = request.query.get("sources", "")
    query = request.query.get("q", "")
    if not sources_param:
        return web.json_response([])
    sources = [s.strip() for s in sources_param.split(",") if s.strip()]
    try:
        limit = min(int(request.query.get("limit", 20)), 100)
    except ValueError:
        limit = 20
    store = get_tag_store()
    return web.json_response(store.search_stacked(sources, query, limit))


@routes.get("/promptchain/tags/similar")
async def _api_tag_similar(request):
    source = request.query.get("source", "")
    tag_name = request.query.get("q", "")
    if not source:
        return error_response("source parameter required")
    tag_id = None
    id_str = request.query.get("id", "")
    if id_str:
        try:
            tag_id = int(id_str)
        except ValueError:
            return error_response("invalid id")
    if not tag_id and not tag_name:
        return error_response("id or q parameter required")
    try:
        limit = min(int(request.query.get("limit", 10)), 50)
    except ValueError:
        limit = 10
    store = get_tag_store()
    return web.json_response({"tags": store.get_similar(source, tag_id=tag_id, tag_name=tag_name, limit=limit)})


@routes.get("/promptchain/tags/states")
async def _api_tag_states(request):
    return web.json_response({"states": get_tag_file_states()})


@routes.post("/promptchain/tags/restore")
async def _api_tag_restore(request):
    data, err = await parse_json(request)
    if err: return err
    filename = data.get("filename", "")
    if not filename:
        return error_response("missing filename")
    if restore_tag_file(filename):
        get_tag_store().reload()
        return ok_response({"filename": filename})
    return error_response("system file not found", 404)
