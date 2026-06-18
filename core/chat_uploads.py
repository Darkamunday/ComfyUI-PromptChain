"""Storage for images uploaded into the AI Assistant chat.

Images live under {user}/PromptChain/chat-uploads/, keyed by content hash —
NOT base64-embedded in the workflow JSON (that would bloat every saved
workflow/PNG). Chat history carries only the hash; the server rehydrates
pixels from disk just-in-time for the turn the image was uploaded on
(caption-once — older turns never re-send the bytes to the model).
"""
import base64
import binascii
import hashlib
import logging
from pathlib import Path

import folder_paths

logger = logging.getLogger("promptchain.chat_uploads")

# media_type → file extension for the formats vision models accept.
_EXT_BY_MEDIA = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_MEDIA_BY_EXT = {v: k for k, v in _EXT_BY_MEDIA.items()}

# Reject oversized uploads before they hit disk / a model. 20 MB decoded is
# well past any sane reference image and keeps a pasted clipboard blob from
# wedging the request.
_MAX_DECODED_BYTES = 20 * 1024 * 1024


def get_uploads_dir() -> Path:
    d = Path(folder_paths.get_user_directory()) / "PromptChain" / "chat-uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_data_url(b64: str) -> tuple[str, str | None]:
    """Accept either a bare base64 string or a full data: URL. Returns
    (data, media_type-or-None)."""
    if b64.startswith("data:"):
        header, _, payload = b64.partition(",")
        media = header[5:].split(";", 1)[0].strip() or None
        return payload, media
    return b64, None


def save_upload(b64: str, media_type: str | None = None) -> dict | None:
    """Decode a base64 image, write it under the uploads dir keyed by content
    hash, and return {hash, media_type, ext, bytes}. Returns None on a
    malformed / disallowed / oversized payload."""
    data, url_media = _strip_data_url(b64 or "")
    media_type = (media_type or url_media or "image/png").lower()
    if media_type not in _EXT_BY_MEDIA:
        logger.warning("rejected upload: unsupported media_type %r", media_type)
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("rejected upload: invalid base64")
        return None
    if not raw or len(raw) > _MAX_DECODED_BYTES:
        logger.warning("rejected upload: empty or over %d bytes", _MAX_DECODED_BYTES)
        return None

    h = hashlib.sha256(raw).hexdigest()
    ext = _EXT_BY_MEDIA[media_type]
    path = get_uploads_dir() / f"{h}.{ext}"
    if not path.exists():  # content-addressed: identical bytes reuse the file
        path.write_bytes(raw)
    return {"hash": h, "media_type": media_type, "ext": ext, "bytes": len(raw)}


def resolve_upload_path(image_hash: str) -> Path | None:
    """Path for a stored upload by hash, or None if absent. Probes known
    extensions since the hash alone doesn't carry the format."""
    if not image_hash:
        return None
    d = get_uploads_dir()
    for ext in _EXT_BY_MEDIA.values():
        p = d / f"{image_hash}.{ext}"
        if p.is_file():
            return p
    return None


def load_image_data(image_hash: str) -> dict | None:
    """Load a stored upload as the {data, media_type} shape that
    ai_api._call_provider_complete expects for its vision calls (distinct
    from the Anthropic block shape of load_image_block)."""
    path = resolve_upload_path(image_hash)
    if not path:
        return None
    media = _MEDIA_BY_EXT.get(path.suffix.lstrip("."), "image/png")
    return {
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "media_type": media,
    }


def load_image_block(image_hash: str) -> dict | None:
    """Load a stored upload as an Anthropic-canonical image block:
        {type:"image", source:{type:"base64", media_type, data}}
    Claude consumes this shape directly; _to_openai_messages converts it to
    OpenAI image_url shape for Ollama / OpenAI-compat providers."""
    path = resolve_upload_path(image_hash)
    if not path:
        return None
    media = _MEDIA_BY_EXT.get(path.suffix.lstrip("."), "image/png")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": data},
    }
