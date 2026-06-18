# Template + Prompt preset API — CRUD endpoints for workflow templates
# and prompt presets, delegating to template_storage and prompt_storage.

from aiohttp import web
import server

from .api_utils import error_response, ok_response

from . import templates as template_storage
from . import prompts as prompt_storage

routes = server.PromptServer.instance.routes


# ── template endpoints ───────────────────────────────────────────

@routes.get("/promptchain/templates/list")
async def _api_templates_list(request):
    arch = request.query.get("arch")
    family = request.query.get("family")
    model_name = request.query.get("name")
    model_hash = request.query.get("hash")
    include_hidden = request.query.get("include_hidden") == "1"
    templates = template_storage.list_templates(arch, family, model_name, model_hash, include_hidden)
    category_order = template_storage.load_category_order()
    return web.json_response({"templates": templates, "category_order": category_order})


@routes.post("/promptchain/templates/reset-all")
async def _api_template_reset_all(request):
    count = template_storage.reset_all()
    return ok_response({"removed": count})


@routes.post("/promptchain/templates/order")
async def _api_template_save_order(request):
    try:
        data = await request.json()
    except Exception:
        return error_response("invalid JSON")
    ordered_ids = data.get("order", [])
    if not isinstance(ordered_ids, list):
        return error_response("order must be a list")
    template_storage.save_order(ordered_ids)
    return ok_response()


@routes.post("/promptchain/templates/category-order")
async def _api_template_save_category_order(request):
    try:
        data = await request.json()
    except Exception:
        return error_response("invalid JSON")
    ordered_categories = data.get("order", [])
    if not isinstance(ordered_categories, list):
        return error_response("order must be a list")
    template_storage.save_category_order(ordered_categories)
    return ok_response()


@routes.get("/promptchain/templates/{template_id}")
async def _api_template_get(request):
    template_id = request.match_info.get("template_id", "")
    tpl = template_storage.load(template_id)
    if not tpl:
        return error_response("not found", 404)
    tpl["_has_system"] = template_storage.has_system_template(template_id)
    tpl["_has_user"] = template_storage.has_user_template(template_id)
    return web.json_response(tpl)


@routes.post("/promptchain/templates")
async def _api_template_save(request):
    try:
        data = await request.json()
    except Exception:
        return error_response("invalid JSON")
    if not data.get("name"):
        return error_response("missing name")
    template_id = template_storage.save(data)
    return ok_response({"id": template_id})


@routes.patch("/promptchain/templates/{template_id}")
async def _api_template_update_metadata(request):
    template_id = request.match_info.get("template_id", "")
    try:
        data = await request.json()
    except Exception:
        return error_response("invalid JSON")
    if template_storage.update_metadata(template_id, data):
        return ok_response()
    return error_response("not found", 404)


@routes.post("/promptchain/templates/{template_id}/reset")
async def _api_template_reset(request):
    template_id = request.match_info.get("template_id", "")
    if template_storage.reset(template_id):
        return ok_response()
    return error_response("no user override found", 404)


@routes.delete("/promptchain/templates/{template_id}")
async def _api_template_delete(request):
    template_id = request.match_info.get("template_id", "")
    if template_storage.delete(template_id):
        return ok_response()
    return error_response("not found", 404)


# ── prompt preset endpoints ──────────────────────────────────────

@routes.get("/promptchain/prompts/list")
async def _api_prompts_list(request):
    arch = request.query.get("arch")
    family = request.query.get("family")
    model_name = request.query.get("name")
    model_hash = request.query.get("hash")
    prompts = prompt_storage.list_prompts(arch, family, model_name, model_hash)
    return web.json_response({"prompts": prompts})


@routes.post("/promptchain/prompts")
async def _api_prompt_save(request):
    try:
        data = await request.json()
    except Exception:
        return error_response("invalid JSON")
    if not data.get("name"):
        return error_response("missing name")
    prompt_id = prompt_storage.save(data)
    return ok_response({"id": prompt_id})


@routes.delete("/promptchain/prompts/{prompt_id}")
async def _api_prompt_delete(request):
    prompt_id = request.match_info.get("prompt_id", "")
    if prompt_storage.delete(prompt_id):
        return ok_response()
    return error_response("not found", 404)
