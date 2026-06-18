# Shared API response helpers — standardized error/success formats
# across all PromptChain REST endpoints.

import json
import logging
import os
import tempfile

from aiohttp import web

logger = logging.getLogger("promptchain")


def error_response(message: str, status: int = 400, **extra) -> web.Response:
    body = {"error": message}
    if extra:
        body.update(extra)
    return web.json_response(body, status=status)


def ok_response(data: dict | None = None) -> web.Response:
    body = {"status": "ok"}
    if data:
        body.update(data)
    return web.json_response(body)


# ── path validation ───────────────────────────────────────────────

def _get_allowed_content_roots() -> list[str]:
    """ComfyUI directories where user content (images, models) may live."""
    roots = []
    try:
        import folder_paths as fp
        for getter in (fp.get_output_directory, fp.get_input_directory, fp.get_temp_directory):
            try:
                roots.append(os.path.realpath(getter()))
            except Exception:
                pass
    except ImportError:
        pass
    return roots


def _get_allowed_model_roots() -> list[str]:
    """ComfyUI directories where model files may live."""
    roots = []
    try:
        import folder_paths as fp
        for key in ("checkpoints", "diffusion_models", "unet", "loras",
                    "vae", "clip", "text_encoders"):
            try:
                for d in fp.get_folder_paths(key):
                    roots.append(os.path.realpath(d))
            except Exception:
                pass
    except ImportError:
        pass
    return roots


def validate_content_path(path: str) -> str | None:
    """Resolve path and verify it's under a ComfyUI content directory.
    Returns the resolved path, or None if validation fails."""
    resolved = os.path.realpath(path)
    for root in _get_allowed_content_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


def validate_model_path(path: str) -> str | None:
    """Resolve path and verify it's under a ComfyUI model directory.
    Returns the resolved path, or None if validation fails."""
    resolved = os.path.realpath(path)
    for root in _get_allowed_model_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


# ── atomic file writes ────────────────────────────────────────────

def atomic_write_json(path, data, indent=2):
    """Write JSON atomically — temp file in same dir, then os.replace()."""
    path_str = str(path)
    dir_name = os.path.dirname(path_str) or "."
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", dir=dir_name,
        delete=False, encoding="utf-8",
    ) as tmp:
        json.dump(data, tmp, indent=indent)
        tmp_path = tmp.name
    os.replace(tmp_path, path_str)


# ── request parsing ───────────────────────────────────────────────

async def parse_json(request) -> tuple[dict | None, web.Response | None]:
    """Parse JSON body, returning (data, None) or (None, error_response)."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return None, error_response("expected JSON object")
        return data, None
    except Exception:
        return None, error_response("invalid JSON body")
