"""
AI provider layer — Claude + OpenAI-compatible (Ollama, llama.cpp, LM Studio...).

Config lives in {user_dir}/PromptChain/ai_config.json and is read server-side
only. API keys are never returned to the client — the config GET returns a
sanitized shape with has_key flags in place of the raw key.

Streaming: generate spawns an asyncio task keyed by request_id. Tokens are
sent to the client via the existing send_ws broadcast channel on the
`promptchain_ai_stream` event. Cancel looks up the task and cancels it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

import aiohttp
from aiohttp import web
import folder_paths
import server

from .api_utils import atomic_write_json, error_response, parse_json
from . import model_settings
from .shared import send_ws
from .tags import get_store as get_tag_store

logger = logging.getLogger("promptchain.ai")
# Dedicated debug channel — dumps full system prompt, user message, body
# buffer, STYLE parsing, splice result. Enabled by default so we can trace
# why a given model went off-script. Mute via logging config to silence.
dbg = logging.getLogger("promptchain.ai.debug")
if dbg.level == logging.NOTSET:
    dbg.setLevel(logging.INFO)
routes = server.PromptServer.instance.routes


def _safe_for_log(text: str) -> str:
    """ComfyUI's default log stream on Windows is cp1252 — characters
    like `→`, emoji, and curly quotes raise UnicodeEncodeError and the
    whole log record gets dropped. Round-trip via cp1252 with
    `errors='replace'` so unencodable code points become `?` instead of
    losing the dump entirely. No-op when the underlying stream is utf-8."""
    if not text:
        return text or ""
    try:
        return text.encode("cp1252", "replace").decode("cp1252")
    except Exception:
        return text.encode("ascii", "replace").decode("ascii")


def _trunc(text: str, limit: int = 6000) -> str:
    if not text:
        return ""
    safe = _safe_for_log(text)
    if len(safe) <= limit:
        return safe
    return safe[:limit] + f"\n...[+{len(safe) - limit} chars truncated]"


def _dump(request_id: str, label: str, body: str):
    safe_label = _safe_for_log(label)
    dbg.info("ai[%s] %s (%d chars):\n%s\n--- /%s ---",
             request_id, safe_label, len(body or ""), _trunc(body or ""), safe_label)

# Model-agnostic safety limits for the aiohttp-side call. Providers
# enforce their own limits on top.
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 300  # 5 min — vision + reasoning can stretch
# Generous cap for reasoning models (Qwen3, DeepSeek-R1, etc.) that burn
# tokens on <think>...</think> before any visible output. With our
# typical ~1K user_chars of directives + bios, reasoning alone can run
# 3-5K tokens. max_tokens is a runaway cap, not a target — models stop
# when done, so erring high costs nothing.
_MAX_TOKENS = 16384

# The local model onboarding offers to pull and the AI assistant's
# prompt-engineering is tuned around. Single source of truth — the rest of
# this file references the tag in comments only.
RECOMMENDED_LOCAL_MODEL = "qwen3-vl:8b-instruct"
_DEFAULT_OLLAMA_ROOT = "http://localhost:11434"


# ── cloud service registry ────────────────────────────────────────
# Labels/help links are on the frontend; the backend only needs the
# routing info (base_url and whether it speaks Anthropic vs OpenAI shape).

_CLOUD_SERVICES: dict[str, dict] = {
    "claude":     {"base_url": None,                                                   "shape": "anthropic"},
    "openai":     {"base_url": "https://api.openai.com/v1",                            "shape": "openai"},
    "grok":       {"base_url": "https://api.x.ai/v1",                                  "shape": "openai"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "shape": "openai"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",                         "shape": "openai"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",                          "shape": "openai"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1",                       "shape": "openai"},
    "mistral":    {"base_url": "https://api.mistral.ai/v1",                            "shape": "openai"},
    "other":      {"base_url": None,                                                   "shape": "openai"},
}


def _cloud_base_url(cloud: dict) -> str:
    """Return the effective base URL for a cloud service (registry entry
    or user-supplied 'other')."""
    service = cloud.get("service") or "claude"
    spec = _CLOUD_SERVICES.get(service)
    if not spec:
        return ""
    if service == "other":
        return (cloud.get("base_url") or "").strip().rstrip("/")
    return spec["base_url"] or ""


# ── config storage ────────────────────────────────────────────────

def _config_path() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "ai_config.json"


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _migrate_config(json.load(f))
    except Exception as e:
        logger.warning("ai_config parse failed: %s", e)
        return {}


def _migrate_config(cfg: dict) -> dict:
    """v1 shape was `{provider: 'claude', claude: {api_key, model}}`. v2
    folds claude into the cloud-services object. Migrate lazily on load
    so existing users don't re-enter their key."""
    if not isinstance(cfg, dict):
        return {}
    if cfg.get("provider") == "claude":
        claude = cfg.pop("claude", None) or {}
        cfg["provider"] = "cloud"
        cfg["cloud"] = {
            "service": "claude",
            "api_key": claude.get("api_key"),
            "model": claude.get("model") or "claude-haiku-4-5",
        }
    return cfg


def _save_config(config: dict):
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, config)


def _sanitize(config: dict) -> dict:
    """Strip secrets before sending to the browser."""
    cloud = config.get("cloud") or {}
    local = config.get("local") or {}
    return {
        "provider": config.get("provider") or None,
        "cloud": {
            "service": cloud.get("service") or "claude",
            "model": cloud.get("model") or "",
            "base_url": cloud.get("base_url") or "",
            "has_key": bool(cloud.get("api_key")),
        },
        "local": {
            "base_url": local.get("base_url") or "",
            "model": local.get("model") or "",
            "auto_start": _local_auto_start(local),
        },
    }


def _local_auto_start(local: dict) -> bool:
    """Default ON — matches the shipped behavior; missing key in legacy
    configs reads as enabled."""
    v = local.get("auto_start")
    return True if v is None else bool(v)


# ── config endpoints ──────────────────────────────────────────────

@routes.get("/promptchain/ai/config")
async def _api_get_config(request):
    return web.json_response(_sanitize(_load_config()))


@routes.post("/promptchain/ai/config")
async def _api_set_config(request):
    body, err = await parse_json(request)
    if err:
        return err

    existing = _load_config()
    provider = body.get("provider")
    if provider not in (None, "", "cloud", "local"):
        return error_response("invalid provider")

    merged = dict(existing)
    merged["provider"] = provider or None
    # Records that the choice came from the user (even when it's None), so the
    # first-run Ollama auto-default never overrides a deliberate selection.
    merged["user_set"] = True

    # Cloud: preserve api_key when a new one isn't supplied (same pattern
    # as the CivitAI key — blank means "keep", empty-string-with-the-key
    # key present means "clear").
    if "cloud" in body:
        c_in = body["cloud"] or {}
        c_cur = dict(existing.get("cloud") or {})
        if "service" in c_in:
            svc = (c_in.get("service") or "").strip().lower()
            c_cur["service"] = svc if svc in _CLOUD_SERVICES else "claude"
        if "model" in c_in:
            c_cur["model"] = (c_in.get("model") or "").strip()
        if "base_url" in c_in:
            c_cur["base_url"] = (c_in.get("base_url") or "").strip().rstrip("/")
        if "api_key" in c_in:
            key = (c_in.get("api_key") or "").strip()
            if key:
                c_cur["api_key"] = key
            else:
                c_cur.pop("api_key", None)
        merged["cloud"] = c_cur

    if "local" in body:
        l_in = body["local"] or {}
        l_cur = dict(existing.get("local") or {})
        if "base_url" in l_in:
            l_cur["base_url"] = (l_in.get("base_url") or "").strip().rstrip("/")
        if "model" in l_in:
            l_cur["model"] = (l_in.get("model") or "").strip()
        if "auto_start" in l_in:
            l_cur["auto_start"] = bool(l_in.get("auto_start"))
        merged["local"] = l_cur

    _save_config(merged)
    return web.json_response(_sanitize(merged))


# ── connection tests / model detection ────────────────────────────

@routes.post("/promptchain/ai/test")
async def _api_test(request):
    """Validate a provider config. Body values take precedence over stored."""
    body, err = await parse_json(request)
    if err:
        return err

    stored = _load_config()
    provider = body.get("provider") or stored.get("provider")
    if provider == "cloud":
        cloud = {**(stored.get("cloud") or {}), **(body.get("cloud") or {})}
        service = cloud.get("service") or "claude"
        api_key = (cloud.get("api_key") or "").strip()
        if not api_key:
            return web.json_response({"ok": False, "error": "API key missing"})
        if service == "claude":
            model = cloud.get("model") or "claude-haiku-4-5"
            return web.json_response(await _test_claude(api_key, model))
        base_url = _cloud_base_url(cloud)
        if not base_url:
            return web.json_response({"ok": False, "error": "Base URL missing"})
        return web.json_response(await _test_openai_compat(base_url, api_key))

    if provider == "local":
        local = {**(stored.get("local") or {}), **(body.get("local") or {})}
        base_url = (local.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            return web.json_response({"ok": False, "error": "Base URL missing"})
        return web.json_response(await _test_local(base_url))

    return web.json_response({"ok": False, "error": "No provider selected"})


def _ollama_exe() -> str | None:
    """Locate the ollama binary. shutil.which resolves PATH as it was when
    ComfyUI booted — a freshly-installed Ollama won't be on that PATH until
    restart, so we also probe the per-OS default install location."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    elif sys.platform == "darwin":
        candidates += [Path("/usr/local/bin/ollama"),
                       Path("/opt/homebrew/bin/ollama"),
                       Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _winget_exe() -> str | None:
    """winget ships with App Installer on Win10 1809+/Win11 but isn't
    guaranteed (LTSC, stripped images, older builds). None == fall back to
    the manual download link."""
    if sys.platform != "win32":
        return None
    return shutil.which("winget")


def _spawn_ollama(exe: str) -> None:
    """Launch `ollama serve` fully detached so it outlives the ComfyUI
    process and doesn't inherit its console."""
    if sys.platform == "win32":
        DETACHED = 0x00000008  # DETACHED_PROCESS
        NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe, "serve"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=DETACHED | NEW_GROUP, close_fds=True,
        )
    else:
        subprocess.Popen(
            [exe, "serve"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )


async def _ensure_ollama_running(ollama_root: str, exe: str | None) -> bool:
    """Return True if Ollama answers on its root URL, spawning it once if
    a binary is available and it isn't already up."""
    if (await _test_local(f"{ollama_root}/v1")).get("ok") or await _is_ollama(ollama_root):
        return True
    if not exe:
        return False
    try:
        _spawn_ollama(exe)
    except Exception:
        return False
    for _ in range(12):
        await asyncio.sleep(0.5)
        if await _is_ollama(ollama_root):
            return True
    return False


@routes.post("/promptchain/ai/wake-local")
async def _api_wake_local(request):
    """Try to start Ollama when the configured local provider isn't
    answering. Windows installs put Ollama in PATH and auto-start on
    login by default, so the typical hit-rate case is 'user killed it
    via Task Manager' — `ollama serve` brings it back in <1s.

    No-op for non-local provider configs. Returns:
      - {ok: True, already_running: True}  — was already up
      - {ok: True, started: True}          — we spawned it; port responded
      - {ok: False, error: "..."}           — not in PATH, or didn't come up
    """
    cfg = _load_config()
    if cfg.get("provider") != "local":
        return web.json_response({"ok": False, "error": "Local provider not configured"})

    local = cfg.get("local") or {}
    base_url = (local.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return web.json_response({"ok": False, "error": "No local base_url configured"})
    if not _local_auto_start(local):
        return web.json_response({"ok": False, "error": "Auto-start disabled in settings."})

    # Already up? Skip the spawn — avoids creating zombie processes when
    # the panel re-opens during a working session.
    if (await _test_local(base_url)).get("ok"):
        return web.json_response({"ok": True, "already_running": True})

    exe = _ollama_exe()
    if not exe:
        return web.json_response({
            "ok": False,
            "error": "Ollama not found in PATH. Install from https://ollama.com/download.",
        })

    try:
        _spawn_ollama(exe)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"Failed to spawn ollama: {e}"})

    # Poll for up to ~6s — cold first-launch on Windows is usually <2s,
    # but the desktop app stack sometimes takes longer on first boot.
    for _ in range(12):
        await asyncio.sleep(0.5)
        if (await _test_local(base_url)).get("ok"):
            return web.json_response({"ok": True, "started": True})

    return web.json_response({
        "ok": False,
        "error": "Spawned ollama but it didn't answer on the configured port.",
    })


def _configured_ollama_root() -> str:
    """Ollama root the setup flow targets: the user's saved local base_url
    if they have one, else the default port. Independent of which provider
    is currently active so onboarding can probe before anything's saved."""
    local = (_load_config().get("local") or {})
    base_url = (local.get("base_url") or "").strip().rstrip("/")
    return _ollama_root(base_url) if base_url else _DEFAULT_OLLAMA_ROOT


async def _list_ollama_model_names(ollama_root: str) -> list[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{ollama_root}/api/tags") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [m.get("name") or m.get("model") for m in (data.get("models") or [])
                        if (m.get("name") or m.get("model"))]
    except Exception:
        return []


def _model_in_list(model: str, names: list[str]) -> bool:
    """Ollama appends `:latest` when a name carries no tag. A tagged request
    (qwen3-vl:8b-instruct) must match exactly; an untagged one matches any
    installed tag of the same base model."""
    if ":" not in model:
        return any(n.split(":")[0] == model for n in names)
    return model in names or f"{model}:latest" in names


# Well-known local LLM server ports, by product convention. We can only
# *detect* these (and use them over the OpenAI-compat path) — unlike Ollama
# we can't start, install, or pull models for them, so they're surfaced as
# "already running? use it" rather than a guided setup.
_LOCAL_PROBE_TARGETS = [
    (11434, "Ollama"),
    (1234, "LM Studio"),
    (8080, "llama.cpp"),
    (5001, "KoboldCpp"),
    (1337, "Jan"),
    (8000, "vLLM / LocalAI"),
]


async def _probe_local_servers() -> list[dict]:
    """Fan out a fast GET to each well-known port and report whichever answer
    an Ollama /api/tags or OpenAI-compat /v1/models. Refused ports fail
    instantly; the short timeout only bounds a port that accepts but hangs."""
    async def probe(port: int, label: str) -> dict | None:
        root = f"http://localhost:{port}"
        try:
            timeout = aiohttp.ClientTimeout(total=1.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    async with session.get(f"{root}/api/tags") as r:
                        if r.status == 200:
                            return {"port": port, "kind": "ollama", "label": "Ollama",
                                    "base_url": f"{root}/v1"}
                except Exception:
                    pass
                async with session.get(f"{root}/v1/models") as r:
                    if r.status == 200:
                        return {"port": port, "kind": "openai", "label": label,
                                "base_url": f"{root}/v1"}
        except Exception:
            return None
        return None

    found = await asyncio.gather(*[probe(p, l) for p, l in _LOCAL_PROBE_TARGETS])
    return [f for f in found if f]


@routes.get("/promptchain/ai/setup-status")
async def _api_setup_status(request):
    """One-shot snapshot for the onboarding/panel AI-setup UI. Triggered on
    open (event-driven) — never polled."""
    ollama_root = _configured_ollama_root()
    running, detected = await asyncio.gather(
        _is_ollama(ollama_root),
        _probe_local_servers(),
    )
    names = await _list_ollama_model_names(ollama_root) if running else []
    return web.json_response({
        "platform": sys.platform,
        "recommended_model": RECOMMENDED_LOCAL_MODEL,
        "ollama_installed": _ollama_exe() is not None,
        "ollama_running": running,
        "model_present": _model_in_list(RECOMMENDED_LOCAL_MODEL, names),
        "winget_available": _winget_exe() is not None,
        "installed_models": names,
        # Non-Ollama OpenAI-compatible servers we found listening locally.
        "detected_servers": [d for d in detected if d["kind"] != "ollama"],
    })


@routes.post("/promptchain/ai/auto-configure")
async def _api_auto_configure(request):
    """First-run default: if the user hasn't configured a provider yet and
    Ollama is already serving the recommended model, select Local so the
    assistant works with zero setup. Skips once a provider is set or the user
    has made any explicit choice (`user_set`) so None is never overridden."""
    cfg = _load_config()
    if cfg.get("provider") or cfg.get("user_set"):
        return web.json_response({"configured": False, "reason": "already chosen"})
    ollama_root = _configured_ollama_root()
    if not await _is_ollama(ollama_root):
        return web.json_response({"configured": False, "reason": "ollama offline"})
    names = await _list_ollama_model_names(ollama_root)
    if not _model_in_list(RECOMMENDED_LOCAL_MODEL, names):
        return web.json_response({"configured": False, "reason": "recommended model missing"})
    _save_config({
        "provider": "local",
        "local": {"base_url": f"{ollama_root}/v1", "model": RECOMMENDED_LOCAL_MODEL},
    })
    return web.json_response({"configured": True, "provider": "local",
                             "model": RECOMMENDED_LOCAL_MODEL})


async def _open_sse(request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    return resp


async def _sse_send(resp: web.StreamResponse, obj: dict) -> None:
    await resp.write(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")


@routes.post("/promptchain/ai/pull-model")
async def _api_pull_model(request):
    """Proxy Ollama's /api/pull progress stream to the browser as SSE so
    the setup UI can render a live progress bar. Spawns Ollama first if a
    binary exists but the server is down."""
    body, err = await parse_json(request)
    if err:
        return err
    model = (body.get("model") or RECOMMENDED_LOCAL_MODEL).strip()
    ollama_root = _configured_ollama_root()

    if not await _ensure_ollama_running(ollama_root, _ollama_exe()):
        return web.json_response(
            {"error": "Ollama isn't running and couldn't be started. Install it first."},
            status=409)

    resp = await _open_sse(request)
    try:
        # No total timeout — a multi-GB pull legitimately runs for minutes.
        timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{ollama_root}/api/pull",
                                    json={"name": model, "stream": True}) as upstream:
                if upstream.status != 200:
                    detail = (await upstream.text())[:200]
                    await _sse_send(resp, {"error": f"HTTP {upstream.status}: {detail}"})
                    return resp
                async for raw in upstream.content:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    await _sse_send(resp, evt)
        await _sse_send(resp, {"done": True})
    except ConnectionResetError:
        pass  # client navigated away mid-pull; Ollama keeps the download
    except Exception as e:
        logger.warning("pull-model stream failed", exc_info=True)
        try:
            await _sse_send(resp, {"error": str(e)})
        except Exception:
            pass
    return resp


@routes.post("/promptchain/ai/install-ollama")
async def _api_install_ollama(request):
    """Install Ollama via winget (Windows only) and stream its output as
    SSE. Every other platform falls back to the manual download link in the
    UI — we don't attempt unattended installs there (macOS is a .app drag,
    Linux needs sudo for the systemd service)."""
    winget = _winget_exe()
    if not winget:
        return web.json_response(
            {"error": "Automatic install needs winget (Windows). "
                      "Download Ollama from https://ollama.com/download."},
            status=400)

    resp = await _open_sse(request)
    try:
        proc = await asyncio.create_subprocess_exec(
            winget, "install", "--id", "Ollama.Ollama", "-e",
            "--silent", "--accept-source-agreements", "--accept-package-agreements",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                await _sse_send(resp, {"line": text})
        code = await proc.wait()
        # winget exits 0 on success. A fresh install isn't on the running
        # ComfyUI's PATH, but _ollama_exe() probes the install dir directly
        # so setup-status will see it without a restart.
        await _sse_send(resp, {"done": True, "code": code,
                               "ok": code == 0, "installed": _ollama_exe() is not None})
    except ConnectionResetError:
        pass
    except Exception as e:
        logger.warning("install-ollama failed", exc_info=True)
        try:
            await _sse_send(resp, {"error": str(e)})
        except Exception:
            pass
    return resp


async def _test_claude(api_key: str, model: str) -> dict:
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            ) as resp:
                if resp.status == 200:
                    return {"ok": True}
                data = await resp.json()
                msg = (data.get("error") or {}).get("message") or f"HTTP {resp.status}"
                return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}"}


async def _test_local(base_url: str) -> dict:
    # Prefer /v1/models (standard OpenAI shape). Fall back to Ollama's
    # /api/tags when /v1/models is missing — that's the telltale sign of
    # raw Ollama running without the /v1 shim.
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{base_url}/models") as resp:
                    if resp.status == 200:
                        return {"ok": True}
            except Exception:
                pass
            ollama = _ollama_root(base_url)
            async with session.get(f"{ollama}/api/tags") as resp:
                if resp.status == 200:
                    return {"ok": True}
                return {"ok": False, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}"}


async def _test_openai_compat(base_url: str, api_key: str) -> dict:
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with session.get(f"{base_url}/models", headers=headers) as resp:
                if resp.status == 200:
                    return {"ok": True}
                text = await resp.text()
                return {"ok": False, "error": f"HTTP {resp.status}: {text[:160]}"}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}"}


def _ollama_root(base_url: str) -> str:
    """Derive Ollama root (http://host:11434) from a config base_url that
    typically ends in /v1. If the user's URL doesn't end in /v1 we return
    it unchanged — Ollama's native endpoints are siblings of /v1 so this
    only helps when the user configured the OpenAI-compat path."""
    if base_url.endswith("/v1"):
        return base_url[:-3]
    return base_url


@routes.post("/promptchain/ai/detect-models")
async def _api_detect_models(request):
    """Try to enumerate models at a base URL. Works for Ollama (native
    /api/tags) and any server exposing /v1/models (OpenAI-compat)."""
    body, err = await parse_json(request)
    if err:
        return err
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return web.json_response({"models": [], "source": "none"})
    # Optional bearer token for cloud providers (OpenAI, Grok, ...).
    # Local servers don't expect auth; absent key = unauthenticated GET.
    api_key = (body.get("api_key") or "").strip()
    auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Ollama native first — richer (capability flags).
            ollama_root = _ollama_root(base_url)
            try:
                async with session.get(f"{ollama_root}/api/tags") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = []
                        for m in (data.get("models") or []):
                            name = m.get("name") or m.get("model")
                            if name:
                                models.append({"name": name})
                        if models:
                            # Second pass to flag vision-capable models.
                            models = await _flag_ollama_vision(session, ollama_root, models)
                            return web.json_response({"models": models, "source": "ollama"})
            except Exception:
                pass

            # Generic OpenAI-compat fallback.
            try:
                async with session.get(f"{base_url}/models", headers=auth_headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data") or data.get("models") or []
                        models = []
                        for m in items:
                            if isinstance(m, dict):
                                name = m.get("id") or m.get("name")
                            else:
                                name = str(m)
                            if name:
                                models.append({"name": name})
                        return web.json_response({"models": models, "source": "openai"})
            except Exception:
                pass
    except Exception as e:
        return web.json_response({"models": [], "error": str(e)})

    return web.json_response({"models": []})


async def _flag_ollama_vision(session, ollama_root: str, models: list[dict]) -> list[dict]:
    """Probe each model's capabilities via /api/show. Best-effort — on any
    failure we leave vision unset (UI treats that as unknown/permissive)."""
    async def probe(m):
        try:
            async with session.post(f"{ollama_root}/api/show", json={"name": m["name"]}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    caps = data.get("capabilities") or []
                    if "vision" in caps:
                        m["vision"] = True
        except Exception:
            pass
        return m

    return await asyncio.gather(*[probe(m) for m in models])


# ── active request tracking ───────────────────────────────────────

_active_requests: dict[str, asyncio.Task] = {}
# Per-request reasoning-char counter set by streaming endpoints when the
# upstream surfaces a `reasoning`/`reasoning_content` delta. Lets callers
# distinguish a genuinely empty response from the "thought hard then
# emitted nothing" failure mode common to abliterated/thinking-mode
# variants — those return empty body with non-empty reasoning, and
# benefit from a retry with an output-forcing nudge.
_request_reasoning_chars: dict[str, int] = {}


def _cleanup_request(request_id: str):
    _active_requests.pop(request_id, None)
    _request_reasoning_chars.pop(request_id, None)


@routes.post("/promptchain/ai/cancel")
async def _api_cancel(request):
    body, err = await parse_json(request)
    if err:
        return err
    request_id = body.get("request_id") or ""
    task = _active_requests.get(request_id)
    if task and not task.done():
        task.cancel()
    return web.json_response({"status": "ok"})


# ── grounding block assembly ──────────────────────────────────────

def _build_grounding(model_hash: str) -> dict:
    cfg = model_settings.load(model_hash) if model_hash else None
    if not cfg:
        return {}
    keys = (
        "display_name", "model_name", "version", "architecture", "family",
        "author", "description", "tags", "url", "trigger",
        "quality_position", "negative", "notes",
        "tag_sources", "tag_format", "prompt_style",
        "default_prompt_id",
    )
    return {k: cfg.get(k) for k in keys if cfg.get(k) is not None}


def _list_arch_prompts(arch: str | None) -> list[dict]:
    if not arch:
        return []
    from . import prompts as _prompts
    try:
        return _prompts.list_prompts(architecture=arch)
    except Exception:
        logger.debug("failed to load prompts for arch %s", arch, exc_info=True)
        return []


def _style_full_name(p: dict) -> str:
    return f"{p.get('category', 'Misc')} > {p.get('name', 'Unnamed')}"


def _find_prompt_by_full_name(prompts_list: list[dict], full_name: str) -> dict | None:
    target = full_name.strip().lower()
    for p in prompts_list:
        if _style_full_name(p).lower() == target:
            return p
    return None


def _find_prompt_by_id(prompts_list: list[dict], prompt_id: str) -> dict | None:
    for p in prompts_list:
        if p.get("id") == prompt_id:
            return p
    return None


# ── style template assembly ────────────────────────────────────────────
# Templates in `data/prompts/*.json` come in three shapes (per the B0
# probe of all 587 templates):
#   1. Flux/Illustrious: "// Your Tags\n{cursor}\n\n// SectionHeader\n
#      <modifiers>" with optional trailing "\n\nNegative Prompt:\n<negs>"
#   2. Pony quality: "score_9, score_8_up, score_7_up, {cursor}" — cursor
#      inline among modifiers.
#   3. Pony style: bare modifier line, no cursor, no wrapper.
# All three flatten to (positive_modifiers, negative_modifiers) for
# server-side `// Style:` section injection.

_TEMPLATE_YOUR_TAGS_BLOCK = re.compile(
    r"^\s*//\s*Your\s+Tags\s*\n\s*\{cursor\}\s*\n\s*\n",
    re.IGNORECASE,
)
_TEMPLATE_LEADING_HEADER = re.compile(r"^\s*//\s*[^\n]+\n")
_TEMPLATE_NEG_MARKER = re.compile(
    r"\n\s*Negative\s+Prompt\s*:\s*\n", re.IGNORECASE,
)


def _parse_style_template_text(text: str) -> tuple[list[str], list[str]]:
    """Pull positive modifiers and embedded negatives out of a prompt
    template's `text` field. Both lists are bare comma-split tokens
    suitable for direct insertion into output sections.

    Returns ([], []) when the template body is empty after wrapper-
    stripping (template was just `{cursor}` — nothing to inject)."""
    if not text:
        return [], []
    body = text
    body = _TEMPLATE_YOUR_TAGS_BLOCK.sub("", body, count=1)
    body = body.replace("{cursor}", "").strip()
    if not body:
        return [], []
    neg_split = _TEMPLATE_NEG_MARKER.split(body, maxsplit=1)
    positive_part = neg_split[0].strip()
    negative_part = neg_split[1].strip() if len(neg_split) > 1 else ""
    positive_part = _TEMPLATE_LEADING_HEADER.sub("", positive_part, count=1)
    positive_part = positive_part.strip()
    pos_tokens = [
        t.strip() for t in _split_prompt_tokens(positive_part) if t.strip()
    ]
    neg_tokens = [
        t.strip() for t in _split_prompt_tokens(negative_part) if t.strip()
    ]
    return pos_tokens, neg_tokens


def _build_style_section(template: dict) -> dict | None:
    """Construct a `// Style: <Name>` section dict from a template.
    Returns None if the template has no usable positive modifiers
    (after stripping wrapper + cursor).

    `body_text` holds the joined positive prose (mirrors what natlang
    assembly emits) so prose-mode output keeps the template body verbatim
    rather than re-joining comma-fragments — matters when the template
    body has multi-clause prose where ", ".join would alter spacing."""
    name = (template.get("name") or "").strip() or "Style"
    pos_tokens, _ = _parse_style_template_text(template.get("text") or "")
    if not pos_tokens:
        return None
    return {
        "header": f"// Style: {name}",
        "tokens": pos_tokens,
        "body_text": ", ".join(pos_tokens),
        "is_style_injected": True,
    }


def _replace_or_append_style_section(sections: list[dict],
                                     style_section: dict) -> list[dict]:
    """Replace any existing `// Style:` section in `sections` with the
    new one, or append it as the last positive section if none exists.
    Negative Prompt sections always stay last in output_text assembly,
    so appending here puts Style after Setting/Scene but before Neg."""
    out: list[dict] = []
    inserted = False
    for s in sections:
        if (not s.get("is_negative")
                and _section_key_from_header(s.get("header") or "") == "style"):
            if not inserted:
                out.append(style_section)
                inserted = True
            continue
        out.append(s)
    if inserted:
        return out
    # Insert before the first negative section (so Style stays in the
    # positive block) or at the end if no negs exist.
    final: list[dict] = []
    placed = False
    for s in out:
        if not placed and s.get("is_negative"):
            final.append(style_section)
            placed = True
        final.append(s)
    if not placed:
        final.append(style_section)
    return final


def _merge_template_negatives(sections: list[dict],
                              template: dict) -> list[dict]:
    """Pull the embedded `Negative Prompt:` line out of a style template
    and merge into the output's Negative Prompt section. Deduped by
    canonical (lowercase, weighted-stripped) form so user-typed negs
    aren't overwritten and re-applying the same style doesn't duplicate.

    No-op if the template carries no negatives or every negative is
    already present. If there's no Negative Prompt section in
    `sections`, one is created at the end."""
    _, neg_tokens = _parse_style_template_text(template.get("text") or "")
    if not neg_tokens:
        return sections

    target: dict | None = None
    for s in sections:
        if s.get("is_negative"):
            target = s
            break
    if target is None:
        target = {
            "header": "Negative Prompt:",
            "tokens": [],
            "is_negative": True,
        }
        sections = sections + [target]

    existing: set[str] = set()
    for t in target.get("tokens") or []:
        canon = _canonicalize_token(t).lower()
        if canon:
            existing.add(canon)
        m = _WEIGHTED_TOKEN_RE.match(t)
        if m:
            inner = _canonicalize_token(m.group(1)).lower()
            if inner:
                existing.add(inner)

    for t in neg_tokens:
        canon = _canonicalize_token(t).lower()
        if canon and canon in existing:
            continue
        target["tokens"].append(t)
        if canon:
            existing.add(canon)

    return sections


_STYLE_HEADER_RE = re.compile(r"^//\s*style\b\s*:?", re.IGNORECASE)
_GENERIC_HEADER_RE = re.compile(r"^//\s*\w[\w\s\-]*\s*:")


def _swap_style_template_in_prompt(prompt: str, template: dict) -> str:
    """Replace the `// Style: <X>` section (header + body) and the
    `Negative Prompt:` section content with the given template's full
    contents. Used as a post-pass after rails/hybrid finishes a style
    apply — the rails dispatcher works at sentence-level and doesn't
    promote a style intent to a full template swap, so leftover scene-
    shaped sentences and stale negatives persist without this pass.

    If the prompt has no `// Style:` section, one is inserted before
    the Negative Prompt section (or at the end). If the prompt has no
    Negative Prompt section, one is appended.

    No-op when the template carries no positive tokens — treats that
    as "not really a usable template" rather than wiping the section.
    """
    if not template or not prompt:
        return prompt
    pos_tokens, neg_tokens = _parse_style_template_text(
        template.get("text") or ""
    )
    if not pos_tokens and not neg_tokens:
        return prompt

    name = (template.get("name") or "").strip()
    new_style_header = f"// Style: {name}" if name else "// Style:"
    new_style_body = ", ".join(pos_tokens) if pos_tokens else ""
    new_neg_body = ", ".join(neg_tokens) if neg_tokens else ""

    lines = prompt.splitlines()
    out: list[str] = []
    i = 0
    style_replaced = False
    neg_replaced = False

    def _is_section_boundary(s: str) -> bool:
        return bool(_GENERIC_HEADER_RE.match(s.strip())) or s.strip() == "Negative Prompt:"

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _STYLE_HEADER_RE.match(stripped):
            # Swap entire style section: header + body until next
            # section boundary or EOF. Ensure a blank line follows
            # so it visually separates from the next section.
            out.append(new_style_header)
            if new_style_body:
                out.append(new_style_body)
            i += 1
            while i < len(lines) and not _is_section_boundary(lines[i]):
                i += 1
            # If a next section follows, leave a blank line gap.
            if i < len(lines):
                out.append("")
            style_replaced = True
            continue

        if stripped == "Negative Prompt:":
            out.append("Negative Prompt:")
            if new_neg_body:
                out.append(new_neg_body)
            i += 1
            while i < len(lines) and not _is_section_boundary(lines[i]):
                i += 1
            if i < len(lines):
                out.append("")
            neg_replaced = True
            continue

        out.append(line)
        i += 1

    if not style_replaced and (new_style_body or new_style_header):
        # No style section existed. Insert before Negative Prompt (if
        # we already emitted it via the negative branch) or at end.
        if neg_replaced:
            for j in range(len(out) - 1, -1, -1):
                if out[j].strip() == "Negative Prompt:":
                    if out and out[max(0, j - 1)].strip():
                        out.insert(j, "")
                    out.insert(j, new_style_header)
                    if new_style_body:
                        out.insert(j + 1, new_style_body)
                    break
        else:
            if out and out[-1].strip():
                out.append("")
            out.append(new_style_header)
            if new_style_body:
                out.append(new_style_body)

    if not neg_replaced and new_neg_body:
        if out and out[-1].strip():
            out.append("")
        out.append("Negative Prompt:")
        out.append(new_neg_body)

    return "\n".join(out)


_SECTION_HEADER_RE_MC = re.compile(r"^//\s*(\w[\w\-]*)\s*[:](.*)$", re.IGNORECASE)


def _parse_existing_prompt_state(node_prompt: str) -> dict:
    """Light parse of an existing `// Section:` prompt. Used by the
    multi-char edit compose path to preserve outfit / style / negs
    from the existing prompt when rebuilding around a new character.

    Returns:
        {
          'character_sections': [(header_line, body_text), ...],
          'outfit_body': str,        # combined Outfit section body
          'pose_body': str,          # combined Pose section body
          'scene_body': str,
          'style_body': str,
          'quality_body': str,
          'negative_body': str,
        }
    """
    out = {
        "character_sections": [],
        "outfit_body": "",
        "pose_body": "",
        "scene_body": "",
        "style_header_line": "",
        "style_body": "",
        "quality_body": "",
        "negative_body": "",
    }
    if not (node_prompt or "").strip():
        return out
    lines = node_prompt.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m = _SECTION_HEADER_RE_MC.match(stripped)
        if m:
            concept = m.group(1).lower()
            body_lines: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if _SECTION_HEADER_RE_MC.match(nxt) or nxt == "Negative Prompt:":
                    break
                if nxt:
                    body_lines.append(lines[i])
                i += 1
            body = "\n".join(body_lines).strip()
            if concept == "character":
                out["character_sections"].append((stripped, body))
            elif concept == "outfit":
                out["outfit_body"] = (
                    (out["outfit_body"] + "\n" + body).strip()
                    if out["outfit_body"] else body
                )
            elif concept == "pose":
                out["pose_body"] = (
                    (out["pose_body"] + "\n" + body).strip()
                    if out["pose_body"] else body
                )
            elif concept == "scene":
                out["scene_body"] = (
                    (out["scene_body"] + "\n" + body).strip()
                    if out["scene_body"] else body
                )
            elif concept == "style":
                out["style_body"] = (
                    (out["style_body"] + "\n" + body).strip()
                    if out["style_body"] else body
                )
                if not out["style_header_line"]:
                    out["style_header_line"] = stripped
            elif concept == "quality":
                out["quality_body"] = (
                    (out["quality_body"] + "\n" + body).strip()
                    if out["quality_body"] else body
                )
            continue
        if stripped == "Negative Prompt:":
            i += 1
            neg_lines: list[str] = []
            while i < len(lines):
                nxt = lines[i].strip()
                if _SECTION_HEADER_RE_MC.match(nxt) or nxt == "Negative Prompt:":
                    break
                if nxt:
                    neg_lines.append(lines[i])
                i += 1
            out["negative_body"] = "\n".join(neg_lines).strip()
            continue
        i += 1
    return out


async def _maybe_compose_multichar_edit(
    request: web.Request,
    node_prompt: str,
    user_request: str,
    character_queries: list[str] | None,
    model_hash: str,
    bios: list[dict],
) -> str | None:
    """Edit-mode multi-character compose path.

    Triggers when the chat agent passed 2+ entries in character_queries
    but the existing node_prompt has 0-1 `// Character:` sections —
    i.e., the user is ADDING a character. The agent's request text is
    unreliable in this transition (treats new chars as scene content).

    Bypass the hybrid path entirely:
      1. Build a planner-shaped dict from character_queries (cast),
         existing prompt state (preserved outfit/style/negs), and the
         user request decompose (interaction verb + scene_text).
      2. compose_from_plan → structured `// Section:` body
      3. compose_scene_paragraph → inline-bundle cinematic prose
      4. Re-attach the preserved negative block verbatim.

    Returns the final prompt as cinematic prose, or None if conditions
    don't match — caller falls through to normal hybrid path. Single-
    character paths are untouched.
    """
    cleaned_queries = [
        q.strip() for q in (character_queries or [])
        if q and q.strip()
    ]
    if len(cleaned_queries) < 2:
        return None
    existing_char_count = sum(
        1 for line in (node_prompt or "").splitlines()
        if line.strip().lower().startswith("// character:")
    )
    if existing_char_count >= 2:
        # Already multi-char; let hybrid + polish post-pass handle.
        return None

    logger.info(
        "multichar-edit: trigger queries=%r existing_chars=%d",
        cleaned_queries, existing_char_count,
    )

    existing_state = _parse_existing_prompt_state(node_prompt)

    # Resolve each character via the tag-builder DB so we get series +
    # base_natlang. Bios passed in by the chat agent's preflight may
    # already have this, but re-resolve for safety so a missing field
    # doesn't drop a character.
    import os as _os
    import sqlite3
    db_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "data", "tag-builder", "tag-builder.db",
    )
    if not _os.path.exists(db_path):
        logger.warning("multichar-edit: tag-builder.db not at %r — fallback", db_path)
        return None

    resolved_bios: list[dict] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for q in cleaned_queries:
                # Normalize '-'<->'_' on BOTH sides: the chat agent emits
                # underscore-canonical Danbooru tags (chun_li), but a few
                # characters are stored hyphenated (chun-li). Without this
                # the lookup misses, <2 resolve, and the whole multi-char
                # compose silently falls back to hybrid. No two distinct
                # character tags differ only by separator, so this is safe.
                row = conn.execute(
                    "SELECT tag, display, series, base_natlang "
                    "FROM characters WHERE "
                    "LOWER(REPLACE(tag, '-', '_')) = "
                    "LOWER(REPLACE(?, '-', '_')) LIMIT 1",
                    (q,),
                ).fetchone()
                if row:
                    resolved_bios.append(dict(row))
                else:
                    logger.info(
                        "multichar-edit: tag=%r not in characters table — skipping",
                        q,
                    )
        finally:
            conn.close()
    except Exception:
        logger.warning("multichar-edit: bio lookup failed", exc_info=True)
        return None

    if len(resolved_bios) < 2:
        logger.info(
            "multichar-edit: only %d/%d characters resolved — fallback",
            len(resolved_bios), len(cleaned_queries),
        )
        return None

    # Run decompose on the user request to extract interaction + scene.
    # Pose intents map to the interaction verb. Scene intents map to
    # scene_text. Other intents are intentionally ignored — the
    # composer derives outfits from existing state + DB defaults.
    try:
        _pc_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _pc_root not in sys.path:
            sys.path.insert(0, _pc_root)
        from scripts.natlang_decompose_probe import decompose
    except Exception:
        logger.warning("multichar-edit: decompose import failed", exc_info=True)
        return None
    try:
        decomposed, _decompose_raw = await decompose(user_request)
    except Exception:
        logger.warning("multichar-edit: decompose call failed", exc_info=True)
        return None
    intents = (decomposed or {}).get("intents") or []
    interaction_text = ""
    scene_text = ""
    for it in intents:
        c = (it.get("concept") or "").lower()
        t = (it.get("text") or "").strip()
        if not t:
            continue
        if c in ("pose", "action") and not interaction_text:
            interaction_text = t
        elif c == "scene" and not scene_text:
            scene_text = t
    # Heuristic fallback when decompose doesn't isolate the interaction:
    # the user's literal request usually contains the verb. Common
    # interaction verbs surface here even when decompose is noisy.
    if not interaction_text:
        lc = (user_request or "").lower()
        for verb in (
            "fighting", "sparring", "dueling", "racing", "dancing",
            "kissing", "hugging", "embracing", "chasing", "wrestling",
            "boxing", "punching", "kicking", "blocking",
        ):
            if verb in lc:
                interaction_text = verb
                break

    # Decide outfit_text per character. Preserve existing prompt's
    # outfit body for the EXISTING character (matched by display name
    # in any of the existing `// Character:` headers). New characters
    # get outfit_text="" + outfit_source="canon" (use their default).
    existing_displays_lc: set[str] = set()
    for header, _body in existing_state["character_sections"]:
        # Header shape: "// Character: <Display> (<Series>)"
        m = re.match(r"//\s*character\s*:\s*(.+?)(?:\s*\([^)]+\))?\s*$",
                     header, re.IGNORECASE)
        if m:
            existing_displays_lc.add(m.group(1).strip().lower())

    per_character: list[dict] = []
    for bio in resolved_bios:
        display = (bio.get("display") or "").strip()
        tag = (bio.get("tag") or "").strip()
        if display.lower() in existing_displays_lc and existing_state["outfit_body"]:
            # Existing character — carry forward the outfit verbatim
            outfit_text = existing_state["outfit_body"]
            outfit_source = "literal"
        else:
            # New character — use her default canon outfit
            outfit_text = ""
            outfit_source = "canon"
        per_character.append({
            "tag": tag,
            "outfit_text": outfit_text,
            "outfit_source": outfit_source,
            "pose_text": "",
        })

    cast = [
        {"tag": b.get("tag"), "display": b.get("display")}
        for b in resolved_bios
    ]

    plan_dict = {
        "cast": cast,
        "per_character": per_character,
        "interaction": interaction_text,
        "scene_text": scene_text,
        "style_text": "",  # style is preserved separately, see below
        "lighting_text": "",
    }

    logger.info(
        "multichar-edit: plan cast=%s interaction=%r scene=%r",
        [c["display"] for c in cast],
        interaction_text or "(none)",
        scene_text or "(none)",
    )

    # compose_from_plan builds the structured `// Section:` body. We
    # then INJECT the preserved style + negs back in (compose_from_plan
    # doesn't preserve existing prompt state — it builds from the plan
    # only) before polishing.
    try:
        from scripts.natlang_compose_from_plan import compose_from_plan
        structured = compose_from_plan(
            plan_dict, resolved_bios, db_path,
            default_negative_block="",  # we re-attach existing negs below
        )
    except Exception:
        logger.warning("multichar-edit: compose_from_plan failed", exc_info=True)
        return None
    if not (structured or "").strip():
        logger.warning("multichar-edit: compose_from_plan returned empty")
        return None

    # Polish to inline-bundle prose. Skip the style step in the
    # composer — we re-attach the preserved `// Style: <Name>` section
    # AFTER the prose so the style-template swap pipeline (which
    # operates on section headers) continues to work on multi-char
    # output. Style as a global modifier doesn't need to be inside the
    # subject prose for the encoder; only the per-character bundles +
    # scene need that binding.
    try:
        from scripts.natlang_scene_composer import compose_scene_paragraph
        paragraph, _raw = await compose_scene_paragraph(
            structured,
            include_style_in_prose=False,
        )
    except Exception:
        logger.warning("multichar-edit: scene composer failed", exc_info=True)
        return None
    if not (paragraph or "").strip():
        logger.warning("multichar-edit: scene composer returned empty")
        return None

    # Sanity check: every named character must survive.
    char_display_list = [b.get("display") for b in resolved_bios]
    dropped = [
        d for d in char_display_list
        if d and d.lower() not in paragraph.lower()
    ]
    if dropped:
        logger.warning(
            "multichar-edit: composer dropped %s — fallback to hybrid",
            dropped,
        )
        return None

    # Assemble final: prose + // Style: <preserved> + Negative Prompt.
    pieces: list[str] = [paragraph.rstrip()]
    style_header = existing_state.get("style_header_line") or ""
    style_body = existing_state.get("style_body") or ""
    if style_header and style_body:
        pieces.append(f"{style_header}\n{style_body.strip()}")
    elif style_body:
        pieces.append(f"// Style:\n{style_body.strip()}")
    neg = existing_state["negative_body"].strip()
    if neg:
        pieces.append(f"Negative Prompt:\n{neg}")
    final = "\n\n".join(pieces)
    logger.info(
        "multichar-edit: composed %d -> %d chars (cast=%s, style_split=%s)",
        len(node_prompt or ""), len(final), char_display_list,
        bool(style_header or style_body),
    )
    return final


async def _polish_multichar_to_prose(final_prompt: str,
                                     pipeline_name: str = "hybrid-v1") -> str:
    """When `final_prompt` has 2+ `// Character:` sections, fold the
    sectioned body into ONE flowing cinematic paragraph via the
    scene composer. Single-character prompts are returned verbatim
    (sectioned form is KB-rich and structure-preserving is preferred).

    Modern natlang T2I encoders (T5-XXL on Flux, Qwen3-4B on Z-Image,
    Qwen2.5-VL on Qwen-Image) don't honor `// Section:` boundaries —
    a multi-character prompt with shared `// Outfit:` and `// Pose:`
    sections leaves the encoder with no way to bind features per
    subject. Inline-bundle prose with spatial anchors
    (`On the left, A wears X, doing Y. On the right, B wears Z…`)
    is what these models train on and render reliably.

    Sanity check: every named character must survive the rewrite —
    if the composer drops one (LLM nondeterminism), keep the
    structured form rather than ship a broken paragraph.

    Negative Prompt block is preserved verbatim and re-attached.
    No-op when the composer isn't importable, the prompt is empty,
    or only one character is in scope. Errors fall back to input.
    """
    if not final_prompt or not final_prompt.strip():
        return final_prompt
    char_section_count = sum(
        1 for line in final_prompt.splitlines()
        if line.strip().lower().startswith("// character:")
    )
    if char_section_count < 2:
        return final_prompt
    try:
        from scripts.natlang_scene_composer import compose_scene_paragraph
    except Exception:
        logger.warning(
            "ai-patch (%s): scene composer unavailable for multi-char polish",
            pipeline_name,
        )
        return final_prompt
    if "\n\nNegative Prompt:" in final_prompt:
        positive, _, negative = final_prompt.partition("\n\nNegative Prompt:")
    elif final_prompt.startswith("Negative Prompt:"):
        positive = ""
        negative = final_prompt[len("Negative Prompt:"):]
    else:
        positive = final_prompt
        negative = ""
    char_names: list[str] = []
    for line in positive.splitlines():
        if line.strip().lower().startswith("// character:"):
            name = line.split(":", 1)[1].strip()
            name = re.sub(r"\s*\([^)]+\)\s*$", "", name).strip()
            if name:
                char_names.append(name)
    try:
        paragraph, _raw = await compose_scene_paragraph(positive)
    except Exception:
        logger.warning(
            "ai-patch (%s): scene composer raised", pipeline_name, exc_info=True,
        )
        return final_prompt
    if not (paragraph or "").strip():
        logger.warning(
            "ai-patch (%s): scene composer returned empty — keeping structured",
            pipeline_name,
        )
        return final_prompt
    dropped = [
        name for name in char_names
        if name and name.lower() not in paragraph.lower()
    ]
    if dropped:
        logger.warning(
            "ai-patch (%s): multi-char polish dropped %s — keeping structured",
            pipeline_name, dropped,
        )
        return final_prompt
    logger.info(
        "ai-patch (%s): multi-char polish %d -> %d chars (chars=%s)",
        pipeline_name, len(positive), len(paragraph), char_names,
    )
    if negative.strip():
        return paragraph.rstrip() + "\n\nNegative Prompt:" + negative
    return paragraph


def _find_style_intent(intents: list[dict]) -> dict | None:
    """Find the last `concept=style` intent in the trace that resolved
    against a known template (resolved_source=='style'). Returns the
    intent dict (with `resolved_match_name`) or None.
    Last-wins semantics: if a turn applied two styles, the final state
    is the second swap."""
    found = None
    for it in intents or []:
        if ((it.get("concept") or "").lower() == "style"
                and (it.get("resolved_source") or "").lower() == "style"
                and (it.get("op") or "replace").lower() in ("replace", "add")
                and it.get("resolved_match_name")):
            found = it
    return found


_OUTER_WEIGHT_RE = re.compile(r"^\((.+):(\d+\.?\d*)\)$")


def _format_output(text: str, tag_format: str) -> str:
    """Post-process a single tag token. Model emits Danbooru-canonical
    underscored tags; convert to spaces when the target model expects
    spaces, AND escape any literal parens that come from canonical-tag
    franchise suffixes (`mythra_(xenoblade)` → `mythra \\(xenoblade\\)`).

    The outermost `(content:weight)` weight-wrapper is preserved as-is —
    its parens are A1111 syntax, not literal characters. Only the
    parens *inside* the tag name get escaped, because SD's parser
    otherwise reads them as nested weight markers and the whole token
    structure breaks."""
    if tag_format != "spaces" or not text:
        return text
    text = text.replace("_", " ")
    m = _OUTER_WEIGHT_RE.match(text)
    if m:
        # Weighted form: only escape parens INSIDE the content.
        content = _escape_unescaped_parens(m.group(1))
        return f"({content}:{m.group(2)})"
    # Non-weighted token: escape all unescaped parens.
    return _escape_unescaped_parens(text)


def _escape_unescaped_parens(s: str) -> str:
    """Add `\\` before `(` / `)` chars not already preceded by `\\`.
    Idempotent: applying twice gives the same result."""
    s = re.sub(r"(?<!\\)\(", r"\\(", s)
    s = re.sub(r"(?<!\\)\)", r"\\)", s)
    return s


async def _call_provider_complete(
    request_id: str,
    provider: str,
    config: dict,
    system: str,
    user_request: str,
    images: list[dict] | None = None,
) -> str:
    images = images or []
    if provider == "cloud":
        cloud = config.get("cloud") or {}
        service = cloud.get("service") or "claude"
        api_key = (cloud.get("api_key") or "").strip()
        model = (cloud.get("model") or "").strip()
        if not api_key or not model:
            return ""
        if service == "claude":
            return await _claude_complete(request_id, api_key, model, system, user_request, images)
        base_url = _cloud_base_url(cloud)
        if not base_url:
            return ""
        return await _openai_compat_complete(
            request_id, base_url, model, system, user_request, images, api_key=api_key,
        )
    if provider == "local":
        local = config.get("local") or {}
        base_url = (local.get("base_url") or "").strip().rstrip("/")
        model = (local.get("model") or "").strip()
        if not base_url or not model:
            return ""
        return await _openai_compat_complete(
            request_id, base_url, model, system, user_request, images,
        )
    return ""


async def _claude_complete(request_id, api_key, model, system, user, images) -> str:
    content = []
    for img in images:
        data = img.get("data")
        media = img.get("media_type") or "image/jpeg"
        if not data:
            continue
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": data},
        })
    content.append({"type": "text", "text": user})
    payload = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status != 200:
                err = (await resp.text())[:200]
                logger.warning("claude_complete[%s] HTTP %d: %s", request_id, resp.status, err)
                return ""
            data = await resp.json()
            blocks = data.get("content") or []
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


async def _openai_compat_complete(
    request_id, base_url, model, system, user, images, api_key: str | None = None,
) -> str:
    content = []
    for img in images:
        data = img.get("data")
        media = img.get("media_type") or "image/jpeg"
        if not data:
            continue
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        })
    content.append({"type": "text", "text": user})
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content if images else user},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": _MAX_TOKENS,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url}/chat/completions", json=payload, headers=headers,
        ) as resp:
            if resp.status != 200:
                err = (await resp.text())[:200]
                logger.warning("openai_complete[%s] HTTP %d: %s", request_id, resp.status, err)
                return ""
            data = await resp.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            return msg.get("content") or ""


async def _run_generation(request_id: str, provider: str, config: dict,
                          system: str, user_request: str, images: list[dict]) -> str:
    """Streams from the configured provider with thinking events for the
    live counter, then returns the collected body for the caller to parse
    + validate. Visible deltas are not emitted — `/ai/patch` (the only
    caller) reads the full body post-stream as JSON."""
    try:
        if provider == "cloud":
            cloud = config.get("cloud") or {}
            service = cloud.get("service") or "claude"
            api_key = (cloud.get("api_key") or "").strip()
            model = (cloud.get("model") or "").strip()
            if not api_key:
                _emit(request_id, "error", error="Cloud API key not configured")
                return ""
            if not model:
                _emit(request_id, "error", error="Cloud model not configured")
                return ""
            if service == "claude":
                return await _stream_claude(
                    request_id, api_key, model, system, user_request, images,
                )
            base_url = _cloud_base_url(cloud)
            if not base_url:
                _emit(request_id, "error", error="Cloud base URL missing")
                return ""
            return await _stream_openai_compat(
                request_id, base_url, model, system, user_request, images,
                api_key=api_key,
            )
        if provider == "local":
            return await _stream_local(
                request_id, config, system, user_request, images,
            )
        _emit(request_id, "error", error=f"unknown provider {provider}")
        return ""
    except asyncio.CancelledError:
        _emit(request_id, "cancelled")
        raise
    except Exception as e:
        logger.exception("generation failed")
        _emit(request_id, "error", error=str(e))
        return ""


def _emit(request_id: str, event: str, *, content: str = "", error: str = "",
          tokens: int | None = None):
    payload = {
        "request_id": request_id,
        "event": event,
        "content": content,
        "error": error,
        "t": time.time(),
    }
    if tokens is not None:
        payload["tokens"] = tokens
    send_ws("promptchain_ai_stream", payload)


# ── Claude streaming ──────────────────────────────────────────────

async def _stream_claude(request_id: str, api_key: str, model: str,
                         system: str, user_request: str, images: list[dict]) -> str:
    """Streams from Claude with thinking events for the live counter, then
    returns the collected body for the caller to parse + validate. Visible
    deltas are suppressed — the patch endpoint reads the full body post-
    stream and emit-as-JSON would be noise."""
    content = []
    for img in images:
        data = img.get("data")
        media = img.get("media_type") or "image/jpeg"
        if not data:
            continue
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": data},
        })
    content.append({"type": "text", "text": user_request})

    payload = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "stream": True,
    }

    logger.info("stream_claude[%s] model=%s images=%d user_chars=%d",
                request_id, model, len(images), len(user_request))
    chars_streamed = 0
    deltas_received = 0
    saw_stop = False
    parse_failures = 0
    body_buffer: list[str] = []

    timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        ) as resp:
            logger.info("stream_claude[%s] HTTP %d", request_id, resp.status)
            if resp.status != 200:
                data = await resp.text()
                logger.warning("stream_claude[%s] error body: %s", request_id, data[:500])
                _emit(request_id, "error", error=f"HTTP {resp.status}: {data[:200]}")
                return ""

            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    parse_failures += 1
                    logger.debug("stream_claude[%s] json parse fail: %s | line=%r", request_id, e, data_str[:200])
                    continue
                evt_type = evt.get("type")
                if evt_type == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        deltas_received += 1
                        chars_streamed += len(text)
                        body_buffer.append(text)
                elif evt_type == "message_stop":
                    saw_stop = True
                    return "".join(body_buffer)
                elif evt_type == "error":
                    err = (evt.get("error") or {}).get("message") or "stream error"
                    logger.warning("stream_claude[%s] stream error event: %s", request_id, err)
                    _emit(request_id, "error", error=err)
                    return ""
    _log_stream_exit("stream_claude", request_id, deltas_received, chars_streamed, saw_stop, parse_failures)
    return "".join(body_buffer)


def _log_stream_exit(fn: str, request_id: str, deltas: int, chars: int, saw_stop: bool, parse_failures: int):
    # Stream loop exited without an explicit stop event — log whether we
    # got any content at all so "empty response" cases are visible in the
    # server log rather than silently resolving as a zero-delta success.
    level = logging.WARNING if chars == 0 else logging.INFO
    logger.log(
        level,
        "%s[%s] stream loop exited without stop: deltas=%d chars=%d parse_failures=%d",
        fn, request_id, deltas, chars, parse_failures,
    )


# ── Local (OpenAI-compat + Ollama native) streaming ───────────────

async def _stream_local(request_id: str, config: dict, system: str,
                        user_request: str, images: list[dict]) -> str:
    local = config.get("local") or {}
    base_url = (local.get("base_url") or "").strip().rstrip("/")
    model = (local.get("model") or "").strip()
    if not base_url:
        _emit(request_id, "error", error="Local base URL not configured")
        return ""
    if not model:
        _emit(request_id, "error", error="Local model not configured")
        return ""

    # If the user has images AND we're talking to Ollama, prefer the native
    # /api/chat path since it takes images at the message level (cleaner
    # than the OpenAI-compat data: URL shape). Otherwise use /v1/chat/completions
    # universally — works for llama.cpp, LM Studio, Ollama's /v1, KoboldCPP.
    ollama_root = _ollama_root(base_url)
    use_ollama_native = bool(images) and await _is_ollama(ollama_root)

    if use_ollama_native:
        return await _stream_ollama_native(
            request_id, ollama_root, model, system, user_request, images,
        )
    # Probe Ollama once even on the no-images branch so we can pass
    # think=false through the OpenAI-compat path (Ollama tolerates it,
    # cloud providers reject unknown fields).
    is_ollama = await _is_ollama(ollama_root)
    return await _stream_openai_compat(
        request_id, base_url, model, system, user_request, images,
        is_ollama=is_ollama,
    )


async def _is_ollama(ollama_root: str) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{ollama_root}/api/tags") as resp:
                return resp.status == 200
    except Exception:
        return False


async def _is_ollama_model_loaded(ollama_root: str, model: str) -> bool:
    """True if `model` is currently loaded into Ollama's VRAM (`/api/ps`).
    Used to gate the 'Loading model' status indicator on cold first
    calls. Returns False on any probe failure — the request continues
    normally without the indicator rather than blocking."""
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        target = (model or "").strip().lower()
        if not target:
            return False
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{ollama_root}/api/ps") as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
        for m in (data.get("models") or []):
            for key in ("name", "model"):
                v = (m.get(key) or "").strip().lower()
                if v == target:
                    return True
        return False
    except Exception:
        return False


async def _warmup_ollama_model(ollama_root: str, model: str) -> None:
    """Force Ollama to load `model` into VRAM by hitting /api/generate
    with no prompt — Ollama loads the model and returns once it's ready.
    This makes the 'Loading model' status linger accurately during the
    actual VRAM populate, instead of being clobbered by the next stage's
    status the moment we emit it. Quietly no-ops on failure; the real
    chat call would have triggered the load anyway, we just lose the
    indicator."""
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{ollama_root}/api/generate",
                json={"model": model, "keep_alive": "5m"},
            ) as resp:
                await resp.read()
    except Exception:
        logger.warning("ollama warmup failed", exc_info=True)


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>([\s\S]*?)</think>", re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)


async def _stream_ollama_native(request_id: str, ollama_root: str, model: str,
                                system: str, user_request: str, images: list[dict]) -> str:
    messages = [{"role": "system", "content": system}]
    msg = {"role": "user", "content": user_request}
    if images:
        msg["images"] = [img.get("data") for img in images if img.get("data")]
    messages.append(msg)

    # Suppress reasoning blocks: qwen3-thinking and similar burn 5-20k tokens
    # of <think>...</think> per turn. We're feeding the model curated bundle
    # data, not asking it to reason — pure waste. Ollama 0.5+ honors `think`.
    # num_ctx: Ollama defaults to 2048 unless the Modelfile sets higher. Our
    # system prompt + bio + bundles + thinking can exceed that easily, which
    # silently truncates input mid-prompt and confuses the model — likely
    # cause of the recurring deadlock pattern. 32K gives qwen3-vl plenty of
    # room (model itself supports 256K).
    # repeat_penalty=1.3: 8B Qwen occasionally degenerates into a
    # comma-list repetition loop when given thin context (empty bios,
    # short user message). Default 1.1 isn't strong enough to break the
    # KV-cache reinforcement once it starts. 1.3 stops the runaway
    # without hurting legitimate output. Seen in the wild: the
    # `mythra_(xenoblade_chronicles_2)` matcher-miss → 175s of
    # `mythra_serene, mythra_graceful, mythra_radiant, ...` cycle.
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {"num_ctx": 32768, "repeat_penalty": 1.3},
    }
    logger.info("stream_ollama[%s] url=%s model=%s images=%d user_chars=%d",
                request_id, f"{ollama_root}/api/chat", model, len(images), len(user_request))
    chars_streamed = 0
    deltas_received = 0
    saw_done = False
    parse_failures = 0
    body_buffer: list[str] = []
    thinking_buffer: list[str] = []
    # Ollama streams approximately one detokenized output token per chunk,
    # so chunk count is a close proxy for token count. eval_count on the
    # final done event is the ground truth if we ever need to reconcile.
    reasoning_tokens = 0
    saw_think_block = False
    first_event_logged = False

    timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{ollama_root}/api/chat", json=payload) as resp:
            logger.info("stream_ollama[%s] HTTP %d", request_id, resp.status)
            if resp.status != 200:
                body = await resp.text()
                logger.warning("stream_ollama[%s] error body: %s", request_id, body[:500])
                _emit(request_id, "error", error=f"HTTP {resp.status}: {body[:200]}")
                return ""
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception as e:
                    parse_failures += 1
                    logger.debug("stream_ollama[%s] json parse fail: %s | line=%r", request_id, e, line[:200])
                    continue
                if not first_event_logged:
                    first_event_logged = True
                    dbg.info("ai[%s] ollama first event keys=%s message_keys=%s raw=%s",
                             request_id, list(evt.keys()),
                             list((evt.get("message") or {}).keys()),
                             _trunc(json.dumps(evt, ensure_ascii=True), 1500))
                msg = evt.get("message") or {}
                # Newer Ollama splits reasoning into a dedicated `thinking`
                # field. Surface it via a separate WS event so the client
                # can show "Reasoning…" without polluting the prompt body.
                thinking_chunk = msg.get("thinking") or ""
                if thinking_chunk:
                    thinking_buffer.append(thinking_chunk)
                    reasoning_tokens += 1
                    _request_reasoning_chars[request_id] = (
                        _request_reasoning_chars.get(request_id, 0) + len(thinking_chunk)
                    )
                    _emit(request_id, "thinking", content=thinking_chunk, tokens=reasoning_tokens)
                chunk = msg.get("content", "")
                if chunk:
                    if not saw_think_block and _THINK_OPEN_RE.search(chunk):
                        saw_think_block = True
                        dbg.info("ai[%s] inline <think> block detected in content "
                                 "(chunk=%r) — body_buffer will contain reasoning",
                                 request_id, chunk[:200])
                    deltas_received += 1
                    chars_streamed += len(chunk)
                    body_buffer.append(chunk)
                if evt.get("done"):
                    saw_done = True
                    # Ollama puts timing/eval counts on the done event.
                    dbg.info(
                        "ai[%s] ollama done metadata: total=%sms load=%sms "
                        "prompt_eval=%s tokens (%sms) eval=%s tokens (%sms) "
                        "thinking_chars=%d body_chars=%d inline_think=%s",
                        request_id,
                        _ms(evt.get("total_duration")), _ms(evt.get("load_duration")),
                        evt.get("prompt_eval_count"), _ms(evt.get("prompt_eval_duration")),
                        evt.get("eval_count"), _ms(evt.get("eval_duration")),
                        sum(len(t) for t in thinking_buffer),
                        chars_streamed, saw_think_block,
                    )
                    if thinking_buffer:
                        _dump(request_id, "thinking_buffer", "".join(thinking_buffer))
                    return "".join(body_buffer)
    _log_stream_exit("stream_ollama", request_id, deltas_received, chars_streamed, saw_done, parse_failures)
    if thinking_buffer:
        _dump(request_id, "thinking_buffer", "".join(thinking_buffer))
    return "".join(body_buffer)


def _ms(ns) -> str:
    """Ollama reports durations in nanoseconds. Render as integer ms for logs."""
    if not isinstance(ns, (int, float)):
        return "?"
    return f"{int(ns / 1_000_000)}"


async def _stream_openai_compat(request_id: str, base_url: str, model: str,
                                system: str, user_request: str, images: list[dict],
                                api_key: str | None = None,
                                is_ollama: bool = False) -> str:
    content = []
    for img in images:
        data = img.get("data")
        media = img.get("media_type") or "image/jpeg"
        if not data:
            continue
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        })
    content.append({"type": "text", "text": user_request})

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content if images else user_request},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": _MAX_TOKENS,
    }
    if is_ollama:
        # Ollama-served reasoning models burn 5-20k tokens on <think>
        # blocks per turn. We're feeding curated bundle data, not asking
        # the model to derive anything — disable reasoning. Field is
        # Ollama-specific; OpenAI/Anthropic reject unknown payload keys.
        payload["think"] = False
        # Match the native /api/chat path — bump num_ctx so our system
        # prompt + bio + bundles + thinking aren't truncated to 2048.
        # qwen3-vl supports 256K natively; 32K is comfortable headroom.
        # repeat_penalty=1.3: breaks comma-list degenerate loops on
        # thin-context calls (see _stream_ollama_native for the
        # observed-in-wild cause).
        payload["options"] = {"num_ctx": 32768, "repeat_penalty": 1.3}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    logger.info("stream_openai_compat[%s] url=%s model=%s images=%d user_chars=%d",
                request_id, f"{base_url}/chat/completions", model, len(images), len(user_request))
    chars_streamed = 0
    deltas_received = 0
    saw_done = False
    parse_failures = 0
    body_buffer: list[str] = []
    thinking_buffer: list[str] = []
    # Most OpenAI-compat servers stream ~1 token per delta chunk; counting
    # reasoning_content deltas is a close (not exact) proxy for token count.
    reasoning_tokens = 0
    saw_think_block = False
    first_event_logged = False

    timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT, sock_read=_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base_url}/chat/completions", json=payload, headers=headers) as resp:
            logger.info("stream_openai_compat[%s] HTTP %d", request_id, resp.status)
            if resp.status != 200:
                body = await resp.text()
                logger.warning("stream_openai_compat[%s] error body: %s", request_id, body[:500])
                _emit(request_id, "error", error=f"HTTP {resp.status}: {body[:200]}")
                return ""
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    if thinking_buffer:
                        _dump(request_id, "thinking_buffer", "".join(thinking_buffer))
                    return "".join(body_buffer)
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except Exception as e:
                    parse_failures += 1
                    logger.debug("stream_openai_compat[%s] json parse fail: %s | line=%r", request_id, e, data_str[:200])
                    continue
                if not first_event_logged:
                    first_event_logged = True
                    delta_obj = (evt.get("choices") or [{}])[0].get("delta") or {}
                    dbg.info("ai[%s] openai-compat first event delta_keys=%s raw=%s",
                             request_id, list(delta_obj.keys()),
                             _trunc(json.dumps(evt, ensure_ascii=True), 1500))
                choices = evt.get("choices") or []
                if not choices:
                    continue
                delta_obj = choices[0].get("delta") or {}
                # DeepSeek-R1, Qwen-Reasoning, vLLM-with-think etc. expose
                # reasoning via `reasoning_content`. Capture and surface as
                # a separate event so the client can show "Reasoning…".
                reasoning = delta_obj.get("reasoning_content") or delta_obj.get("reasoning") or ""
                if reasoning:
                    thinking_buffer.append(reasoning)
                    reasoning_tokens += 1
                    _request_reasoning_chars[request_id] = (
                        _request_reasoning_chars.get(request_id, 0) + len(reasoning)
                    )
                    _emit(request_id, "thinking", content=reasoning, tokens=reasoning_tokens)
                delta = delta_obj.get("content") or ""
                if delta:
                    if not saw_think_block and _THINK_OPEN_RE.search(delta):
                        saw_think_block = True
                        dbg.info("ai[%s] inline <think> block detected in content "
                                 "(chunk=%r) — body_buffer will contain reasoning",
                                 request_id, delta[:200])
                    deltas_received += 1
                    chars_streamed += len(delta)
                    body_buffer.append(delta)
                finish = choices[0].get("finish_reason")
                if finish:
                    logger.info("stream_openai_compat[%s] finish_reason=%s deltas=%d chars=%d",
                                request_id, finish, deltas_received, chars_streamed)
    _log_stream_exit("stream_openai_compat", request_id, deltas_received, chars_streamed, saw_done, parse_failures)
    if thinking_buffer:
        _dump(request_id, "thinking_buffer", "".join(thinking_buffer))
    return "".join(body_buffer)


# ── patch planner ─────────────────────────────────────────────────
# Single endpoint backing the AI Assistant panel. The model streams
# internally for the live token counter but returns one round-trip
# response shaped as sectioned tag output (or natlang prose); the
# server validates and post-processes before reply.

def _patch_system_prompt(bios: list[dict] | None = None,
                         has_node_prompt: bool = False,
                         prompt_style: str = "tags") -> str:
    bios = bios or []
    has_bio = bool(bios)
    if prompt_style == "natural":
        return _patch_system_prompt_natlang_impl(
            has_bio=has_bio,
            has_node_prompt=has_node_prompt,
        )
    has_default_outfit_slots = any(
        (b.get("default_outfit") or {}).get("slots") for b in bios
    )
    has_override_outfit_slots = any(
        (b.get("user_requested_outfit") or {}).get("slots") for b in bios
    )
    has_any_outfit_slots = has_default_outfit_slots or has_override_outfit_slots
    multi_char = sum(1 for b in bios if b and b.get("tag")) >= 2
    return _patch_system_prompt_impl(
        has_bio=has_bio,
        has_default_outfit_slots=has_default_outfit_slots,
        has_override_outfit_slots=has_override_outfit_slots,
        has_any_outfit_slots=has_any_outfit_slots,
        has_node_prompt=has_node_prompt,
        multi_char=multi_char,
    )


def _patch_system_prompt_natlang_impl(has_bio: bool,
                                       has_node_prompt: bool = False) -> str:
    """Natlang-mode system prompt for checkpoints trained on natural-
    language captions (z-image, Qwen-Image, etc.). Mirrors the tag-mode
    sectioned output structure — same `// Section:` headers, same order
    — but section bodies are prose paragraphs instead of comma-separated
    tag tokens. Negatives stay tag-shaped (template authoring convention,
    downstream conditioning expectation)."""
    parts = [
        "You are writing a Stable Diffusion prompt for the user. "
        "The target checkpoint is trained on natural-language captions, "
        "not Danbooru tags — section bodies are descriptive PROSE, not "
        "comma-separated tag lists.",
        "",
    ]
    if has_node_prompt:
        parts.extend([
            "PATCH MODE — a non-empty node_prompt is provided in the user "
            "message. You are PATCHING it, not rebuilding it.",
            "- Reproduce every section that exists in node_prompt VERBATIM. "
            "Section headers, ordering, and prose bodies survive unchanged "
            "unless the user's request directly modifies them.",
            "- The ONLY edits you may make are direct consequences of the "
            "user's request (an explicit add, remove, or swap).",
            "- Sentences the user did not mention MUST appear unchanged in "
            "their original section. Sections the user did not mention MUST "
            "appear unchanged in your output.",
            "- Do NOT \"clean up\", \"streamline\", or \"focus\" the existing "
            "prompt. Minimum-change is the rule.",
            "- AUTO-DECOMPOSE: if node_prompt has NO `// Section:` headers "
            "(it's flat prose pasted from somewhere else), distribute its "
            "content across the canonical sections (Character / Outfit / "
            "Pose, Action & Prop / Expression / Setting / Scene) in your "
            "output. The content survives verbatim where possible — you're "
            "just splitting it into the right buckets, not rewording.",
            "",
        ])
    parts.extend([
        "Output ONLY the prompt body — no preamble, no explanations, no "
        "markdown fences, no commentary. Output sectioned plain text in this "
        "exact format:",
        "",
        "// Section Header",
        "<prose paragraph describing this section>",
        "",
        "// Next Section Header",
        "<prose paragraph for that section>",
        "",
        "Each section gets:",
        "- A `// Section: <Name>` header line — use the exact header supplied "
        "in the user message bio block, including any ` from Character: <name>` "
        "or `(signature)` suffix.",
        "- A blank line between sections; one prose body per section "
        "(multi-sentence, descriptive).",
        "- Body is PROSE — flowing sentences, not comma-separated tokens. "
        "Do NOT emit Danbooru-canonical tag forms (`cammy_white`, `1girl`, "
        "`presenting_foot`) in section bodies — write the natural-language "
        "equivalent.",
        "",
        "SECTION STRUCTURE for fresh prompts (only emit a section if the "
        "user or bio supplies content for it; do NOT add empty/speculative "
        "sections):",
        "  // Character: <Name> (<Series>)",
        "    <prose describing the character — appearance, build, "
        "distinguishing features. Weave from the bio's base_natlang field.>",
        "  // Outfit: <Outfit Name> from Character: <Character>",
        "    <prose describing what they're wearing. Weave from the bio's "
        "outfit_natlang field.>",
        "  // Pose:  (or `// Pose: <Name> (signature) from Character: <X>` "
        "when a bio matched a named pose — use that exact header verbatim "
        "from the user message bio block)",
        "    <prose describing body position, limb arrangement, gestures, "
        "gaze direction, presented body parts, what they're doing, and any "
        "props they're interacting with.>",
        "  // Expression",
        "    <one-sentence prose describing facial affect ONLY — what the "
        "face is showing emotionally (smiling, smirking, blushing, neutral). "
        "Gaze direction does NOT belong here; it goes in Pose, Action & Prop. "
        "OMIT this section entirely if the user did not mention an expression "
        "or facial affect — do NOT invent one. Bios do not carry expression "
        "fields, so the only valid trigger is the user's request.>",
        "  // Setting / Scene",
        "    <prose describing the environment, location, mood, lighting, "
        "atmosphere, time of day, weather. OMIT this section entirely if "
        "the user did not mention an environment, location, lighting, or "
        "mood — do NOT invent a scene (no \"dimly lit rooftop\", no \"sunlit "
        "garden\", no \"misty forest\" unless the user asked for one). Bios "
        "do not carry setting fields, so the only valid trigger is the user's "
        "request.>",
        "",
        "ORDER inside the prompt body (top to bottom): character → outfit "
        "→ pose → expression → setting/scene. These five are the only "
        "positive sections that exist. The server may inject an additional "
        "`// Style: <Name>` section after setting; do not emit one yourself. "
        "Do NOT invent any other section header.",
        "",
        "BIO SOURCE PRIORITY:",
        "  1. If a character bio block is provided in the user message, use "
        "its `base_natlang`, `outfit_natlang`, and `pose_natlang` text as the "
        "source of truth. Reproduce the prose verbatim where possible; weave "
        "in user modifications minimally.",
        "  2. For everything else, write a short descriptive prose phrase. "
        "Do not invent details that contradict the bio.",
        "",
        "NEGATIVE PROMPT:",
        "If a Negative Prompt is needed, place it at the very end on its own "
        "line, starting with `Negative Prompt:` followed by a comma-separated "
        "list of canonical-tag tokens (NOT prose). Negatives describe what to "
        "suppress in latent space and are tag-shaped regardless of positive "
        "shape.",
        "",
        "RULES:",
        "- Never invent character or outfit details you aren't sure about. "
        "If unsure, omit.",
        "- Make the minimum change the user requested. Only emit a section "
        "if the user or bio supplies content for it; never speculatively add "
        "a section the user didn't mention.",
        "- Do not include placeholder tags from examples in this prompt as "
        "content.",
        "- 'wearing only X' / 'just X' / 'only X' is a STRIP instruction, NOT "
        "a confirmation. The user wants to REMOVE every other outfit item "
        "and keep ONLY X. Rewrite the // Outfit prose to describe ONLY the "
        "named item(s); the rest is implicitly nude.",
        "    * The new Outfit prose must NOT reference any removed items, "
        "even as location markers or anchors. Wrong: 'red socks visible "
        "above the open tops of the boots' (boots are removed). Right: "
        "'Wearing only red socks; otherwise nude.'",
        "    * The new Outfit prose must NOT enumerate what was removed "
        "(\"the leotard, beret, etc. are all removed\"). Just describe the "
        "current state.",
        "    * CASCADE: if Outfit was stripped, audit the // Pose, Action & "
        "Prop and // Expression and // Setting / Scene sections. Any "
        "sentence that references a now-removed outfit item (boots, "
        "leotard, beret, gloves, harness, etc.) MUST be rewritten to drop "
        "that reference. Example: pose said 'feet visible through the open "
        "tops of her combat boots'; outfit now has no boots; pose should "
        "be rewritten to 'feet visible' (or whatever the action is) "
        "without the boot anchor. The pose stays — only the removed-item "
        "anchors get stripped.",
        "",
    ])
    if has_bio:
        parts.append(
            "- A character bio is attached below. Treat its natlang fields "
            "(`base_natlang`, outfit `natlang`, pose `natlang`) as "
            "authoritative — use the prose verbatim where the user's request "
            "doesn't require modification."
        )
    parts.extend(["", "/no_think"])
    return "\n".join(parts)


def _patch_system_prompt_impl(has_bio: bool,
                              has_default_outfit_slots: bool,
                              has_override_outfit_slots: bool,
                              has_any_outfit_slots: bool,
                              has_node_prompt: bool = False,
                              multi_char: bool = False) -> str:
    """Sectioned plain-text output. No JSON, no tools. The patch UX
    (remove/add/keep chips) is derived client-side by diffing the new
    output against existing editor content."""
    parts = [
        "You are writing a Stable Diffusion prompt for the user.",
        "",
    ]
    if has_node_prompt:
        # The model's biggest failure mode in patch flow is dropping
        # untouched sections — it reads "only emit a section if the user
        # supplies content" (a build-mode rule) and inverts it into "drop
        # any section the user didn't mention this turn." Lead with an
        # explicit patch-mode rule so untouched sections survive.
        parts.extend([
            "PATCH MODE — a non-empty node_prompt is provided in the user "
            "message. You are PATCHING it, not rebuilding it.",
            "- Reproduce every section that exists in node_prompt VERBATIM. "
            "Sections, headers, ordering, and tags survive unchanged unless the "
            "user's request directly modifies them.",
            "- The ONLY edits you may make are direct consequences of the "
            "user's request (an explicit add, remove, or swap).",
            "- Tags the user did not mention MUST appear unchanged in their "
            "original section. Sections the user did not mention MUST appear "
            "unchanged in your output.",
            "- Do NOT \"clean up\", \"streamline\", or \"focus\" the existing "
            "prompt. Minimum-change is the rule.",
            "- This rule overrides the section-template guidance below — "
            "the templates are for FRESH prompts only.",
            "- AUTO-DECOMPOSE: if node_prompt has NO `// Section:` headers "
            "(it's a flat tag list pasted from somewhere else), distribute "
            "its tokens across the canonical sections (Character / Outfit / "
            "Pose, Action & Prop / Expression / Setting / Scene) in your "
            "output. Tokens survive verbatim — you're just routing each "
            "token to the right section based on what it describes.",
            "",
        ])
    else:
        # BUILD MODE symmetric to the PATCH MODE block above. qwen3-vl:8b
        # routinely speculates // Pose, // Setting / Scene, and // Quality
        # on bare requests like "cammy white" or "set up cammy_white",
        # even though the inline "only if supplied" rule says not to.
        # Lead with an explicit no-speculation rule so the inline rule
        # isn't the only line of defense.
        parts.extend([
            "BUILD MODE — node_prompt is empty. You are composing a FRESH "
            "prompt from scratch using the user's request and the character "
            "bio (if provided).",
            "- Emit ONLY sections the user explicitly asked for, plus // "
            "Character and // Outfit when a character bio is attached.",
            "- DO NOT speculate. If the user said \"cammy white\", emit // "
            "Character and // Outfit (from bio) — nothing else. Do not invent "
            "// Pose, // Expression, // Setting / Scene, or // Quality "
            "content the user did not request.",
            "- // Style is server-managed — do not emit a // Style section "
            "unless the user explicitly named a style. The server injects "
            "the model's default style template automatically.",
            "- // Quality is user-owned and OPTIONAL. Emit it only when the "
            "user explicitly named quality tokens (e.g. `add masterpiece, "
            "absurdres`). Generic SD prompting tokens like `highres`, "
            "`detailed`, `sharp focus`, `cinematic lighting` are NOT defaults "
            "you should add on your own — they belong only when the user "
            "asked.",
            "",
        ])
    parts.extend([
        "Output ONLY the prompt body — no preamble, no explanations, no markdown "
        "fences, no commentary. Output sectioned plain text in this exact format:",
        "",
        "// Section Header",
        "tag1, tag2, tag3, ...",
        "",
        "// Next Section Header",
        "tag1, tag2, ...",
        "",
        "Each section gets:",
        "- A `// Section: <Name>` header line",
        "- A blank line is fine between sections; one comma-separated tag line per section.",
        "- Use Danbooru-canonical underscored tag forms (lowercase, words joined by `_`). "
        "The output post-processor converts to spaces if the target model wants spaces.",
        "- Weighted form `(tag:1.1)` is a single literal token — preserve verbatim.",
        "",
        "SECTION STRUCTURE for fresh prompts (only emit a section if the user or bio supplies "
        "content for it; do NOT add empty/speculative sections):",
        "  // Character: <name from bio>",
        "    <subject count, weighted character tag, appearance tags>",
        "  // Outfit: <outfit name>",
        "    <outfit tags — clothing items, garments, footwear, accessories, leotards, etc>",
        "  // Pose, Action & Prop  (comma-separated canonical danbooru tags — covers body "
        "position, limb arrangement, gestures, where the head/eyes are directed, presented "
        "body parts, and props the character interacts with. Break the user's natural-language "
        "pose into its component tags; do NOT fuse them into one phrase.)",
        "    <pose/action/prop/gaze tags>",
        "  // Expression  (facial affect ONLY — what the face shows emotionally. Gaze "
        "direction does NOT belong here; it goes in Pose, Action & Prop.)",
        "    <facial expression tag>",
        "  // Setting / Scene",
        "    <scene tags>",
        "  // Style: <template name>  (optional — user-owned. Carries style "
        "template body like `masterpiece, best quality, very awa` or "
        "`Photorealistic, sharp focus`. Server may inject or override this.)",
        "    <style tags>",
        "  // Quality  (optional — user-owned. Quality/aesthetic tokens "
        "the user wants applied to the whole image.)",
        "    <quality tags>",
        "",
        "ORDER inside the prompt body (top to bottom):",
        "  character → outfit → pose+action+prop → expression → setting → style → quality.",
        "Do NOT invent sections outside this list. Do NOT emit "
        "negative-prompt-style slop in any positive section.",
        "",
        "PATCH-MODE PRESERVATION for // Style and // Quality:",
        "- These sections are user-owned content. When node_prompt has them "
        "and the user's request doesn't ask to change them, REPRODUCE them "
        "VERBATIM in your output (header + body). Don't strip them.",
        "- Only modify them if the user explicitly asks (`switch style to "
        "hyperrealistic`, `add absurdres`, etc.). The server handles style "
        "alias resolution and template injection separately — your job is "
        "just to preserve the user's existing content.",
        "",
        "TAG SOURCE PRIORITY:",
        "  1. If a character bio is provided in the user message, use its base_tags "
        "and outfit tags VERBATIM. Do not paraphrase, drop, or rename them.",
        "  2. For everything else, use canonical Danbooru tag forms you know "
        "(underscored, lowercase). Prefer the canonical tag over a paraphrase.",
        "  3. If a concept doesn't have a clean canonical form, write it as a short "
        "natural-language phrase. Mixing canonical Danbooru tags with short phrasal "
        "tokens is fine — modern SD models are trained on both styles.",
        "",
        "RULES:",
        "- Never invent character canonical tags you aren't sure about. If unsure, omit.",
        "- A weighted form `(tag:1.1)` is one atomic token. Reproduce it VERBATIM, parens and all. "
        "Never strip the parens to write `tag:1.1`, never split it into `tag, 1.1`, never reweight it.",
        "- Make the minimum change the user requested. Only emit a section if the user or "
        "bio supplies content for it; never speculatively add a section the user didn't "
        "mention. When the user supplies a value for a section, emit only what they said — "
        "do not pad with adjacent details they didn't ask for.",
        "- Do not include placeholder tags from examples in this prompt as content.",
        "",
    ])
    if multi_char:
        # Patch flow keeps the `// Character: name` / `// Outfit: name`
        # output structure (no SUBJECT_N labels), so the aggregate count
        # token lives in the FIRST character's section and shared
        # sections live ONCE at the end after all per-character blocks.
        parts.extend([
            "",
            "MULTI-CHARACTER COMPOSITION (this turn has 2+ character bios):",
            "- Subject count: AGGREGATE across characters in the OUTPUT. Each "
            "bio's `subject count:` slot in the user message is THAT character's "
            "individual count (1girl/1boy). The composition needs ONE aggregate "
            "token: 1girl × 2 → `2girls`; 1girl + 1boy → `1boy, 1girl`; "
            "2girls + 1boy → `1boy, 2girls`; 1boy × 2 → `2boys`.",
            "- Place the aggregate count token in the FIRST character's "
            "`// Character` section (replacing the per-character `1girl`/`1boy`). "
            "Do NOT include `1girl`/`1boy` in subsequent characters' "
            "`// Character` sections — the aggregate covers everyone.",
            "- Per-character sections — `// Character` and `// Outfit` are emitted "
            "PER CHARACTER. Each character's `// Character` body uses that "
            "character's `character tag:` + `appearance tags:` slots.",
            "- SHARED sections — emit ONCE at the END after ALL per-character "
            "blocks: ONE `// Pose, Action & Prop` for the interaction, ONE "
            "`// Expression` (or omit entirely if not implied), ONE `// Setting "
            "/ Scene`. Do NOT duplicate these sections per character.",
            "- Output order for multi-character: `// Character: A` → "
            "`// Outfit: A` → `// Character: B` → `// Outfit: B` → "
            "`// Pose, Action & Prop` → `// Expression` → `// Setting / Scene`.",
            "- OUTFIT BORROW (this rule OVERRIDES BOTH the per-character "
            "rule above AND PATCH MODE's verbatim-preservation rule for the "
            "// Outfit section): if a bio is marked `Outfit Source: <name>` "
            "OR the user's request asks for one character to WEAR ANOTHER "
            "CHARACTER'S OUTFIT (e.g. `cammy in chun-li's outfit`, `change "
            "outfit to chun-li's outfit`, `wearing chun-li's clothes`), this "
            "is NOT multi-character. Do ALL of the following:",
            "    1. Keep ONLY the primary character's `// Character` section "
            "unchanged. Do NOT emit the source character's `// Character` "
            "section.",
            "    2. REPLACE the `// Outfit: <old name>` header with "
            "`// Outfit: <source outfit name> from Character: <source "
            "character tag>` — this is a REQUIRED header rename, not "
            "preservation. The old outfit name (e.g. `Delta Red`) MUST be "
            "replaced with the source outfit's name (e.g. `SF2 Classic`).",
            "    3. REPLACE the // Outfit body tokens with the SOURCE "
            "character's outfit slot tags VERBATIM (each slot's tag becomes "
            "a token in // Outfit). The primary character's old outfit "
            "tokens MUST be removed.",
            "    4. Do NOT compute an aggregate count — the prompt is still "
            "single-subject (`1girl`/`1boy`, not `2girls`).",
            "    5. Do NOT use the source's appearance tags (hair, eyes, "
            "build) — only their outfit.",
        ])
    parts.extend([
        "",
        "SLOT-AWARE CONFLICT RESOLUTION:",
        "- The user message includes an `Available slot modifiers` block. For each row, "
        "use your own semantic understanding to decide whether the user's request implies "
        "that modifier — match by intent, not by exact phrase. For each modifier you decide applies:",
        "    1. If the row says `ADD <tag> to // Outfit` or `ADD <tag> to // Pose, Action & Prop`, "
        "add that exact canonical_tag to the named section verbatim (do not paraphrase).",
        "    2. If the row says `also ADD <tag> to // Outfit`, additionally add that tag to // Outfit.",
        "    3. Drop every outfit slot phrase whose slot appears in the modifier's `clears` list. "
        "(No-op if there's no slot-decomposed outfit in the bio.)",
        "- For a color/style swap stated as `<color> <item>` in the user request:",
        "    1. Find the outfit slot whose item matches and ADD `<color>_<item>` to // Outfit.",
        "    2. Drop the displaced source_phrase from // Outfit.",
        "- When a decomposed intent is tagged `strip:` (the user wants the named garment to "
        "BE the entire outfit — no accessories, no base layer), the user message will only "
        "show the user's named item(s); the bio's default-outfit slot list is suppressed so "
        "you don't accidentally emit it. Just write the named item(s) into // Outfit. The "
        "server auto-negates the displaced default-outfit phrases.",
        "- When a decomposed intent is tagged `outfit:` and the user's named garment "
        "occupies one slot of the bio's layered outfit (leotard, gloves, boots, etc.), emit "
        "the user's item AND keep the other bio slots that aren't displaced — gloves/boots/"
        "headwear stack on top of a leotard.",
        "- If no conflict touches a filled slot, leave the outfit alone.",
    ])
    if has_any_outfit_slots:
        if has_default_outfit_slots:
            parts.extend([
                "",
                "OUTFIT HEADER: keep the // Outfit: <name> section header matching the outfit's "
                "name from the bio. Do NOT rename the header based on user modifications "
                "(color swaps, modifiers, etc.).",
                "(The Negative Prompt section for default-outfit drops is handled deterministically "
                "by the server — you don't need to emit one. Just drop displaced phrases from "
                "// Outfit per the rules above.)",
            ])
    parts.extend(["", "/no_think"])
    if has_bio:
        parts.append(
            "- A character bio is attached below. Treat its tags as authoritative — "
            "use them verbatim instead of re-deriving, EXCEPT where slot-aware conflict "
            "resolution requires dropping a tag (see above)."
        )
    return "\n".join(parts)


def _strip_leading_section_header(prose: str) -> str:
    """Some curated natlang fields begin with a `// Section: ...` line
    from the source format. The server now constructs section headers
    itself in `_patch_user_message_natlang`, so strip the duplicate
    when present to avoid emitting two consecutive header lines."""
    if not prose:
        return prose
    lines = prose.splitlines()
    if lines and lines[0].lstrip().startswith("//"):
        return "\n".join(lines[1:]).lstrip()
    return prose


def _build_natlang_slot_conflicts(user_request: str,
                                   bios: list[dict] | None,
                                   node_prompt: str = "") -> str:
    """Generate a slot-conflict-resolution block for the natlang user
    message. Two directions:

    Forward: user fires a modifier (e.g. `barefoot` clears [footwear,
    legwear]) → tell model to drop matching outfit slots from // Outfit.

    Reverse: user fills a slot (e.g. `wearing red socks` fills legwear)
    → that displaces a modifier WHOSE PHRASES ALREADY APPEAR IN
    `node_prompt`. Only fire when the prior prose actually contains the
    modifier's alias (`barefoot`, `nude`, etc.) — otherwise the alert
    instructs the model to scrub phrases that aren't there, which is
    cheating: the system prompt would tell the model about modifiers
    that have never been part of state.

    Returns empty string when no conflict applies. Surfaced ONLY in
    natlang mode — tag mode handles both directions at token level via
    `_drop_displaced_modifiers` and `_enforce_applies_modifiers`."""
    if not bios or not user_request:
        return ""
    detected = _detect_modifiers_in_text(user_request)
    displaced_canonicals = _resolve_slot_displacements(
        user_request, node_prompt=node_prompt,
    )
    user_filled_slots = _detect_user_filled_slots(user_request)
    if not detected and not displaced_canonicals:
        return ""
    blocks: list[str] = []

    # Forward: modifier fired → drop matching outfit slots.
    for b in bios:
        outfit = b.get("user_requested_outfit") or b.get("default_outfit")
        if not outfit:
            continue
        slots = outfit.get("slots") or []
        if not slots:
            continue
        display = (b.get("display") or "").strip() or b.get("tag") or ""
        char_clears: dict[str, list[dict]] = {}
        for mod in detected:
            cleared = [s.strip().lower() for s in (mod.get("clears_slots") or []) if s.strip()]
            if not cleared:
                continue
            for slot_row in slots:
                slot_name = (slot_row.get("slot") or "").strip().lower()
                if slot_name in cleared:
                    char_clears.setdefault(mod["canonical_tag"], []).append(slot_row)
        if not char_clears:
            continue
        for canonical_tag, matched_slots in char_clears.items():
            phrase_list = ", ".join(
                f"`{(s.get('source_phrase') or s.get('item') or '').strip()}`"
                + (f" ({(s.get('color') or '').strip()})"
                   if s.get('color') else "")
                for s in matched_slots
                if (s.get('source_phrase') or s.get('item'))
            )
            slot_set = sorted({(s.get("slot") or "").strip().lower()
                               for s in matched_slots
                               if s.get("slot")})
            blocks.append(
                f"- Character `{display}`: user fired modifier "
                f"`{canonical_tag}` which clears slots [{', '.join(slot_set)}]. "
                f"REMOVE all references to these items from the // Outfit "
                f"prose: {phrase_list}. Keep all other outfit items intact."
            )

    # Reverse: user filled a slot → modifier displaced. Surface its
    # alias phrases so the model can scrub them from any prose section
    # (typically // Pose where the previous turn may have written
    # 'bare feet prominently displayed' or similar implying barefoot).
    if displaced_canonicals:
        modifiers_by_canon = {
            m["canonical_tag"]: m for m in _load_slot_modifiers()
        }
        for canonical_tag in sorted(displaced_canonicals):
            mod = modifiers_by_canon.get(canonical_tag)
            if not mod:
                continue
            aliases = mod.get("aliases") or []
            if canonical_tag.replace("_", " ") not in aliases:
                aliases = [canonical_tag.replace("_", " ")] + list(aliases)
            phrase_quoted = ", ".join(f"'{a}'" for a in aliases)
            slot_set = sorted(user_filled_slots) if user_filled_slots else []
            blocks.append(
                f"- User added an item filling slot(s) "
                f"[{', '.join(slot_set)}], which displaces modifier "
                f"`{canonical_tag}`. Scan ALL prose sections (especially "
                f"// Pose) and REMOVE any sentence or clause referring to "
                f"{phrase_quoted} — the character is no longer in that "
                f"state. Also remove descriptive phrases like 'feet "
                f"prominently displayed', 'soles visible', 'toes spread' "
                f"that implied the now-displaced modifier."
            )

    if not blocks:
        return ""
    header = (
        "SLOT CONFLICT ALERT (resolve outfit/pose contradictions before "
        "writing your output):"
    )
    return header + "\n" + "\n".join(blocks)


def _patch_user_message_natlang(node_prompt: str, user_request: str,
                                bios: list[dict] | None = None) -> str:
    """Natlang-mode user message. Surfaces bio fields in the same
    `// Section: <Name>` format the model is expected to mirror back —
    `// Character: <display> (<series>)`, `// Outfit: <name> from
    Character: <display>`, `// Pose: <name>(signature) from Character:
    <display>`. Section bodies are the curated prose from the DB.

    Format consistency between input bio block and expected model output
    reduces drift; model learns the exact section header by example."""
    sections: list[str] = []
    if bios:
        bio_lines = [
            "Character bios from local database. Each `// Section:` block "
            "below is a header you MUST mirror in your output (verbatim, "
            "including any `from Character:` and `(signature)` suffixes), "
            "with the prose body either reproduced or minimally modified per "
            "the user's request:",
        ]
        for b in bios:
            if not b or not b.get("tag"):
                continue
            display = (b.get("display") or "").strip() or b["tag"]
            series = (b.get("series") or "").strip()
            char_header = (
                f"// Character: {display}"
                + (f" ({series})" if series else "")
            )
            base_nat = _strip_leading_section_header(
                (b.get("base_natlang") or "").strip()
            )
            base_tags = (b.get("base_tags") or "").strip()
            bio_lines.append("")
            bio_lines.append(char_header)
            if base_nat:
                bio_lines.append(base_nat)
            elif base_tags:
                # Fallback: bio has only tag form. Surface tags as a
                # comma-joined English list and trust the model to
                # naturalize. Curate base_natlang to fix.
                bio_lines.append(
                    f"(no natlang available, derived from tags: {base_tags})"
                )
            outfit = b.get("user_requested_outfit") or b.get("default_outfit")
            if outfit:
                outfit_name = (outfit.get("name") or "").strip()
                outfit_nat = _strip_leading_section_header(
                    (outfit.get("natlang") or "").strip()
                )
                outfit_tags = (outfit.get("tags") or "").strip()
                outfit_header = (
                    f"// Outfit: {outfit_name} from Character: {display}"
                    if outfit_name
                    else f"// Outfit from Character: {display}"
                )
                bio_lines.append("")
                bio_lines.append(outfit_header)
                if outfit_nat:
                    bio_lines.append(outfit_nat)
                elif outfit_tags:
                    bio_lines.append(
                        f"(no natlang available, derived from tags: "
                        f"{outfit_tags})"
                    )
            pose = b.get("matched_pose")
            if pose:
                pose_name = (pose.get("name") or "").strip()
                pose_nat = _strip_leading_section_header(
                    (pose.get("natlang") or "").strip()
                )
                pose_tags = (pose.get("tags") or "").strip()
                signature_suffix = (
                    " (signature)" if pose.get("is_signature") else ""
                )
                pose_header = (
                    f"// Pose: {pose_name}{signature_suffix} "
                    f"from Character: {display}"
                    if pose_name
                    else f"// Pose from Character: {display}"
                )
                bio_lines.append("")
                bio_lines.append(pose_header)
                if pose_nat:
                    bio_lines.append(pose_nat)
                elif pose_tags:
                    bio_lines.append(
                        f"(no natlang available, derived from tags: "
                        f"{pose_tags})"
                    )
        sections.append("\n".join(bio_lines))

    conflict_block = _build_natlang_slot_conflicts(user_request, bios, node_prompt)
    if conflict_block:
        sections.append(conflict_block)

    if node_prompt:
        sections.append(
            "Existing node_prompt (modify this prose; preserve sentences "
            "you aren't asked to change):\n" + node_prompt
        )

    sections.append(f"User request:\n{user_request}")
    return "\n\n".join(sections)


# Subject-count tokens (1girl, 2boys, solo, multiple_girls, etc.). When
# multi-character bios are sent to the patch flow we slice this slot
# out of base_tags so the LLM sees a labeled `subject count:` line per
# character and aggregates across them ("1girl × 2 → 2girls", "1girl
# + 1boy → 1boy, 1girl") instead of emitting per-character `1girl`
# tokens that bypass the multi-char composition signal in the latent
# space.
_SUBJECT_COUNT_RE = re.compile(
    r"^(?:\d+(?:girls?|boys?|others?)|solo|solo_focus|multiple_(?:girls|boys|others))$",
    re.IGNORECASE,
)


def _split_base_tags(base_tags: str) -> dict:
    """Slice `base_tags` into {character, subject, appearance} buckets.

    - character: weighted form like `(cammy_white:1.1)` — one atomic token
    - subject: subject-count tokens (`1girl`, `2girls`, `solo`, ...)
    - appearance: everything else (hair, eyes, build, etc.)"""
    out = {"character": [], "subject": [], "appearance": []}
    if not base_tags:
        return out
    weighted_re = re.compile(r":\s*\d+(?:\.\d+)?\s*\)$")
    for raw in base_tags.split(","):
        tok = raw.strip()
        if not tok:
            continue
        if tok.startswith("(") and tok.endswith(")") and weighted_re.search(tok):
            out["character"].append(tok)
            continue
        if _SUBJECT_COUNT_RE.match(tok):
            out["subject"].append(tok)
            continue
        out["appearance"].append(tok)
    return out


_NUM_GENDER_COUNT_RE = re.compile(
    r"^(\d+)(girls?|boys?|others?)$", re.IGNORECASE,
)


def _compute_aggregate_count(bios: list[dict]) -> list[str]:
    """Canonical Danbooru-format aggregate subject count tokens for the
    composition. Sums per-bio counts from each bio's subject slot
    (parsed from base_tags). Order: boys, girls, others — Danbooru
    convention puts boys first. Examples: 1boy + 1girl -> ['1boy',
    '1girl']; 2 girls -> ['2girls']; 1 boy + 2 girls -> ['1boy', '2girls']."""
    girls = 0
    boys = 0
    others = 0
    for b in bios or []:
        if not b or not b.get("tag"):
            continue
        slots = _split_base_tags(b.get("base_tags") or "")
        for tok in slots["subject"]:
            m = _NUM_GENDER_COUNT_RE.match(tok.strip())
            if not m:
                continue
            n = int(m.group(1))
            kind = m.group(2).lower()
            if "girl" in kind:
                girls += n
            elif "boy" in kind:
                boys += n
            elif "other" in kind:
                others += n
    parts: list[str] = []
    for n, sing, plur in (
        (boys, "1boy", f"{boys}boys"),
        (girls, "1girl", f"{girls}girls"),
        (others, "1other", f"{others}others"),
    ):
        if n == 1:
            parts.append(sing)
        elif n > 1:
            parts.append(plur)
    return parts


def _strip_franchise_tokens_from_scene_style(
    sections: list[dict], request_id: str,
) -> list[dict]:
    """Drop franchise/series/IP tokens from // Setting / Scene and
    // Style section bodies. The patch model frequently emits a
    franchise name from its world knowledge (primed by a bio's
    canonical tag like `(ryu_(street_fighter):1.1)`) and stuffs it
    into the scene/style section even after the user_request line
    has been franchise-stripped. Belt-and-suspenders deterministic
    post-process. Matches franchises >= 6 chars to avoid generic
    short-name false positives."""
    franchises = _load_known_franchise_names()
    if not franchises:
        return sections
    target_keys = {"setting", "scene", "style"}
    long_franchises = [f for f in franchises if f and len(f) >= 6]
    if not long_franchises:
        return sections
    out: list[dict] = []
    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        section_key = _section_key_from_header(s.get("header") or "")
        if section_key not in target_keys:
            out.append(s)
            continue
        kept: list[str] = []
        for t in s.get("tokens") or []:
            t_norm = re.sub(r"[\s_]+", " ", (t or "").strip().lower())
            if not t_norm:
                continue
            is_franchise = False
            for f in long_franchises:
                if t_norm == f or f in t_norm:
                    is_franchise = True
                    break
            if is_franchise:
                dropped.append(f"[{section_key}] {t}")
            else:
                kept.append(t)
        out.append({**s, "tokens": kept})
    if dropped:
        dbg.info(
            "ai-patch[%s] franchise-strip output: %s",
            request_id, ", ".join(dropped),
        )
    return out


_CHAR_HEADER_RE = re.compile(
    r"^\s*//\s*Character\s*:\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


_WEIGHTED_TAG_RE = re.compile(
    r"\(\s*(?P<inner>[^()]*(?:\([^()]*\)[^()]*)*?)\s*:\s*[0-9.]+\s*\)",
)


def _bio_weighted_tag_for(canon: str, bios: list[dict]) -> str | None:
    """Look up the weighted character tag form (`(canon:1.1)`) for a
    character canonical from the bios list. Returns None if the
    canonical doesn't match any bio."""
    canon_lc = (canon or "").strip().lower()
    if not canon_lc:
        return None
    for b in bios or []:
        bio_canon = (b.get("tag") or "").strip().lower()
        if bio_canon != canon_lc:
            continue
        slots = _split_base_tags(b.get("base_tags") or "")
        if slots["character"]:
            return slots["character"][0]
    return None


_CHAR_HEADER_PREFIX_RE = re.compile(r"^\s*//\s*character\s*:", re.IGNORECASE)
_OUTFIT_HEADER_PREFIX_RE = re.compile(r"^\s*//\s*outfit\s*[:]", re.IGNORECASE)


def _reorder_multi_char_sections(
    sections: list[dict], bios: list[dict], request_id: str,
) -> list[dict]:
    """Multi-char structural reorder: ensure each `// Outfit: <canon>`
    immediately follows its `// Character: <canon>` section. The 8B
    patch model occasionally clusters all character sections first,
    then all outfit sections (despite the system prompt's interleave
    rule). Without this fix the downstream `BREAK` insertion in
    `_enforce_multi_char_composition` lands between the two char
    sections but leaves no separator between the second character
    and the first outfit — sagat's chunk pulls in ryu's outfit when
    the prompt is flat-rendered.

    Pairs by canonical-name substring match first (header normalized
    via `_normalize_separators`, longest-canon first to disambiguate
    `cammy_white` from `cammy`). Then falls back to matching the
    outfit header against each bio's `default_outfit.name` /
    `user_requested_outfit.name` — the system prompt instructs the
    model to emit `// Outfit: <outfit name>` (just the outfit name,
    not the character canon prefix), so canonical-name matching alone
    misses real production output. Outfit sections matching neither
    flow through as "other" sections — partial pairing beats nothing.

    Also fires for single-char output: when the model emits sections
    in non-canonical order (e.g. // Character at the bottom after a
    swap), this places it first. Other sections retain their relative
    order."""
    if not sections or not bios:
        return sections

    bio_canons = [
        _normalize_separators(b.get("tag") or "")
        for b in bios if b and b.get("tag")
    ]
    bio_canons = sorted({c for c in bio_canons if c}, key=len, reverse=True)
    if not bio_canons:
        return sections

    # Build outfit-name → bio-canon map. The patch model emits
    # `// Outfit: Classic Gi` (just the outfit name from the system-
    # prompt format), so we need a second matching axis beyond bio
    # canonical. Each bio's outfit name (default or user-requested) is
    # collected; longest-name first to bias toward more specific matches.
    outfit_name_to_canon: list[tuple[str, str]] = []
    for b in bios or []:
        if not b:
            continue
        canon = _normalize_separators(b.get("tag") or "")
        if not canon:
            continue
        for outfit_key in ("user_requested_outfit", "default_outfit"):
            outfit = b.get(outfit_key) or {}
            name = (outfit.get("name") or "").strip()
            if name:
                outfit_name_to_canon.append((_normalize_separators(name), canon))
    outfit_name_to_canon.sort(key=lambda p: len(p[0]), reverse=True)

    char_sections: list[tuple[dict, str]] = []
    outfit_by_canon: dict[str, dict] = {}
    unmatched_outfits: list[dict] = []
    other_sections: list[dict] = []

    def _match_canon_in_header(header: str) -> str:
        h_norm = _normalize_separators(header)
        for c in bio_canons:
            if c in h_norm:
                return c
        return ""

    def _match_outfit_to_canon(header: str) -> str:
        h_norm = _normalize_separators(header)
        for name, canon in outfit_name_to_canon:
            if name and name in h_norm:
                return canon
        return ""

    for s in sections:
        header = s.get("header") or ""
        if _CHAR_HEADER_PREFIX_RE.match(header):
            char_sections.append((s, _match_canon_in_header(header)))
            continue
        if _OUTFIT_HEADER_PREFIX_RE.match(header):
            canon = _match_canon_in_header(header) or _match_outfit_to_canon(header)
            if canon and canon not in outfit_by_canon:
                outfit_by_canon[canon] = s
            else:
                # Either no canon match (model emitted an outfit name
                # that doesn't appear in any bio's outfit name and lacks
                # a canon prefix in the header) OR duplicate canon
                # (model paraphrased both outfits to similar names).
                # Held aside for emission-order fallback below.
                unmatched_outfits.append(s)
            continue
        other_sections.append(s)

    if not char_sections:
        return sections

    out: list[dict] = []
    used: set[str] = set()
    paired = 0
    unpaired_chars: list[dict] = []
    for s, canon in char_sections:
        out.append(s)
        if canon and canon in outfit_by_canon and canon not in used:
            out.append(outfit_by_canon[canon])
            used.add(canon)
            paired += 1
        else:
            unpaired_chars.append(s)

    # Emission-order fallback: when name-matching couldn't pair every
    # outfit (model paraphrased outfit names away from the bio form,
    # e.g. bio says `Classic Muay Thai (Purple)` but model emits
    # `// Outfit: Muay Thai Shorts`), pair the leftover outfits to
    # characters that didn't get an outfit, in emission order. The
    # 8B model emits outfit-A before outfit-B in the same order it
    # emitted char-A before char-B, so positional pairing is reliable
    # in practice. Without this fallback the cluster shape (char-A,
    # char-B, outfit-A, outfit-B) survives whenever the model
    # paraphrases outfit names.
    if unmatched_outfits and unpaired_chars:
        # Rebuild `out` so each unpaired char gets its positional outfit
        # inserted right after it.
        char_to_outfit_fallback: dict[int, dict] = {}
        for idx, char_s in enumerate(unpaired_chars):
            if idx >= len(unmatched_outfits):
                break
            char_to_outfit_fallback[id(char_s)] = unmatched_outfits[idx]
        rebuilt: list[dict] = []
        consumed_fallbacks: set[int] = set()
        for s in out:
            rebuilt.append(s)
            fb = char_to_outfit_fallback.get(id(s))
            if fb is not None and id(fb) not in consumed_fallbacks:
                rebuilt.append(fb)
                consumed_fallbacks.add(id(fb))
                paired += 1
        out = rebuilt
        # Any unmatched outfits beyond the unpaired-char count still
        # need to flow through somewhere — append at the end.
        for s in unmatched_outfits:
            if id(s) not in consumed_fallbacks:
                out.append(s)
    else:
        # No fallback needed: dump any leftover unmatched outfits at the
        # end (this matches the original behavior).
        out.extend(unmatched_outfits)

    out.extend(other_sections)

    if out != sections:
        dbg.info(
            "ai-patch[%s] multi-char reorder: paired %d char/outfit, "
            "char_sections=%d outfit_sections=%d",
            request_id, paired, len(char_sections),
            len(outfit_by_canon) + len(unmatched_outfits),
        )
    return out


def _enforce_multi_char_composition(
    sections: list[dict], bios: list[dict], request_id: str,
) -> list[dict]:
    """Deterministic post-process for multi-character output:
      1. Compute the canonical aggregate count from bios (server-side
         truth, not model-emitted).
      2. Drop ALL subject-count tokens (1girl/1boy/2girls/solo/etc.)
         from every // Character section.
      3. Inject the aggregate count as the FIRST token of the FIRST
         // Character section. Subsequent character sections have no
         count tokens (the aggregate covers everyone).
      4. Insert `BREAK` separator at chunk boundaries: prepended to
         each // Character section after the first, and prepended to
         the first non-character/outfit section that follows the last
         character block. The compiler's _smart_join recognizes
         `BREAK` and surrounds it with spaces in the assembled flat
         prompt — text encoders chunk on it and stop attribute bleed
         (char A's hair color leaking onto char B's tags). Bare form
         matches the A1111 / dfl-clip-with-break / asagi4 prompt-
         control convention; bracketed `[BREAK]` is recognized by
         nothing in the wild.
      5. Repair character-tag copy-paste errors: the patch model
         occasionally emits char A's weighted tag (`(ryu:1.1)`) in
         char B's // Character section body. The header is
         authoritative — drop any weighted tag whose canonical
         doesn't match the section's header canonical, and inject
         the correct weighted tag from the matching bio if missing.
    Single-char output (bios < 2): no aggregate to compute, no chunk
    boundary needed. Strip any stale `BREAK` tokens left over from
    a prior multi-char turn — `BREAK` is a chunk separator for
    multi-character text-encoder slicing and has no role in single-
    character output."""
    if not sections or not bios:
        return sections

    # Count how many // Character sections the model actually emitted.
    # If fewer than bios, the model did NOT compose multi-character —
    # most commonly because the user said something like
    # `change outfit to chun-li's outfit` which matched 2 character
    # tokens (cammy_white + chun-li) in the request but is semantically
    # an outfit-borrow on a single subject. Injecting `2girls` aggregate
    # + BREAK in that case produces a phantom-second-girl prompt.
    # Bail to the single-char scrub path.
    emitted_char_sections = sum(
        1 for s in sections
        if not s.get("is_negative")
        and (s.get("header") or "").lower().startswith("// character")
    )
    single_char_mode = len(bios) < 2 or emitted_char_sections < len(bios)
    if single_char_mode:
        if len(bios) >= 2 and emitted_char_sections < len(bios):
            dbg.info(
                "ai-patch[%s] multi-char composer bailed: bios=%d but only "
                "%d // Character section(s) emitted — treating as single-char "
                "output (likely outfit-borrow, not multi-subject)",
                request_id, len(bios), emitted_char_sections,
            )
        out: list[dict] = []
        scrubbed = 0
        for s in sections:
            if s.get("is_negative"):
                out.append(s)
                continue
            tokens = s.get("tokens") or []
            kept = [t for t in tokens if t.strip().upper() != "BREAK"]
            if len(kept) != len(tokens):
                scrubbed += len(tokens) - len(kept)
                out.append({**s, "tokens": kept})
            else:
                out.append(s)
        if scrubbed:
            dbg.info(
                "ai-patch[%s] single-char: stripped %d stale BREAK token(s)",
                request_id, scrubbed,
            )
        return out

    aggregate_tokens = _compute_aggregate_count(bios)

    out: list[dict] = []
    seen_first_char = False
    inserted_pre_shared = False

    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        header_lc = (s.get("header") or "").lower()
        is_char = header_lc.startswith("// character")
        is_outfit = header_lc.startswith("// outfit")
        is_char_block = is_char or is_outfit

        new_tokens = list(s.get("tokens") or [])

        if is_char:
            # Strip subject counts from EVERY character section. They
            # only live in the first one as the aggregate.
            new_tokens = [
                t for t in new_tokens
                if not _SUBJECT_COUNT_RE.match(t.strip())
            ]

            # Repair character-tag copy-paste: the model sometimes
            # emits char A's `(canonA:1.1)` in char B's // Character
            # section. The section HEADER (// Character: <canon>) is
            # authoritative — strip any weighted tag whose canonical
            # doesn't match the header, then inject the correct one
            # from the bio if missing.
            header_match = _CHAR_HEADER_RE.match(s.get("header") or "")
            section_canon = (
                header_match.group("name").strip().lower()
                if header_match else ""
            )
            if section_canon:
                expected_weighted = _bio_weighted_tag_for(section_canon, bios)
                expected_canon_lc = section_canon
                kept_after_repair: list[str] = []
                injected_weighted = False
                for t in new_tokens:
                    m = _WEIGHTED_TAG_RE.fullmatch(t.strip())
                    if m:
                        inner_lc = m.group("inner").strip().lower()
                        if inner_lc == expected_canon_lc:
                            kept_after_repair.append(t)
                            injected_weighted = True
                        # else: drop wrong-character weighted tag
                    else:
                        kept_after_repair.append(t)
                if not injected_weighted and expected_weighted:
                    # Re-insert correct weighted tag at the front
                    kept_after_repair = (
                        [expected_weighted] + kept_after_repair
                    )
                new_tokens = kept_after_repair

            if not seen_first_char:
                new_tokens = aggregate_tokens + new_tokens
                seen_first_char = True
            else:
                if not new_tokens or new_tokens[0] != "BREAK":
                    new_tokens = ["BREAK"] + new_tokens

        elif seen_first_char and not is_char_block and not inserted_pre_shared:
            if not new_tokens or new_tokens[0] != "BREAK":
                new_tokens = ["BREAK"] + new_tokens
            inserted_pre_shared = True

        new_section = {**s, "tokens": new_tokens}
        out.append(new_section)

    if aggregate_tokens or out != sections:
        dbg.info(
            "ai-patch[%s] multi-char compose: aggregate=%r "
            "char_sections=%d break_inserted=%s",
            request_id, ", ".join(aggregate_tokens),
            sum(1 for s in out
                if (s.get("header") or "").lower().startswith("// character")),
            inserted_pre_shared,
        )
    return out


def _patch_user_message(node_prompt: str, user_request: str,
                        bios: list[dict] | None = None,
                        sub_intents: list[dict] | None = None,
                        tag_candidates: list[dict] | None = None,
                        prompt_style: str = "tags",
                        modify_outfit_hint: str = "") -> str:
    if prompt_style == "natural":
        return _patch_user_message_natlang(
            node_prompt, user_request, bios=bios,
        )
    sections: list[str] = []
    # Suppress the bio's default-outfit slot list ONLY when decompose
    # tagged the user's garment as `strip:` (semantic outfit-replace —
    # `wearing only X` or single-piece outfits like nightgown/kimono/
    # hospital gown). For `outfit:` intents (slot replacement — leotard,
    # gloves, boots), the bio default stays visible so the patch model
    # can swap the named slot and keep the rest of the layered outfit.
    # user_requested_outfit (a curated outfit the matcher picked from
    # "killer bee outfit" etc.) is NEVER suppressed — that IS what the
    # user asked for.
    user_strips_outfit = bool(sub_intents) and any(
        (i.get("section") or "").lower() == "strip"
        for i in (sub_intents or [])
    )

    # Patch-mode outfit-preservation: when node_prompt has an existing
    # // Outfit body AND the user's request has no outfit-related
    # sub-intent (no `outfit:` / `strip:`), suppress the bio's
    # default_outfit so the patch model doesn't see new-character
    # outfit data and overwrite the user's existing outfit. Triggered
    # by character-swap requests like `switch character to mythra` —
    # decompose produces `[clear] character` + `[character] mythra`,
    # no outfit intent, but mythra's bio carries her default outfit
    # which would otherwise replace the user's curated outfit.
    has_outfit_intent = bool(sub_intents) and any(
        (i.get("section") or "").lower() in ("outfit", "strip")
        for i in (sub_intents or [])
    )
    existing_outfit_body = bool(re.search(
        r"^\s*//\s*Outfit\s*:[^\n]*\n[^\n/]+",
        node_prompt or "", re.MULTILINE | re.IGNORECASE,
    ))
    patch_preserves_outfit = (
        bool(node_prompt) and existing_outfit_body and not has_outfit_intent
    )
    if bios:
        # Multi-character bios get the slot-split treatment so the LLM can
        # aggregate subject count across characters per the system prompt
        # rule. Single-character keeps the legacy `base_tags: ...` shape
        # — no aggregation to do, no benefit to a slot split that costs
        # tokens for no reason.
        multi_char = sum(1 for b in (bios or []) if b and b.get("tag")) >= 2
        bio_lines = ["Character bios from local database — use these tags VERBATIM:"]
        for b in bios:
            if not b or not b.get("tag"):
                continue
            is_outfit_source = bool(b.get("_outfit_source_only"))
            if is_outfit_source:
                # Outfit-borrow source: render WITHOUT appearance tags so
                # the model can't mistake this character for a subject.
                # Only their outfit slots are useful here. The OUTFIT
                # BORROW system prompt instruction tells the model to
                # use these slots under // Outfit: <name> from Character:
                # <tag> on the primary subject — not as a second
                # // Character section.
                bio_lines.append(
                    f"\nOutfit Source: {b['tag']}  "
                    f"[reference only — use this character's outfit slots "
                    f"on the primary subject; do NOT emit a // Character "
                    f"section for {b['tag']}]"
                )
            else:
                bio_lines.append(f"\nCharacter: {b['tag']}")
            base = (b.get("base_tags") or "").strip()
            if is_outfit_source:
                # Skip appearance/subject tags entirely — they're not
                # used for borrow.
                pass
            elif base and multi_char:
                split = _split_base_tags(base)
                if split["subject"]:
                    bio_lines.append(f"  subject count: {', '.join(split['subject'])}")
                if split["character"]:
                    bio_lines.append(f"  character tag: {', '.join(split['character'])}")
                if split["appearance"]:
                    bio_lines.append(f"  appearance tags: {', '.join(split['appearance'])}")
            elif base:
                bio_lines.append(f"  base_tags: {base}")
            outfit = b.get("user_requested_outfit") or b.get("default_outfit")
            is_user_requested_outfit = bool(b.get("user_requested_outfit"))
            suppress_default_slots = (
                (user_strips_outfit or patch_preserves_outfit)
                and not is_user_requested_outfit
            )
            if suppress_default_slots:
                # Bio's default outfit is the FALLBACK when the user
                # doesn't describe an outfit — but the user IS describing
                # one this turn. Showing the slot list anyway nudges the
                # model to emit those tokens alongside the user's named
                # item, which is exactly the failure we're avoiding.
                outfit = None
            if outfit and outfit.get("tags"):
                outfit_label = "user-requested outfit" if is_user_requested_outfit else "default outfit"
                outfit_name = outfit.get("name") or ""
                slots = outfit.get("slots") or []
                # Slots are authoritative when present — emit them in place of
                # the flat outfit_tags blob to avoid the model cross-checking
                # two representations of the same data.
                if slots:
                    bio_lines.append(
                        f"  {outfit_label}{f' ({outfit_name})' if outfit_name else ''} (slot-decomposed; this IS the outfit):"
                    )
                    for s in slots:
                        slot = s.get("slot") or "?"
                        phrase = s.get("source_phrase") or ""
                        item = s.get("item") or ""
                        color = s.get("color") or ""
                        attrs = ", ".join(p for p in (f"color={color}" if color else "", f"item={item}" if item else "") if p)
                        bio_lines.append(
                            f"      {slot}: {phrase}" + (f"  [{attrs}]" if attrs else "")
                        )
                else:
                    bio_lines.append(
                        f"  {outfit_label}{f' ({outfit_name})' if outfit_name else ''}: "
                        f"{outfit['tags']}"
                    )
            pose = b.get("matched_pose")
            if pose and pose.get("tags"):
                pose_name = pose.get("name") or ""
                bio_lines.append(
                    f"  user-requested pose{f' ({pose_name})' if pose_name else ''}: "
                    f"{pose['tags']}"
                )
        sections.append("\n".join(bio_lines))
    if node_prompt:
        sections.append("Existing node_prompt (modify this; preserve sections you aren't asked to change):\n" + node_prompt)
    # Send the full modifier table to the AI (not just alias-matched rows)
    # so it can semantically match user intent — e.g. "pointing her feet at
    # viewer" → presenting_foot — without us maintaining an exhaustive alias
    # list. The AI's language understanding bridges paraphrase to canonical.
    # Sent regardless of whether a character bio is present: even freestyle
    # prompts benefit from the substitute-tag mapping (the slot-clearing
    # behavior just no-ops when there's no outfit to clear).
    all_modifiers = _load_slot_modifiers()
    if all_modifiers:
        # Two-stage gating to prevent the "rule-text-as-content" failure
        # mode where listing all 7 modifiers with their full rule blocks
        # caused the model to parrot slot names ("footwear, legwear") and
        # rule keywords ("pose, action, prop, gaze") back as tags.
        #
        # Stage 1 — alias scan (deterministic): word-boundary regex match
        # on each modifier's curated aliases. Hits are [APPLIES]: model
        # MUST follow the rule.
        #
        # Stage 2 — bi-encoder semantic match (bge-small over the wiki
        # gloss): top-k modifiers above cosine 0.65 are [SEMANTIC]
        # candidates. Calibrated against real and adversarial prompts —
        # foot-presenting paraphrases score 0.64-0.83 and distractors
        # ("girl walking through forest") top out at 0.50-0.58.
        #
        # Modifiers that match NEITHER stage are completely omitted from
        # the prompt — no rule text for them at all. Cuts the modifier
        # block from ~21 lines down to 0-6 lines depending on relevance,
        # which is what stopped the parroting.
        detected = _detect_modifiers_in_text(user_request)
        applies_by_tag = {d["canonical_tag"]: d for d in detected}
        if applies_by_tag:
            dbg.info(
                "patch[modifiers] alias-detected: %s",
                ", ".join(f"{t} (matched {d['matched_alias']!r})"
                          for t, d in applies_by_tag.items()),
            )

        from . import modifier_search
        # 0.65 was noise-level — bge-small cosine similarity starts being
        # meaningful around 0.75+. Marginal scores like 0.653 surfaced
        # `presenting_foot` for unrelated requests like 'enrich scene
        # with cyberpunk elements', which 8B then dutifully applied.
        # Direct phrase mentions ('presenting her foot') still get caught
        # by alias-detected (the [APPLIES] path), independent of this
        # semantic threshold.
        semantic_hits = modifier_search.search(user_request, top_k=2, threshold=0.75)
        semantic_by_tag = {h["canonical_tag"]: h for h in semantic_hits
                           if h["canonical_tag"] not in applies_by_tag}
        if semantic_by_tag:
            dbg.info(
                "patch[modifiers] semantic-matched: %s",
                ", ".join(f"{t} ({h['score']:.3f})"
                          for t, h in semantic_by_tag.items()),
            )

        def _action_clause(m: dict) -> str:
            parts: list[str] = []
            if m["is_substitute"]:
                section = "// Pose, Action & Prop" if m["substitute_section"] == "pose" else "// Outfit"
                parts.append(f"ADD `{m['canonical_tag']}` to {section}")
            slots = ", ".join(m["clears_slots"])
            if slots:
                parts.append(f"clears [{slots}]")
            if m["implies_outfit_tag"]:
                parts.append(f"also ADD `{m['implies_outfit_tag']}` to // Outfit")
            if not m["is_substitute"] and not parts:
                parts.append("no substitute")
            return "; ".join(parts)

        # Preserve the canonical sort_order from the DB while filtering
        # to only the gated subset. Keeps consistent ordering across
        # requests so cache-hit potential is not poisoned by set order.
        shown = [m for m in all_modifiers
                 if m["canonical_tag"] in applies_by_tag
                 or m["canonical_tag"] in semantic_by_tag]
        if shown:
            mod_lines = [
                "Slot modifiers — entries marked [APPLIES] already matched a phrase "
                "in your request and MUST be followed verbatim. Entries marked "
                "[SEMANTIC] are candidates whose definition is semantically close to "
                "your request — apply only if your request actually implies them. "
                "Modifiers not listed here are not relevant; do not invent them.",
            ]
            for m in shown:
                tag = m["canonical_tag"]
                applied = applies_by_tag.get(tag)
                label = "[APPLIES]" if applied else "[SEMANTIC]"
                header = f"  {label} {tag}"
                if applied:
                    header += f" (matched: \"{applied['matched_alias']}\")"
                mod_lines.append(header)
                gloss = _neutralize_gloss(m.get("definition") or "")
                if gloss:
                    mod_lines.append(f"      Definition: {gloss}")
                verb = "Action:" if applied else "Action if applies:"
                mod_lines.append(f"      {verb} {_action_clause(m)}")
            sections.append("\n".join(mod_lines))
    # Decomposed sub-intents — surface them so the model knows what
    # we already split out. Helps when picking section assignments for
    # individual tags.
    if sub_intents and len(sub_intents) > 1:
        intent_lines = ["Decomposed visual intents from your request:"]
        for si in sub_intents:
            intent_lines.append(f"  • [{si['section']}] {si['text']}")
        sections.append("\n".join(intent_lines))

    # Split the retrieval menu into three visually-distinct blocks:
    #   - Canonical resolutions (resolver output — the LLM proposed these
    #     Danbooru tags from the user's described concept and they
    #     validated against the tag table). The user did NOT type these
    #     words verbatim — these are the *canonical form* for what the
    #     user described. Highest priority block, listed first.
    #   - Anchor candidates (literal substring matches — `anchor`/`alias`).
    #     The user actually typed these words. Strong literal evidence.
    #   - Semantic candidates (bge-small wiki retrieval). Possibly
    #     related; only apply if their definition fits user intent.
    # The split (instead of one merged "Direct text matches" block)
    # exists because the merged label LIED about resolver outputs:
    # `focus on feet` resolves to `foot_focus` but the user never wrote
    # `foot_focus` — when the model parsed "user wrote these words" it
    # disqualified the canonical and emitted the user's literal
    # `focus_on_feet` instead, which trace-check then dropped. Splitting
    # gives each channel a label that's actually true.
    # 3000-char total budget across all three blocks.
    if tag_candidates:
        canonical_block: list[str] = []
        direct_block: list[str] = []
        semantic_block: list[str] = []
        budget = 3000
        used = 0
        for c in tag_candidates:
            tag = c.get("tag") or ""
            if not tag:
                continue
            gloss = _neutralize_gloss(c.get("body_summary") or "")
            line = f"  • {tag} — {gloss}" if gloss else f"  • {tag}"
            if used + len(line) > budget:
                break
            used += len(line)
            via = c.get("matched_via")
            if via in ("resolved", "resolved-rerank"):
                canonical_block.append(line)
            elif via in ("anchor", "alias"):
                direct_block.append(line)
            else:
                semantic_block.append(line)
        if canonical_block:
            sections.append(
                "Canonical Danbooru tags for your described concepts — "
                "these ARE the tag form for what the user described. Use "
                "these tags VERBATIM even if the user's exact words don't "
                "appear here (e.g. user wrote 'focus on feet' → canonical "
                "tag is `foot_focus`; user wrote 'sitting cross-legged' → "
                "canonical tag is `indian_style`). PREFER these canonical "
                "tags over emitting the user's literal phrasing as a tag:\n"
                + "\n".join(canonical_block)
            )
        if direct_block:
            sections.append(
                "Direct text matches (the user wrote these words — prefer "
                "these tags when describing the matching concept):\n"
                + "\n".join(direct_block)
            )
        if semantic_block:
            sections.append(
                "Related candidates (use only if their definition fits the "
                "user's intent — do not pick all of them):\n"
                + "\n".join(semantic_block)
            )

    # Strip franchise/series disambiguators from the user_request
    # line before passing to the patch model. The bio resolver
    # already used `from <franchise>` / `(<franchise>)` etc. to
    # match the right canonical character; the patch model only
    # needs to assemble tags. Leaving the franchise reference in
    # primes the model to compound it into synthetic tokens
    # (`<franchise>_battle`, `<franchise>_style`) and emit it as a
    # // Style header. Skipped when the user explicitly asked for
    # franchise visual style (`in the style of X`, `X-style`, etc.)
    # so legitimate franchise-style requests survive.
    cleaned_request = _strip_franchise_for_patch(user_request)

    # OUTFIT MODIFY MODE: when the user is asking to modify (not replace)
    # the existing // Outfit, surface explicit instructions the model
    # can reason against. Without this hint, patch-mode preservation
    # ('preserve sections you aren't asked to change') wins and the
    # outfit silently no-ops. The hint tells the model: yes, this user
    # IS asking to change // Outfit; here's the base; here's the
    # modifier; emit a modified // Outfit body that keeps the base
    # spirit but applies the modifier.
    if modify_outfit_hint:
        sections.append(modify_outfit_hint)

    sections.append(f"User request:\n{cleaned_request}")
    return "\n\n".join(sections)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _strip_json_fences(text: str) -> str:
    """Strip optional leading/trailing markdown code fences."""
    text = text.strip()
    text = _JSON_FENCE_RE.sub("", text).strip()
    # Some models still emit a closing fence after the json object.
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text


# ── Query decomposition ───────────────────────────────────────────
# User requests like "cammy white in an orange leotard sitting with her
# legs up bare foot spreading her toes and pointing her feet at viewer,
# background of a dungeon" pack 8+ visual concepts into one sentence.
# A single bge-small embedding of the whole sentence smears these
# together — the resulting vector is the "average meaning", missing
# the long tail of distinct intents.
#
# Decomposition: ask the model itself to split the request into atomic
# visual concepts before retrieval. Each sub-intent then gets its own
# focused embedding and retrieval pass, results are unioned. Far better
# coverage than a single full-prompt vector, especially for compound
# requests. See _harness/probe_qwen_decompose.py for the validation that
# both 30B and 8B Qwen models produce equivalent decomposition quality.

_DECOMPOSE_SYSTEM_PROMPT = (
    "You decompose Stable Diffusion image-generation requests into "
    "atomic typed action lines. Output ONLY a list with one action per "
    "line in the format `section[-action]: text` — no preamble, no "
    "markdown, no commentary, no JSON, no brackets.\n"
    "\n"
    "section is one of: character, outfit, pose, expression, setting, "
    "style, clear.\n"
    "\n"
    "Optional action suffix commits you to a SPECIFIC type of change. "
    "Use it whenever the action is clear — it removes ambiguity from "
    "downstream processing. Outfit actions in particular:\n"
    "  - `outfit-swap: <named_outfit>` — switch to a complete named "
    "outfit (`outfit-swap: killer bee`, `outfit-swap: delta red`).\n"
    "  - `outfit-fill: <color> <item>` — add a clothing item to its "
    "slot. The user typed an item they want present (`blue socks`, "
    "`red gloves`, `combat boots`). Default for `outfit:` lines when "
    "no action specified.\n"
    "  - `outfit-remove: <item>` — REMOVE a specific clothing item "
    "from its slot. Trigger phrases: `no <item>`, `remove the <item>`, "
    "`without <item>`, `drop the <item>`, `take off the <item>`, "
    "`without her <item>`, `kicks off her <item>`. Examples: "
    "`outfit-remove: socks`, `outfit-remove: boots`, `outfit-remove: "
    "gloves`. ALWAYS prefer outfit-remove for negation-of-clothing "
    "phrasings over generic outfit:.\n"
    "  - `outfit-modifier: <canonical>` — apply a body-state modifier. "
    "Canonical names: barefoot, topless, bottomless, nude, "
    "completely_nude, no_bra, presenting_foot. Trigger phrases: "
    "`barefoot`, `bare feet`, `topless`, `naked`, `nude`. Examples: "
    "`outfit-modifier: barefoot`, `outfit-modifier: topless`.\n"
    "  - `outfit-strip: <kept_item>` — wear ONLY the kept item; "
    "everything else clears. Trigger phrases: `wearing only X`, `just "
    "X`, `nothing but X`, `in a <full_outfit_garment>` (single-piece "
    "garments like nightgown, kimono, bathrobe, wedding dress, towel). "
    "Examples: `outfit-strip: red socks`, `outfit-strip: pink "
    "nightgown`.\n"
    "  - `outfit: <text>` (no action) — fallback when the action "
    "isn't clear; downstream parser figures it out. Avoid when you "
    "can use a specific action.\n"
    "\n"
    "Plain `pose:`, `expression:`, `setting:`, `style:`, `character:` "
    "(no action suffix) are the only forms for those sections — they "
    "have just one action shape per section.\n"
    "\n"
    "Rules:\n"
    "- Each line describes ONE visual element (one pose, one "
    "garment, one body-part action, one setting).\n"
    "- Split lists. When the user joins multiple visual elements with "
    "`and`, `with`, `+`, or commas (`legs up and wide`, `kicking and "
    "punching`, `arms raised, fists clenched`), emit each element as "
    "its own intent line. NEVER output a fused phrase containing `and` "
    "between two distinct tags — those become wrong synthetic tokens "
    "like `legs_up_and_wide` downstream. Exception: keep modifiers "
    "attached to their noun (see next rule).\n"
    "- Keep modifiers attached to their noun (`red dress` stays "
    "together; do not split into `red` and `dress`).\n"
    "- Drop pronouns and articles when they don't add meaning "
    "(`her bare feet` → `bare feet`).\n"
    "- If the request is a single atomic concept, output a single line.\n"
    "- Use `pose` for body position, limb arrangement, gestures, "
    "gaze, body-part actions, AND camera framing/focus phrases like "
    "'focus on feet', 'close-up of hands', 'emphasis on X', 'X to "
    "viewer' (the framing concepts the model treats as composition "
    "rather than facial affect).\n"
    "- Outfit changes use typed actions (see above): `outfit-fill` to "
    "add an item, `outfit-remove` to drop a specific item, "
    "`outfit-modifier` to apply a body-state modifier, `outfit-swap` "
    "to change to a named outfit, `outfit-strip` for "
    "wearing-only-X / single-piece-outfit semantics. Pick the action "
    "that fits the user's intent.\n"
    "- DO NOT use `outfit-strip` for body-state modifier words "
    "(barefoot, topless, bottomless, nude, naked, fully clothed) — "
    "those are modifiers, not strip targets. Use `outfit-modifier: "
    "<canonical>`. The modifier IS the change.\n"
    "- DO NOT use `outfit-fill` for negation phrases like "
    "`no socks`, `without gloves`, `remove the boots`. Those are "
    "removals — use `outfit-remove: <item>`.\n"
    "- Use `character` for subject identity, count (1girl, 1boy), "
    "appearance (hair, eyes, body type), AND anatomy modifications to "
    "specific body parts. Phrases like `bigger feet`, `longer hair`, "
    "`larger eyes`, `broader shoulders`, `narrower waist`, `make her "
    "feet bigger`, `give her longer legs`, `her hair is shorter` "
    "describe the character's PHYSICAL FEATURES and route to "
    "`character`. The body part name alone (e.g. `feet`, `hands`) "
    "WITHOUT a size/shape qualifier and WITHOUT a pose verb is NOT "
    "a character intent -- only emit `character:` when a "
    "modification (bigger/smaller/longer/shorter/etc.) is present.\n"
    "- Use `setting` for location, environment, background. Furniture "
    "the subject interacts with is part of `setting`, but the "
    "interaction verb (`sitting on`, `kneeling on`, `leaning against`, "
    "`standing in front of`) is a separate `pose` intent. Split, "
    "don't fuse: `sitting on a couch` is `pose: sitting` + `setting: "
    "couch`, NOT `setting: sitting on a couch`.\n"
    "- Use `expression` ONLY for facial affect (smiling, frowning, "
    "blushing, glaring). Camera/framing concepts are NOT expression.\n"
    "- Use `style` for art-style or render-look phrases: `change style "
    "to X`, `make it X style`, `in the style of X`, `X anime`, `X look`, "
    "`render it like X`. Output the requested style description as the "
    "text after `style:`.\n"
    "- Series, franchise, IP, game-title, or anime-title names that "
    "appear AS CHARACTER DISAMBIGUATION CONTEXT are NOT settings or "
    "styles. Patterns to recognize: `<character> from <franchise>`, "
    "`<character> of <franchise>`, `<character> in <franchise>`, "
    "`<character> (<franchise>)`. The franchise name is identifying "
    "WHICH character — it is not the scene the character is in nor "
    "the visual style of the render. Skip these mentions entirely: "
    "do NOT emit a `setting:` line and do NOT emit a `style:` line "
    "for the franchise/series/IP/game name itself. Only emit a "
    "setting or style intent for a franchise name when the user is "
    "explicitly asking for that franchise's VISUAL STYLE (`render in "
    "the style of X`, `X-style art`, `the X aesthetic`) or for a "
    "SCENE TYPE drawn from that franchise (`a battle in the X arena`, "
    "`the X hub world`). When the franchise is mentioned only as "
    "character context, drop it.\n"
    "- Use `clear` to REMOVE a section entirely. Phrases like "
    "`remove scene`, `no setting`, `clear the expression`, "
    "`remove pose`, `no style`, `take away the background` map to "
    "`clear: <section_name>`. The section_name is the bare word: "
    "scene / setting / expression / pose / style. Use this for "
    "deletion intent — do NOT classify these as set-the-section-to-"
    "the-string-`remove`. `remove scene` is `clear: scene`, never "
    "`setting: remove scene`.\n"
    "\n"
    "Examples:\n"
    "Input: `cammy white in an orange leotard sitting with her legs "
    "up, bare foot, in a dungeon`\n"
    "Output:\n"
    "character: cammy white\n"
    "outfit-fill: orange leotard\n"
    "pose: sitting\n"
    "pose: legs up\n"
    "outfit-modifier: barefoot\n"
    "setting: dungeon\n"
    "\n"
    "Input: `wearing only red socks`\n"
    "Output:\n"
    "outfit-strip: red socks\n"
    "\n"
    "Input: `cammy white in delta red outfit wearing only red socks`\n"
    "Output:\n"
    "character: cammy white\n"
    "outfit-swap: delta red\n"
    "outfit-strip: red socks\n"
    "\n"
    "Input: `cammy in killer bee outfit with legs up and blue socks at viewer`\n"
    "Output:\n"
    "character: cammy\n"
    "outfit-swap: killer bee\n"
    "pose: legs up\n"
    "outfit-fill: blue socks\n"
    "pose: at viewer\n"
    "\n"
    "Input: `no socks`\n"
    "Output:\n"
    "outfit-remove: socks\n"
    "\n"
    "Input: `remove the boots`\n"
    "Output:\n"
    "outfit-remove: boots\n"
    "\n"
    "Input: `without the gloves`\n"
    "Output:\n"
    "outfit-remove: gloves\n"
    "\n"
    "Input: `drop the dress`\n"
    "Output:\n"
    "outfit-remove: dress\n"
    "\n"
    "Input: `change style to hyper realistic anime`\n"
    "Output:\n"
    "style: hyper realistic anime\n"
    "\n"
    "Input: `change to barefoot`\n"
    "Output:\n"
    "outfit-modifier: barefoot\n"
    "\n"
    "Input: `make her bare foot`\n"
    "Output:\n"
    "outfit-modifier: barefoot\n"
    "\n"
    "Input: `tifa kneeling on a balcony at sunset`\n"
    "Output:\n"
    "character: tifa\n"
    "pose: kneeling\n"
    "setting: balcony at sunset\n"
    "\n"
    "Input: `mythra in a pink nightgown`\n"
    "Output:\n"
    "character: mythra\n"
    "outfit-strip: pink nightgown\n"
    "\n"
    "Input: `cammy in an orange leotard`\n"
    "Output:\n"
    "character: cammy\n"
    "outfit-fill: orange leotard\n"
    "\n"
    "Input: `make her topless`\n"
    "Output:\n"
    "outfit-modifier: topless\n"
    "\n"
    "Input: `remove the scene`\n"
    "Output:\n"
    "clear: scene\n"
    "\n"
    "Input: `no expression`\n"
    "Output:\n"
    "clear: expression\n"
    "\n"
    "Input: `remove pose and clear the setting`\n"
    "Output:\n"
    "clear: pose\n"
    "clear: setting\n"
    "\n"
    "Input: `replace legs up with legs up and wide`\n"
    "Output:\n"
    "pose: legs up\n"
    "pose: legs wide\n"
    "\n"
    "Input: `bigger feet`\n"
    "Output:\n"
    "character: bigger feet\n"
    "\n"
    "Input: `make her feet bigger`\n"
    "Output:\n"
    "character: bigger feet\n"
    "\n"
    "Input: `give her longer hair and broader shoulders`\n"
    "Output:\n"
    "character: longer hair\n"
    "character: broader shoulders\n"
    "\n"
    "Input: `change pose to kicking and punching`\n"
    "Output:\n"
    "pose: kicking\n"
    "pose: punching\n"
    "/no_think"
)


_INGEST_SYSTEM_PROMPT = (
    "You read an existing Stable Diffusion image prompt (already-written "
    "prose) and do TWO jobs:\n"
    "  1. Extract structured facts so they can be edited incrementally.\n"
    "  2. Segment the prose into per-section bodies (verbatim from the "
    "input — no rewording).\n"
    "\n"
    "Output ONLY a list with one entry per line in the format "
    "`field: text` — no preamble, no markdown, no commentary, no JSON, "
    "no brackets.\n"
    "\n"
    "Fact fields (one entry per atomic concept):\n"
    "  character / outfit / modifier / pose / expression / setting / style\n"
    "\n"
    "Section-body fields (one entry, copying the user's prose for that "
    "section verbatim):\n"
    "  character_body / outfit_body / pose_body / expression_body / "
    "setting_body / style_body\n"
    "\n"
    "Rules for fact fields:\n"
    "- `character`: subject identity (one line). Lowercased underscored "
    "name token (e.g. `cammy_white`).\n"
    "- `outfit`: ONE clothing/accessory item per line. Examples: "
    "`blue leotard`, `red gloves`, `garrison cap`. Skip body-state "
    "modifiers like barefoot/topless — those go on `modifier:` lines.\n"
    "- `modifier`: body-state modifiers like barefoot, topless, "
    "bottomless, nude, naked. ONE per line.\n"
    "- `pose`: body position / limb arrangement / gestures / gaze / "
    "presented body parts / camera framing. ONE atomic concept per line.\n"
    "- `expression`: facial affect ONLY. ONE per line.\n"
    "- `setting`: location/environment/background. ONE per line. Only "
    "if the prose explicitly mentions an environment scene — `seated on "
    "the floor` is pose, not setting.\n"
    "- `style`: art-style or render-look phrases. ONE summary line.\n"
    "\n"
    "Rules for body fields:\n"
    "- Copy the user's prose VERBATIM, character-for-character. "
    "PRESERVE typos (`garisson`, `Atheltic`, `bost`, `expresson`, "
    "`torwards`, `feed` for `feet`), idiosyncratic capitalization "
    "(`Highleg`), and odd word choices. Do NOT auto-correct, do NOT "
    "normalize spelling, do NOT swap synonyms, do NOT 'tidy up' the "
    "prose. If the user wrote `Atheltic`, the body field has "
    "`Atheltic` (not `Athletic`). Treating the user's text as "
    "untouchable is the WHOLE point of the body fields.\n"
    "- The body fields are ONE-LINE strings — replace newlines inside "
    "with spaces.\n"
    "- Omit a body field if that section has no prose in the input.\n"
    "- Skip the Negative Prompt block entirely (handled separately).\n"
    "- Each clause goes in EXACTLY ONE body. If a sentence mixes pose "
    "+ expression (e.g. `sitting with a sultry expression`), put the "
    "pose part in pose_body and the FACIAL-AFFECT part only in "
    "expression_body. Don't duplicate.\n"
    "- expression_body is ONLY the facial-affect phrase (smiling, "
    "frowning, sultry expression, blushing). Hand gestures, gaze "
    "direction, and limb positions are POSE — never copy them into "
    "expression_body.\n"
    "\n"
    "Example:\n"
    "Input:\n"
    "  Cammy White from Street Fighter, blonde with twin braids. She's "
    "wearing a green leotard with red gauntlets, brown boots, barefoot. "
    "Sitting with legs up, smiling. In a dungeon. Photorealistic style.\n"
    "Output:\n"
    "character: cammy_white\n"
    "character_body: Cammy White from Street Fighter, blonde with twin braids.\n"
    "outfit: green leotard\n"
    "outfit: red gauntlets\n"
    "outfit: brown boots\n"
    "modifier: barefoot\n"
    "outfit_body: She's wearing a green leotard with red gauntlets, brown boots, barefoot.\n"
    "pose: sitting\n"
    "pose: legs up\n"
    "pose_body: Sitting with legs up.\n"
    "expression: smiling\n"
    "expression_body: smiling.\n"
    "setting: dungeon\n"
    "setting_body: In a dungeon.\n"
    "style: photorealistic\n"
    "style_body: Photorealistic style.\n"
    "\n"
    "Example with mixed pose + expression in one paragraph:\n"
    "Input:\n"
    "  She's seated on the floor with legs up, feet pointed at the "
    "viewer, with a sultry expression on her face. One hand is tugging "
    "at her lips.\n"
    "Output:\n"
    "pose: seated on the floor\n"
    "pose: legs up\n"
    "pose: feet pointed at viewer\n"
    "pose: one hand tugging at lips\n"
    "pose_body: She's seated on the floor with legs up, feet pointed "
    "at the viewer, with a sultry expression on her face. One hand is "
    "tugging at her lips.\n"
    "expression: sultry\n"
    "expression_body: sultry expression\n"
    "(Note: pose_body keeps the user's exact paragraph verbatim. "
    "expression_body extracts ONLY the facial-affect phrase, NOT the "
    "hand-tugging gesture or the rest of the pose paragraph.)\n"
    "/no_think"
)


_INGEST_LINE_RE = re.compile(
    r"^\s*(?:[-*•·]\s*|\d+[.)]\s*)?"
    r"(character|outfit|modifier|pose|expression|setting|style|"
    r"character_body|outfit_body|pose_body|expression_body|"
    r"setting_body|style_body)"
    r"\s*[:|\-\t]\s*"
    r"(.+?)\s*$",
    re.IGNORECASE,
)


async def _ingest_node_prompt_to_facts(request_id: str, provider: str,
                                        config: dict,
                                        node_prompt: str) -> list[dict] | None:
    """Call the LLM to extract structured facts from existing prose.
    Returns a list of {field, text} dicts or None on failure.

    Used when a user has hand-written or pasted prose into the editor and
    pcrPromptState is empty — without this, the next /ai/patch call would
    rebuild state from bios + user_request, losing the user's custom facts."""
    if not node_prompt or not node_prompt.strip():
        return []
    # Strip the negative prompt block — those are tag-shaped, not prose facts.
    body = re.split(r"^\s*Negative\s+Prompt\s*:", node_prompt,
                    flags=re.IGNORECASE | re.MULTILINE)[0].strip()
    if not body:
        return []
    try:
        raw = await _run_generation(
            f"{request_id}-ingest", provider, config,
            _INGEST_SYSTEM_PROMPT, body, [],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ai-patch[%s] ingest call failed", request_id)
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = _strip_json_fences(raw)
    out: list[dict] = []
    for line in cleaned.splitlines():
        m = _INGEST_LINE_RE.match(line)
        if not m:
            continue
        field = m.group(1).lower()
        text = m.group(2).strip().strip("`'\"").strip()
        if not text:
            continue
        text = text.rstrip(",;.")
        if not text:
            continue
        out.append({"field": field, "text": text})
    if not out:
        dbg.info(
            "ai-patch[%s] ingest: no parseable lines, raw=%s",
            request_id, _trunc(cleaned, 800),
        )
        return None
    return out


_DECOMPOSE_LINE_RE = re.compile(
    # Accepts `section: text`, `section-action: text`, `section | text`,
    # or `section\ttext`. Tolerates leading bullets/numbers like `- `
    # or `1. `. Section must be one of the canonical names; optional
    # action suffix lets decompose commit to a specific typed action
    # (`outfit-remove: socks` for ClearSlotDelta, `outfit-modifier:
    # barefoot` for ApplyModifierDelta) instead of the loose
    # `outfit: <text>` that downstream code has to re-parse.
    #
    # Separator narrowed to `:` and `|` and tab — the hyphen is no
    # longer a valid intent separator since it's now used for the
    # action suffix.
    r"^\s*(?:[-*•·]\s*|\d+[.)]\s*)?"
    r"(character|outfit|strip|pose|expression|setting|style|clear)"
    r"(?:-(swap|fill|remove|modifier|strip|clear|chip|bio|set|add))?"
    r"\s*[:|\t]\s*"
    r"(.+?)\s*$",
    re.IGNORECASE,
)


_franchise_cache: frozenset[str] | None = None


def _load_known_franchise_names() -> frozenset[str]:
    """Distinct lowercase franchise/series names from the characters
    table. Used to filter decompose sub-intents whose text matches a
    franchise mention (the user said `X from <franchise>` and decompose
    routed `<franchise>` into setting:/style:). Module-level cache —
    the DB is a static asset."""
    global _franchise_cache
    if _franchise_cache is not None:
        return _franchise_cache
    try:
        from .tag_builder import get_db
        db = get_db()
        rows = db.execute(
            "SELECT DISTINCT LOWER(series) FROM characters "
            "WHERE series IS NOT NULL AND series != ''"
        ).fetchall()
        _franchise_cache = frozenset(
            (r[0] or "").strip() for r in rows if r and r[0]
        )
    except Exception:
        logger.warning("franchise-filter: series load failed",
                       exc_info=True)
        _franchise_cache = frozenset()
    return _franchise_cache


_EXPLICIT_STYLE_RE = re.compile(
    r"\b(in the style of|style of|art style|aesthetic|"
    r"render (it |this |)(in |as |like )|make it look|"
    r"in the look of|-style\b)",
    re.IGNORECASE,
)


def _strip_franchise_for_patch(user_request: str) -> str:
    """Remove franchise-context phrases (`from <franchise>`, `of
    <franchise>`, `in <franchise>`, `(<franchise>)`) from the user's
    request text before feeding it to the patch model. Only strips
    when the franchise is from the known characters.series set —
    so generic phrases like 'in a forest' aren't affected. Skipped
    entirely when the user has an explicit style cue, preserving
    legitimate franchise-style requests."""
    if not user_request:
        return user_request
    if _EXPLICIT_STYLE_RE.search(user_request):
        return user_request
    franchises = _load_known_franchise_names()
    if not franchises:
        return user_request
    out = user_request
    # Sort longest-first so multi-word franchise names match before
    # any embedded shorter ones.
    for f in sorted((x for x in franchises if x), key=len, reverse=True):
        f_re = re.escape(f)
        patterns = [
            rf"\s*\(\s*{f_re}\s*\)",
            rf"\s+from\s+{f_re}\b",
            rf"\s+of\s+{f_re}\b",
            rf"\s+in\s+{f_re}\b",
        ]
        for p in patterns:
            new = re.sub(p, "", out, flags=re.IGNORECASE)
            if new != out:
                out = new
    # Collapse double spaces that may have been left behind.
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out or user_request


def _filter_franchise_setting_style(
    sub_intents: list[dict] | None, user_request: str, request_id: str,
) -> list[dict] | None:
    """Drop setting:/style: sub-intents whose text matches a known
    franchise name. Preserves legitimate franchise-style requests
    (`make it street fighter style`) by keeping a `style:` franchise
    intent when the user's original text has an explicit style cue.
    setting: franchise intents are always dropped — users don't
    describe scenes by naming a franchise alone (they'd say `the X
    arena` or `X-themed environment`, which decomposes differently)."""
    if not sub_intents:
        return sub_intents
    franchises = _load_known_franchise_names()
    if not franchises:
        return sub_intents
    has_explicit_style = bool(
        _EXPLICIT_STYLE_RE.search(user_request or "")
    )
    out: list[dict] = []
    dropped: list[str] = []
    for si in sub_intents:
        section = (si.get("section") or "").lower()
        text = (si.get("text") or "").strip().lower()
        if section not in ("setting", "style") or text not in franchises:
            out.append(si)
            continue
        if section == "style" and has_explicit_style:
            out.append(si)
            continue
        dropped.append(f"[{section}] {text}")
    if dropped:
        dbg.info(
            "ai-patch[%s] franchise-filter dropped: %s",
            request_id, ", ".join(dropped),
        )
    return out


_POSE_CHIP_PICKER_SYSTEM = (
    "You pick which curated pose chip best captures the user's pose "
    "intent. Read the user's full request and the numbered candidate "
    "chips — each candidate has its display name, its taxonomic group, "
    "its canonical tags, and its authored description. The chip whose "
    "tags + description most fully cover what the user is describing "
    "wins; partial-coverage chips lose to fuller-coverage chips. "
    "Reply with ONLY the number (`1`, `2`, `3`, ...), or `none` if no "
    "candidate plausibly captures the user's pose. No explanation.\n\n"
    "/no_think"
)


async def _llm_pick_pose_chip(request_id: str, provider: str,
                              config: dict,
                              user_request: str,
                              candidates: list[dict]) -> dict | None:
    """Ask the LLM which of the bge-retrieved chip candidates best
    matches the user's pose intent. The chip's authored natlang then
    renders verbatim — the LLM only chooses, it does not author prose.

    Mirrors tag mode's "retrieve candidates, LLM picks" pattern: bge is
    a sieve, the LLM is the decider. Returns the chosen candidate dict
    or None if the model declined / output unparseable / call failed.

    The candidate list is presented numbered with each chip's display
    name and authored natlang body — the LLM sees exactly the prose
    that will render if it picks that chip. Choices stay grounded in
    the curated chip table; no chip names or fixture phrases appear
    in the system prompt."""
    if not candidates:
        return None
    # Reorder so signature-group chips (curator-marked as covering a
    # specific named intent — `presenting_feet`, `top-down_bottom-up`,
    # etc.) appear first within the candidate list. This honors the
    # user's taxonomy: `item_group='signature'` means "this chip captures
    # a specific named pose intent, prefer it for nuanced matches".
    # Generic body-position chips (item_group='legs' / 'hands' / 'feet')
    # still appear, just after the signature picks. Within each group
    # cosine order is preserved.
    ordered = sorted(
        candidates,
        key=lambda c: (
            0 if (c.get("item_group") or "").strip().lower() == "signature" else 1,
            -(c.get("adjusted_score") or 0.0),
        ),
    )
    options_lines = []
    for i, c in enumerate(ordered, start=1):
        name = (c.get("display_name") or c.get("item_tag") or "").strip()
        natlang = (c.get("base_natlang") or "").strip()
        tags = (c.get("base_tags") or "").strip()
        group = (c.get("item_group") or "").strip()
        group_line = f"\n     group: {group}" if group else ""
        tag_line = f"\n     tags: {tags}" if tags else ""
        nat_line = f"\n     description: {natlang}" if natlang else ""
        options_lines.append(f"{i}. {name}{group_line}{tag_line}{nat_line}")
    user_prompt = (
        f"User request: {user_request}\n\n"
        f"Candidate chips:\n"
        + "\n".join(options_lines)
        + "\n\nChoice (number only, or `none`):"
    )
    try:
        raw = await _run_generation(
            f"{request_id}-chip-pick", provider, config,
            _POSE_CHIP_PICKER_SYSTEM, user_prompt, [],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ai-patch[%s] chip pick call failed", request_id)
        return None
    raw = (raw or "").strip().lower()
    if not raw:
        dbg.info("ai-patch[%s] chip pick: empty response", request_id)
        return None
    # Find the LAST numeric token / `none` on its own line — the model
    # may include reasoning before the final answer. Search bottom-up.
    final_answer = None
    for line in reversed(raw.splitlines()):
        line = line.strip().strip("`'\".,;:")
        if not line:
            continue
        if line == "none":
            final_answer = "none"
            break
        m = re.fullmatch(r"([0-9]+)", line)
        if m:
            final_answer = m.group(1)
            break
    # Fallback: any number in the raw text (last occurrence) if no
    # bare-answer line was found.
    if final_answer is None:
        nums = re.findall(r"\b([0-9]+)\b", raw)
        if "none" in raw and not nums:
            final_answer = "none"
        elif nums:
            final_answer = nums[-1]
    if final_answer is None or final_answer == "none":
        dbg.info(
            "ai-patch[%s] chip pick: none (final=%r raw[:80]=%r)",
            request_id, final_answer, raw[:80],
        )
        return None
    idx = int(final_answer) - 1
    if 0 <= idx < len(ordered):
        chosen = ordered[idx]
        dbg.info(
            "ai-patch[%s] chip pick: %d -> %s (cosine=%.3f)",
            request_id, idx + 1,
            chosen.get("item_tag") or chosen.get("display_name"),
            chosen.get("adjusted_score", 0.0),
        )
        return chosen
    dbg.info(
        "ai-patch[%s] chip pick: out-of-range idx=%d (have %d)",
        request_id, idx, len(ordered),
    )
    return None


async def _decompose_user_request(request_id: str, provider: str,
                                  config: dict,
                                  user_request: str) -> list[dict] | None:
    """Call the configured AI provider to split the user request into
    atomic visual sub-intents. Returns list of {text, section} dicts on
    success, None on any failure mode (provider down, empty response,
    no parseable lines).

    Caller is expected to surface None as a hard failure to the user —
    the v2 architecture requires the LLM to function. We do NOT return
    a synthesized fallback because that would silently produce wrong
    output."""
    if not user_request:
        return []
    try:
        raw = await _run_generation(
            f"{request_id}-decompose", provider, config,
            _DECOMPOSE_SYSTEM_PROMPT, user_request, [],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ai-patch[%s] decomposition call failed", request_id)
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = _strip_json_fences(raw)

    # Line-based parser (replaces the JSON parser we previously used).
    # The model kept emitting the JSON in subtly-different malformed
    # ways every run — `{ {obj}, {obj} }` one time, `[text, section]`
    # array pairs the next. Each parser strategy chased the previous
    # bug, never the next one. Pivot: ask the model for a format that's
    # genuinely hard to mangle — `section: text` one-per-line, no
    # brackets, no quotes, no schema to mess up. Then accept any line
    # matching `<section> <delimiter> <text>` where section is one of
    # the canonical names.
    out: list[dict] = []
    for line in cleaned.splitlines():
        m = _DECOMPOSE_LINE_RE.match(line)
        if not m:
            continue
        section = m.group(1).lower()
        action = (m.group(2) or "").lower() or None
        text = m.group(3).strip().strip("`'\"").strip()
        if not text:
            continue
        # Strip trailing punctuation the model sometimes adds.
        text = text.rstrip(",;.")
        if not text:
            continue
        sub = {"text": text, "section": section}
        if action:
            sub["action"] = action
        out.append(sub)

    if not out:
        dbg.info(
            "ai-patch[%s] decompose: no parseable lines, falling back. Raw=%s",
            request_id, _trunc(cleaned, 800),
        )
        return None
    out = _split_conjoined_intents(out, request_id)
    return out


# Split " and "/" + "/", " patterns inside a single decomposed intent so
# multi-tag phrases like `legs up and wide` become two intents
# (`legs up`, `legs wide`) rather than one fused phrase that the patch
# model later turns into a synthetic `legs_up_and_wide` token.
#
# Only applies to sections whose tags are atomic Danbooru forms (pose,
# expression, setting). Character / outfit / strip intents stay intact —
# multi-character names ('cammy and chun-li') are handled by
# match-characters preflight, and outfit phrases like 'red dress and
# black socks' are already split by commas at decompose time.

_AND_SPLIT_RE = re.compile(r"\s+(?:and|,|\+|;)\s+", re.IGNORECASE)
_AND_SPLITTABLE_SECTIONS = {"pose", "expression", "setting"}


def _distribute_implicit_noun(parts: list[str]) -> list[str]:
    """When the user writes `legs up and wide`, the simple `and` splitter
    yields `['legs up', 'wide']`. The right side `wide` is a bare
    modifier — it lost the implicit subject `legs`. We re-attach the
    leading noun from the previous part so the result is
    `['legs up', 'legs wide']`.

    Heuristic: a part is bare if it's a single word AND the previous
    part has 2+ words AND the previous part's first word is not already
    present in this part. Conservative — won't fire on `kicking and
    punching` (both single-word), `arms up and legs apart` (both
    multi-word), or `red dress and black socks` (multi/multi)."""
    if len(parts) <= 1:
        return parts
    out = list(parts)
    for i in range(1, len(out)):
        cur_words = out[i].split()
        prev_words = out[i - 1].split()
        if len(cur_words) == 1 and len(prev_words) >= 2:
            leading_noun = prev_words[0]
            if leading_noun.lower() not in (w.lower() for w in cur_words):
                out[i] = f"{leading_noun} {out[i]}"
    return out


# Pattern signaling the user is naming an outfit AESTHETIC ('in a
# steampunk outfit' / 'wearing a goth outfit' / 'change to a victorian
# outfit'). When the generic-outfit scan misses (novel aesthetic, no DB
# entry), we still need to set the strip flag so bio.default_outfit
# stops dominating — the model vibes the named aesthetic from world
# knowledge instead of stacking it on top of the bio's stock outfit.
# The actual outfit-name extraction is the scan's job; this regex only
# answers "did the user name SOMETHING outfit-shaped".
_NAMED_OUTFIT_STRIP_FLAG_RE = re.compile(
    r"\b(?:in|wearing|dressed\s+in|change(?:d)?\s+to|switch(?:ed)?\s+to|put\s+on)"
    r"\s+(?:a|an|the)\s+[\w\s\-]+?\s+outfit\b",
    re.IGNORECASE,
)

# Adjective set that signals an outfit MODIFICATION rather than a stock
# replacement. 'bikini version of the maid outfit' / 'make it skimpier'
# / 'gothic variant' — these describe a delta to apply to the existing
# outfit, not a fresh aesthetic to vibe from scratch. Detection routes
# to MODIFY mode: skip tier-3 DB lookup, skip rewrite, hand the patch
# model the base outfit + user's modifier and let it compute the delta.
_MODIFY_OUTFIT_ADJECTIVES = (
    r"bikini|skimpy|skimpier|topless|nude|naked|casual|formal|fancy|"
    r"fancier|simpler|sexy|kinky|slutty|cute|edgy|gothic|punk|tomboy|"
    r"girly|lewd|lewder|alternate|alt|alternative|wet|sheer|see-through|"
    r"transparent|leather|latex|short|shorter|longer|tighter|looser"
)
_MODIFY_OUTFIT_PHRASE_RE = re.compile(
    rf"\b(?:"
    rf"(?:{_MODIFY_OUTFIT_ADJECTIVES})\s+(?:version|variant|outfit)\b"
    rf"|(?:version|variant)\s+of\s+(?:the|a|an|my|her|his|their)\b"
    rf"|make\s+it\s+(?:a\s+|an\s+|more\s+)?(?:{_MODIFY_OUTFIT_ADJECTIVES})\b"
    # 'make it [adjective]er' — comparative form catches sexier /
    # tighter / longer / shorter / cuter / edgier without enumerating.
    rf"|make\s+it\s+(?:a\s+|an\s+|more\s+)?\w{{3,}}er\b"
    rf"|(?:variant|alt|alternative)\s+of\b"
    rf")",
    re.IGNORECASE,
)


# Suffixes / words that identify a token as a BODY GARMENT — the main
# coverage piece(s) of an outfit. Body garments are the slots a MODIFY
# operation replaces ('bikini version' swaps the dress/shirt/pants for
# a bikini). Everything NOT matching this list is treated as
# preservable accessory/headwear/footwear/ornament.
_BODY_GARMENT_SUFFIXES = (
    "dress", "gown", "robe", "kimono", "yukata", "qipao", "uniform",
    "shirt", "blouse", "tank_top", "tee", "tshirt", "vest", "tunic",
    "sweater", "hoodie", "cardigan",
    "leotard", "bodysuit", "swimsuit", "swimwear", "onepiece",
    "pants", "jeans", "trousers", "skirt", "miniskirt", "shorts",
    "minishorts", "leggings", "joggers",
    "coat", "jacket", "blazer",
)

_BODY_GARMENT_TOKEN_RE = re.compile(
    r"(?:^|_)(?:"
    + "|".join(_BODY_GARMENT_SUFFIXES)
    + r")$",
    re.IGNORECASE,
)


def _is_body_garment_token(token: str) -> bool:
    """True if the token is a body garment (main coverage piece) rather
    than an accessory/headwear/footwear/ornament. Used by MODIFY mode
    to decide which slot(s) the modifier replaces vs which to preserve."""
    if not token:
        return False
    t = token.strip().strip("(").strip(")").strip()
    # Strip weight suffix '(token:1.1)' or trailing ':1.1'
    t = re.sub(r"^\\?\(\s*", "", t)
    t = re.sub(r"\s*:\s*[\d.]+\s*\\?\)?\s*$", "", t).strip()
    return bool(_BODY_GARMENT_TOKEN_RE.search(t))


def _classify_outfit_tokens_for_modify(
    outfit_body_tokens: list[str],
) -> tuple[list[str], list[str]]:
    """Split an outfit body's tokens into (body_garments, preserved).
    Body garments are MODIFY-replaceable; preserved are the accessories,
    headwear, footwear, and ornaments that survive a modifier swap."""
    body: list[str] = []
    preserve: list[str] = []
    for t in outfit_body_tokens:
        if _is_body_garment_token(t):
            body.append(t)
        else:
            preserve.append(t)
    return body, preserve


def _detect_modify_outfit_intent(current_user_text: str) -> dict | None:
    """Return {'phrase': matched_substring} when the user's current
    message names an outfit MODIFICATION rather than a stock replace.
    Triggers MODIFY mode in the patch flow: skip tier-3 DB lookup,
    skip node_prompt outfit rewrite, surface base + modifier to the
    patch model, and let it reason about the delta.

    Examples that match:
      - 'make it a bikini version of the french maid outfit'
      - 'goth variant of her current dress'
      - 'skimpier version of this'
      - 'make it sexier'

    Examples that do NOT match (these are stock replaces):
      - 'in a cowboy outfit'
      - 'change outfit to french maid'
      - 'wearing a pirate outfit'"""
    if not current_user_text:
        return None
    m = _MODIFY_OUTFIT_PHRASE_RE.search(current_user_text)
    if not m:
        return None
    return {"phrase": m.group(0)}


def _ensure_strip_for_named_outfit_phrase(
    sub_intents: list[dict] | None, current_user_text: str, request_id: str,
) -> list[dict] | None:
    """Inject a `[strip]` sub_intent when the user's current message
    matches the named-outfit phrase pattern but no [strip] intent
    already exists. Lets `user_strips_outfit` fire downstream so
    bio.default_outfit slots get suppressed for novel aesthetics that
    the generic-outfits DB doesn't have an entry for."""
    if not current_user_text:
        return sub_intents
    if not _NAMED_OUTFIT_STRIP_FLAG_RE.search(current_user_text):
        return sub_intents
    sub_intents = list(sub_intents or [])
    if any((si.get("section") or "").lower() == "strip" for si in sub_intents):
        return sub_intents
    sub_intents.append({"section": "strip", "text": "novel outfit (named via phrase)"})
    dbg.info(
        "ai-patch[%s] named-outfit phrase detected -> strip flag (for default-outfit suppression)",
        request_id,
    )
    return sub_intents


_NODE_OUTFIT_HEADER_RE = re.compile(r"^\s*//\s*Outfit\s*:\s*([^\n]*)$", re.IGNORECASE | re.MULTILINE)


# Map decompose's `[clear] X` text values to the section header prefix
# the server should remove from node_prompt. Without this map, [clear]
# intents emitted by decompose are silently dropped — the patch model
# preserves the section per patch-mode rules and the user's 'reset
# scene' has no effect. Character clears are handled separately by the
# character-swap enforcer; outfit clears handled by the outfit rewrite
# / strip flag flow.
_CLEAR_TARGET_TO_HEADER_PREFIX = {
    "scene": "// setting",
    "setting": "// setting",
    "background": "// setting",
    "pose": "// pose",
    "action": "// pose",
    "stance": "// pose",
    "expression": "// expression",
    "face": "// expression",
    "style": "// style",
    "quality": "// quality",
}


def _apply_clear_intents_to_node_prompt(
    node_prompt: str, sub_intents: list[dict] | None, request_id: str,
) -> str:
    """Remove `// Section` blocks from node_prompt for each `[clear] X`
    sub_intent. The patch system prompt preserves existing sections
    verbatim — server-side removal is the only way to honor the user's
    explicit 'reset scene' / 'clear pose' / etc. intent. New same-section
    intents on the same turn (if any) drive the model to emit a fresh
    section in place of the cleared one."""
    if not node_prompt or not sub_intents:
        return node_prompt
    clear_prefixes: list[str] = []
    cleared_targets: list[str] = []
    for si in sub_intents:
        if (si.get("section") or "").lower() != "clear":
            continue
        target = (si.get("text") or "").strip().lower()
        prefix = _CLEAR_TARGET_TO_HEADER_PREFIX.get(target)
        if prefix and prefix not in clear_prefixes:
            clear_prefixes.append(prefix)
            cleared_targets.append(target)
    if not clear_prefixes:
        return node_prompt
    lines = node_prompt.splitlines(keepends=False)
    out_lines: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("//"):
            skipping = any(stripped.startswith(p) for p in clear_prefixes)
            if skipping:
                continue
        if re.match(r"^\s*Negative\s+Prompt\s*:", line, re.IGNORECASE):
            # Negative Prompt is its own section — never skip it.
            skipping = False
        if skipping:
            continue
        out_lines.append(line)
    dbg.info(
        "ai-patch[%s] clear-intent removed sections: %s",
        request_id, ", ".join(cleared_targets),
    )
    return "\n".join(out_lines)


def _rewrite_node_prompt_outfit_for_user_requested(
    node_prompt: str, bios: list[dict], request_id: str,
) -> str:
    """When a bio has `user_requested_outfit` set (matcher attached a
    character outfit, or tier-3 generic-outfit scan attached an
    archetype) AND node_prompt has an existing `// Outfit: <X>` section
    with a DIFFERENT name, rewrite node_prompt's // Outfit body to use
    the requested outfit's slot phrases.

    Why: the patch system prompt tells the model to preserve existing
    sections verbatim. Without this rewrite, the model sees the
    user_requested_outfit as a bio hint AND the prior outfit as a
    'preserved' section — and chooses to keep the preserved one, so
    the swap silently no-ops. By rewriting node_prompt server-side,
    the 'preserved' content already matches the user's request and
    patch-mode preservation works in our favor."""
    if not node_prompt or not bios:
        return node_prompt
    target = None
    for b in bios:
        uro = b.get("user_requested_outfit") or {}
        if uro and uro.get("name"):
            target = uro
            break
    if not target:
        return node_prompt
    new_name = (target.get("name") or "").strip()
    slots = target.get("slots") or []
    new_body = ", ".join(
        (s.get("source_phrase") or "").strip()
        for s in slots if s.get("source_phrase")
    )
    if not new_name or not new_body:
        return node_prompt
    m = _NODE_OUTFIT_HEADER_RE.search(node_prompt)
    if not m:
        return node_prompt
    existing_name = (m.group(1) or "").strip()
    if existing_name.lower() == new_name.lower():
        return node_prompt
    lines = node_prompt.splitlines(keepends=False)
    out_lines: list[str] = []
    i = 0
    rewrote = False
    while i < len(lines):
        line = lines[i]
        if not rewrote and re.match(r"^\s*//\s*Outfit\s*:", line, re.IGNORECASE):
            out_lines.append(f"// Outfit: {new_name}")
            out_lines.append(new_body)
            i += 1
            # Skip the prior body lines until the next `//` header or
            # blank-then-EOF or the Negative Prompt marker.
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped.startswith("//"):
                    break
                if re.match(r"^\s*Negative\s+Prompt\s*:", lines[i], re.IGNORECASE):
                    break
                i += 1
            rewrote = True
            continue
        out_lines.append(line)
        i += 1
    if not rewrote:
        return node_prompt
    dbg.info(
        "ai-patch[%s] node_prompt outfit rewrite: %r -> %r (per "
        "user_requested_outfit)", request_id, existing_name, new_name,
    )
    return "\n".join(out_lines)


def _apply_generic_outfit_to_bios(
    sub_intents: list[dict] | None, bios: list[dict],
    current_user_text: str, request_id: str,
) -> tuple[list[dict] | None, list[dict]]:
    """Tier 3 of outfit resolution: scan the user's CURRENT message for
    any generic-outfit name/alias from the tag_builder.generic_outfits
    KB. On a hit, attach the curated slot decomposition as
    user_requested_outfit on the (sole) bio. The patch flow then renders
    those slots verbatim instead of expanding the user's outfit phrase
    via bge retrieval (which leaks scene props like horse/gun).

    Scanning the CURRENT message (not multi-turn extract) avoids a
    common failure: previous turn 'in a cowboy outfit' phrase polluting
    this turn's 'change outfit to french maid' classification.

    Only fires when:
      - exactly one bio (multi-char generic-outfit attribution is
        ambiguous; skip and let canonical_resolver handle it)
      - that bio has no user_requested_outfit yet (tier 1/2 already
        resolved a more specific match — don't override)

    Marks any [strip]/[outfit] sub-intents with `pre_resolved=True` so
    canonical_resolver skips them (otherwise the bge expansion of
    'cowboy outfit' still pulls horse/gun candidates into patch_user)."""
    if not bios or not current_user_text:
        return sub_intents, bios
    real_bios = [b for b in bios if b and b.get("tag")]
    if len(real_bios) != 1:
        return sub_intents, bios
    target = real_bios[0]
    if target.get("user_requested_outfit"):
        return sub_intents, bios
    from . import tag_builder as _tb
    matched = _tb.scan_for_generic_outfit(current_user_text)
    if not matched:
        return sub_intents, bios
    target["user_requested_outfit"] = {
        "name": matched["name"],
        "tags": matched["tags"],
        "natlang": matched["natlang"],
        "slots": matched["slots"],
    }
    # Suppress canonical_resolver / bge expansion of any [strip]/[outfit]
    # intent — we've already attached the authoritative outfit data.
    if sub_intents:
        for si in sub_intents:
            section = (si.get("section") or "").lower()
            if section in ("strip", "outfit"):
                si["pre_resolved"] = True
    dbg.info(
        "ai-patch[%s] generic-outfit: scanned %r -> %s (%d slots) on bio=%s",
        request_id, current_user_text[:80], matched["name"],
        len(matched["slots"]), target.get("tag"),
    )
    return sub_intents, bios


def _split_conjoined_intents(intents: list[dict], request_id: str) -> list[dict]:
    out: list[dict] = []
    split_log: list[tuple[str, list[str]]] = []
    for intent in intents:
        section = (intent.get("section") or "").strip().lower()
        text = (intent.get("text") or "").strip()
        if section not in _AND_SPLITTABLE_SECTIONS or not text:
            out.append(intent)
            continue
        raw_parts = [p.strip() for p in _AND_SPLIT_RE.split(text) if p.strip()]
        # Single part => no split happened; keep original.
        if len(raw_parts) <= 1:
            out.append(intent)
            continue
        # Reject splits where any part is too short (likely a modifier
        # like `bare and dirty foot` shouldn't split into `bare`/`dirty
        # foot`). 3+ chars per part is a safe floor — Danbooru tags rarely
        # have 1-2 char canonical forms.
        if any(len(p) < 3 for p in raw_parts):
            out.append(intent)
            continue
        parts = _distribute_implicit_noun(raw_parts)
        for p in parts:
            out.append({"text": p, "section": section})
        split_log.append((text, parts))
    if split_log:
        for original, parts in split_log:
            dbg.info(
                "ai-patch[%s] decompose: split conjoined intent %r -> %s",
                request_id, original, parts,
            )
    return out


# When a modifier with this canonical_tag is in [APPLIES], retrieval
# candidates whose tag-group is in the listed groups get filtered out.
# Stops the bge-small noise where a foot-presenting modifier is firing
# but the menu also surfaces gesture-domain tags like `pointing_down`.
# Uses tag-group data we already have indexed.
_MODIFIER_CONFLICT_GROUPS: dict[str, list[str]] = {
    "presenting_foot": ["gestures"],
}

# Sections that match-characters preflight already handles. No need to
# run retrieval for character-domain sub-intents — bio is authoritative.
_RETRIEVAL_SKIP_SECTIONS = {"character"}


_LITERAL_NGRAM_RE = re.compile(r"[a-z0-9']+")

# Pronouns / articles / aux verbs that interrupt n-gram compounds without
# adding meaning. "spreading her toes" -> drop "her" -> we can build
# `spreading_toes` (then -ing-strip to `spread_toes`).
_LITERAL_STOP_WORDS = frozenset({
    "a", "an", "the", "her", "his", "their", "its", "is", "are",
    "was", "were", "be", "to", "of", "and", "or",
})


def _literal_anchor_candidates(text: str, applies_by_tag: dict,
                               modifier_canon: set[str],
                               bio_known: set[str],
                               conflict_tags: set[str] | None = None
                               ) -> list[dict]:
    """Substring lookup: for any 1-4-word substring of `text` that
    matches a real Danbooru general tag (ranking >= 200), return it as
    a candidate. Used to anchor literal user words like `sitting` or
    `dungeon` that bge-small ranks below specific variants. Filters out
    bio-known and modifier-fired tags so we don't double-surface them.

    Two enrichments to handle gerund / pronoun phrasings the user types
    naturally:
      - Drop _LITERAL_STOP_WORDS from the word list before n-gramming.
        "spreading her toes" -> ["spreading", "toes"] -> can build
        `spreading_toes` (then strip -ing -> `spread_toes`).
      - For each n-gram, also try a variant with -ing stripped from
        the first word (gerund -> bare verb). Catches `spread_toes`
        from user's "spreading toes" phrasing.

    Conflict_tags is a set of tag names blocked by a fired modifier's
    declared conflict groups (e.g. gestures-group when presenting_foot
    fires). Anchor honors this so `pointing` isn't anchored alongside
    [APPLIES] presenting_foot.

    Score is set to a sentinel high value so anchored candidates always
    survive the global cap downstream — the user explicitly typed these
    words, they should not be evicted by random-cosine competition."""
    if not text:
        return []
    raw_words = _LITERAL_NGRAM_RE.findall(text.lower())
    words = [w for w in raw_words if w not in _LITERAL_STOP_WORDS]
    if not words:
        return []
    candidates: set[str] = set()
    for n in range(1, 5):
        for i in range(len(words) - n + 1):
            slice_ = words[i:i + n]
            candidates.add("_".join(slice_))
            # Gerund -> bare-verb variant: `spreading_toes` -> `spread_toes`,
            # `pointing_feet` -> `point_feet` (latter isn't a tag, harmless).
            first = slice_[0]
            if len(first) > 4 and first.endswith("ing"):
                candidates.add("_".join([first[:-3]] + slice_[1:]))
    if not candidates:
        return []
    try:
        from .tag_builder import get_db
        db = get_db()
        placeholders = ",".join("?" for _ in candidates)
        rows = db.execute(
            f"SELECT t.tag, t.ranking, w.body_summary, w.body_full "
            f"FROM danbooru_tags t "
            f"LEFT JOIN danbooru_tag_wikis w ON w.tag = t.tag "
            f"WHERE t.tag IN ({placeholders}) "
            f"AND t.category = 'general' AND t.ranking >= 200",
            list(candidates),
        ).fetchall()
    except Exception:
        logger.exception("literal-anchor lookup failed for %r", text)
        return []
    out: list[dict] = []
    conflict_tags = conflict_tags or set()
    for r in rows:
        tag = (r["tag"] or "").lower()
        if not tag or tag in bio_known or tag in modifier_canon:
            continue
        if tag in conflict_tags:
            continue  # blocked by fired modifier's conflict-group rule
        out.append({
            "tag": r["tag"],
            "ranking": int(r["ranking"] or 0),
            "body_summary": r["body_summary"] or "",
            "body_full": r["body_full"] or "",
            "score": 0.99,  # sentinel — anchored tags always survive cap
        })
    return out


def _retrieve_tag_candidates(sub_intents: list[dict],
                             bio_known: set[str],
                             modifier_canon: set[str],
                             applies_by_tag: dict | None = None,
                             top_per: int = 8,
                             threshold: float = 0.55,
                             total_cap: int = 24,
                             on_status: Callable[[str], None] | None = None) -> list[dict]:
    """For each sub-intent, retrieve candidate tags via two paths:

      1. Literal-word anchor: any 1-4-word substring of the sub-intent
         that's a real Danbooru tag is included with sentinel score.
         Catches direct hits like `sitting`, `dungeon` that semantic
         retrieval ranks below specific variants.
      2. Bi-encoder semantic search: top-K tag wikis above threshold.

    Filters applied per sub-intent:
      - Skip character-section sub-intents entirely (match-characters
        preflight covers character identity).
      - Section-aware: when sub-intent has section X, drop semantic
        hits whose preferred section per tag-group index is a different
        section. Tags with no indexed section pass through (ambiguous).
      - Modifier-conflict: if [APPLIES] modifier is in the conflict map,
        drop semantic hits whose tag is a member of the conflicting
        tag_group. Stops gestures-pollution when presenting_foot fires.

    Per-sub-intent slot cap (round-robin) ensures each sub-intent gets
    representation in the final menu without global-cosine competition
    evicting niche-but-relevant matches.

    Returns list of dicts with `matched_intent` field for debugging."""
    if not sub_intents:
        return []
    try:
        from . import tag_search
    except Exception:
        logger.exception("ai-patch: tag_search import failed")
        return []

    applies_by_tag = applies_by_tag or {}
    section_index = _build_tag_section_index()

    # Build the union of conflict-tag-groups based on which modifiers
    # fired. E.g. presenting_foot fired → gestures group becomes
    # forbidden-domain for retrieval candidates.
    conflict_groups: set[str] = set()
    for canon in applies_by_tag:
        for g in _MODIFIER_CONFLICT_GROUPS.get(canon, []):
            conflict_groups.add(g)
    conflict_tags: set[str] = set()
    for g in conflict_groups:
        conflict_tags.update(_load_tag_group(g))

    # Allocate slots per sub-intent so each gets representation in the
    # final menu. With 8 sub-intents and total_cap=24, that's 3 each.
    active_intents = [
        si for si in sub_intents
        if isinstance(si, dict) and (si.get("text") or "").strip()
        and (si.get("section") or "").lower() not in _RETRIEVAL_SKIP_SECTIONS
    ]
    if not active_intents:
        return []
    slots_per = max(2, total_cap // max(1, len(active_intents)))

    seen_tags: set[str] = set()
    per_intent_results: list[list[dict]] = []
    for si in active_intents:
        text = si["text"].strip()
        section = (si.get("section") or "").lower()
        candidates: list[dict] = []

        # Curator-authored alias scan first — deterministic, encoder-
        # independent. Solves the "close up on feet" -> foot_focus
        # failure where bge-small's bag-of-words can't bridge
        # definitional paraphrase. Aliases sourced from
        # data/tag-builder/tag-aliases-seed.json + curator edits.
        for h in tag_search.alias_scan(text):
            tag = (h.get("tag") or "").lower()
            if not tag or tag in seen_tags:
                continue
            if tag in bio_known or tag in modifier_canon:
                continue
            if tag in conflict_tags:
                continue
            entry = dict(h)
            entry["matched_intent"] = text
            entry["matched_via"] = "alias"
            candidates.append(entry)
            seen_tags.add(tag)

        # Literal anchor (substring against danbooru_tags) — high-priority,
        # always survives. Catches direct word matches like `sitting`.
        for h in _literal_anchor_candidates(
            text, applies_by_tag, modifier_canon, bio_known,
            conflict_tags=conflict_tags,
        ):
            tag = h["tag"].lower()
            if tag in seen_tags:
                continue
            entry = dict(h)
            entry["matched_intent"] = text
            entry["matched_via"] = "anchor"
            candidates.append(entry)
            seen_tags.add(tag)

        # Semantic retrieval — wider, filtered by section + modifier conflicts.
        try:
            hits = tag_search.search(text, top_k=top_per, threshold=threshold, on_status=on_status)
        except Exception:
            logger.exception("ai-patch: tag_search.search failed for %r", text)
            hits = []
        for h in hits:
            tag = (h.get("tag") or "").lower()
            if not tag or tag in seen_tags:
                continue
            if tag in bio_known or tag in modifier_canon:
                continue
            # Section-aware: drop if preferred section disagrees with
            # the sub-intent's section. Tags without an indexed section
            # are allowed through (genuinely ambiguous).
            preferred = section_index.get(tag)
            if preferred and section in {"character", "outfit", "pose",
                                          "expression", "setting"} \
               and preferred != section:
                continue
            # Modifier-conflict: drop if a fired modifier owns this domain.
            if tag in conflict_tags:
                continue
            entry = dict(h)
            entry["matched_intent"] = text
            entry["matched_via"] = "semantic"
            candidates.append(entry)
            seen_tags.add(tag)
            if len(candidates) >= slots_per:
                break

        per_intent_results.append(candidates)

    # Flatten while preserving sub-intent diversity (round-robin order).
    out: list[dict] = []
    max_len = max((len(r) for r in per_intent_results), default=0)
    for i in range(max_len):
        for r in per_intent_results:
            if i < len(r):
                out.append(r[i])
                if len(out) >= total_cap:
                    return out
    return out


_WEIGHTED_TOKEN_RE = re.compile(r"^\(\s*(.+?)\s*:\s*[\d.]+\s*\)$")


# SDXL/LAION-style captioning drops English articles inside descriptive
# phrases — `pointing gun at viewer` renders cleaner than the verbose
# `pointing a gun at viewer`. The AI emits articles inconsistently
# regardless of system prompt because it's drawing on training data
# that has both forms. Strip them here as a deterministic post-pass on
# phrasal tokens (tokens containing whitespace). Tag-form tokens like
# `looking_at_viewer` have no whitespace so they're untouched.
_ARTICLE_STRIP_RE = re.compile(r"\b(?:a|an|the)\s+", re.IGNORECASE)


def _strip_articles_from_phrasal(token: str) -> str:
    """Drop bare a/an/the followed by whitespace from phrasal tokens.
    Returns input unchanged if the token has no whitespace (tag form),
    or if the result would be empty (e.g. a token that's just 'the')."""
    if " " not in token:
        return token
    cleaned = _ARTICLE_STRIP_RE.sub("", token)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else token


# ── DB-driven modifier detection ──────────────────────────────────
# Loaded from slot_modifiers table — DB-driven so adding a new modifier
# (e.g. a fresh slang phrasing) is an INSERT, not a code change.

_SLOT_MODIFIERS_CACHE: list[dict] | None = None


def _load_slot_modifiers() -> list[dict]:
    global _SLOT_MODIFIERS_CACHE
    if _SLOT_MODIFIERS_CACHE is not None:
        return _SLOT_MODIFIERS_CACHE
    try:
        from .tag_builder import get_db
        db = get_db()
        rows = db.execute(
            "SELECT canonical_tag, aliases, clears_slots, is_substitute, "
            "substitute_section, implies_outfit_tag, definition "
            "FROM slot_modifiers ORDER BY sort_order"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            aliases = [a.strip().lower() for a in (r["aliases"] or "").split("|") if a.strip()]
            canon_phrase = (r["canonical_tag"] or "").replace("_", " ").lower()
            if canon_phrase and canon_phrase not in aliases:
                aliases.append(canon_phrase)
            slots = [s.strip() for s in (r["clears_slots"] or "").split(",") if s.strip()]
            sub_section = (r["substitute_section"] or "outfit").strip().lower()
            implies = (r["implies_outfit_tag"] or "").strip()
            definition = (r["definition"] or "").strip()
            out.append({
                "canonical_tag": r["canonical_tag"],
                "aliases": aliases,
                "clears_slots": slots,
                "is_substitute": bool(r["is_substitute"]),
                "substitute_section": sub_section,
                "implies_outfit_tag": implies or None,
                "definition": definition,
            })
        _SLOT_MODIFIERS_CACHE = out
    except Exception:
        logger.warning("could not load slot_modifiers — using empty list", exc_info=True)
        _SLOT_MODIFIERS_CACHE = []
    return _SLOT_MODIFIERS_CACHE


# ── Danbooru tag-group data ────────────────────────────────────────
# Lists of tags per Danbooru wiki tag_group (gestures, posture, attire,
# eyes_tags, locations). Used by `_drop_misplaced_tokens` to enforce
# section assignment — a tag in tag_group:gestures emitted under //
# Character is a misassignment regardless of intent.
#
# The hand-curated foot/gestures input/output filters that used to
# dispatch off these lists were retired in Phase 5 of the wiki-RAG
# rollout (semantic retrieval handles polysemy via gloss meaning).

_TAG_GROUPS_DIR = Path(__file__).parent.parent / "data" / "tag-builder" / "danbooru-tag-groups"
_TAG_GROUP_CACHE: dict[str, set[str]] = {}


def _load_tag_group(name: str) -> set[str]:
    if name in _TAG_GROUP_CACHE:
        return _TAG_GROUP_CACHE[name]
    path = _TAG_GROUPS_DIR / f"{name}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tags = set(data.get("tags") or [])
    except FileNotFoundError:
        logger.info("tag_group %s not found at %s", name, path)
        tags = set()
    except Exception:
        logger.warning("tag_group %s load failed", name, exc_info=True)
        tags = set()
    _TAG_GROUP_CACHE[name] = tags
    return tags


# Negation cues that, if they precede a matched alias within a small
# window, mean the user is asking to *remove* the modifier — not apply
# it. "remove barefoot" / "no socks" / "without the leotard" should NOT
# fire the [APPLIES] action.
_NEGATION_PREFIX_RE = re.compile(
    r"\b(?:remove|drop|delete|take\s+off|get\s+rid\s+of|"
    r"no(?:t)?|without|don'?t\s+(?:want|use|have))"
    r"(?:\s+\w+){0,2}\s+$",
    re.IGNORECASE,
)


def _is_negated_match(text: str, match_start: int) -> bool:
    """True if the alias match at `match_start` is preceded by a negation
    cue within a 4-word window. Lets 'remove barefoot' skip the
    [APPLIES] action without false-positiving on 'kneeling' (no
    negation), 'wearing barefoot leotard' (no negation cue), etc."""
    window = text[max(0, match_start - 40):match_start]
    return bool(_NEGATION_PREFIX_RE.search(window))


def _detect_modifiers_in_text(text: str) -> list[dict]:
    """Word-boundary alias scan; returns matched modifiers deduped by canonical_tag.
    Each result has canonical_tag, matched_alias, clears_slots, is_substitute,
    substitute_section, implies_outfit_tag, definition.

    Negation-aware: 'remove X' / 'no X' / 'without X' suppress the match
    so the modifier doesn't fire as [APPLIES] when the user is asking
    to drop the concept."""
    if not text:
        return []
    matched: dict[str, dict] = {}
    for mod in _load_slot_modifiers():
        for alias in mod["aliases"]:
            if not alias:
                continue
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.IGNORECASE)
            if not m:
                continue
            if _is_negated_match(text, m.start()):
                continue
            if mod["canonical_tag"] not in matched:
                matched[mod["canonical_tag"]] = {
                    "canonical_tag": mod["canonical_tag"],
                    "matched_alias": alias,
                    "clears_slots": mod["clears_slots"],
                    "is_substitute": mod["is_substitute"],
                    "substitute_section": mod["substitute_section"],
                    "implies_outfit_tag": mod["implies_outfit_tag"],
                    "definition": mod.get("definition", ""),
                }
            break
    return list(matched.values())


# ── slot resolver + modifier conflict map ────────────────────────
# The TB schema has slot data — clothing_items / pose_items / appearance_items
# all have an item_group column that IS the slot. But outfit_tags are stored
# as flat strings, so we have to classify each token at apply time. Hierarchy:
#   1. Direct lookup in clothing_items / appearance_items by item_tag
#   2. Color/material prefix strip (`brown_pantyhose` → `pantyhose` → legwear)
#   3. Suffix pattern fallback (`white_boots` → footwear via *_boots rule)
# Returns a slot like "clothing:footwear", "appearance:hair_color", etc., or None.

# Suffix → group map for clothing terms not directly in clothing_items.
# Covers the common case where outfit_tags reference colored / styled
# variants that don't have their own entry. Hand-curated; can be expanded.
_CLOTHING_SUFFIX_GROUPS = {
    # footwear
    "boots": "footwear", "shoes": "footwear", "sandals": "footwear",
    "heels": "footwear", "sneakers": "footwear", "loafers": "footwear",
    "slippers": "footwear", "geta": "footwear", "flats": "footwear",
    # tops
    "shirt": "tops", "blouse": "tops", "tank_top": "tops", "tanktop": "tops",
    "tube_top": "tops", "crop_top": "tops", "halter_top": "tops",
    "sweater": "tops", "hoodie": "tops", "jacket": "tops", "vest": "tops",
    "cardigan": "tops", "coat": "tops",
    # bottoms
    "skirt": "bottoms", "pants": "bottoms", "shorts": "bottoms",
    "jeans": "bottoms", "leggings": "bottoms", "trousers": "bottoms",
    # dresses (full-body garments — a single one of these covers the
    # whole body, so the strip-pass treats them as outfit replacements)
    "dress": "dresses", "gown": "dresses", "robe": "dresses",
    "kimono": "dresses", "qipao": "dresses",
    "nightie": "dresses", "nightgown": "dresses", "nightdress": "dresses",
    "negligee": "dresses", "chemise": "dresses", "babydoll": "dresses",
    "sundress": "dresses", "jumpsuit": "dresses", "romper": "dresses",
    "leotard": "dresses", "bodysuit": "dresses", "uniform": "dresses",
    # legwear
    "pantyhose": "legwear", "stockings": "legwear", "socks": "legwear",
    "tights": "legwear", "thighhighs": "legwear", "kneehighs": "legwear",
    # underwear / lingerie
    "panties": "underwear", "bra": "lingerie", "underwear": "underwear",
    "thong": "underwear",
    # swimwear
    "bikini": "swimwear", "swimsuit": "swimwear", "one-piece_swimsuit": "swimwear",
    # handwear
    "gloves": "handwear", "mittens": "handwear", "gauntlets": "handwear",
    # headwear
    "hat": "headwear", "helmet": "headwear", "cap": "headwear", "beret": "headwear",
    "headband": "headwear", "tiara": "headwear", "crown": "headwear", "hood": "headwear",
    # neckwear
    "collar": "neckwear", "choker": "neckwear", "necklace": "neckwear",
    "scarf": "neckwear", "tie": "neckwear", "ribbon": "neckwear",
    # accessories
    "ring": "accessories", "bracelet": "accessories", "earring": "accessories",
    "earrings": "accessories", "harness": "accessories", "holster": "accessories",
    "belt": "accessories",
}

# Modifier tokens that REMOVE other tokens by group when added. E.g. adding
# `barefoot` means existing footwear should go. Keys are canonical (space form,
# lower case), values are clothing group names from clothing_groups.
_MODIFIER_CONFLICTS: dict[str, list[str]] = {
    "barefoot": ["footwear"],
    "no_shoes": ["footwear"],
    "shoes_removed": ["footwear"],
    "unworn_footwear": ["footwear"],
    "bottomless": ["bottoms", "underwear", "dresses"],
    "no_panties": ["underwear"],
    "topless": ["tops", "dresses", "swimwear"],
    "no_bra": ["lingerie"],
    "shirtless": ["tops"],
    "nude": ["tops", "bottoms", "dresses", "underwear", "lingerie",
             "swimwear", "footwear", "legwear", "handwear"],
    "completely_nude": ["tops", "bottoms", "dresses", "underwear", "lingerie",
                        "swimwear", "footwear", "legwear", "handwear",
                        "headwear", "neckwear", "accessories"],
    "naked": ["tops", "bottoms", "dresses", "underwear", "lingerie",
              "swimwear", "footwear", "legwear", "handwear"],
}

# Cached color/material prefixes loaded from clothing_colors / clothing_materials.
# Lazily built on first classify call so module import doesn't hit the DB.
_CLOTHING_PREFIX_CACHE: list[str] | None = None


def _get_clothing_prefixes() -> list[str]:
    global _CLOTHING_PREFIX_CACHE
    if _CLOTHING_PREFIX_CACHE is not None:
        return _CLOTHING_PREFIX_CACHE
    try:
        from .tag_builder import get_db
        db = get_db()
        prefixes: set[str] = set()
        for table in ("clothing_colors", "clothing_materials", "clothing_patterns"):
            try:
                for r in db.execute(f"SELECT tag FROM {table}").fetchall():
                    if r["tag"]:
                        prefixes.add(r["tag"])
            except Exception:
                pass
        # Sort longest-first so `dark_red` strips before `red`.
        _CLOTHING_PREFIX_CACHE = sorted(prefixes, key=len, reverse=True)
    except Exception:
        logger.warning("could not preload clothing prefixes — using empty list", exc_info=True)
        _CLOTHING_PREFIX_CACHE = []
    return _CLOTHING_PREFIX_CACHE


def _classify_token(token: str) -> str | None:
    """Return a slot id like 'footwear', 'tops', 'hair_color', or None.
    Uses underscore form internally — caller should pass canonicalize-then-
    underscore-substitute form (e.g. 'white_boots', not 'white boots')."""
    if not token:
        return None
    needle = token.strip().lower()
    if not needle:
        return None
    try:
        from .tag_builder import get_db
        db = get_db()
    except Exception:
        return None
    # 1. Direct hit in clothing_items
    row = db.execute(
        "SELECT item_group FROM clothing_items WHERE item_tag = ?", [needle]
    ).fetchone()
    if row and row["item_group"]:
        return row["item_group"]
    # 2. Direct hit in appearance_items / pose_items (returned with prefix to
    #    avoid colliding with clothing slots if we add cross-slot logic later).
    row = db.execute(
        "SELECT item_group FROM appearance_items WHERE item_tag = ?", [needle]
    ).fetchone()
    if row and row["item_group"]:
        return f"appearance:{row['item_group']}"
    row = db.execute(
        "SELECT item_group FROM pose_items WHERE item_tag = ?", [needle]
    ).fetchone()
    if row and row["item_group"]:
        return f"pose:{row['item_group']}"
    # 3. Color/material prefix strip — `brown_pantyhose` → strip `brown` →
    #    `pantyhose` → legwear. Try clothing_items first since most prefixed
    #    forms are clothing.
    for prefix in _get_clothing_prefixes():
        if needle.startswith(prefix + "_"):
            stripped = needle[len(prefix) + 1:]
            row = db.execute(
                "SELECT item_group FROM clothing_items WHERE item_tag = ?", [stripped]
            ).fetchone()
            if row and row["item_group"]:
                return row["item_group"]
            # Also try suffix on stripped
            for suffix, group in _CLOTHING_SUFFIX_GROUPS.items():
                if stripped == suffix or stripped.endswith("_" + suffix):
                    return group
            break  # only try one prefix strip
    # 4. Suffix pattern — `white_boots` → footwear via `_boots`
    for suffix, group in _CLOTHING_SUFFIX_GROUPS.items():
        if needle == suffix or needle.endswith("_" + suffix):
            return group
    return None


def _expand_patch_with_slot_logic(patch: dict, node_prompt: str, request_id: str) -> dict:
    """Augment the AI's `remove` list with auto-detected conflicts from slot
    classification. Two cases:

      1. Modifier add (barefoot, bottomless, etc.) — pulled from
         _MODIFIER_CONFLICTS. Removes ALL source tokens whose slot is in the
         modifier's declared groups.
      2. Within-slot replacement (red_dress when source has blue_dress) —
         classify the add token's slot, remove existing source tokens in the
         same slot.

    Detected auto-removes are added to patch['remove'], deduplicated. Every
    decision is logged so failures are diagnosable from comfyui.log."""
    if not isinstance(patch, dict):
        return patch
    adds = patch.get("add") or []
    removes = list(patch.get("remove") or [])
    explicit_remove_canon = {_canonicalize_token(t) for t in removes}

    src_tokens = _split_prompt_tokens(node_prompt)
    src_with_slot: list[tuple[str, str | None]] = []
    for src in src_tokens:
        canon = _canonicalize_token(src)
        # Strip weighted wrapper for classification: `(red_gi:1.3)` → `red_gi`
        m = _WEIGHTED_TOKEN_RE.match(canon)
        bare = m.group(1) if m else canon
        slot = _classify_token(bare.replace(" ", "_"))
        src_with_slot.append((src, slot))

    auto_removes: list[str] = []
    decisions: list[str] = []

    for add_raw in adds:
        add_canon = _canonicalize_token(add_raw)
        underscored = add_canon.replace(" ", "_")
        # Modifier conflict — remove anything in declared groups
        if underscored in _MODIFIER_CONFLICTS:
            target_groups = _MODIFIER_CONFLICTS[underscored]
            for src, slot in src_with_slot:
                if slot in target_groups and _canonicalize_token(src) not in explicit_remove_canon:
                    if _canonicalize_token(src) == add_canon:
                        continue  # don't remove the add itself
                    auto_removes.append(src)
                    decisions.append(
                        f"  modifier {add_canon!r} ({target_groups}) → auto-remove {src!r} (slot={slot})"
                    )
            continue
        # Within-slot replacement
        add_slot = _classify_token(underscored)
        if not add_slot:
            decisions.append(f"  add {add_canon!r}: no slot classification, no auto-remove")
            continue
        for src, slot in src_with_slot:
            if slot == add_slot and _canonicalize_token(src) != add_canon \
               and _canonicalize_token(src) not in explicit_remove_canon:
                auto_removes.append(src)
                decisions.append(
                    f"  add {add_canon!r} (slot={add_slot}) → auto-remove {src!r} (same slot)"
                )

    # Dedupe preserving order; explicit-removes first, auto-removes after.
    deduped: list[str] = []
    seen: set[str] = set()
    for t in removes + auto_removes:
        c = _canonicalize_token(t)
        if c in seen:
            continue
        seen.add(c)
        deduped.append(t)

    if decisions:
        dbg.info("ai-patch[%s] slot-aware expansion:\n%s",
                 request_id, "\n".join(decisions))
    else:
        dbg.info("ai-patch[%s] slot-aware expansion: no auto-removes triggered", request_id)

    return {**patch, "remove": deduped, "auto_removed": auto_removes}


def _canonicalize_token(s: str) -> str:
    """Normalize a token for matching: strip whitespace, drop backslash
    escapes used by SD to literalize parens, swap underscores to spaces,
    collapse internal whitespace. Case is preserved (capitalization can
    carry meaning in tags)."""
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace("\\(", "(").replace("\\)", ")")
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _split_prompt_tokens(prompt: str) -> list[str]:
    """Split a prompt body on commas while respecting paren/bracket depth
    so weighted forms like '(red, blue:1.2)' survive as one token."""
    tokens: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in prompt:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            t = "".join(buf).strip()
            if t:
                tokens.append(t)
            buf = []
        else:
            buf.append(ch)
    t = "".join(buf).strip()
    if t:
        tokens.append(t)
    return tokens


def _node_prompt_token_sets(node_prompt: str) -> tuple[set[str], list[str]]:
    """Section-aware parse of node_prompt. Returns (positive_lc_tokens,
    negative_tokens_verbatim). The positive set is canonical-form
    lowercase + weighted-inner (so `(cammy_white:1.1)` matches
    `cammy white`). The negative list preserves original token strings
    so we can carry them forward verbatim in patch-mode output."""
    pos: set[str] = set()
    neg_tokens: list[str] = []
    if not node_prompt or not node_prompt.strip():
        return pos, neg_tokens
    for sec in _parse_sectioned_output(node_prompt):
        if sec.get("is_negative"):
            neg_tokens.extend(sec.get("tokens") or [])
            continue
        for tok in sec.get("tokens") or []:
            canon = _canonicalize_token(tok).lower()
            if canon:
                pos.add(canon)
            m = _WEIGHTED_TOKEN_RE.match(tok)
            if m:
                inner = _canonicalize_token(m.group(1)).lower()
                if inner:
                    pos.add(inner)
    return pos, neg_tokens


def _drop_unchanged_positives(sections: list[dict], node_prompt: str) -> list[dict]:
    """Diff section tokens against the existing node_prompt so the chip UI
    shows only what *changed*. Tokens that already exist in node_prompt —
    in any equivalent form (weighted/unweighted, underscore/space) — are
    dropped from the diff. Positive-section tokens diff against source
    positives (chip = "added"); negative-section tokens diff against
    source negatives (chip = "newly removed").

    Caller must build output_text BEFORE invoking this; output_text needs
    the full new prompt (for paste/apply), the chip UI needs the diff."""
    if not node_prompt or not node_prompt.strip():
        return sections
    existing_pos_lc, src_negs = _node_prompt_token_sets(node_prompt)
    existing_neg_lc = {_canonicalize_token(t).lower() for t in src_negs}
    existing_neg_lc.discard("")
    if not existing_pos_lc and not existing_neg_lc:
        return sections
    out: list[dict] = []
    for sec in sections:
        is_neg = bool(sec.get("is_negative"))
        existing_lc = existing_neg_lc if is_neg else existing_pos_lc
        if not existing_lc:
            out.append(sec)
            continue
        kept: list[str] = []
        for tok in sec.get("tokens") or []:
            canon = _canonicalize_token(tok).lower()
            if canon and canon in existing_lc:
                continue
            m = _WEIGHTED_TOKEN_RE.match(tok)
            if m:
                inner = _canonicalize_token(m.group(1)).lower()
                if inner and inner in existing_lc:
                    continue
            kept.append(tok)
        if kept:
            out.append({**sec, "tokens": kept})
    return out


def _add_positive_removal_chips(diffed: list[dict],
                                full_sections: list[dict],
                                node_prompt: str) -> list[dict]:
    """Surface positive-section tokens that were *dropped* from
    node_prompt as red `(removed)` chips, so a swap like 'barefoot →
    white_socks' shows both adds and removes in the diff UI.

    Pure UI aid — output_text was built from full_sections BEFORE this
    runs and already contains the correct end-state prompt; this only
    appends synthetic chip rows for the remove side. Per-section
    headers preserved so visual order matches the source."""
    if not node_prompt or not node_prompt.strip():
        return diffed
    src_sections = _parse_sectioned_output(node_prompt)
    if not src_sections:
        return diffed

    new_pos_lc: set[str] = set()
    for s in full_sections:
        if s.get("is_negative"):
            continue
        for t in s.get("tokens") or []:
            canon = _canonicalize_token(t).lower()
            if canon:
                new_pos_lc.add(canon)
            m = _WEIGHTED_TOKEN_RE.match(t)
            if m:
                inner = _canonicalize_token(m.group(1)).lower()
                if inner:
                    new_pos_lc.add(inner)

    out = list(diffed)
    for src_sec in src_sections:
        if src_sec.get("is_negative"):
            continue
        removed: list[str] = []
        for tok in src_sec.get("tokens") or []:
            canon = _canonicalize_token(tok).lower()
            if not canon:
                continue
            if canon in new_pos_lc:
                continue
            m = _WEIGHTED_TOKEN_RE.match(tok)
            if m:
                inner = _canonicalize_token(m.group(1)).lower()
                if inner and inner in new_pos_lc:
                    continue
            removed.append(tok)
        if removed:
            # is_negative reflects polarity (these were positive tags in
            # the source). is_removal flags the action so the frontend can
            # render strike-through + X separately from neg-text-color.
            out.append({
                "header": f"{src_sec['header']} (removed)",
                "tokens": removed,
                "is_negative": False,
                "is_removal": True,
            })
    return out


# Top-level body-position posture groups that contradict harshly. A
# token matched into one group is mutually exclusive with tokens matched
# into a different group; within-group is fine (e.g. `sitting` and `seiza`
# can co-occur). Pattern matched against the canonical underscore form
# of the bare token (no parens, lowercase, spaces -> underscores).
#
# User's request was minimal: only the things that contradict harshly.
# sit / stand / (lay | lying | on_*) / (crouch | bent_*). Everything
# else (legs_up, looking_at_viewer, etc.) layers freely with these.
_POSE_CONFLICT_GROUPS: list[tuple[str, re.Pattern]] = [
    ("sitting",   re.compile(r"^sit", re.IGNORECASE)),
    ("standing",  re.compile(r"^stand", re.IGNORECASE)),
    ("lying",     re.compile(r"^(lay|lying|on_back|on_side|on_stomach|reclin|prone|supine)",
                              re.IGNORECASE)),
    ("crouching", re.compile(r"^(crouch|bent_|squat)", re.IGNORECASE)),
    ("kneeling",  re.compile(r"^kneel", re.IGNORECASE)),
    # Locomotion verbs — exclusive with each other AND with stance groups
    ("running",   re.compile(r"^(run|sprint|sprinting)", re.IGNORECASE)),
    ("walking",   re.compile(r"^(walk|striding|stroll)", re.IGNORECASE)),
    ("jumping",   re.compile(r"^(jump|leap|airborne)", re.IGNORECASE)),
]


def _classify_posture(token: str) -> str | None:
    """Return the posture group name for a token, or None if it doesn't
    match any conflict-tracked posture."""
    bare = _bare_form(token).replace(" ", "_")
    if not bare:
        return None
    for name, pat in _POSE_CONFLICT_GROUPS:
        if pat.match(bare):
            return name
    return None


def _postures_in_text(text: str) -> set[str]:
    """Find all posture groups represented in a free-text string by
    word-walk (not whole-token classification). Used on user_request
    so phrasings like 'switch to sitting' identify the sitting group
    even though the request isn't a single Danbooru tag."""
    found: set[str] = set()
    for word in re.findall(r"[A-Za-z_]+", text or ""):
        for name, pat in _POSE_CONFLICT_GROUPS:
            if pat.match(word):
                found.add(name)
                break
    return found


def _resolve_posture_conflicts(sections: list[dict],
                               user_request: str,
                               node_prompt: str,
                               request_id: str) -> list[dict]:
    """Drop posture tokens whose group conflicts with the user's
    posture intent for THIS turn.

    Intent comes from two sources:
      1. user_request — any posture word the user typed this turn
         ("sitting with legs up" -> sitting group is the intent).
      2. NEW tokens in the model output — postures emitted that
         weren't in node_prompt (model-introduced intent, e.g. when
         the user describes a scene that decomposes to a posture).

    The first signal is essential: when the user iterates on a prompt
    that already accumulated contradictions across previous turns
    (`sitting, (standing:1.3)` both preserved), the model preserves
    both per PATCH MODE — and we need to drop the conflicting one
    based on what the user just SAID, not what the model emitted.

    Within-group preservation is fine; only cross-group conflicts
    get resolved."""
    intent_groups = _postures_in_text(user_request)

    node_pos, _ = _node_prompt_token_sets(node_prompt)
    for s in sections:
        if s.get("is_negative"):
            continue
        for t in s.get("tokens") or []:
            group = _classify_posture(t)
            if not group:
                continue
            canon = _canonicalize_token(t).lower()
            if canon and canon in node_pos:
                continue
            intent_groups.add(group)

    if not intent_groups:
        return sections

    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        kept: list[str] = []
        for t in s.get("tokens") or []:
            group = _classify_posture(t)
            if group and group not in intent_groups:
                dropped.append(f"{t} (group={group})")
                continue
            kept.append(t)
        s["tokens"] = kept

    if dropped:
        dbg.info(
            "ai-patch[%s] posture-conflict resolved (intent=%s): dropped %s",
            request_id, ", ".join(sorted(intent_groups)), "; ".join(dropped),
        )

    return [s for s in sections if s.get("tokens") or s.get("is_negative")]




def _preserve_existing_style_section(sections: list[dict], node_prompt: str) -> list[dict]:
    """Patch-mode: when no fresh style alias hit fires, the user's
    existing // Style: section in node_prompt should survive Apply.
    Pulls the prior Style block from node_prompt and inserts it into
    sections (between Setting and Negative) if no Style is already
    present.

    Mirrors _preserve_existing_negatives — both protect against an
    iterative-edit turn silently losing user-authored content."""
    if not node_prompt:
        return sections
    has_style = any(
        (s.get("header") or "").lower().startswith("// style")
        for s in sections
    )
    if has_style:
        return sections
    prior = _parse_sectioned_output(node_prompt) or []
    prior_style = next(
        (s for s in prior if (s.get("header") or "").lower().startswith("// style")),
        None,
    )
    if not prior_style:
        return sections
    # Insert before the Negative Prompt (if any), else append.
    neg_idx = next(
        (i for i, s in enumerate(sections) if s.get("is_negative")),
        len(sections),
    )
    return sections[:neg_idx] + [prior_style] + sections[neg_idx:]


def _preserve_existing_negatives(sections: list[dict], node_prompt: str) -> list[dict]:
    """Patch-mode: never let a model with no Negative Prompt section
    silently wipe the user's existing negs on Apply. Take node_prompt's
    original negative tokens and union them into the output's Negative
    Prompt section. New negs from the model (or from
    _enforce_default_outfit_negation) are preserved on top."""
    _, src_negs = _node_prompt_token_sets(node_prompt)
    if not src_negs:
        return sections
    neg_section = next((s for s in sections if s.get("is_negative")), None)
    if neg_section is None:
        neg_section = {"header": "Negative Prompt:", "tokens": [], "is_negative": True}
        sections.append(neg_section)
    seen_lc = {_canonicalize_token(t).lower() for t in neg_section["tokens"]}
    for tok in src_negs:
        key = _canonicalize_token(tok).lower()
        if key and key not in seen_lc:
            neg_section["tokens"].append(tok)
            seen_lc.add(key)
    return sections


def _build_source_token_set(prompt: str) -> set[str]:
    """Canonical token set used by the hallucination check. Includes the
    inner content of any (tag:weight) wrapper as a separate entry so an
    AI that says `remove: ["red gi"]` against a source `(red gi:1.3)`
    doesn't trip a false-positive 'not in source' warning."""
    out: set[str] = set()
    for raw in _split_prompt_tokens(prompt):
        canon = _canonicalize_token(raw)
        if canon:
            out.add(canon)
        m = _WEIGHTED_TOKEN_RE.match(raw)
        if m:
            inner = _canonicalize_token(m.group(1))
            if inner:
                out.add(inner)
    return out


_SECTION_HEADER_RE = re.compile(r"^\s*//\s*(.+?)\s*$")
_NEGATIVE_PROMPT_RE = re.compile(r"^\s*negative\s+prompt\s*:\s*(.*)$", re.IGNORECASE)
_BARE_WEIGHTED_RE = re.compile(r"^([A-Za-z0-9_\-\\\(\)]+):(\d+\.?\d*)$")


_ALLOWED_SECTION_PREFIXES = (
    "character", "outfit", "pose", "action", "prop",
    "expression", "setting", "scene", "style",
    # Quality / aesthetic tokens (`masterpiece`, `best quality`, `very awa`,
    # `highres`, etc.) — admitted so users' existing quality tags from
    # prior node_prompt survive the patch round-trip. Without this, the
    # filter silently dropped any // Quality section the model emitted.
    "quality", "aesthetic",
)


def _enforce_applies_modifiers(sections: list[dict],
                               applies_by_tag: dict | None,
                               request_id: str) -> list[dict]:
    """Server-side enforcement of [APPLIES] modifier rules. After the
    model emits its sections, check that every fired [APPLIES] modifier
    actually got its required tag into the right section. If not,
    server adds it.

    The model is given the rule in the prompt with explicit "MUST be
    followed verbatim" framing — but it sometimes ignores that in favor
    of options from the retrieval menu (e.g. picking `pointing_at_viewer`
    instead of the [APPLIES]-required `presenting_foot`). This backstop
    closes that gap deterministically.

    Mirrors the design of `_enforce_default_outfit_negation` — we already
    trust the server to handle slot-specific output for one class of
    rule; this extends to the broader [APPLIES] class.

    NOTE: do not early-return when applies_by_tag is empty — the conflict
    scrub at the end of this function also fires when a conflict-anchor
    tag arrives via [SEMANTIC] / literal-anchor / model-knowledge paths,
    which never populate applies_by_tag."""
    applies_by_tag = applies_by_tag or {}

    def _find_or_create_section(target_section: str) -> dict:
        # target_section is "pose" or "outfit"
        prefix = "// pose" if target_section == "pose" else "// outfit"
        for s in sections:
            if s.get("is_negative"):
                continue
            header_lc = (s.get("header") or "").lower().strip()
            if header_lc.startswith(prefix):
                return s
        # Section not present — create it with a default header.
        new_header = (
            "// Pose, Action & Prop" if target_section == "pose"
            else "// Outfit"
        )
        new_section = {"header": new_header, "tokens": []}
        sections.append(new_section)
        return new_section

    forced: list[str] = []
    for canon, mod_data in applies_by_tag.items():
        # The substitute (e.g. `presenting_foot` -> // Pose).
        if mod_data.get("is_substitute"):
            target = mod_data.get("substitute_section") or "outfit"
            sect = _find_or_create_section(target)
            existing = {_bare_form(t).replace(" ", "_") for t in sect["tokens"]}
            if canon not in existing:
                sect["tokens"].append(canon)
                forced.append(f"{canon} -> {sect['header']}")
        # Implies cascade (e.g. presenting_foot.implies = barefoot -> // Outfit).
        implies = (mod_data.get("implies_outfit_tag") or "").strip()
        if implies:
            sect = _find_or_create_section("outfit")
            existing = {_bare_form(t).replace(" ", "_") for t in sect["tokens"]}
            if implies not in existing:
                sect["tokens"].append(implies)
                forced.append(f"{implies} -> {sect['header']} (cascade)")

    if forced:
        dbg.info(
            "ai-patch[%s] [APPLIES]-enforce added: %s",
            request_id, "; ".join(forced),
        )

    # Output-stage conflict scrub: when a tag from _MODIFIER_CONFLICT_GROUPS
    # is in play (either an [APPLIES] modifier fired, or the model emitted
    # the canonical tag itself via [SEMANTIC] / literal anchor / its own
    # knowledge), the conflicting tag groups are mutually exclusive with it.
    # Retrieval already drops conflicts from the *menu*, but the model can
    # still emit a conflicting tag from any path that doesn't pass through
    # the menu filter. Scrub them here so the output mirrors the curated
    # invariant regardless of how the trigger got into the output.
    emitted_pos_tags: set[str] = set()
    for s in sections:
        if s.get("is_negative"):
            continue
        for t in s.get("tokens") or []:
            emitted_pos_tags.add(_bare_form(t).replace(" ", "_").lower())
    conflict_groups: set[str] = set()
    for canon in applies_by_tag:
        for g in _MODIFIER_CONFLICT_GROUPS.get(canon, []):
            conflict_groups.add(g)
    for trigger_tag, groups in _MODIFIER_CONFLICT_GROUPS.items():
        if trigger_tag in emitted_pos_tags:
            conflict_groups.update(groups)
    if conflict_groups:
        conflict_tags: set[str] = set()
        for g in conflict_groups:
            conflict_tags.update(_load_tag_group(g))
        scrubbed: list[str] = []
        for s in sections:
            if s.get("is_negative"):
                continue
            kept: list[str] = []
            for t in s.get("tokens") or []:
                tag = _bare_form(t).replace(" ", "_").lower()
                if tag in conflict_tags:
                    scrubbed.append(t)
                    continue
                kept.append(t)
            s["tokens"] = kept
        if scrubbed:
            dbg.info(
                "ai-patch[%s] [APPLIES]-conflict scrubbed: %s",
                request_id, ", ".join(scrubbed),
            )
    return sections


def _section_has_node_prompt_token(section: dict, node_pos: set[str]) -> bool:
    for tok in section.get("tokens") or []:
        canon = _canonicalize_token(tok).lower()
        if canon and canon in node_pos:
            return True
        m = _WEIGHTED_TOKEN_RE.match(tok)
        if m:
            inner = _canonicalize_token(m.group(1)).lower()
            if inner and inner in node_pos:
                return True
    return False


def _filter_allowed_sections(sections: list[dict],
                              node_pos: set[str] | None = None) -> list[dict]:
    """Drop sections that are model-invented slop. A section is kept if EITHER:
      - its header matches the canonical schema (character/outfit/pose/...), OR
      - it contains at least one token that came from node_prompt (user-
        authored content under a freeform // comment label).

    Negative Prompt section is always preserved (has its own marker).
    The whitelist defends against the original failure mode (model
    inventing `// Quality: masterpiece, best quality` and dumping slop) —
    but a section the user wrote is the user's authority, not ours."""
    out: list[dict] = []
    node_pos = node_pos or set()
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        header = (s.get("header") or "").lstrip("/").strip().lower()
        first = re.split(r"[\s,:]", header, maxsplit=1)[0].strip()
        if first and any(first.startswith(p) for p in _ALLOWED_SECTION_PREFIXES):
            out.append(s)
            continue
        if node_pos and _section_has_node_prompt_token(s, node_pos):
            out.append(s)
            continue
        logger.info("ai-patch: dropped spurious section %r", s.get("header"))
    return out


_SAME_CHAR_OUTFIT_PATTERN = re.compile(
    r"([A-Za-z][\w\s\-]*?)\s+(?:in|wearing)\s+([\w\s\-]+?)\s+outfit\b",
    re.IGNORECASE,
)


def _expand_bios_for_same_char_multi_outfit(
    bios: list[dict], user_request: str,
) -> list[dict]:
    """Detect `<char_name> in <outfit_phrase> outfit` mentioned MULTIPLE
    times for the same character (e.g. `cammy in killer_bee outfit
    fighting cammy in shadaloo outfit`). For each character with >1
    distinct outfit phrase, duplicate the bio with each outfit picked
    from the DB and assign it as user_requested_outfit. Returns the
    expanded bios list.

    No-op when the pattern matches at most once per character, or when
    no bio is loaded for the named character. Narrow regex so the
    false-positive risk is low — must explicitly say `in <X> outfit`."""
    if not bios or not user_request:
        return bios
    matches = list(_SAME_CHAR_OUTFIT_PATTERN.finditer(user_request))
    if len(matches) < 2:
        return bios

    # Build (char_candidate_token_string, outfit_keyword) per match,
    # take the last 1-2 tokens of the char-phrase as the candidate
    # name (same suffix-window approach as outfit-borrow detection).
    per_char: dict[str, list[str]] = {}
    for m in matches:
        char_phrase = m.group(1).strip().lower()
        outfit_phrase = m.group(2).strip().lower()
        char_tokens = re.findall(r"[\w\-]+", char_phrase)
        if not char_tokens:
            continue
        candidates: list[str] = []
        for w in (1, 2, 3):
            if w > len(char_tokens):
                break
            candidates.append(" ".join(char_tokens[-w:]))
        # Match against bios
        for b in bios:
            if not b or b.get("_outfit_source_only"):
                continue
            tag = (b.get("tag") or "").lower()
            display = (b.get("display") or "").lower()
            tag_norm = re.sub(r"[\s_\-]+", " ", tag).strip()
            display_norm = re.sub(r"[\s_\-]+", " ", display).strip()
            for cand in candidates:
                cand_norm = re.sub(r"[\s_\-]+", " ", cand).strip()
                if (cand_norm == tag_norm or cand_norm == display_norm or
                        (len(cand_norm) >= 3 and
                         (cand_norm in tag_norm or cand_norm in display_norm))):
                    per_char.setdefault(tag, [])
                    if outfit_phrase not in per_char[tag]:
                        per_char[tag].append(outfit_phrase)
                    break

    # For each character with >1 distinct outfit phrase, look up
    # outfits in DB and build duplicate bios
    try:
        from .tag_builder import get_db
    except Exception:
        return bios

    expanded = list(bios)
    for tag, outfit_phrases in per_char.items():
        if len(outfit_phrases) < 2:
            continue
        # Skip if upstream already supplied multiple bios for this tag
        # (e.g. agent-side detection or a test harness pre-loading).
        existing_count = sum(
            1 for b in expanded
            if (b.get("tag") or "").lower() == tag
            and not b.get("_outfit_source_only")
        )
        if existing_count >= len(outfit_phrases):
            continue
        # Locate the existing bio for this tag (first non-source one)
        existing = next(
            (b for b in expanded
             if (b.get("tag") or "").lower() == tag
             and not b.get("_outfit_source_only")), None,
        )
        if not existing:
            continue

        # Pick outfit rows by keyword, in order. First match keeps the
        # existing bio (updated with the first outfit); subsequent
        # matches get a duplicate.
        conn = get_db()
        outfit_data_per_phrase: list[dict] = []
        for phrase in outfit_phrases:
            kw = phrase.replace("_", " ").replace("-", " ").strip().lower()
            if not kw:
                continue
            row = conn.execute(
                "SELECT id, outfit_name, outfit_tags, outfit_natlang FROM outfits "
                "WHERE character_tag = ? AND "
                "LOWER(REPLACE(REPLACE(outfit_name, '_', ' '), '-', ' ')) LIKE ? "
                "ORDER BY is_default DESC, sort_order LIMIT 1",
                (existing.get("tag") or tag, f"%{kw}%"),
            ).fetchone()
            if not row:
                continue
            outfit_id = row["id"] if hasattr(row, "keys") else row[0]
            outfit_name = row["outfit_name"] if hasattr(row, "keys") else row[1]
            outfit_tags = row["outfit_tags"] if hasattr(row, "keys") else row[2]
            outfit_natlang = (row["outfit_natlang"] if hasattr(row, "keys")
                              else row[3])
            slot_rows = conn.execute(
                "SELECT slot, item, color, source_phrase FROM outfit_tag_slots "
                "WHERE outfit_id = ? ORDER BY sort_order",
                (outfit_id,),
            ).fetchall()
            slots = [
                {
                    "slot": (s["slot"] if hasattr(s, "keys") else s[0]) or "",
                    "item": (s["item"] if hasattr(s, "keys") else s[1]) or "",
                    "color": (s["color"] if hasattr(s, "keys") else s[2]) or "",
                    "source_phrase": (s["source_phrase"]
                                       if hasattr(s, "keys") else s[3]) or "",
                }
                for s in slot_rows
            ]
            outfit_data_per_phrase.append({
                "id": outfit_id, "name": outfit_name or "",
                "natlang": outfit_natlang or "",
                "tags": outfit_tags or "", "slots": slots,
            })
        if len(outfit_data_per_phrase) < 2:
            continue

        # Update existing bio with first outfit as user_requested_outfit
        existing["user_requested_outfit"] = outfit_data_per_phrase[0]
        existing["outfit_overridden"] = True
        # Insert duplicate bios after `existing` for each additional outfit
        idx = expanded.index(existing)
        for od in outfit_data_per_phrase[1:]:
            dup = dict(existing)
            dup["user_requested_outfit"] = od
            dup["outfit_overridden"] = True
            # Don't copy default_outfit slot mutations — each instance
            # is its own outfit pick. Strip _displaced_phrases too
            # since they're populated by per-bio modifier scrubs.
            dup.pop("_displaced_phrases", None)
            idx += 1
            expanded.insert(idx, dup)

        logger.info(
            "ai-patch: same-char-multi-outfit expanded bio for tag=%r "
            "across outfits=%s",
            tag, [o["name"] for o in outfit_data_per_phrase],
        )

    return expanded


def _bio_outfit_tokens(bio: dict) -> list[str]:
    """Extract canonical tokens from bio outfit. Prefers slot
    source_phrases; falls back to the flat outfit.tags string when
    slots aren't decomposed (some outfits in the DB carry only the
    comma-separated tag blob, no per-slot rows)."""
    outfit = bio.get("user_requested_outfit") or bio.get("default_outfit") or {}
    tokens: list[str] = []
    seen: set[str] = set()
    slots = outfit.get("slots") or []
    for s in slots:
        phrase = (s.get("source_phrase") or "").strip()
        if not phrase:
            continue
        canon = phrase.lower().replace(" ", "_")
        if canon in seen:
            continue
        seen.add(canon)
        tokens.append(canon)
    if tokens:
        return tokens
    # Fallback: flat outfit_tags blob, comma-split.
    flat = (outfit.get("tags") or "").strip()
    if flat:
        for raw in flat.split(","):
            t = raw.strip().lower().replace(" ", "_")
            if not t or t in seen:
                continue
            seen.add(t)
            tokens.append(t)
    return tokens


def _ensure_same_char_multi_outfit_sections(
    sections: list[dict], bios: list[dict], request_id: str,
) -> list[dict]:
    """Two-of-the-same-character-in-different-outfits scenario. If bios
    has duplicate `tag` entries (same canonical character, different
    user_requested_outfit), the patch model usually emits only one
    // Character + // Outfit section regardless — because the two bios
    look identical at the header level. Server-side: detect duplicate
    tags, duplicate the existing // Character section per instance,
    and pair each with the matching outfit slots. Headers get
    disambiguated with the outfit name (e.g.
    `// Character: cammy_white (Killer Bee)`) so the downstream
    `_dedup_section_headers` doesn't collapse them.

    Skips bios with `_outfit_source_only=True` (outfit-borrow case).
    """
    if not bios or not sections:
        return sections
    from collections import defaultdict
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for b in bios:
        if not b or not b.get("tag"):
            continue
        if b.get("_outfit_source_only"):
            continue
        by_tag[(b.get("tag") or "").lower()].append(b)
    dup_tags = {t: bs for t, bs in by_tag.items() if len(bs) > 1}
    if not dup_tags:
        return sections

    out = list(sections)
    for tag, dup_bios in dup_tags.items():
        # Find existing // Character section for this tag
        char_idx = None
        for i, s in enumerate(out):
            if s.get("is_negative"):
                continue
            header_lc = (s.get("header") or "").lower()
            if header_lc.startswith("// character") and tag in header_lc:
                char_idx = i
                break
        if char_idx is None:
            # No existing section to clone — skip (a totally different
            # failure mode that _ensure_bio_outfits_emitted will catch)
            continue

        first_char_section = out[char_idx]
        char_body_tokens = list(first_char_section.get("tokens") or [])

        first_outfit = (dup_bios[0].get("user_requested_outfit")
                        or dup_bios[0].get("default_outfit") or {})
        first_outfit_name = (first_outfit.get("name") or "").strip()

        # Rename existing header with outfit suffix to prevent dedup
        # collapse with the duplicate(s) we're about to insert
        first_char_section["header"] = (
            f"// Character: {tag} ({first_outfit_name})"
            if first_outfit_name else f"// Character: {tag}"
        )

        # Replace/insert outfit section for bios[0]
        outfit_idx_first: int | None = None
        for j in range(char_idx + 1, len(out)):
            s = out[j]
            if s.get("is_negative"):
                continue
            jh = (s.get("header") or "").lower()
            if jh.startswith("// outfit"):
                outfit_idx_first = j
                break
            if jh.startswith("// character"):
                break

        first_outfit_tokens = _bio_outfit_tokens(dup_bios[0])
        first_outfit_header = (
            f"// Outfit: {first_outfit_name}"
            if first_outfit_name else "// Outfit"
        )
        if outfit_idx_first is not None and first_outfit_tokens:
            out[outfit_idx_first]["header"] = first_outfit_header
            out[outfit_idx_first]["tokens"] = list(first_outfit_tokens)
            out[outfit_idx_first]["body_text"] = ""
            insert_after = outfit_idx_first
        elif first_outfit_tokens:
            new_outfit = {
                "header": first_outfit_header,
                "tokens": list(first_outfit_tokens),
                "body_text": "",
                "is_negative": False,
            }
            out.insert(char_idx + 1, new_outfit)
            insert_after = char_idx + 1
        else:
            insert_after = char_idx

        # Insert duplicate char + outfit sections for bios[1:]
        for b in dup_bios[1:]:
            outfit = (b.get("user_requested_outfit")
                      or b.get("default_outfit") or {})
            outfit_name = (outfit.get("name") or "variant").strip()
            new_char = {
                "header": f"// Character: {tag} ({outfit_name})",
                "tokens": list(char_body_tokens),
                "body_text": "",
                "is_negative": False,
            }
            tokens = _bio_outfit_tokens(b)
            new_outfit = {
                "header": (f"// Outfit: {outfit_name}"
                            if outfit_name else "// Outfit"),
                "tokens": tokens,
                "body_text": "",
                "is_negative": False,
            }
            insert_after += 1
            out.insert(insert_after, new_char)
            insert_after += 1
            out.insert(insert_after, new_outfit)

        dbg.info(
            "ai-patch[%s] same-char-multi-outfit: tag=%r expanded to %d "
            "instances with outfits=%s",
            request_id, tag, len(dup_bios),
            [(b.get("user_requested_outfit")
              or b.get("default_outfit") or {}).get("name")
             for b in dup_bios],
        )
    return out


def _ensure_bio_outfits_emitted(
    sections: list[dict], bios: list[dict], request_id: str,
) -> list[dict]:
    """When a character bio has a default outfit and the model emitted
    a `// Character: <tag>` section but NO matching `// Outfit:` section
    after it, inject the outfit from the bio's slot data. Catches the
    multi-char-build failure mode where qwen3-vl:8b emits both
    `// Character: cammy_white` and `// Character: chun-li` but skips
    BOTH outfit sections — leaving SDXL to freelance clothing.

    Skips:
      - bios with `_outfit_source_only=True` (handled by
        `_apply_outfit_borrow_overwrite`)
      - bios with no `default_outfit` or no slots
      - characters who DO have a matching `// Outfit:` section already
    """
    if not bios:
        return sections
    # Track which character sections have a following outfit section
    # in the current output ordering. Walk sections and pair char→outfit
    # by adjacency until the next character or end.
    char_indices: list[tuple[int, str]] = []  # (index_in_sections, char_tag_lc)
    for i, s in enumerate(sections):
        if s.get("is_negative"):
            continue
        header = (s.get("header") or "")
        header_lc = header.lower()
        if header_lc.startswith("// character"):
            # Match the character tag from the header AND from token body.
            # Header form is `// Character: <tag>` or `// Character: <display>`.
            after_colon = header.split(":", 1)[1].strip().lower() if ":" in header else ""
            char_indices.append((i, after_colon))

    # For each char index, find the next char (or end) and look for
    # any outfit section in between.
    has_outfit_for_char: dict[int, bool] = {}
    for idx, (ci, _ctag) in enumerate(char_indices):
        next_ci = char_indices[idx + 1][0] if idx + 1 < len(char_indices) else len(sections)
        found = False
        for j in range(ci + 1, next_ci):
            sj = sections[j]
            if sj.get("is_negative"):
                continue
            if (sj.get("header") or "").lower().startswith("// outfit"):
                found = True
                break
        has_outfit_for_char[ci] = found

    def _slot_tokens_for_bio(bio: dict) -> list[str]:
        outfit = bio.get("default_outfit") or {}
        slots = outfit.get("slots") or []
        tokens: list[str] = []
        seen: set[str] = set()
        for s in slots:
            phrase = (s.get("source_phrase") or "").strip()
            if not phrase:
                continue
            canon = phrase.lower().replace(" ", "_")
            if canon in seen:
                continue
            seen.add(canon)
            tokens.append(canon)
        return tokens

    # Find a bio matching the header text for each char section, then
    # inject if outfit missing. Iterate in REVERSE so insertion indices
    # don't shift for later sections.
    out = list(sections)
    injections = 0
    for ci, ctag_lc in reversed(char_indices):
        if has_outfit_for_char.get(ci, False):
            continue
        # Match bio by tag or display, skip outfit-source-only bios
        bio = None
        for b in bios:
            if not b or b.get("_outfit_source_only"):
                continue
            tag_lc = (b.get("tag") or "").lower()
            display_lc = (b.get("display") or "").lower()
            if (tag_lc and (tag_lc == ctag_lc or tag_lc in ctag_lc
                            or ctag_lc in tag_lc)):
                bio = b
                break
            if (display_lc and (display_lc == ctag_lc
                                or display_lc in ctag_lc
                                or ctag_lc in display_lc)):
                bio = b
                break
        if not bio:
            continue
        outfit = bio.get("default_outfit") or {}
        tokens = _slot_tokens_for_bio(bio)
        if not tokens:
            continue
        outfit_name = (outfit.get("name") or "").strip()
        new_header = (f"// Outfit: {outfit_name}" if outfit_name
                      else "// Outfit")
        new_section = {
            "header": new_header,
            "tokens": list(tokens),
            "body_text": "",
            "is_negative": False,
        }
        out.insert(ci + 1, new_section)
        injections += 1
        dbg.info(
            "ai-patch[%s] auto-injected missing // Outfit for char=%r "
            "(name=%s, tokens=%d)",
            request_id, ctag_lc, outfit_name, len(tokens),
        )
    return out


def _apply_outfit_borrow_overwrite(
    sections: list[dict], bios: list[dict], request_id: str,
) -> list[dict]:
    """When a bio is marked `_outfit_source_only`, the LLM is supposed
    to rewrite the // Outfit section using that bio's outfit slots. The
    PATCH MODE verbatim-preservation rule + the OUTFIT HEADER rule (keep
    name from bio) collude to make qwen3-vl:8b leave the header as-is
    even when it swaps the body tokens. Server-side overwrite the header
    and body deterministically so the borrow always lands.

    Header: `// Outfit: <source outfit name> from Character: <source tag>`
    Body: tokens from source bio's outfit.slots (source_phrase per slot,
    underscored, lowercased) plus any modifier slot canonicals that
    survived from the prior turn (e.g. `barefoot` if user didn't ask to
    fill legwear). The model's body-token guess is discarded — server
    is authoritative."""
    source_bio = next(
        (b for b in (bios or []) if b and b.get("_outfit_source_only")),
        None,
    )
    if not source_bio:
        return sections
    outfit = source_bio.get("default_outfit") or {}
    slots = outfit.get("slots") or []
    if not slots:
        return sections
    source_name = (outfit.get("name") or "").strip()
    source_tag = (source_bio.get("tag") or "").strip()
    new_header = f"// Outfit: {source_name} from Character: {source_tag}".strip()
    new_tokens: list[str] = []
    seen: set[str] = set()
    for s in slots:
        phrase = (s.get("source_phrase") or "").strip()
        if not phrase:
            continue
        canon = phrase.lower().replace(" ", "_")
        if canon in seen:
            continue
        seen.add(canon)
        new_tokens.append(canon)
    if not new_tokens:
        return sections
    out: list[dict] = []
    overwritten = False
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        if (s.get("header") or "").strip().lower().startswith("// outfit"):
            new_section = dict(s)
            new_section["header"] = new_header
            new_section["tokens"] = list(new_tokens)
            new_section["body_text"] = ""
            out.append(new_section)
            overwritten = True
            continue
        out.append(s)
    if not overwritten:
        # No // Outfit section emitted — insert one after the first
        # // Character section.
        new_section = {
            "header": new_header,
            "tokens": list(new_tokens),
            "body_text": "",
            "is_negative": False,
        }
        insertion_idx = 0
        for i, s in enumerate(out):
            if (s.get("header") or "").lower().startswith("// character"):
                insertion_idx = i + 1
                break
        out.insert(insertion_idx, new_section)
        overwritten = True
    if overwritten:
        dbg.info(
            "ai-patch[%s] outfit-borrow overwrite: header=%r tokens=%d "
            "from source=%s outfit=%s",
            request_id, new_header, len(new_tokens), source_tag, source_name,
        )
    return out


# Maps the first word of a // section header to a canonical concept.
# Used by `_filter_unrequested_sections` to drop sections the user did
# not ask for in build/fresh mode (no node_prompt anchor) — qwen3-vl:8b
# routinely speculates `// Pose, Action & Prop`, `// Setting / Scene`,
# and `// Quality` on bare requests like `set up cammy_white`, even
# though the build-mode prompt instructs it not to.
_SECTION_HEADER_TO_CONCEPT = {
    "character": "character",
    "outfit":    "outfit",
    "pose":      "pose",
    "action":    "pose",
    "prop":      "pose",
    "expression": "expression",
    "setting":   "setting",
    "scene":     "setting",
    "style":     "style",
    "quality":   "quality",
    "aesthetic": "quality",
}

# Sub-intent section labels → same canonical concept space.
_SUB_INTENT_TO_CONCEPT = {
    "character": "character",
    "outfit":    "outfit",
    "strip":     "outfit",
    "pose":      "pose",
    "expression": "expression",
    "setting":   "setting",
    "scene":     "setting",
    "style":     "style",
    "quality":   "quality",
}


def _section_concept(header: str) -> str | None:
    h = (header or "").lstrip("/").strip().lower()
    first = re.split(r"[\s,:]", h, maxsplit=1)[0].strip()
    return _SECTION_HEADER_TO_CONCEPT.get(first)


def _filter_unrequested_sections(sections: list[dict],
                                  sub_intents: list[dict] | None,
                                  node_prompt: str,
                                  bios: list[dict],
                                  request_id: str) -> list[dict]:
    """Drop sections the user did not ask for AND that did not exist in
    node_prompt. Defends against build-mode speculation where the model
    invents // Pose/// Setting/// Quality on a request like
    `cammy white` whose decompose only emitted [character].

    A section is KEPT when any of these are true:
      - it's the Negative Prompt section (server-managed)
      - its concept is 'style' (server-managed via auto-seed + injection)
      - its concept is 'character' AND bios is non-empty (bio implies it)
      - its concept is 'outfit' AND some bio has default_outfit (bio implies it)
      - its concept appears in the decomposed sub_intents
      - a section with that concept existed in node_prompt (preserve patch-mode)

    Tag-mode only. Natlang has its own decompose-driven section gate.
    """
    if not sections:
        return sections
    intent_concepts: set[str] = set()
    for si in sub_intents or []:
        c = _SUB_INTENT_TO_CONCEPT.get((si.get("section") or "").lower())
        if c:
            intent_concepts.add(c)

    node_concepts: set[str] = set()
    for s in _parse_sectioned_output(node_prompt) or []:
        c = _section_concept(s.get("header") or "")
        if c:
            node_concepts.add(c)

    bio_has_character = bool(bios)
    bio_has_outfit = any(
        (b.get("default_outfit") or {}).get("name") or (b.get("default_outfit") or {}).get("slots")
        for b in (bios or [])
    )

    out: list[dict] = []
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        concept = _section_concept(s.get("header") or "")
        if concept is None:
            out.append(s)
            continue
        if concept == "style":
            out.append(s)
            continue
        if concept == "character" and bio_has_character:
            out.append(s)
            continue
        if concept == "outfit" and bio_has_outfit:
            out.append(s)
            continue
        if concept in intent_concepts:
            out.append(s)
            continue
        if concept in node_concepts:
            out.append(s)
            continue
        logger.info(
            "ai-patch[%s] dropped unrequested section %r (concept=%s, "
            "intents=%s, node_concepts=%s)",
            request_id, s.get("header"), concept,
            sorted(intent_concepts), sorted(node_concepts),
        )
    return out


def _restore_weighted_parens(sections: list[dict]) -> list[dict]:
    """Wrap any `name:weight` token (no parens) in parens. Bare `name:weight`
    is not a valid SD token form — this is always safe, regardless of bio."""
    for s in sections:
        new_tokens: list[str] = []
        for t in s.get("tokens", []):
            if t.startswith("(") or t.startswith("\\("):
                new_tokens.append(t)
                continue
            m = _BARE_WEIGHTED_RE.match(t)
            if m:
                new_tokens.append(f"({m.group(1)}:{m.group(2)})")
            else:
                new_tokens.append(t)
        s["tokens"] = new_tokens
    return sections


# Color words used by the slot-swap pre-pass below. Curated for
# SD-prompt-relevant colors; not exhaustive. Compound colors like
# "dark green" or "navy blue" aren't handled — extend if needed.
_SLOT_SWAP_COLORS: frozenset[str] = frozenset({
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "white", "black", "brown", "gray", "grey", "tan", "beige",
    "navy", "teal", "cyan", "magenta", "violet", "gold", "silver",
})


def _resolve_color_swaps(bios: list[dict], user_request: str) -> list[dict]:
    """Server-side pre-pass for the slot-aware color swap rule. Detects
    `<color> <slot-item>` patterns in user_request, mutates the matching
    bio default_outfit slot to the user's color, and records the
    displaced source_phrase so the negation backstop can negate it later.

    The smaller backing models (qwen3-vl:8b-instruct etc.) often skip
    the system-prompt rule that asks them to perform this swap. Doing
    it deterministically here means the LLM never sees a stale outfit
    and the negation chain (displacement -> neg) doesn't depend on the
    model emitting the right tokens.

    Mutates bios in place; returns same list."""
    if not bios or not user_request:
        return bios
    user_lc = user_request.lower()
    for b in bios:
        default = b.get("default_outfit")
        if not default:
            continue
        slots = default.get("slots") or []
        if not slots:
            continue
        displaced: list[str] = []
        for i, slot in enumerate(slots):
            item_lc = (slot.get("item") or "").lower().replace("_", " ").strip()
            current_color = (slot.get("color") or "").lower().strip()
            current_phrase = (slot.get("source_phrase") or "").strip()
            if not item_lc:
                continue
            # Try the full item (`combat boots`) first, then fall back to
            # the tail word (`boots`) so `black boots` matches `combat_boots`.
            item_variants = [item_lc]
            tail = item_lc.rsplit(" ", 1)[-1]
            if tail and tail != item_lc:
                item_variants.append(tail)
            for color in _SLOT_SWAP_COLORS:
                if color == current_color:
                    continue
                if not any(f"{color} {v}" in user_lc for v in item_variants):
                    continue
                new_phrase = f"{color}_{item_lc.replace(' ', '_')}"
                slots[i] = {**slot, "color": color, "source_phrase": new_phrase}
                if current_phrase:
                    displaced.append(current_phrase)
                break
        if displaced:
            existing = list(b.get("_displaced_phrases") or [])
            for p in displaced:
                if p not in existing:
                    existing.append(p)
            b["_displaced_phrases"] = existing
    return bios


def _detect_user_filled_slots(user_request: str) -> set[str]:
    """Return slot names the user is explicitly filling with a real item
    in their request. Suffix-based detection via _CLOTHING_SUFFIX_GROUPS
    — 'wearing white socks' -> {'legwear'}, 'red boots' -> {'footwear'}."""
    if not user_request:
        return set()
    text_lc = user_request.lower()
    slots: set[str] = set()
    for suffix, slot in _CLOTHING_SUFFIX_GROUPS.items():
        suffix_word = suffix.replace("_", " ")
        if re.search(r"(?<!\w)" + re.escape(suffix_word) + r"(?!\w)", text_lc):
            slots.add(slot)
    return slots


# Tag-mode structural post-passes — strip narrowing + modifier slot-clear.
# Mirrors the natlang side's typed-delta determinism for two specific
# operations the LLM is unreliable at in patch mode (the "preserve
# verbatim" rule outranks structural rules in its reasoning):
#   1. `wearing only X` → narrow // Outfit to X tokens, drop the rest
#   2. modifier alias detected (barefoot, topless, …) → drop // Outfit
#      tokens whose slot is in modifier.clears_slots
# The natlang side does this via PromptState slot states + apply_deltas.
# Tag mode reuses the LLM's emitted // Outfit tokens, mapping each back
# to its slot via the bio's outfit.slots taxonomy (authoritative when
# present) with a fallback to natlang_facts._resolve_slot_from_phrase
# for user-added tokens not in the bio (e.g. user added `red_socks` last
# turn and now says `barefoot`; legwear must drop even though the bio
# never had legwear).


def _build_token_to_slot_map_from_bios(bios: list[dict]) -> dict[str, str]:
    """Return {source_phrase: slot_name} for every slot row across all
    bios. Authoritative — bio outfits are slot-decomposed in the DB."""
    out: dict[str, str] = {}
    for b in bios or []:
        for outfit_key in ("default_outfit", "user_requested_outfit"):
            outfit = b.get(outfit_key) or {}
            for slot_row in outfit.get("slots") or []:
                phrase = (slot_row.get("source_phrase") or "").strip()
                slot = (slot_row.get("slot") or "").strip()
                if phrase and slot:
                    out.setdefault(phrase, slot)
    return out


def _slot_for_outfit_token(token: str,
                            bio_token_to_slot: dict[str, str]) -> str:
    """Resolve a // Outfit token to its slot. Tries the bio's
    slot-decomposed outfit first (authoritative for default/named
    outfits); falls back to the natlang side's
    `_resolve_slot_from_phrase` for user-added items the bio doesn't
    know about (e.g. red_socks added last turn). Returns "" when no
    confident slot can be inferred — caller treats unknown-slot tokens
    as un-droppable to avoid false positives."""
    direct = bio_token_to_slot.get(token)
    if direct:
        return direct
    try:
        from .natlang_facts import _resolve_slot_from_phrase
    except Exception:
        return ""
    inferred = _resolve_slot_from_phrase(token.replace("_", " "))
    return inferred or ""


# Legacy strip post-pass deleted — the patch system prompt now owns
# `wearing only X` semantics directly (emit only the named item; let
# `_enforce_default_outfit_negation` auto-push dropped default-outfit
# slots to negatives). The previous implementation classified the strip
# target via a hand-curated `_SLOT_KEYWORDS` dict; any garment word not
# in the dict (nightgown, negligee, etc.) silently dropped the user's
# named item along with everything else. Replaced with model reasoning.


def _apply_modifier_clear_post_pass(sections: list[dict],
                                     user_request: str,
                                     bios: list[dict],
                                     request_id: str) -> list[dict]:
    """When the user_request alias-matches a slot_modifier with
    `clears_slots`, drop // Outfit tokens whose slot is in that set.
    Mirrors the natlang side's `update_active_modifiers_from_slots`
    propagation for tag mode.

    Pose-domain modifiers (`is_substitute_section=pose`) are NOT a
    drop trigger here — `_enforce_applies_modifiers` already adds
    those to // Pose; outfit slots are unaffected by pose-only
    modifiers.

    Alias-only by design — `modifier_search` semantic hits are used
    upstream as hints to the LLM (`[SEMANTIC]` modifier rows in the
    user message), never as deterministic clear triggers. e.g. user
    typing `wearing only red socks` cosine-hits `barefoot` at ~0.7
    because "socks" is footwear-adjacent vocabulary; treating that
    as a clear trigger would drop the user's just-added red_socks.
    `_enforce_applies_modifiers` follows the same alias-only rule for
    the same reason.

    Skip cases:
      - No alias-detected modifier
      - No clears_slots in any detected modifier"""
    if not user_request:
        return sections

    detected = _detect_modifiers_in_text(user_request) or []
    all_mods = list(detected)

    cleared_slots: set[str] = set()
    for m in all_mods:
        # Only outfit-domain modifiers drive outfit clears. Pose-domain
        # modifiers (is_substitute=True, substitute_section=pose) like
        # `presenting_foot` list legwear/footwear in their clears_slots
        # purely as correlation hints; they don't actually require the
        # legs/feet to be bare. Treating them as drop triggers would
        # remove user-added socks every time they're presented to the
        # camera. Same guard as `_resolve_slot_displacements`.
        if m.get("is_substitute") and m.get("substitute_section") == "pose":
            continue
        for slot in m.get("clears_slots") or []:
            slot_norm = (slot or "").strip()
            if slot_norm:
                cleared_slots.add(slot_norm)

    if not cleared_slots:
        return sections

    bio_map = _build_token_to_slot_map_from_bios(bios)

    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        header_lc = (s.get("header") or "").lower().strip()
        if not header_lc.startswith("// outfit"):
            continue
        kept: list[str] = []
        for t in s.get("tokens") or []:
            t_slot = _slot_for_outfit_token(t, bio_map)
            if t_slot and t_slot in cleared_slots:
                # Don't drop the modifier's own canonical tag if the
                # LLM placed it in // Outfit (barefoot is a footwear-
                # slot canonical that lives in outfit; we want to keep
                # it as the substitute). Same idea: any outfit token
                # whose value matches a detected modifier's canonical
                # is the modifier itself, not a slot fill.
                if t in {(m.get("canonical_tag") or "") for m in all_mods}:
                    kept.append(t)
                    continue
                dropped.append(t)
                continue
            kept.append(t)
        s["tokens"] = kept

    if dropped:
        dbg.info(
            "ai-patch[%s] modifier-clear post-pass dropped from // Outfit: "
            "%s (cleared_slots=%s)",
            request_id, ", ".join(dropped),
            ", ".join(sorted(cleared_slots)),
        )
    return sections


# Phrases that signal the user wants to ADD onto existing pose, not
# replace it. When any of these prefix the request, the pose-anchor-
# override post-pass skips entirely. Distinct from posture-verb
# detection — the user can still type a posture verb, but `include
# sitting with legs up` is structurally additive.
_ADDITIVE_PREFIX_RE = re.compile(
    r"^\s*(?:include|also|add(?:itionally)?|plus|and)\b",
    re.IGNORECASE,
)


def _apply_pose_anchor_override_post_pass(sections: list[dict],
                                            user_request: str,
                                            node_prompt: str,
                                            request_id: str) -> list[dict]:
    """When the user_request contains a posture verb (`standing up`,
    `kneeling`, `sitting`, …), drop bio-framing pose tokens that were
    preserved from node_prompt. Mirrors natlang's `is_anchor_override=
    True` + `replaces_all=True` on PoseChangeDelta — but with two
    important nuances borrowed from the natlang state model:

    1. Pose-domain slot_modifier canonicals (`presenting_foot`,
       `foot_focus`, …) survive. The natlang side keeps these in
       `pose_modifiers` which is NOT wiped by `replaces_all=True`
       (only `descriptive_facts` is). They describe persistent pose
       facts — `presenting_foot` is true regardless of whether the
       character is sitting, standing, or kneeling. Wiping them on
       any posture change destroys legitimate cross-posture pose
       state.

    2. Additive prefixes (`include X`, `also X`, `add X`) skip the
       override entirely. Even with a posture verb, the user is
       explicitly asking to ADD, not REPLACE.

    Tokens NOT in node_prompt survive in any case — those are the
    LLM's new emissions for this turn (the user's posture verb,
    pose modifiers added by `_enforce_applies_modifiers` this turn).

    Skip cases:
      - No posture verb in user_request
      - User_request starts with an additive prefix
      - No node_prompt (build mode — nothing to override)"""
    if not user_request or not node_prompt:
        return sections
    if _ADDITIVE_PREFIX_RE.search(user_request):
        return sections
    try:
        from .ai_request_parser import _POSTURE_VERBS
    except Exception:
        return sections

    user_lc = user_request.lower()
    has_posture_verb = any(
        re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", user_lc)
        for v in _POSTURE_VERBS
    )
    if not has_posture_verb:
        return sections

    node_pos_set, _ = _node_prompt_token_sets(node_prompt)
    if not node_pos_set:
        return sections

    # Pose-domain slot_modifier canonicals survive the override.
    pose_modifier_canonicals: set[str] = set()
    try:
        for m in _load_slot_modifiers() or []:
            if m.get("is_substitute") and m.get("substitute_section") == "pose":
                canon = (m.get("canonical_tag") or "").strip()
                if canon:
                    pose_modifier_canonicals.add(canon)
    except Exception:
        pass

    # Tokens the user explicitly re-typed this turn must survive even
    # if they happen to also be in node_prompt. `sitting with legs up`
    # on a prompt that already had `sitting` in pose should not drop
    # `sitting` — the user just confirmed it.
    user_lc_compact = re.sub(r"[\s_\-]+", " ", user_lc).strip()

    def _user_mentioned(token: str) -> bool:
        tc = re.sub(r"[\s_\-]+", " ", token.lower()).strip()
        if not tc:
            return False
        return tc in user_lc_compact

    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        header_lc = (s.get("header") or "").lower().strip()
        if not header_lc.startswith("// pose"):
            continue
        kept: list[str] = []
        for t in s.get("tokens") or []:
            t_canon = _canonicalize_token(t).lower()
            # Pose modifier canonicals always survive — cross-posture
            # facts the user established in a prior turn.
            if (t in pose_modifier_canonicals
                    or t_canon in pose_modifier_canonicals):
                kept.append(t)
                continue
            # User-mentioned-this-turn survives too — handles repeats
            # like `sitting with legs up` over an existing prompt that
            # already had `sitting`.
            if _user_mentioned(t) or _user_mentioned(t_canon):
                kept.append(t)
                continue
            if t_canon and t_canon in node_pos_set:
                dropped.append(t)
                continue
            kept.append(t)
        s["tokens"] = kept

    if dropped:
        dbg.info(
            "ai-patch[%s] pose-anchor-override dropped preserved pose "
            "tokens: %s",
            request_id, ", ".join(dropped),
        )
    return sections


def _resolve_slot_displacements(user_request: str,
                                 node_prompt: str = "") -> set[str]:
    """Return the canonical_tags of modifiers displaced by the user
    explicitly filling a slot the modifier covers. Inverse of
    _resolve_modifier_clears: 'wearing white socks' fills legwear, and
    `barefoot` (clears_slots=[footwear, legwear], substitute_section=
    outfit) is now displaced — but ONLY if `barefoot` was actually
    active before this turn. The signal we have for that is the
    modifier's alias phrases (`barefoot`, `bare feet`, `no shoes`...)
    appearing in `node_prompt`.

    Without the node_prompt gate, this function listed every modifier
    whose clears_slots intersects the user's filled slot — for a
    fresh-build "blue socks" request that produced three alerts
    (`barefoot`, `nude`, `completely_nude`) telling the LLM to scrub
    phrases that never existed in any prior state. That's stuffing the
    system prompt with hypotheticals, which the user explicitly flagged
    as cheating.

    Only outfit-section modifiers count. `presenting_foot` is a pose
    that *implies* legwear/footwear are empty and lists those in
    clears_slots, but the pose itself is compatible with the user
    adding white_socks — the character can present a sock-clad foot.
    Pose-domain modifiers must not be stripped just because the user
    added an item in a slot the pose merely correlates with.

    Self-collision guard: if the user explicitly invoked the modifier
    (e.g. `remove brown boots, add barefoot`), the suffix scanner sees
    `boots` and flags footwear as filled, then the displacement loop
    would scrub `barefoot` itself because barefoot.clears includes
    footwear. The fix is to skip any modifier the user named directly
    via `_detect_modifiers_in_text` — that match means the modifier IS
    the user's intent, not a victim of displacement."""
    user_slots = _detect_user_filled_slots(user_request)
    if not user_slots:
        return set()
    explicit_modifier_tags = {
        (m.get("canonical_tag") or "").lower()
        for m in _detect_modifiers_in_text(user_request)
    }
    node_prompt_lc = (node_prompt or "").lower()
    out: set[str] = set()
    for mod in _load_slot_modifiers():
        section = (mod.get("substitute_section") or "").strip().lower()
        if section != "outfit":
            continue
        canon = (mod.get("canonical_tag") or "").lower()
        if canon and canon in explicit_modifier_tags:
            continue
        clears = {(s or "").lower() for s in (mod.get("clears_slots") or [])}
        if not (clears & user_slots):
            continue
        # Real-displacement gate: only count the modifier as displaced
        # if its alias phrases actually appear in node_prompt. Without
        # node_prompt (build mode) there's nothing to displace.
        if not node_prompt_lc:
            continue
        aliases = list(mod.get("aliases") or [])
        if canon:
            aliases = [canon.replace("_", " "), canon] + aliases
        present = False
        for alias in aliases:
            alias_lc = (alias or "").strip().lower()
            if not alias_lc:
                continue
            if re.search(
                rf"(?<!\w){re.escape(alias_lc)}(?!\w)", node_prompt_lc,
            ):
                present = True
                break
        if present and canon:
            out.add(canon)
    return out


def _resolve_modifier_clears(bios: list[dict], user_request: str) -> list[dict]:
    """Apply [APPLIES] modifier `clears_slots` server-side: when a modifier
    like `barefoot` fires with clears=[footwear, legwear], drop matching
    bio outfit slots BEFORE the LLM sees them and record their source
    phrases as displaced so negation can negate them.

    Smaller backing models (qwen3-vl:8b-instruct) often skip the slot-
    clear rule; doing it deterministically here makes the modifier
    contract self-enforcing regardless of model capability.

    Mutates bios in place; returns same list."""
    if not bios or not user_request:
        return bios
    detected = _detect_modifiers_in_text(user_request)
    if not detected:
        return bios
    cleared_slot_names: set[str] = set()
    for d in detected:
        for s in (d.get("clears_slots") or []):
            s = (s or "").strip().lower()
            if s:
                cleared_slot_names.add(s)
    if not cleared_slot_names:
        return bios

    for b in bios:
        default = b.get("default_outfit")
        if not default:
            continue
        slots = default.get("slots") or []
        if not slots:
            continue
        kept: list[dict] = []
        displaced: list[str] = []
        for slot in slots:
            slot_name = (slot.get("slot") or "").strip().lower()
            if slot_name in cleared_slot_names:
                phrase = (slot.get("source_phrase") or "").strip()
                if phrase:
                    displaced.append(phrase)
            else:
                kept.append(slot)
        if displaced:
            default["slots"] = kept
            existing = list(b.get("_displaced_phrases") or [])
            for p in displaced:
                if p not in existing:
                    existing.append(p)
            b["_displaced_phrases"] = existing
            # Also strip the cleared phrases from the comma-separated
            # `outfit_tags` string so the model doesn't re-emit them
            # from that field.
            tags_str = (default.get("tags") or "").strip()
            if tags_str:
                disp_canon = {_canonicalize_token(p) for p in displaced}
                kept_tags = [
                    t.strip() for t in tags_str.split(",") if t.strip()
                    and _canonicalize_token(t.strip()) not in disp_canon
                ]
                default["tags"] = ", ".join(kept_tags)
    return bios


def _normalize_separators(s: str) -> str:
    """Lowercase and collapse hyphen/space/underscore separator runs to a
    single `_`. Both bio names and intent text are normalized this way
    before fuzzy matching, so `chun-li`, `chun_li`, and `chun li` all
    match interchangeably regardless of which form decompose emitted on
    a given stochastic turn."""
    return re.sub(r"[\s\-_]+", "_", (s or "").lower().strip())


def _bio_name_forms(bio: dict) -> set[str]:
    """Separator-normalized name-form variants. All forms come back
    underscore-collapsed; intent text is normalized via
    `_normalize_separators` the same way before substring check.
    Includes paren-suffix-stripped form (`mythra_(xenoblade)` →
    `mythra`) and first-token form (`cammy` from `cammy_white`)."""
    out: set[str] = set()
    tag = (bio.get("tag") or "").lower().strip()
    display = (bio.get("display") or "").lower().strip()
    for s in (tag, display):
        if not s:
            continue
        out.add(_normalize_separators(s))
        # Strip parenthetical disambiguation suffix.
        bare = re.sub(r"\s*\([^)]*\)", "", s).strip()
        if bare and bare != s:
            out.add(_normalize_separators(bare))
        # First-token form (handles `cammy white` matching bio
        # `cammy_white`, etc.).
        words = re.split(r"[_\s\-]+", s)
        if words and words[0]:
            out.add(_normalize_separators(words[0]))
    return {f for f in out if f}


def _resolve_bio_roles(bios: list[dict], sub_intents: list[dict],
                        request_id: str,
                        user_request: str = "") -> list[dict]:
    """Borrow attribution for outfit AND pose. Identify donor bios — a
    character whose OUTFIT or POSE is being borrowed onto a different
    subject — re-attach the borrowed data to the subject, and drop the
    donor from the bios list so multi-character composition doesn't
    false-positive `2girls`.

    A bio is an OUTFIT-DONOR when no `character:` intent contains its
    name AND an `outfit:` or `strip:` intent contains a possessive-form
    reference to it (`name's`, `name s'`). Pose-donor mirrors this on
    `pose:` intents.

    A single donor bio can play both roles ("cammy in pyra's outfit
    doing pyra's victory pose") — pyra contributes both her outfit and
    her matched_pose to cammy.

    A bio that's possessive-named in donor text but ALSO appears in a
    `character:` intent stays a subject (safer default — over-emit
    rather than wrongly drop a render target).

    v1 scope: 1 subject + ≤1 outfit-donor + ≤1 pose-donor (covers the
    cross_char harness scenarios `cammy in chunli's outfit with mythra's
    pose`). When we detect more than one of either donor type or more
    than one subject, bail and let multi-char composition handle it.

    Mutates the chosen subject in place; returns subjects-only list."""
    if not bios or not sub_intents:
        return bios

    char_intents = [i for i in sub_intents
                    if (i.get("section") or "").lower() == "character"]
    outfit_intents = [i for i in sub_intents
                      if (i.get("section") or "").lower() in ("outfit", "strip")]
    pose_intents = [i for i in sub_intents
                    if (i.get("section") or "").lower() == "pose"]

    if not char_intents or (not outfit_intents and not pose_intents):
        return bios

    char_texts = [_normalize_separators(i.get("text") or "")
                  for i in char_intents]
    outfit_texts = [_normalize_separators(i.get("text") or "")
                    for i in outfit_intents]
    pose_texts = [_normalize_separators(i.get("text") or "")
                  for i in pose_intents]
    # Decompose occasionally drops the possessive form when extracting
    # an outfit/pose intent (`cammy in pyra's red outfit` -> `[outfit]
    # red outfit` — pyra elided). Without this fallback the bio for
    # pyra falls through to "ambiguous -> subject" and the borrow
    # never fires. Use the original user_request as a secondary signal
    # for possessive-form classification when the intent text alone is
    # insufficient.
    user_text_norm = _normalize_separators(user_request or "")

    def _possessive_in(forms: set[str], intent_texts: list[str]) -> bool:
        # All possessive shapes after separator normalization:
        # `name's`, `name_'s`, `names'`. The middle form catches
        # `name 's` (space before apostrophe) once normalized.
        for t in intent_texts:
            for form in forms:
                possessives = (f"{form}'s", f"{form}_'s", f"{form}s'")
                if any(p in t for p in possessives):
                    return True
        return False

    forms_by_id: dict[int, set[str]] = {}
    for b in bios:
        if b and b.get("tag"):
            forms_by_id[id(b)] = _bio_name_forms(b)

    subjects: list[dict] = []
    outfit_donor: dict | None = None
    pose_donor: dict | None = None
    extra_donor_count = 0

    for b in bios:
        if not b or not b.get("tag"):
            subjects.append(b)
            continue
        forms = forms_by_id.get(id(b), set())

        is_subject = any(
            form in t for t in char_texts for form in forms
        )
        if is_subject:
            subjects.append(b)
            continue

        is_outfit_donor = _possessive_in(forms, outfit_texts)
        is_pose_donor = _possessive_in(forms, pose_texts)

        # Fallback: decompose lost the possessive — check raw
        # user_request. Only fires when the bio's possessive form
        # is in the user's text directly (not just in the elided
        # intent). Classify by which intent types exist: outfit-only
        # -> outfit_donor; pose-only -> pose_donor; both -> outfit
        # (heuristic — outfit is the more common borrow shape and
        # the user can disambiguate by re-saying if wrong).
        if (not is_outfit_donor and not is_pose_donor
                and _possessive_in(forms, [user_text_norm])):
            if outfit_intents and not pose_intents:
                is_outfit_donor = True
            elif pose_intents and not outfit_intents:
                is_pose_donor = True
            elif outfit_intents and pose_intents:
                is_outfit_donor = True

        if not is_outfit_donor and not is_pose_donor:
            subjects.append(b)  # ambiguous → safer default
            continue

        if is_outfit_donor:
            if outfit_donor is None:
                outfit_donor = b
            else:
                extra_donor_count += 1
        if is_pose_donor:
            if pose_donor is None:
                pose_donor = b
            else:
                extra_donor_count += 1

    if outfit_donor is None and pose_donor is None:
        return bios

    if len(subjects) != 1 or extra_donor_count > 0:
        dbg.info(
            "ai-patch[%s] bio-role: %d subject(s), outfit_donor=%s "
            "pose_donor=%s extra=%d — multi-subject borrow not yet "
            "supported, treating all as subjects",
            request_id, len(subjects),
            (outfit_donor or {}).get("tag"),
            (pose_donor or {}).get("tag"),
            extra_donor_count,
        )
        return bios

    subject = subjects[0]
    transferred: list[str] = []

    if outfit_donor is not None:
        donor_outfit = (outfit_donor.get("user_requested_outfit")
                        or outfit_donor.get("default_outfit"))
        if donor_outfit and not subject.get("user_requested_outfit"):
            subject["user_requested_outfit"] = dict(donor_outfit)
            transferred.append(
                f"outfit={donor_outfit.get('name') or '(unnamed)'} "
                f"from {outfit_donor.get('tag')}"
            )

    if pose_donor is not None:
        donor_pose = pose_donor.get("matched_pose")
        if donor_pose and not subject.get("matched_pose"):
            subject["matched_pose"] = dict(donor_pose)
            transferred.append(
                f"pose={donor_pose.get('name') or '(unnamed)'} "
                f"from {pose_donor.get('tag')}"
            )

    if transferred:
        dbg.info(
            "ai-patch[%s] bio-role: subject=%s borrows %s",
            request_id, subject.get("tag"), "; ".join(transferred),
        )
    return subjects


def _drop_displaced_modifiers(sections: list[dict], displaced: set[str],
                              request_id: str) -> list[dict]:
    """Scrub modifier canonical tags from positive sections when the
    user has explicitly filled a slot they cover. Catches the case
    where patch mode preserves a modifier from node_prompt (e.g.
    `barefoot`) but the user's request fills the same slot with a
    real item (e.g. `wearing white socks`). Without this scrub the
    output contains both, which is contradictory."""
    if not displaced:
        return sections
    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        kept: list[str] = []
        for t in s.get("tokens") or []:
            tag = _bare_form(t).replace(" ", "_").lower()
            if tag in displaced:
                dropped.append(t)
                continue
            kept.append(t)
        s["tokens"] = kept
    if dropped:
        dbg.info(
            "ai-patch[%s] slot-displacement scrubbed: %s",
            request_id, ", ".join(dropped),
        )
    return sections


_PRIOR_CHAR_HEADER_RE = re.compile(
    r"^\s*//\s*Character\s*:\s*([^\n]+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _enforce_character_swap(
    sections: list[dict], bios: list[dict],
    sub_intents: list[dict] | None, request_id: str,
) -> list[dict]:
    """Bios is the truth signal for who's in the rendered scene. Any
    `// Character: X` section whose canonical isn't in bios is stale.

    Two failure shapes the 8B produces:
      1. Echoes prior char's section AND appends new one (two // Character
         blocks, second one matches bios). We drop the stale one.
      2. Echoes ONLY the prior char's section verbatim because patch-mode
         preservation said to preserve existing sections (single stale
         // Character block, no replacement). When bios has an "orphan"
         canonical not yet represented anywhere, we RENAME the stale
         section's header to the orphan canonical and rebuild the body
         from the bio so the swap actually completes. Without this the
         section just gets dropped and the editor loses its character.

    Doesn't gate on sub_intents — decompose output for `change
    character to X` / `switch character to X` / `swap with X` / etc.
    varies stochastically, so trusting bios is robust across phrasings.

    For legitimate multi-char additions (`cammy and chun-li sparring`),
    the agent dispatches BOTH in character_queries so bios has both —
    neither // Character section is stale."""
    if not sections or not bios:
        return sections

    new_canons: list[str] = []
    bio_by_canon: dict[str, dict] = {}
    for b in bios:
        tag = (b.get("tag") or "").strip()
        if not tag:
            continue
        canon = _normalize_separators(tag)
        if canon and canon not in bio_by_canon:
            new_canons.append(canon)
            bio_by_canon[canon] = b
    if not new_canons:
        return sections

    represented: set[str] = set()
    for s in sections:
        header = s.get("header") or ""
        m = re.match(
            r"^\s*//\s*Character\s*:\s*(.+?)\s*$", header, re.IGNORECASE,
        )
        if m:
            section_canon = _normalize_separators(m.group(1))
            if section_canon in bio_by_canon:
                represented.add(section_canon)
    orphan_canons = [c for c in new_canons if c not in represented]

    out: list[dict] = []
    dropped_canons: list[str] = []
    renamed: list[tuple[str, str]] = []
    for s in sections:
        header = s.get("header") or ""
        m = re.match(
            r"^\s*//\s*Character\s*:\s*(.+?)\s*$", header, re.IGNORECASE,
        )
        if m:
            section_canon = _normalize_separators(m.group(1))
            if section_canon and section_canon not in bio_by_canon:
                if orphan_canons:
                    target_canon = orphan_canons.pop(0)
                    target_bio = bio_by_canon[target_canon]
                    weighted = _bio_weighted_tag_for(target_canon, [target_bio])
                    new_tokens = [weighted] if weighted else []
                    out.append({
                        **s,
                        "header": f"// Character: {target_canon}",
                        "tokens": new_tokens,
                    })
                    renamed.append((section_canon, target_canon))
                    continue
                dropped_canons.append(section_canon)
                continue
        out.append(s)

    if dropped_canons or renamed:
        dbg.info(
            "ai-patch[%s] character-swap: renamed=%s dropped=%s bios=%s",
            request_id,
            ", ".join(f"{a}->{b}" for a, b in renamed) or "none",
            ", ".join(dropped_canons) or "none",
            ", ".join(sorted(bio_by_canon.keys())),
        )
    return out


def _scrub_prior_character_default_negs(
    sections: list[dict], bios: list[dict], node_prompt: str,
    request_id: str,
) -> list[dict]:
    """On a character-swap patch, remove preserved negative tokens that
    were auto-added by a prior turn's `_enforce_default_outfit_negation`
    against the OLD character's default-outfit slot phrases. Without
    this scrub, a swap from cammy_white -> mythra_(xenoblade) leaves
    cammy's `green leotard, beret, red gloves, ...` in the negative
    prompt forever.

    Detection: prior // Character header in node_prompt names a
    canonical that's absent from the new bios list. If so, query the DB
    for the prior character's default-outfit slot source_phrases and
    drop any matching tokens from the negative prompt. The new
    character's default-outfit negs (added by
    `_enforce_default_outfit_negation` upstream) survive untouched."""
    if not bios or not node_prompt:
        return sections

    new_canons = {
        (b.get("tag") or "").lower().strip()
        for b in bios if b and b.get("tag")
    }
    if not new_canons:
        return sections

    prior_match = _PRIOR_CHAR_HEADER_RE.search(node_prompt)
    if not prior_match:
        return sections
    prior_canon = prior_match.group(1).strip().lower()
    prior_canon = re.sub(r"\s+", " ", prior_canon).replace(" ", "_")
    if not prior_canon or prior_canon in new_canons:
        return sections

    try:
        from .tag_builder import get_db
        db = get_db()
        rows = db.execute(
            "SELECT ots.source_phrase FROM outfit_tag_slots ots "
            "JOIN outfits o ON ots.outfit_id = o.id "
            "WHERE LOWER(o.character_tag) = ? AND o.is_default = 1",
            (prior_canon,),
        ).fetchall()
    except Exception:
        logger.warning(
            "ai-patch[%s] prior-char neg scrub: DB lookup failed for %s",
            request_id, prior_canon, exc_info=True,
        )
        return sections

    prior_phrases: set[str] = set()
    for r in rows:
        phrase = (r["source_phrase"] or "").strip()
        if phrase:
            prior_phrases.add(_canonicalize_token(phrase).lower())
    if not prior_phrases:
        return sections

    neg = next((s for s in sections if s.get("is_negative")), None)
    if neg is None or not neg.get("tokens"):
        return sections

    kept: list[str] = []
    scrubbed: list[str] = []
    for t in neg["tokens"]:
        if _canonicalize_token(t).lower() in prior_phrases:
            scrubbed.append(t)
        else:
            kept.append(t)
    if scrubbed:
        neg["tokens"] = kept
        dbg.info(
            "ai-patch[%s] prior-char neg scrub: char swap %s -> %s "
            "removed %d stale negs: %s",
            request_id, prior_canon,
            ", ".join(sorted(new_canons)),
            len(scrubbed), ", ".join(scrubbed),
        )
    return sections


def _enforce_default_outfit_negation(sections: list[dict], bios: list[dict]) -> list[dict]:
    """For each bio, diff the trained-canonical default outfit's slot
    source_phrases against the AI's // Outfit section. Any phrase NOT in
    the active outfit goes into Negative Prompt automatically — the model
    has the character heavily entangled with their trained default in
    latent space, so picking a different outfit needs explicit pushback
    or default items leak into the image (e.g. user picks Killer Bee but
    `green_leotard`/`beret` show up because Delta Red is Cammy's trained
    default).

    Reads `canonical_default_slot_phrases` (always shipped by
    `_api_match_characters` regardless of which outfit was chosen).
    Diffs against the LIVE outfit section the LLM produced — when the
    active outfit IS the default, every default phrase is present and
    the diff is empty (correct: nothing to negate)."""
    if not bios:
        return sections

    # Collect every canonical-default source_phrase across all bios. Also
    # include phrases displaced by the server-side color-swap pre-pass
    # (_resolve_color_swaps) so a swap like green_leotard -> orange_leotard
    # negates the original even though the model emitted the new one.
    expected: list[str] = []
    for b in bios:
        canonical_phrases = b.get("canonical_default_slot_phrases") or []
        if not canonical_phrases:
            # Fallback for bios that came from a path that didn't populate
            # canonical_default (older clients, harness bios that haven't
            # been updated). Old behavior: only fire for default_outfit.
            default = b.get("default_outfit")
            if default and not b.get("user_requested_outfit"):
                for s in default.get("slots") or []:
                    phrase = (s.get("source_phrase") or "").strip()
                    if phrase:
                        expected.append(phrase)
        else:
            for phrase in canonical_phrases:
                phrase = (phrase or "").strip()
                if phrase and phrase not in expected:
                    expected.append(phrase)
        for phrase in (b.get("_displaced_phrases") or []):
            phrase = (phrase or "").strip()
            if phrase and phrase not in expected:
                expected.append(phrase)
    if not expected:
        return sections

    # Find what's in the AI's outfit section(s).
    outfit_canon: set[str] = set()
    for s in sections:
        if s.get("is_negative"):
            continue
        header = (s.get("header") or "").lstrip("/").strip().lower()
        if header.startswith("outfit"):
            for t in s.get("tokens", []):
                outfit_canon.add(_canonicalize_token(t))

    # Anything expected but not present in outfit → it was dropped → negate.
    dropped = [p for p in expected if _canonicalize_token(p) not in outfit_canon]
    # Anything expected AND present in outfit → user has the canonical
    # piece active (either always was, or just brought back). If a prior
    # turn added it to Negative as a displacement backstop, scrub it now —
    # otherwise `_dedupe_negatives_from_positives` would kill the positive
    # the user just asked for. The LLM preserves Negative verbatim across
    # turns per the patch system prompt, so without this scrub the
    # displaced entry persists across reverts.
    restored = {_canonicalize_token(p) for p in expected
                if _canonicalize_token(p) in outfit_canon}
    neg = next((s for s in sections if s.get("is_negative")), None)
    if neg is not None and restored:
        kept = []
        scrubbed = []
        for t in neg.get("tokens", []):
            if _canonicalize_token(t) in restored:
                scrubbed.append(t)
            else:
                kept.append(t)
        if scrubbed:
            neg["tokens"] = kept

    if not dropped:
        return sections

    # Find or create the Negative Prompt section.
    if neg is None:
        neg = {"header": "Negative Prompt:", "tokens": [], "is_negative": True}
        sections.append(neg)
    existing_canon = {_canonicalize_token(t) for t in neg.get("tokens", [])}
    for phrase in dropped:
        if _canonicalize_token(phrase) not in existing_canon:
            neg["tokens"].append(phrase)
            existing_canon.add(_canonicalize_token(phrase))

    # Weight outfit replacements that share an item-suffix with a negated
    # canonical default. For "make boots red" against Cammy's SF2 Classic:
    # `white_boots` lands in Negative as the trained-default backstop, but
    # the LLM-emitted `red_boots` in // Outfit is unweighted — SDXL's
    # latent pull toward the trained default plus the negative push of
    # `white_boots` outweighs an unweighted positive, so the new color
    # silently drops out. Weighting it to 1.1 lets the swap actually win.
    # Match by last word (boots ↔ boots, dress ↔ dress) — same slot in
    # spirit; safer than slot-decomposition lookups that not every bio
    # carries.
    def _item_suffix(phrase: str) -> str:
        parts = (phrase or "").lower().replace("_", " ").split()
        return parts[-1] if parts else ""
    dropped_items = {_item_suffix(p) for p in dropped if _item_suffix(p)}
    if dropped_items:
        for s in sections:
            if s.get("is_negative"):
                continue
            header = (s.get("header") or "").lstrip("/").strip().lower()
            if not header.startswith("outfit"):
                continue
            new_tokens = []
            for t in s.get("tokens", []):
                if _WEIGHTED_TOKEN_RE.match(t):
                    new_tokens.append(t)
                    continue
                if _item_suffix(t) in dropped_items:
                    new_tokens.append(f"({t}:1.1)")
                else:
                    new_tokens.append(t)
            s["tokens"] = new_tokens
    return sections


def _dedupe_negatives_from_positives(sections: list[dict]) -> list[dict]:
    """If a phrase appears in the Negative Prompt section, remove it from every
    other section. Negative is authoritative for 'this must not be in positives'."""
    neg_canon: set[str] = set()
    for s in sections:
        if s.get("is_negative"):
            for t in s.get("tokens", []):
                neg_canon.add(_canonicalize_token(t))
    if not neg_canon:
        return sections
    for s in sections:
        if s.get("is_negative"):
            continue
        s["tokens"] = [t for t in s.get("tokens", [])
                       if _canonicalize_token(t) not in neg_canon]
    # Drop sections that became empty.
    return [s for s in sections if s.get("is_negative") or s.get("tokens")]


# Editorial qualifier in Danbooru wiki glosses ("…in a deliberately
# suggestive or lewd manner.") biases the AI against applying the
# modifier in non-NSFW contexts even when the visual concept matches
# (e.g. presenting_foot for "lifting up their legs and pointing feet"
# — model refused because the request "isn't suggestive"). The visual
# concept is what matters; we strip the editorial framing when surfacing
# the gloss to the AI without modifying the DB (faithful to wiki source).
_LEWD_QUALIFIER_RE = re.compile(
    r"\s+in a (?:[a-z]+\s+)*(?:suggestive|lewd|sexual|erotic|provocative|pornographic)\s+manner\.?",
    re.IGNORECASE,
)


def _neutralize_gloss(text: str) -> str:
    if not text:
        return text or ""
    cleaned = _LEWD_QUALIFIER_RE.sub(".", text)
    return re.sub(r"\.\.+$", ".", cleaned).strip()


def _is_tag_form(token: str) -> bool:
    """True if the token looks like a canonical Danbooru tag — has an
    underscore outside of paren/escape syntax. `presenting_foot` and
    `(legs_up:1.1)` are tag-form; `looking sad` and `bare feet` are
    phrase-form. Used by trace-check to apply stricter rules to tokens
    that claim canonical-tag identity."""
    if not token:
        return False
    s = token.strip()
    m = _WEIGHTED_TOKEN_RE.match(s)
    if m:
        s = m.group(1)
    s = s.replace("\\(", "").replace("\\)", "")
    return "_" in s


# Tag-group → preferred prompt section mapping. Source data is Danbooru's
# tag_group:* wikis (CC0); each enumerates the canonical tags belonging
# to a semantic domain. We map domain → which prompt section the tag
# belongs in, so the model emitting a posture tag (`sitting`, `legs_up`)
# under // Character gets the misplaced token dropped at output stage.
#
# A single tag in multiple groups uses first-match-wins by iteration
# order (more specific section listed first). Tags in NO group are
# left ambiguous — no constraint applied.
_TAG_GROUP_TO_SECTION = {
    "gestures": "pose",      # pointing, waving, salute, ...
    "posture": "pose",       # sitting, legs_up, kneeling, ...
    "attire": "outfit",      # clothing, accessories, footwear, ...
    "eyes_tags": "character",  # eye colors, eye shapes — appearance
    "locations": "setting",  # bedroom, forest, classroom, ...
}

# Per-tag overrides applied AFTER tag-group inference. The eyes_tags
# group contains both appearance tokens (`blue_eyes`, `red_eyes`) AND
# gaze-direction tokens (`looking_back`, `looking_at_viewer`, etc.) —
# only the appearance ones belong in // Character. Gaze and camera-
# framing tokens are POSE concepts (where the subject is looking,
# how the camera frames them). Without this override, the section-
# mismatch filter silently drops legitimate pose tokens emitted in
# // Pose, Action & Prop because their "canonical" section was
# misclassified as character.
_TAG_SECTION_OVERRIDES_TO_POSE: frozenset[str] = frozenset({
    # Gaze / head direction
    "looking_at_viewer", "looking_away", "looking_back", "looking_down",
    "looking_up", "looking_to_the_side", "looking_ahead", "looking_afar",
    "looking_around", "looking_outside", "looking_over_eyewear",
    "looking_through_own_legs", "sideways_glance",
    # Camera framing / subject orientation
    "from_behind", "from_above", "from_below", "from_side",
    # Body-part focus tags
    "ass_focus", "feet_focus", "foot_focus", "hand_focus",
    "breast_focus", "thigh_focus", "leg_focus", "back_focus",
    # Common confident-stance / dynamic-pose tokens that get mis-grouped
    "confident_stance",
})

# Tags whose Danbooru classification is too context-dependent to
# deterministically route. `glasses` is in the eyewear group (→ outfit)
# but also means drinking glasses (→ setting). When the model puts a
# context-ambiguous tag in any section, trust the model's contextual
# read rather than evicting against a fixed index.
_AMBIGUOUS_SECTION_TAGS: frozenset[str] = frozenset({
    "glasses",
})

_TAG_SECTION_INDEX_CACHE: dict[str, str] | None = None


def _build_tag_section_index() -> dict[str, str]:
    """Returns {tag_underscored: preferred_section_name}. Cached after
    first build; cleared by changing _TAG_GROUP_TO_SECTION (rare)."""
    global _TAG_SECTION_INDEX_CACHE
    if _TAG_SECTION_INDEX_CACHE is not None:
        return _TAG_SECTION_INDEX_CACHE
    out: dict[str, str] = {}
    for group_name, section in _TAG_GROUP_TO_SECTION.items():
        for tag in _load_tag_group(group_name):
            # First section wins for ambiguous tags (gestures listed
            # before posture so a gesture-and-posture overlap stays pose
            # either way; outfit/character/setting are mutually exclusive
            # in practice).
            out.setdefault(tag, section)
    # Modifier rules are authoritative for their canonical tag — override
    # any tag-group inference. presenting_foot's substitute_section=pose
    # is what tells _drop_misplaced_tokens to evict it from // Outfit
    # when the model puts it there.
    for m in _load_slot_modifiers():
        canon = (m.get("canonical_tag") or "").strip().lower()
        sec = (m.get("substitute_section") or "").strip().lower()
        if canon and sec:
            out[canon] = sec
    # Hard overrides for gaze / camera-framing tokens that the
    # eyes_tags group mis-classified as character. See
    # _TAG_SECTION_OVERRIDES_TO_POSE for the curated list.
    for tag in _TAG_SECTION_OVERRIDES_TO_POSE:
        out[tag] = "pose"
    # Strip context-ambiguous tags so section-mismatch never evicts them.
    for tag in _AMBIGUOUS_SECTION_TAGS:
        out.pop(tag, None)
    _TAG_SECTION_INDEX_CACHE = out
    return out


# Map prompt-section header text → canonical section key. Headers vary
# slightly ("// Pose, Action & Prop" vs "// Pose" vs "// Pose / Action").
_HEADER_PREFIXES = (
    ("// pose",       "pose"),
    ("// outfit",     "outfit"),
    ("// expression", "expression"),
    ("// setting",    "setting"),
    ("// scene",      "setting"),
    ("// character",  "character"),
    ("// style",      "style"),
    ("// quality",    "quality"),
    ("// aesthetic",  "quality"),
)


def _section_key_from_header(header: str) -> str | None:
    h = (header or "").lower().strip()
    for prefix, key in _HEADER_PREFIXES:
        if h.startswith(prefix):
            return key
    return None


def _drop_misplaced_tokens(sections: list[dict], request_id: str) -> list[dict]:
    """For each emitted token in a positive section, look up its preferred
    section from the tag_group → section map. If the model put it in a
    section that doesn't match, drop it from this section (don't try to
    move — the model also emitted it where it belongs in many cases, and
    keeping the wrong copy double-weights the tag in SD).

    Tokens with no entry in the index (most tags) pass through — we
    only constrain tags whose semantic domain is known."""
    index = _build_tag_section_index()
    if not index:
        return sections
    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        actual = _section_key_from_header(s.get("header") or "")
        if not actual:
            continue
        # Style sections are curated template content — descriptive
        # English (oil painting, 70s film grain, Fujifilm XT3) that
        # doesn't fit the Danbooru tag-group taxonomy. Skip section-
        # misplacement check for them.
        if actual == "style":
            continue
        kept = []
        for t in s.get("tokens", []):
            underscore = _bare_form(t).replace(" ", "_")
            preferred = index.get(underscore)
            if preferred and preferred != actual:
                dropped.append(f"{t} (in //{actual}, expected //{preferred})")
            else:
                kept.append(t)
        s["tokens"] = kept
    if dropped:
        dbg.info(
            "ai-patch[%s] section-mismatch dropped: %s",
            request_id, "; ".join(dropped),
        )
    return [s for s in sections if s.get("tokens") or s.get("is_negative")]


# Compositional-token validator data. Built lazily on first call by
# `_load_compositional_components`. Used by trace-check to permit
# tag-form compounds like `pink_polka_dot_leotard` whose pieces are all
# valid concepts even though the whole compound isn't in danbooru_tags.
_COMPOSITIONAL_CACHE: set[str] | None = None
# Min length for an individual component to count as "valid". Stops
# trivial fragments like `a`, `at`, `of` from rubber-stamping random
# compounds.
_COMPONENT_MIN_LEN = 3


def _load_compositional_components() -> set[str]:
    """Returns the union of all "valid component" tag forms used by
    `_decompose_token` to validate compositional tag-form tokens.

    Sources (every entry is lowercase, underscored, length >= 3):
      - clothing_colors, clothing_materials, clothing_patterns
      - furniture_colors, furniture_materials, furniture_patterns
      - clothing_items / appearance_items / pose_items / scene_items /
        expression_items / action_items / nsfw_action_items (item_tag)
      - danbooru_tags.tag (any category — we trust real tag forms)

    Cached at module level. Cleared whenever _CLOTHING_PREFIX_CACHE is
    cleared (they share sources)."""
    global _COMPOSITIONAL_CACHE
    if _COMPOSITIONAL_CACHE is not None:
        return _COMPOSITIONAL_CACHE
    out: set[str] = set()
    try:
        from .tag_builder import get_db
        db = get_db()
        # Color/material/pattern tables share a `tag` column convention.
        for table in (
            "clothing_colors", "clothing_materials", "clothing_patterns",
            "furniture_colors", "furniture_materials", "furniture_patterns",
        ):
            try:
                for r in db.execute(f"SELECT tag FROM {table}").fetchall():
                    t = (r["tag"] or "").strip().lower()
                    if len(t) >= _COMPONENT_MIN_LEN:
                        out.add(t)
            except Exception:
                pass
        for table in (
            "clothing_items", "appearance_items", "pose_items",
            "scene_items", "expression_items", "action_items",
            "nsfw_action_items",
        ):
            try:
                for r in db.execute(f"SELECT item_tag FROM {table}").fetchall():
                    t = (r["item_tag"] or "").strip().lower()
                    if len(t) >= _COMPONENT_MIN_LEN:
                        out.add(t)
            except Exception:
                pass
        try:
            # Tag-level: every real Danbooru tag with length >= 3 is a
            # valid component. `feet`, `leotard`, `barefoot` all land here.
            for r in db.execute(
                "SELECT tag FROM danbooru_tags WHERE LENGTH(tag) >= ?",
                (_COMPONENT_MIN_LEN,),
            ).fetchall():
                t = (r["tag"] or "").strip().lower()
                if t:
                    out.add(t)
            # Compositional sub-words: split every compound general-category
            # tag and admit each part as a component. `focus` isn't a
            # standalone tag but appears in 37 *_focus compounds (foot_focus,
            # breast_focus, hand_focus, ...) — it's real Danbooru
            # compositional vocabulary that just never exists alone. Same
            # for `hair`, `print`, `uniform`, color words, etc. Without
            # this, legitimate compositional inventions like `feet_focus`
            # fail trace-check even though every piece is valid by
            # Danbooru's own usage. ranking >= 200 keeps us on the
            # well-trodden compositional vocabulary; rare-tag noise stays
            # out.
            for r in db.execute(
                "SELECT tag FROM danbooru_tags "
                "WHERE category = 'general' AND ranking >= 200 "
                "AND tag LIKE '%\\_%' ESCAPE '\\'"
            ).fetchall():
                t = (r["tag"] or "").strip().lower()
                if not t:
                    continue
                for part in t.split("_"):
                    if len(part) >= _COMPONENT_MIN_LEN:
                        out.add(part)
        except Exception:
            pass
    except Exception:
        logger.warning("could not preload compositional components",
                       exc_info=True)
    _COMPOSITIONAL_CACHE = out
    return out


def _decompose_token(token_underscore: str,
                     components: set[str],
                     user_lc: str) -> bool:
    """Greedy left-to-right longest-prefix match: split on `_`, walk
    the parts list and consume the longest prefix that's a valid
    component at each step. Component is valid if it's in `components`
    OR appears as a contiguous substring in `user_lc`. Returns True
    only if every part is consumed.

    Examples (assuming standard tag-builder DB):
      pink_polka_dot_leotard  → pink + polka_dot + leotard ✓
      teal_leotard            → teal + leotard ✓
      feet_positioned_forward → feet + ??? → False
                                (positioned_forward isn't a component
                                and doesn't appear in user text)
      red_pantyhose_pointing  → red + pantyhose + pointing ✓
                                (technically passes but
                                _drop_misplaced_tokens catches the
                                wrong-domain composition)
    """
    if not token_underscore:
        return False
    parts = [p for p in token_underscore.split("_") if p]
    if not parts:
        return False
    i = 0
    while i < len(parts):
        matched = False
        for j in range(len(parts), i, -1):
            candidate = "_".join(parts[i:j])
            if len(candidate) < _COMPONENT_MIN_LEN:
                continue
            if candidate in components:
                i = j
                matched = True
                break
            # Allow user-typed phrases (with or without underscores) too.
            phrase = candidate.replace("_", " ")
            if phrase and phrase in user_lc:
                i = j
                matched = True
                break
        if not matched:
            return False
    return True


# Body-part word pairs where Danbooru sometimes uses plural, sometimes
# singular, in compound tags. Model occasionally mashes the wrong variant
# (saw `feet_only` and `foot_focus` in the menu, wrote `feet_focus` —
# not a real tag). When the emitted compound isn't in `allowed` but a
# single body-part swap lands in `allowed`, prefer the canonical variant.
# Compounds without these words (`red_micro_bikini`, `pink_polka_dot_leotard`)
# are untouched — no swap considered, original compositional path runs.
_BODY_PART_VARIANTS: list[tuple[str, str]] = [
    ("feet", "foot"),
    ("hands", "hand"),
    ("eyes", "eye"),
    ("fingers", "finger"),
    ("toes", "toe"),
    ("knees", "knee"),
    ("legs", "leg"),
    ("ears", "ear"),
    ("breasts", "breast"),
    ("thighs", "thigh"),
]


def _canonical_body_part_variant(token: str, allowed: set[str]) -> str | None:
    """If the token's bare form isn't in `allowed` but exactly one
    body-part-word substitution lands in `allowed`, return the corrected
    token (preserving weight wrappers via word-boundary replace). Else
    None. Single-substitution only — `feet_eyes_focus` won't get
    multi-edited into something arbitrary."""
    bare = _bare_form(token)
    if not bare or bare in allowed:
        return None
    parts = bare.split(" ")
    for i, p in enumerate(parts):
        for plural, singular in _BODY_PART_VARIANTS:
            if p == plural:
                candidate = " ".join(parts[:i] + [singular] + parts[i + 1:])
                if candidate in allowed:
                    return re.sub(
                        r"(?<![A-Za-z])" + re.escape(plural) + r"(?![A-Za-z])",
                        singular, token,
                    )
            if p == singular:
                candidate = " ".join(parts[:i] + [plural] + parts[i + 1:])
                if candidate in allowed:
                    return re.sub(
                        r"(?<![A-Za-z])" + re.escape(singular) + r"(?![A-Za-z])",
                        plural, token,
                    )
    return None


def _bare_form(token: str) -> str:
    """Canonical bare form for cross-source identity comparison: strip
    weight wrapper, escape backslashes, swap underscores to spaces,
    lowercase. `(cammy_white:1.1)` → `cammy white`. Used by trace-check
    and output-scope filters that need to compare emitted tokens against
    sets sourced from bios, modifier rows, danbooru_tags, etc."""
    canon = _canonicalize_token(token).lower()
    m = _WEIGHTED_TOKEN_RE.match(canon)
    if m:
        canon = m.group(1).strip()
    return canon


def _drop_untraceable_tokens(sections: list[dict], user_request: str,
                             bios: list[dict], all_modifiers: list[dict],
                             request_id: str,
                             node_prompt: str = "") -> list[dict]:
    """Trace-check: every emitted positive-section token must trace to
    one of these authoritative sources:

      - bio.base_tags or outfit slot source_phrase
      - modifier canonical_tag or implies_outfit_tag
      - any positive token already present in node_prompt (patch mode —
        the user's existing prompt is authoritative; preserved tokens
        must survive even if their compositional form was validated
        against an earlier turn's user text and lost that anchor here)
      - canonical-scan output for this request
      - a substring of the user's typed request (so freestyle phrasings
        the user wrote stay even if they aren't canonical Danbooru tags)
      - any row in the danbooru_tags table (any category, any ranking —
        even low-rank tags are real, just niche)

    Tokens matching none of those are model hallucinations and get
    dropped with a log line. Negative Prompt sections are skipped —
    they're explicit deletions, not generated content.

    The allowed set is rebuilt per request because canonical scan output
    and bios change every time. Single SQL query at the end batches the
    danbooru_tags lookup so this is fast."""
    allowed: set[str] = set()

    # 1. Bio base_tags + outfit tags / slot phrases (authoritative, verbatim).
    # The bio's outfit comes in two shapes:
    #   - slot-decomposed (slots list populated): each slot's source_phrase
    #     is the human-readable token shown to the model.
    #   - flat (slots empty, outfit['tags'] is the only source): the
    #     comma-separated string IS what the model sees.
    # Trace-check needs both — without flat-form coverage, characters
    # whose curated outfits aren't slot-decomposed (M. Bison's Shadaloo
    # Uniform: 'red military uniform, cape, cap, Shadaloo dictator
    # attire') get most of their tokens dropped because they're prose,
    # not Danbooru tags.
    for b in bios or []:
        for raw in (b.get("base_tags") or "").split(","):
            bare = _bare_form(raw)
            if bare:
                allowed.add(bare)
        outfit = b.get("user_requested_outfit") or b.get("default_outfit") or {}
        for slot in outfit.get("slots") or []:
            phrase = _bare_form(slot.get("source_phrase") or "")
            if phrase:
                allowed.add(phrase)
        for raw in (outfit.get("tags") or "").split(","):
            bare = _bare_form(raw)
            if bare:
                allowed.add(bare)

    # 2. Modifier canonical_tags + implies (alias output is always valid).
    for m in all_modifiers or []:
        canon = (m.get("canonical_tag") or "").replace("_", " ").lower()
        if canon:
            allowed.add(canon)
        implies = (m.get("implies_outfit_tag") or "").replace("_", " ").lower()
        if implies:
            allowed.add(implies)

    # 3. Existing node_prompt positives (patch mode). After format
    # conversion, a previously-validated compound like `red_micro_bikini`
    # becomes `red micro bikini` (phrase form) — the decomposition path
    # is gated behind tag form so it can't re-validate it, and the
    # original user-text anchor is gone the next turn. Treat the user's
    # existing prompt as authoritative the same way bio source_phrases are.
    src_pos, _ = _node_prompt_token_sets(node_prompt)
    for tok_lc in src_pos:
        if tok_lc:
            allowed.add(tok_lc)

    # 4. Tag-wiki retrieval for this request — looser threshold than the
    # prompt-block retrieval (0.50 vs 0.55) so we err on permissive when
    # building the allowed-set. If tag_search isn't loaded yet (cold
    # boot before first ingest), this returns an empty list and the
    # allowed-set is just bio + modifier — trace-check stays
    # well-defined, just slightly stricter.
    try:
        from . import tag_search
        for c in tag_search.search(user_request, top_k=30, threshold=0.50):
            bare = (c.get("tag") or "").replace("_", " ").lower()
            if bare:
                allowed.add(bare)
    except Exception:
        logger.warning("trace-check: tag_search lookup failed", exc_info=True)

    user_lc = (user_request or "").lower()

    # 5. Collect emitted tokens that need cross-checking against the full
    # danbooru_tags table. Skip ones already in `allowed` (no point
    # querying for them).
    candidates: set[str] = set()
    for s in sections:
        if s.get("is_negative"):
            continue
        for t in s.get("tokens", []):
            bare = _bare_form(t)
            if bare and bare not in allowed and bare not in user_lc:
                # underscore form is what danbooru_tags stores
                candidates.add(bare.replace(" ", "_"))

    if candidates:
        try:
            from .tag_builder import get_db
            db = get_db()
            placeholders = ",".join("?" for _ in candidates)
            for r in db.execute(
                f"SELECT tag FROM danbooru_tags WHERE tag IN ({placeholders})",
                list(candidates),
            ).fetchall():
                allowed.add((r["tag"] or "").replace("_", " ").lower())
        except Exception:
            logger.warning("trace-check: danbooru_tags lookup failed",
                           exc_info=True)

    # Three-tier traceability (Phase 4: component-aware):
    #
    #   - Authoritative match: token's bare form is in `allowed` (came
    #     from bio / modifier / retrieval / danbooru_tags). Pass.
    #   - Phrase form (no underscores in original token): pass if its
    #     bare form is a substring of the user's typed request. The
    #     model's tag-source rule 3 says it can fall back to natural
    #     language phrases when no canonical exists, so we honor that.
    #   - Tag form (underscores present): try compositional decomposition.
    #     If every underscore-separated piece is itself a valid component
    #     (clothing color/material/pattern, item tag, real Danbooru tag,
    #     or appears in user text), pass. This admits legitimate
    #     compounds like `pink_polka_dot_leotard` while still rejecting
    #     hallucinated ones like `feet_positioned_forward` whose pieces
    #     don't all check out.
    #
    # `_drop_misplaced_tokens` runs after this and catches wrong-domain
    # compositions that pass the structural check but belong in a
    # different section (defense in depth).
    components = _load_compositional_components()
    dropped: list[str] = []
    normalized: list[tuple[str, str]] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        # Style sections are server-injected from curated templates —
        # `Fujifilm XT3`, `oil painting`, `subsurface scattering` etc.
        # aren't Danbooru tags and won't pass trace-check. Tokens come
        # from a curator's hand-authored template, not model output, so
        # there's nothing to validate against hallucination here.
        if _section_key_from_header(s.get("header") or "") == "style":
            continue
        kept = []
        for t in s.get("tokens", []):
            bare = _bare_form(t)
            if not bare:
                continue
            if bare in allowed:
                kept.append(t)
                continue
            # User-text fallback BEFORE variant normalization. Tag-form
            # tokens like `legs_up` get skipped from the danbooru_tags
            # batch when their bare form is already in user_lc (an
            # optimization). That leaves them missing from `allowed`,
            # which would then wrongly trigger body-part variant
            # normalization (`legs_up` -> `leg_up`). If the user typed
            # the words explicitly this turn, trust them — no variant
            # rewrite, just keep as-is.
            if bare in user_lc:
                kept.append(t)
                continue
            # Body-part variant normalization. If the token isn't in the
            # authoritative set but a single feet↔foot / hands↔hand /
            # eyes↔eye swap lands in `allowed`, prefer that — it's the
            # canonical form the menu offered. Only swaps when the
            # variant is in `allowed`, so legitimate compositional
            # tags without body-part words (`red_micro_bikini`) and
            # legitimate compositions where neither variant is in the
            # menu (`pink_polka_dot_leotard`) flow to decomposition
            # untouched.
            canon = _canonical_body_part_variant(t, allowed)
            if canon is not None:
                kept.append(canon)
                normalized.append((t, canon))
                continue
            # User-text fallback: phrase-form tokens whose words all
            # appear in the user request pass even when written in a
            # different order. "make boots red" → token `red boots` —
            # substring fails on word reversal, but the word-set check
            # catches it. Stays gated on phrase form so canonical
            # Danbooru compounds (e.g. `pink_polka_dot_leotard`) still
            # flow to the structural decompose check below.
            tag_form = _is_tag_form(t)
            if not tag_form and all(w in user_lc for w in bare.split()):
                kept.append(t)
                continue
            # Compositional decompose: also runs on phrase form, not
            # only tag form. The LLM emits phrase form when the model's
            # tag_format is `spaces`, but a "<color> <item>" compound
            # like "red boots" is the same structurally as `red_boots` —
            # SDXL/LAION-trained models accept both. Underscoring before
            # decomposition lets the components-table lookup fire.
            underscore = bare.replace(" ", "_")
            if _decompose_token(underscore, components, user_lc):
                kept.append(t)
                continue
            dropped.append(t)
        s["tokens"] = kept

    if dropped:
        dbg.info(
            "ai-patch[%s] trace-check dropped untraceable tokens: %s",
            request_id, ", ".join(dropped),
        )
    if normalized:
        dbg.info(
            "ai-patch[%s] trace-check normalized body-part variants: %s",
            request_id, ", ".join(f"{a}->{b}" for a, b in normalized),
        )
    return [s for s in sections if s.get("tokens") or s.get("is_negative")]




def _parse_sectioned_output(text: str) -> list[dict]:
    """Parse the model's plain-text output into [{header, tokens, body_text}].
    Section headers start with `//`. Tag lines are comma-separated.
    A literal `Negative Prompt:` line opens a special section whose
    header is preserved verbatim (no `//`) so the assembled output
    matches PromptChain's A1111-style negative-prompt marker that the
    compiler splits on. Lines that aren't a header or a tag line are
    folded into the most recent section's tokens.

    `body_text` preserves the raw body lines verbatim for natlang prose
    bodies that would round-trip lossily through comma-split/join (e.g.
    multi-line paragraphs, internal sentence punctuation). Tag mode
    ignores body_text and assembles from `tokens` as before."""
    sections: list[dict] = []
    current: dict | None = None
    body_lines_for_current: list[str] | None = None
    def _flush_body():
        if current is not None and body_lines_for_current is not None:
            current["body_text"] = "\n".join(body_lines_for_current)
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m_neg = _NEGATIVE_PROMPT_RE.match(s)
        if m_neg:
            _flush_body()
            current = {
                "header": "Negative Prompt:",
                "tokens": [],
                "is_negative": True,
                "body_text": "",
            }
            sections.append(current)
            body_lines_for_current = []
            inline = m_neg.group(1).strip()
            if inline:
                current["tokens"].extend(t.strip() for t in _split_prompt_tokens(inline) if t.strip())
                body_lines_for_current.append(inline)
                current["body_text"] = inline
            continue
        m = _SECTION_HEADER_RE.match(s)
        if m:
            _flush_body()
            current = {
                "header": f"// {m.group(1)}",
                "tokens": [],
                "body_text": "",
            }
            sections.append(current)
            body_lines_for_current = []
            continue
        # Tag content line — comma-split for tag-mode tokens, AND
        # appended verbatim to body_text for natlang assembly.
        tokens = [t.strip() for t in _split_prompt_tokens(s) if t.strip()]
        if current is None:
            current = {"header": "// Prompt", "tokens": [], "body_text": ""}
            sections.append(current)
            body_lines_for_current = []
        if tokens:
            current["tokens"].extend(tokens)
        if body_lines_for_current is not None:
            body_lines_for_current.append(s)
    _flush_body()
    # Keep sections that have either tokens (tag mode) or body_text (natlang prose).
    sections = [s for s in sections if s["tokens"] or (s.get("body_text") or "").strip()]
    # Dedup sections with the same header — small models occasionally
    # emit duplicate `// Style: X` / `// Setting / Scene` blocks (one
    # near the section's natural position and a copy at the very end of
    # the output). Merge tokens uniquely, preserve first-seen body_text.
    return _dedup_section_headers(sections)


def _dedup_section_headers(sections: list[dict]) -> list[dict]:
    """Merge sections that share the same header (case-insensitive). The
    first-seen wins for body_text and section flags; later duplicates
    contribute their unique tokens to the merged `tokens` list, then are
    dropped. Defends against the 8B Qwen failure mode where a slightly-
    unusual edit causes the model to emit the same section header twice
    in one response — without dedup the renderer ships both copies."""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for s in sections:
        key = (s.get("header") or "").strip().lower()
        if not key:
            out.append(s)
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = s
            out.append(s)
            continue
        # Append tokens that aren't already in the kept section. Order-
        # preserving — first-seen ordering wins.
        existing_set = {t for t in existing.get("tokens") or []}
        for t in s.get("tokens") or []:
            if t not in existing_set:
                existing.setdefault("tokens", []).append(t)
                existing_set.add(t)
        # Don't merge body_text — keep first-seen; later duplicates are
        # almost always trailing copies that would corrupt natlang prose
        # if concatenated.
    return out


def _resolve_style_from_text(text: str, arch_prompts: list) -> dict | None:
    """Best-template match for a style sub-intent text. Multi-stage:
      1. Whitespace-normalized substring match — handles `hyper realistic`
         finding `Hyperrealistic`.
      2. Anime-aware filter — when user mentions `anime`, prefer
         templates with `anime` in their id/name (so `hyper realistic
         anime` doesn't pick the 3D Realistic Unreal Engine template).
      3. Fallback to the existing token-overlap scorer.

    Returns the matched template dict or None."""
    if not text or not arch_prompts:
        return None
    text_lc = text.lower().strip()
    text_normalized = re.sub(r"[\s\-_]+", "", text_lc)
    user_words = set(re.findall(r"\w{4,}", text_lc))

    # Anime-aware filter
    user_says_anime = "anime" in text_lc
    candidate_pool = arch_prompts
    if user_says_anime:
        anime_only = [
            p for p in arch_prompts
            if "anime" in (p.get("id") or "").lower()
            or "anime" in (p.get("name") or "").lower()
        ]
        if anime_only:
            candidate_pool = anime_only

    # Stage 1: normalized-substring match.
    best_norm = None
    best_norm_len = 0
    for p in candidate_pool:
        name_lc = (p.get("name") or "").lower()
        if not name_lc:
            continue
        name_normalized = re.sub(r"[\s\-_]+", "", name_lc)
        if name_normalized and name_normalized in text_normalized:
            if len(name_normalized) > best_norm_len:
                best_norm = p
                best_norm_len = len(name_normalized)
    if best_norm and best_norm_len >= 6:
        return best_norm

    # Stage 2: token-overlap scorer (same as the existing one, but
    # restricted to candidate_pool).
    best = None
    best_score = 0.0
    for p in candidate_pool:
        name = (p.get("name") or "").lower()
        name_words = set(re.findall(r"\w{4,}", name))
        if not name_words:
            continue
        overlap = name_words & user_words
        if not overlap:
            continue
        overlap_chars = sum(len(w) for w in overlap)
        name_chars = sum(len(w) for w in name_words)
        if name_chars == 0:
            continue
        adj = (overlap_chars ** 2) / name_chars
        if adj > best_score:
            best = p
            best_score = adj
    if best and best_score >= 4.0:
        return best
    return None


async def _run_natlang_v2(request_id, body, user_request, bios, node_prompt,
                          style_template, provider, config,
                          _t_request_start, _timing,
                          _mark, _status, _dump,
                          arch_prompts: list | None = None):
    """vibrant-rendering-loom Phase E — delta-based natlang pipeline.

    Replaces the structural-intent + ad-hoc-helper flow with:
      1. parse_user_request_to_deltas → ordered list[Delta]
      2. apply_deltas → cumulative state mutation (modifier propagation
         folded in, no separate apply_reverse_displacement pass)
      3. render_all_sections → per-section prose (server for
         Character/Outfit, model for Pose, server for Expression/Setting)
      4. Section post-pass (style + neg merge) — reuses existing helpers."""
    from . import natlang_facts as _nlf
    from . import natlang_render_v2 as _nrv2
    from .prompt_state import PromptState as _PromptState, StyleState as _StyleState

    _mark("build_prompt")

    # 1. Load incoming state and refresh bio fields against current bios.
    # Per the plan: "refresh bio fields from current bios" on every load
    # so stale outfit anchors / slot rows / pose anchors don't survive
    # turn-over-turn.
    incoming_state = _PromptState.from_dict(body.get("prompt_state"))

    # FRESH BUILD GATE: when there's no prose in the editor, prior state
    # has nothing to anchor to — preserving it leaks unrequested data
    # into the new prompt (sultry expressions, prior pose-anchor overrides
    # that wipe the new bio's anchor, user_extras that bypass the outfit
    # bio short-circuit). State without prose is meaningless; reset it.
    if not (node_prompt and node_prompt.strip()):
        incoming_state = _PromptState()

    # 1a. Ingestion path: when the user has hand-written / pasted prose
    # into the editor and pcrPromptState is empty, the LLM extracts
    # structured facts from the prose so subsequent edits work
    # incrementally. Without this, the user's custom prose gets nuked
    # the first time they hit Apply because state has nothing to edit
    # against — the renderer would compose fresh from bios + this turn's
    # deltas and lose every custom phrasing.
    # Ingest when:
    # (a) pcrPromptState is empty (first run), OR
    # (b) node_prompt has no `// Section:` headers (flat user prose —
    #     they pasted custom text, state is stale from a prior session
    #     and doesn't reflect what's now in the editor).
    # Without (b), a user pasting custom prose into a node that has
    # leftover pcrPromptState gets bio-driven recomposition, completely
    # losing their prose.
    has_section_headers = bool(re.search(
        r"(?m)^\s*//\s*(character|outfit|pose|expression|scene|setting|style)\b",
        node_prompt or "",
        re.IGNORECASE,
    ))
    should_ingest = (
        node_prompt and node_prompt.strip()
        and (incoming_state.is_empty() or not has_section_headers)
        # Ingest is character-centric — extracting `character:`,
        # `outfit:`, `pose:` facts. For non-character prompts (no bios
        # matched), ingest mis-classifies scene content as outfit_body.
        # Skip it; the no-bio fallback below treats the prose as a
        # // Scene body and applies edits via FRUIT-style LLM rewrite.
        and bool(bios)
    )

    just_ingested = False
    if should_ingest:
        # Reset incoming_state so seeded facts replace stale data.
        if not incoming_state.is_empty() and not has_section_headers:
            incoming_state = _PromptState()
        ingested = await _ingest_node_prompt_to_facts(
            request_id, provider, config, node_prompt,
        )
        if ingested:
            dbg.info(
                "ai-patch[%s] ingest: %d facts from %d-char node_prompt",
                request_id, len(ingested), len(node_prompt),
            )
            _nlf.seed_state_from_ingested_facts(incoming_state, bios, ingested)
            # Replace flat node_prompt with a sectioned synthesis built
            # from ingested per-section bodies — the rest of the pipeline
            # uses // Section: headers to identify what to preserve, and
            # flat user prose has none.
            char_for_header = incoming_state.primary_character()
            synth = _nlf.synthesize_node_prompt_from_ingest(
                ingested,
                char_display=(char_for_header.display if char_for_header else ""),
                char_series=(char_for_header.series if char_for_header else ""),
                outfit_name=(char_for_header.outfit.name if char_for_header else ""),
            )
            if synth:
                node_prompt = synth
                dbg.info(
                    "ai-patch[%s] ingest: synthesized %d-char sectioned node_prompt",
                    request_id, len(synth),
                )
            just_ingested = True

    # Refresh bio fields against current bios — only when state wasn't
    # just ingested. Ingested state IS the truth; merging bios would
    # re-add items the user explicitly omitted from their prose
    # (e.g. fingerless_gloves from Killer Bee bio when the user only
    # wrote `blue leotard, garrison cap, red gloves, yellow necktie`).
    if just_ingested:
        state = incoming_state
    else:
        state = _nlf.refresh_state_with_bios(incoming_state, bios)

    # 2. Decompose first, then parse each sub-intent in its own section
    # context. Per the plan: split into atomic per-section sub-intents
    # before parsing — each sub-intent only emits deltas for its own
    # section, so cross-section bleed (outfit text reaching pose, etc.)
    # is structurally impossible. Falls back to monolithic parsing if
    # decompose returns the trivial single-fallback (network down).
    # Capture sub_intents for the v3 refinement pass. The decompose call
    # happens inside parse_request_via_decompose; closure captures the
    # result so we can route candidates without decomposing twice.
    captured_sub_intents: list[dict] = []
    async def _decompose(user_text: str):
        try:
            result = await _decompose_user_request(
                request_id, provider, config, user_text,
            )
        except Exception:
            logger.exception("ai-patch[%s] natlang_v2 decompose failed", request_id)
            return None
        if result:
            captured_sub_intents.extend(result)
        return result

    # Style resolver — when a `style` sub-intent comes through decompose,
    # resolve it against the same arch_prompts the upstream alias scan
    # uses. The resolver is anime-aware and handles compound words like
    # `hyper realistic` matching `Hyperrealistic`.
    arch_prompts_for_style = arch_prompts or []
    def _style_resolver(text: str):
        return _resolve_style_from_text(text, arch_prompts_for_style)

    # Pose-chip LLM picker — bge-small narrows the chip pool to top-K
    # candidates; the LLM reads each candidate's authored natlang and
    # picks the chip whose meaning best captures the user's pose
    # intent. Mirrors tag mode's "retrieve candidates, LLM picks"
    # pattern. No chip prose is authored here — the LLM only chooses,
    # then the chosen chip's natlang renders verbatim.
    async def _pose_chip_picker(user_text: str, candidates: list[dict]):
        return await _llm_pick_pose_chip(
            request_id, provider, config, user_text, candidates,
        )

    # No-bio fallback: when match-characters returned 0 bios, the user's
    # prompt has no character anchor. Decompose is character-centric and
    # mis-classifies generic objects ("bowl" / "fruit") as clothing.
    # Skip decompose; the entire request becomes the // Scene body.
    #
    # Iteration vs build: when node_prompt has a prior // Scene body,
    # treat user_request as an EDIT instruction and apply it via FRUIT-
    # style LLM rewrite (preserving unaffected clauses). Otherwise treat
    # user_request as the fresh scene description.
    no_bio_changed_sections: set | None = None
    if not bios:
        prior_sections_for_no_bio = _nrv2._parse_prior_sections(node_prompt or "")
        prior_scene = (
            (prior_sections_for_no_bio.get("scene") or {}).get("body_text", "")
            or (prior_sections_for_no_bio.get("setting") or {}).get("body_text", "")
        ).strip()

        async def _no_bio_compose(system_prompt: str, user_prompt: str) -> str:
            return await _run_generation(
                f"{request_id}-no-bio-scene", provider, config,
                system_prompt, user_prompt, [],
            )

        new_scene = await _nrv2.edit_scene_no_bio(
            prior_scene, user_request, model_compose=_no_bio_compose,
        )
        state.setting = new_scene
        # Force "setting" into changed_sections so the orchestrator
        # re-renders with our state.setting instead of preserving the
        # prior // Scene from node_prompt.
        no_bio_changed_sections = {"setting"}
        dbg.info(
            "ai-patch[%s] natlang_v2 no-bio fallback: prior_scene=%d chars, "
            "new_scene=%d chars (edit=%s)",
            request_id, len(prior_scene), len(new_scene),
            "yes" if prior_scene else "no",
        )
        deltas: list = []
    else:
        try:
            deltas = await _nlf.parse_request_via_decompose(
                user_request, bios, state, _decompose,
                style_resolver=_style_resolver,
                pose_chip_picker_fn=_pose_chip_picker,
            )
        except _nlf.DecomposeUnavailable as e:
            logger.warning(
                "ai-patch[%s] natlang_v2 decompose unavailable: %s",
                request_id, e,
            )
            return error_response(
                "AI service unavailable: cannot decompose your request. "
                "Make sure ollama is running and the configured model is loaded.",
                503,
            )
    dbg.info(
        "ai-patch[%s] natlang_v2 deltas: %s",
        request_id,
        [type(d).__name__ for d in deltas],
    )

    # If v2 produced a SwapStyleDelta (style sub-intent fired), it has
    # already mutated state.style via apply_deltas below. Override the
    # upstream-resolved style_template so the post-pass uses the v2
    # resolution instead.
    for d in deltas:
        if isinstance(d, _nlf.SwapStyleDelta) and d.template_id:
            v2_style = _find_prompt_by_id(arch_prompts_for_style, d.template_id)
            if v2_style is not None:
                style_template = v2_style
                break

    # 3. Apply deltas to state (in place).
    # Snapshot active modifiers BEFORE applying deltas so we can detect
    # which modifiers got auto-dropped by `update_active_modifiers_
    # from_slots` (runs inside apply_deltas after FillSlotDelta). When
    # the user adds e.g. blue socks against state with active barefoot,
    # state correctly drops barefoot -- but v4 only sees the prose
    # (which still says "Barefoot."), so the prose carries the stale
    # modifier word. Capture the diff so v4 can be told to scrub it.
    prior_active_modifiers: set[str] = set()
    for _c in state.characters:
        prior_active_modifiers.update(_c.outfit.active_modifiers)

    _nlf.apply_deltas(state, deltas, bios)

    post_active_modifiers: set[str] = set()
    for _c in state.characters:
        post_active_modifiers.update(_c.outfit.active_modifiers)
    dropped_modifiers: set[str] = prior_active_modifiers - post_active_modifiers

    # Text-based modifier conflict detection: state-tracking is
    # insufficient when prior-turn prose has a stale modifier word
    # (e.g. "Barefoot" left over from a misrouted earlier turn) that
    # state never recorded as active. Scan node_prompt for modifier
    # aliases that conflict with this turn's filled slots, and add
    # any matches to dropped_modifiers so v4 emits cleanup directives
    # for them.
    fill_slots: set[str] = set()
    for _d in deltas or []:
        if isinstance(_d, _nlf.FillSlotDelta):
            slot = (getattr(_d, "slot", "") or "").lower().strip()
            if slot:
                fill_slots.add(slot)
    if fill_slots and node_prompt:
        for _m in _load_slot_modifiers():
            clears = {s.lower().strip() for s in (_m.get("clears_slots") or [])}
            if not (clears & fill_slots):
                continue
            canonical = (_m.get("canonical_tag") or "").strip()
            if not canonical:
                continue
            # Match canonical + aliases as whole words in the prose.
            tokens = [canonical] + list(_m.get("aliases") or [])
            for tok in tokens:
                if not tok:
                    continue
                pat = re.compile(rf"\b{re.escape(tok)}\b", re.IGNORECASE)
                if pat.search(node_prompt):
                    dropped_modifiers.add(canonical)
                    break

    # Backstop-touched sections tracker. Each backstop that mutates
    # state needs to surface its change to `changed_sections` so the
    # affected section gets re-rendered (otherwise it'd be preserved
    # verbatim from node_prompt and the backstop's effect would be
    # invisible in the output). Dev-log bug 2026-05-16 21:41: "make
    # her bare foot" fired barefoot modifier via backstop, cleared
    # legwear, but `outfit` wasn't in changed_sections → outfit
    # preserved with old "blue socks" still in body.
    backstop_changed_sections: set[str] = set()

    # 3a. Text-based modifier detection backstop. Decompose at 8B often
    # routes body-modifier phrases ("bare feet", "topless") to the
    # `pose` section instead of emitting them as explicit modifier
    # sub-intents. Without this, the modifier never fires and the
    # corresponding slot stays filled — e.g. "showing bare feet" leaves
    # footwear=brown_boots in state, and the FRUIT pass preserves
    # "knee-high brown leather boots" in the // Outfit prose.
    # Scan the raw user_request for modifier aliases (same scan the
    # tag-mode slot-conflicts block uses) and inject any missing
    # canonical as an ApplyModifierDelta.
    detected_modifiers = _detect_modifiers_in_text(user_request)
    if detected_modifiers and state.characters:
        primary = state.primary_character()
        if primary is not None:
            already = set(primary.outfit.active_modifiers)
            # When a pose chip already applied this turn (ApplyPoseChipDelta
            # via bucket_search), the chip's authored natlang IS the pose
            # body — re-injecting a pose-section modifier like
            # `presenting_foot` would re-populate pose.pose_modifiers,
            # defeat the render short-circuit, and route through the FRUIT
            # pose compose path that rewrites the chip natlang. Skip
            # pose-section modifiers when chip is anchoring the pose.
            chip_applied = (
                primary.pose.natlang_anchor.strip()
                and primary.pose.bio_pose_id is None
                and primary.pose.name.strip()
            )
            for mod in detected_modifiers:
                canon = (mod.get("canonical_tag") or "").strip().lower()
                if not canon or canon in already:
                    continue
                substitute_section = (mod.get("substitute_section") or "").lower()
                if chip_applied and substitute_section == "pose":
                    continue
                _nlf.apply_deltas(state, [_nlf.ApplyModifierDelta(
                    canonical=canon,
                    clears_slots=list(mod.get("clears_slots") or []),
                    substitute_section=substitute_section,
                )], bios)
                already.add(canon)
                # Mark affected sections changed. Outfit-section
                # modifiers touch outfit slots; pose-section modifiers
                # touch pose.pose_modifiers (but chip-applied path
                # skips those above).
                if substitute_section == "pose":
                    backstop_changed_sections.add("pose")
                else:
                    backstop_changed_sections.add("outfit")

    # 3a-bis-outfit. Outfit-swap backstop. "in X outfit" / "wearing X
    # outfit" routed by decompose to a non-outfit section is lost
    # otherwise. Run only if no SwapOutfitDelta already fired this
    # turn — decompose-side path takes priority when it works.
    if state.characters:
        primary = state.primary_character()
        if primary is not None:
            already_swapped = any(
                isinstance(d, _nlf.SwapOutfitDelta) for d in deltas
            )
            if not already_swapped:
                swap_delta = _nlf.extract_outfit_swap_from_text(user_request, bios)
                if swap_delta is not None:
                    _nlf.apply_deltas(state, [swap_delta], bios)
                    backstop_changed_sections.add("outfit")

    # 3a-bis-pre. Strip-intent backstop. "Wearing only X" / "just X" /
    # "in nothing but X" routed by decompose to anywhere but `strip:`
    # would lose the strip semantics. Same brittleness pattern as slot
    # fills. Strip extractor scans user_request, emits StripDelta +
    # FillSlotDelta for each detected strip phrase. Must run BEFORE the
    # slot-fill backstop so the strip's keep-set is established first.
    if state.characters:
        primary = state.primary_character()
        if primary is not None:
            # Only fire if decompose didn't already emit a strip-shaped
            # delta this turn — otherwise we'd double-strip.
            already_stripped = any(
                isinstance(d, _nlf.StripDelta) for d in deltas
            )
            if not already_stripped:
                strip_deltas = _nlf.extract_strip_intents_from_text(user_request)
                if strip_deltas:
                    _nlf.apply_deltas(state, strip_deltas, bios)
                    backstop_changed_sections.add("outfit")

    # 3a-bis. Full-request slot-fill backstop. Symmetric to the modifier
    # backstop above: scans the raw user_request for clothing items and
    # fills the corresponding slots regardless of how decompose chose
    # to route the tokens. 8B decompose is non-deterministic on routing
    # — same input gives `pose: legs up showing blue socks at viewer`
    # one run and `outfit: blue socks` + `pose: ...` the next. Pipeline
    # cannot depend on decompose section choices. Mirror of tag mode's
    # parallel-extractor pattern (alias scan + literal anchor + semantic
    # search all run on the full user_request).
    if state.characters:
        primary = state.primary_character()
        if primary is not None:
            backstop_fills = _nlf.extract_slot_fills_from_text(user_request)
            # Don't re-fill slots already user-filled this turn or in
            # prior state — those took the explicit path; the backstop
            # only covers slots decompose dropped.
            from .prompt_state import SLOT_STATE_FILLED, ORIGIN_USER
            for fill in backstop_fills:
                slot_name = fill.slot
                cur = primary.outfit.slot_states.get(slot_name)
                if (cur and cur.state == SLOT_STATE_FILLED
                        and (cur.origin or "") == ORIGIN_USER):
                    continue
                _nlf.apply_deltas(state, [fill], bios)
                backstop_changed_sections.add("outfit")

    # 3a-bis-clear. Slot-clear backstop. "no socks" / "remove the boots"
    # / "without gloves" / "drop the leotard" — per-slot clearing via
    # negation patterns. Independent of slot_modifier aliases so the
    # user doesn't need to add per-clothing-item modifiers to fire
    # specific clears. Cleared items keep prior item/color so the
    # render-layer prose strippers can locate and remove them.
    if state.characters:
        primary = state.primary_character()
        if primary is not None:
            clear_deltas = _nlf.extract_slot_clears_from_text(user_request)
            if clear_deltas:
                _nlf.apply_deltas(state, clear_deltas, bios)
                backstop_changed_sections.add("outfit")

    # 3a-ter. Chip-implied slot clears. When a pose chip applies, scan
    # its authored tags + natlang against `slot_modifiers` aliases. Any
    # matched modifier's `clears_slots` applies — the chip is describing
    # a pose state that implies certain slots are empty/clear.
    #
    # Example: chip `presenting_feet` has tags `(legs up:1.1), sitting,
    # presenting feet, soles, foot focus`. The phrase "presenting feet"
    # is an alias of slot_modifier `presenting_foot` with
    # clears_slots=[footwear, legwear]. So when the chip applies, the
    # bio's brown boots (footwear) get cleared — the pose explicitly
    # shows feet, boots would hide them.
    #
    # User fills take priority: when a slot to-be-cleared was filled by
    # the user this turn, the fill wins and the implied clear is dropped
    # for that slot only (same logic as
    # `update_active_modifiers_from_slots`). For "blue socks at viewer":
    # legwear=blue_socks (user fill) survives, footwear=brown_boots
    # (bio default) clears.
    chip_deltas = [d for d in deltas if isinstance(d, _nlf.ApplyPoseChipDelta)]
    if chip_deltas and state.characters:
        primary = state.primary_character()
        if primary is not None:
            from .prompt_state import SLOT_STATE_FILLED, ORIGIN_USER, SlotState, SLOT_STATE_CLEARED
            all_modifiers = _load_slot_modifiers()
            for chip in chip_deltas:
                bag_lc = " ".join([
                    chip.display_name or "",
                    chip.base_tags or "",
                    chip.base_natlang or "",
                    chip.chip_tag or "",
                ]).lower()
                for mod in all_modifiers:
                    aliases = list(mod.get("aliases") or [])
                    canon = (mod.get("canonical_tag") or "").strip().lower()
                    if canon:
                        aliases = [canon.replace("_", " "), canon] + aliases
                    matched = False
                    for alias in aliases:
                        alias_lc = (alias or "").strip().lower()
                        if not alias_lc:
                            continue
                        if re.search(rf"(?<!\w){re.escape(alias_lc)}(?!\w)", bag_lc):
                            matched = True
                            break
                    if not matched:
                        continue
                    clears = [s.strip().lower() for s in (mod.get("clears_slots") or []) if s and s.strip()]
                    if not clears:
                        continue
                    for slot_name in clears:
                        cur = primary.outfit.slot_states.get(slot_name)
                        # Respect user fills this turn — don't undo
                        # what the user just put there.
                        if (cur and cur.state == SLOT_STATE_FILLED
                                and (cur.origin or "") == ORIGIN_USER):
                            continue
                        # Preserve prior item/color for downstream
                        # body-prose stripping (same shape as
                        # `_apply_modifier`'s preservation).
                        prior_item = (cur.item if cur else "") or ""
                        prior_color = (cur.color if cur else "") or ""
                        primary.outfit.slot_states[slot_name] = SlotState(
                            state=SLOT_STATE_CLEARED,
                            by_modifier=canon,
                            item=prior_item,
                            color=prior_color,
                        )
                        backstop_changed_sections.add("outfit")

    # 3b. Style template overlay (handled outside the delta system so we
    # don't lose it on bio-driven rebuilds).
    if style_template:
        state.style = _StyleState(
            template_id=(style_template.get("id") or "").strip(),
            name=(style_template.get("name") or "").strip(),
        )

    # 4. Compose section bodies.
    #
    # No-bio path: state.setting carries the FRUIT-authored scene from
    # edit_scene_no_bio earlier; render_all_sections composes it into a
    # `// Scene` section deterministically. No LLM authoring needed.
    #
    # Bio path: LLM authors the full prompt body via the natlang patch
    # system prompt, with bios (V2 chip data + enrichers) as reference.
    # Mirrors tag-mode's authoring path — chips are helpers, not
    # templates. State (PromptState) remains the structured backing for
    # TagBuilder2 round-trip, derived from typed deltas above; the LLM
    # output is the prose. Both reflect the user's request because
    # decompose + delta application + LLM authoring all consume the
    # same user_request and bios.
    _mark("inference")
    # Deterministic compositor handles all paths. V2 chip composers (in
    # tag_builder.py) feed enriched natlang into bios; render_all_sections
    # surfaces it via the bio-anchor short-circuit (build mode, no user
    # fills) or FRUIT-edits with chip prose as seed (patch / user fills).
    #
    # LLM body authoring was prototyped here for build mode but 8B couldn't
    # preserve bio chip detail reliably (hallucinated pose bodies, invented
    # scenes the user never asked for, dropped header attributions). The
    # deterministic compositor + V2 chips delivers richer faithful output
    # than the LLM authoring path at the production model size.
    async def _model_compose(system_prompt: str, user_prompt: str) -> str:
        return await _run_generation(
            request_id, provider, config,
            system_prompt, user_prompt, [],
        )
    if not node_prompt.strip():
        changed_sections = None
    elif no_bio_changed_sections is not None:
        changed_sections = no_bio_changed_sections
    else:
        changed_sections = _nlf.changed_sections_from_deltas(deltas)
        # Merge backstop-applied changes — modifier / outfit-swap /
        # strip / slot-fill / chip-implied-clears all mutate state
        # outside the initial deltas list, so changed_sections_from_deltas
        # can't see them. Without this merge, the affected section
        # would be preserved verbatim from node_prompt and the
        # backstop's state mutation would be invisible in the output.
        if backstop_changed_sections:
            changed_sections = set(changed_sections) | backstop_changed_sections
        # Force pose re-render when outfit slots changed AND the
        # character has a chip-anchored pose body mentioning a body
        # part. The slot-context injection in render_pose_section
        # depends on a re-render; without it, preserved-verbatim pose
        # bodies stay decoupled from the new slot state (image model
        # sees "presenting feet with focus on soles" + outfit "pink
        # socks" as two separate sentences, defaults feet to bare).
        if "outfit" in changed_sections and "pose" not in changed_sections:
            primary = state.primary_character() if state.characters else None
            if (primary is not None
                    and primary.pose.natlang_anchor.strip()
                    and primary.pose.bio_pose_id is None
                    and primary.pose.name.strip()):
                anchor_lc = primary.pose.natlang_anchor.lower()
                # Match the body-part scope of the injection: foot-region only.
                if any(part in anchor_lc for part in (
                    "feet", "foot", "soles", "sole", "toes", "toe",
                )):
                    changed_sections = set(changed_sections) | {"pose"}
    # ── v4 identify-rewrite branch ─────────────────────────────────
    # When deltas have no swap-shape entries, v2's state-driven render
    # is destructive: it recomposes section bodies from PromptState,
    # which doesn't track rich modifier prose (insignia descriptions,
    # leg openings, chest harness details). The FRUIT recompose drops
    # that prose. Route to v4 identify-rewrite instead -- surgical
    # clause-level edits against the original node_prompt, with
    # algorithmic byte-identity guarantees on sections the user didn't
    # touch. Gated to iteration mode (non-empty node_prompt) with
    # bios; fresh-build and no-bio paths still use v2.
    from . import natlang_v4 as _nv4
    v4_active = (
        bool(node_prompt and node_prompt.strip())
        and bool(bios)
        and not _nv4.has_swap_shape_delta(deltas)
        and no_bio_changed_sections is None
        and not just_ingested
    )
    v4_authored_text = ""
    if v4_active:
        async def _v4_compose(system_prompt: str, user_prompt: str) -> str:
            return await _run_generation(
                f"{request_id}-v4", provider, config,
                system_prompt, user_prompt, [],
            )
        # Augment user_request with cleanup directives for modifiers
        # that the state machine dropped this turn (e.g. user added
        # blue socks -> legwear is user-filled now -> barefoot was
        # auto-dropped from state, but the prose still says "Barefoot"
        # from the prior turn). v4's split_intents picks these up as
        # additional atomic removal intents.
        v4_user_request = user_request
        if dropped_modifiers:
            cleanup_phrases = [
                f"remove the {m.replace('_', ' ')} mention"
                for m in sorted(dropped_modifiers)
            ]
            # Cleanup directives MUST come FIRST so removal of stale
            # modifier prose happens before the user's additive intent.
            # Otherwise: "give her blue socks" runs first against state
            # with active Barefoot -> rewrites "[OUTFIT] Barefoot" to
            # "[OUTFIT] Barefoot, blue socks". Then "remove barefoot"
            # finds the new combined clause, deletes the whole thing,
            # losing the socks. Cleanup first -> Barefoot gone ->
            # blue socks lands on a non-Barefoot anchor.
            v4_user_request = (
                ". ".join(cleanup_phrases) + ". " + user_request.lstrip(". ")
            )
            dbg.info(
                "ai-patch[%s] v4: state-dropped modifiers %s; augmenting "
                "user_request with cleanup directives",
                request_id, sorted(dropped_modifiers),
            )
        v4_result = await _nv4.edit_prompt(
            node_prompt, v4_user_request, _v4_compose,
        )
        v4_output = (v4_result or {}).get("output") or ""
        if v4_output.strip() and "// " in v4_output:
            # v4's output is the FINAL text. Its reassemble step
            # already preserves every untouched clause byte-identical
            # (including the negative prompt block) -- piping the
            # text back through v2's post-passes (style preservation,
            # _preserve_existing_negatives) would double the negative
            # prompt because the post-pass appends a new is_negative
            # section when it can't find one in the list.
            # Skip post-passes entirely; set output_text directly.
            v4_authored_text = v4_output
            # Still build a rendered_sections list for the response
            # payload shape (TagBuilder UI parses this). Mark the
            # negative block as is_negative so downstream consumers
            # recognize it.
            parsed = _nrv2._parse_prior_sections(v4_output)
            section_list: list[dict] = []
            if isinstance(parsed, dict):
                for kind, sec in parsed.items():
                    section_list.append({
                        "header": sec.get("header") or f"// {kind.title()}:",
                        "body_text": sec.get("body_text") or "",
                        "tokens": [sec.get("body_text") or ""],
                    })
            rendered_sections = section_list
            dbg.info(
                "ai-patch[%s] natlang_v4 applied: intents=%s statuses=%s "
                "output_chars=%d",
                request_id, v4_result.get("intents"),
                v4_result.get("statuses"), len(v4_output),
            )
        else:
            dbg.info(
                "ai-patch[%s] natlang_v4 produced empty body -- "
                "falling back to v2 render", request_id,
            )
            v4_active = False

    if not v4_active:
        rendered_sections = await _nrv2.render_all_sections(
            state,
            node_prompt=node_prompt,
            changed_sections=changed_sections,
            model_compose=_model_compose,
        )

    # 5. Section-level post-pass — reuse existing style + negative helpers.
    # SKIPPED when v4 authored: v4's reassemble already preserves the
    # style sentence and negative prompt block byte-identical from the
    # input. Running _preserve_existing_negatives on top would APPEND a
    # second negative-prompt section because it doesn't find one marked
    # is_negative in our rendered_sections list (we built that list for
    # response-payload shape only -- v4's text is authoritative).
    if not v4_active:
        if style_template:
            new_style = _build_style_section(style_template)
            if new_style:
                rendered_sections = _replace_or_append_style_section(
                    rendered_sections, new_style,
                )
                rendered_sections = _merge_template_negatives(
                    rendered_sections, style_template,
                )
        rendered_sections = _preserve_existing_style_section(
            rendered_sections, node_prompt,
        )
        rendered_sections = _preserve_existing_negatives(
            rendered_sections, node_prompt,
        )

    # 6. Assemble output text.
    from . import natlang_render as _nlr
    if v4_active and v4_authored_text:
        output_text = v4_authored_text
    else:
        output_text = _nlr.assemble_output_text(rendered_sections)
    _dump(request_id, "patch_rendered_v2", output_text)
    _mark("post")

    # 7. v3 LLM authoring refinement. v2's state-driven render handles
    # modifier slot clearing, named outfit/pose swaps, and chip
    # substitution faithfully -- but it can't represent free-form
    # anatomy/clothing modifications like "bigger feet", "stretched
    # ears", "wearing red socks" since they don't map to delta types.
    # The refinement pass hands v2's body to the LLM with the original
    # user_request + curated candidates; the LLM applies any unaddressed
    # intent in-place and preserves sections v2 already got right.
    # Bypassed when v2 produced no body (defensive -- v2's render is
    # always expected to succeed) or no characters matched.
    # Skip v3 when there's no edit to apply -- ingestion turns have no
    # user_request, fresh-build turns have empty deltas, and either way
    # v3 would just re-author v2's body for no benefit (risk of dropping
    # preserved sections the LLM didn't recognize as worth keeping).
    v3_should_run = (
        output_text.strip()
        and bios
        and (user_request or "").strip()
        and (deltas or captured_sub_intents)
        # Fresh-build mode (no prior node_prompt) doesn't need v3 -- v2
        # authored from scratch with the right deltas, and v3 LLM tends
        # to invent placeholder sections like "// Scene: (unchanged)"
        # when there's no prior prose to anchor against.
        and node_prompt
        and node_prompt.strip()
        # v4 already authored body via identify-rewrite; re-running v3
        # would just add latency and risk over-authoring.
        and not v4_active
    )
    if v3_should_run:
        try:
            from . import natlang_v3 as _nv3
            v3_candidates = _nv3.candidates_from_v2_sections(
                rendered_sections, deltas,
            )
            v3_patch_user = _nv3.build_patch_user(
                bios, output_text, user_request, v3_candidates,
            )
            v3_body = await _run_generation(
                f"{request_id}-v3-author", provider, config,
                _nv3.V3_SYSTEM, v3_patch_user, [],
            )
            v3_body = (v3_body or "").strip()
            if v3_body and "// " in v3_body:
                v3_body = _nv3.strip_placeholder_sections(v3_body)
                v3_body = _nv3.strip_invented_sections(v3_body, output_text)
                v3_body = _nv3.enforce_candidate_substitutions(
                    v3_body, v3_candidates,
                )
                _dump(request_id, "patch_rendered_v3", v3_body)
                output_text = v3_body
                dbg.info(
                    "ai-patch[%s] natlang_v3 refinement applied: "
                    "candidates=%d chars=%d",
                    request_id, len(v3_candidates), len(v3_body),
                )
            else:
                dbg.info(
                    "ai-patch[%s] natlang_v3 refinement empty -- keeping v2 body",
                    request_id,
                )
        except Exception:
            logger.exception(
                "ai-patch[%s] natlang_v3 refinement failed -- keeping v2 body",
                request_id,
            )

    total = time.perf_counter() - _t_request_start
    dbg.info(
        "ai-patch[%s] natlang_v2 done: deltas=%s sections=%d chars=%d total=%.2fs",
        request_id,
        [type(d).__name__ for d in deltas],
        len(rendered_sections),
        len(output_text),
        total,
    )

    return web.json_response({
        "request_id": request_id,
        "output_text": output_text,
        "sections": rendered_sections,
        "raw": "",
        "prompt_state": state.to_dict(),
    })


@routes.post("/promptchain/ai/patch")
async def _api_patch(request):
    body, err = await parse_json(request)
    if err:
        return err

    node_prompt = (body.get("node_prompt") or "").strip()
    user_request = (body.get("user_request") or "").strip()
    # Original user message (chat agent's view of the user's actual
    # words). Used to recover intents the agent's distilled `request`
    # may have paraphrased away. Falls back to user_request when not
    # provided (legacy single-shot panel path).
    latest_user_text = (body.get("latest_user_text") or "").strip() or user_request
    # Current-turn message ONLY (no multi-turn concat). Used for
    # outfit-name scan and other current-turn intent classification
    # where prior-turn phrases would pollute. Falls back to
    # latest_user_text -> user_request for the legacy single-shot path.
    current_user_text = (
        (body.get("current_user_text") or "").strip()
        or latest_user_text
        or user_request
    )
    bios = body.get("bios") or []
    if not isinstance(bios, list):
        bios = []

    if not user_request:
        return error_response("user_request is empty", 400)

    # Same-character-multi-outfit pre-pass. Production scenario:
    # `cammy in killer_bee outfit fighting cammy in shadaloo outfit`.
    # Character matcher dedups by tag → bios has 1 cammy. Detect
    # `<char_name> in <outfit_phrase> outfit` patterns in user_request
    # and, for each character with >1 distinct outfit phrase, duplicate
    # the bio with each outfit picked from the DB. Downstream multi-
    # char machinery + _ensure_same_char_multi_outfit_sections handle
    # the rest.
    bios = _expand_bios_for_same_char_multi_outfit(bios, user_request)

    # Resolved up-front so any logging in the style/grounding block
    # below can use it. Other code that follows still references it.
    request_id = (body.get("request_id") or "").strip() or uuid.uuid4().hex

    # ── Railed-thinking pipeline (NATLANG mode only) ─────────────────
    # Routes to decompose -> resolve -> locate-infill -> apply in
    # _harness/natlang_rails_probe.py. Hybrid/rails operates entirely on
    # `// Section:` headers and prose section bodies — it is a natlang-
    # only pipeline. Tag mode keeps the legacy multi-stage path below
    # which composes comma-separated tag sections.
    #
    # Override paths:
    #   - PROMPTCHAIN_USE_LEGACY=1  : never use rails (emergency rollback)
    #   - body.use_legacy: true     : per-request legacy
    #   - body.use_rails: true      : force rails even in build mode
    _prompt_style_for_routing = (
        (body.get("prompt_style") or "").strip().lower()
        or "tags"
    )
    _force_legacy = (
        os.environ.get("PROMPTCHAIN_USE_LEGACY", "").strip() in ("1", "true", "yes")
        or bool(body.get("use_legacy"))
    )
    _force_rails = bool(body.get("use_rails"))
    # Hybrid edit-path router is the DEFAULT for natlang. Rails handles
    # whole-section ops + KB injection, fragment-rewrite handles
    # anatomy_mod/modify/remove (which rails' locate-infill mishandles
    # — e.g. EXAMPLE 5 produced 'barefoot, bigger feet' peer-comma
    # output for footwear anatomy mods, which is the slot-exclusivity
    # bug). To force pure rails-v2 (no fragment path), set
    # PROMPTCHAIN_USE_RAILS_ONLY=1 or body.use_rails_only=true.
    _force_rails_only = (
        os.environ.get("PROMPTCHAIN_USE_RAILS_ONLY", "").strip() in ("1", "true", "yes")
        or bool(body.get("use_rails_only"))
    )
    _use_hybrid = not _force_rails_only
    _use_rails = (not _force_legacy) and (_prompt_style_for_routing == "natural")

    # Tag-rails is the DEFAULT for tag mode. Replaces the legacy
    # monolithic patch-generation LLM call with a deterministic compose
    # pipeline (scripts/tag_rails_v1.py). Opt-out paths:
    #   - PROMPTCHAIN_USE_LEGACY=1 / body.use_legacy: true → legacy
    #   - PROMPTCHAIN_USE_TAG_RAILS=0 → force-disable rails specifically
    #   - body.use_tag_rails: false → per-request legacy
    # See dev-promptchain/docs/plans/tag-rails-migration-plan.md for
    # architecture and migration sequence.
    _tag_rails_explicit_off = (
        os.environ.get("PROMPTCHAIN_USE_TAG_RAILS", "").strip()
            in ("0", "false", "no")
        or body.get("use_tag_rails") is False
    )
    _use_tag_rails = (
        (not _force_legacy)
        and _prompt_style_for_routing == "tags"
        and not _tag_rails_explicit_off
    )
    if _use_tag_rails:
        _pc_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _pc_root not in sys.path:
            sys.path.insert(0, _pc_root)
        try:
            from scripts.tag_rails_v1 import run_tag_rails as _ai_run_tag_rails
        except Exception as e:
            logger.error("ai-patch: tag-rails import failed: %s", e, exc_info=True)
            return error_response(f"tag-rails unavailable: {e}", 500)
        try:
            trace = await _ai_run_tag_rails(
                node_prompt, user_request, bios,
                model_hash=(body.get("model_hash") or "").strip(),
                request_id=request_id,
                tag_format=(body.get("tag_format") or "spaces"),
                is_standalone_main=bool(body.get("is_standalone_main")),
            )
        except Exception as e:
            logger.error("ai-patch (tag-rails-v1): pipeline error: %s",
                         e, exc_info=True)
            return error_response(f"tag-rails pipeline error: {e}", 500)
        return web.json_response({
            "request_id": request_id,
            "output_text": trace.get("final_prompt") or node_prompt,
            "sections": trace.get("sections") or [],
            "raw": {
                "pipeline": "tag-rails-v1",
                "intents": trace.get("intents", []),
                "trace": trace.get("trace", []),
            },
            "prompt_state": body.get("prompt_state") or None,
            "pipeline": "tag-rails-v1",
        })

    if _use_rails:
        # Multi-character EDIT compose path. When the chat agent
        # passes 2+ characters in character_queries but the existing
        # node_prompt has 0-1 // Character: sections (i.e., the user
        # is ADDING a character via natural language), bypass the
        # hybrid path entirely and build inline-bundle prose via
        # compose_from_plan + scene composer. The chat agent's
        # request text is unreliable in this transition (treats new
        # chars as scene content rather than as a character add).
        # Single-character paths AND already-multi-char edits both
        # fall through to hybrid.
        _mc_character_queries = body.get("character_queries") or []
        if isinstance(_mc_character_queries, str):
            _mc_character_queries = [_mc_character_queries]
        if not isinstance(_mc_character_queries, list):
            _mc_character_queries = []
        try:
            _mc_composed = await _maybe_compose_multichar_edit(
                request,
                node_prompt=node_prompt,
                user_request=user_request,
                character_queries=_mc_character_queries,
                model_hash=(body.get("model_hash") or "").strip(),
                bios=bios or [],
            )
        except Exception:
            logger.exception("ai-patch: multichar-edit compose raised")
            _mc_composed = None
        if _mc_composed:
            return web.json_response({
                "request_id": request_id,
                "output_text": _mc_composed,
                "sections": _parse_sectioned_output(_mc_composed),
                "raw": {
                    "pipeline": "multichar-edit-compose",
                    "character_queries": _mc_character_queries,
                },
                "prompt_state": body.get("prompt_state") or None,
                "pipeline": "multichar-edit-compose",
            })

        # ComfyUI's custom-node loader doesn't put the extension root on
        # sys.path, so a plain `import scripts.X` fails. Inject the
        # PromptChain root explicitly.
        _pc_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _pc_root not in sys.path:
            sys.path.insert(0, _pc_root)
        try:
            if _use_hybrid:
                from scripts.natlang_hybrid_v1 import (
                    run_hybrid_turn as _ai_run_turn,
                )
                _pipeline_name = "hybrid-v1"
            else:
                from scripts.natlang_rails_v2 import (
                    run_turn_v2 as _ai_run_turn,
                )
                _pipeline_name = "rails-v2"
        except Exception as e:
            logger.error("ai-patch: failed to import %s pipeline: %s",
                         "hybrid" if _use_hybrid else "rails",
                         e, exc_info=True)
            return error_response(
                f"pipeline unavailable: {e}", 500
            )
        try:
            trace = await _ai_run_turn(
                node_prompt, user_request,
                model_hash=(body.get("model_hash") or "").strip() or None,
                bios=bios,
            )
        except Exception as e:
            logger.error("ai-patch (%s): pipeline error: %s",
                         _pipeline_name, e, exc_info=True)
            return error_response(f"{_pipeline_name} pipeline error: {e}", 500)

        # Style-template promotion post-pass. The rails dispatcher
        # works at sentence-level and replaces only the style sentence
        # it matched — leaving the section header stuck on the prior
        # template name, scene-shaped sentences orphaned in the style
        # section, and the negatives still pointing at the auto-seeded
        # default. This post-pass detects a style intent that resolved
        # to a known template and does the full swap: header, body,
        # negatives. Behavior matches what a tag-builder dropdown apply
        # would produce.
        final_prompt = trace.get("final_prompt") or node_prompt
        style_intent = _find_style_intent(trace.get("intents") or [])
        if style_intent:
            model_hash_for_style = (body.get("model_hash") or "").strip()
            try:
                _grounding = _build_grounding(model_hash_for_style)
                from . import prompts as _prompts_for_style
                _arch_prompts = _prompts_for_style.list_prompts(
                    architecture=(_grounding.get("architecture") or "").strip()
                                  or None,
                    family=_grounding.get("family"),
                    model_name=(_grounding.get("model_name")
                                or _grounding.get("display_name")),
                    model_hash=model_hash_for_style or None,
                )
                _target_name = (
                    style_intent.get("resolved_match_name") or ""
                ).strip().lower()
                _template = next(
                    (p for p in _arch_prompts
                     if (p.get("name") or "").strip().lower() == _target_name),
                    None,
                )
                if _template:
                    swapped = _swap_style_template_in_prompt(final_prompt, _template)
                    if swapped and swapped != final_prompt:
                        logger.info(
                            "ai-patch (%s): style-template post-pass swapped "
                            "to template=%r (id=%s)",
                            _pipeline_name,
                            _template.get("name"),
                            _template.get("id"),
                        )
                        final_prompt = swapped
                else:
                    logger.info(
                        "ai-patch (%s): style post-pass found intent with "
                        "name=%r but no matching template in arch_prompts",
                        _pipeline_name, _target_name,
                    )
            except Exception:
                logger.warning(
                    "ai-patch (%s): style-template post-pass failed",
                    _pipeline_name, exc_info=True,
                )

        # Multi-character polish post-pass. When the final prompt has
        # 2+ // Character: sections (typical of edit-mode multi-char
        # adds — "make her fighting chun-li"), modern T2I encoders
        # (T5-XXL, Qwen3-4B, Qwen2.5-VL) can't bind a shared //
        # Outfit/Pose section to the right subject because they don't
        # honor // section boundaries. Fold into inline-bundle prose
        # with spatial anchors via the scene composer. Single-char
        # outputs return verbatim (composer call is gated internally).
        # Build-mode multi-char is already polished inside rails-v2 by
        # the time it reaches here, so this is a no-op for those.
        final_prompt = await _polish_multichar_to_prose(
            final_prompt, _pipeline_name,
        )

        # Build a panel-compatible response. `output_text` is the new
        # full node_prompt. `raw` carries the pipeline trace.
        intents_summary = [
            {
                "concept": it.get("concept"),
                "op": it.get("op"),
                "text": it.get("text"),
                "resolved_source": it.get("resolved_source"),
                "dispatch_kind": it.get("dispatch_kind"),
                "parent": it.get("parent"),
                "search": it.get("search"),
                "replace": it.get("replace"),
                "insert_after": it.get("insert_after"),
                "method": it.get("method"),
                "route": it.get("route"),
            }
            for it in trace.get("intents", [])
        ]
        return web.json_response({
            "request_id": request_id,
            "output_text": final_prompt,
            # Build mode has no edit-diff, but the proposal card and the chat
            # agent's success check both read `sections`. Derive them from the
            # built prompt so the card shows // Character/Outfit/Pose blocks
            # and the agent doesn't misread an empty list as a no-op.
            "sections": _parse_sectioned_output(final_prompt),
            "raw": {
                "pipeline": _pipeline_name,
                "decompose": trace.get("decompose"),
                "scan": trace.get("scan"),
                "intents": intents_summary,
                "routing": trace.get("routing"),
            },
            "prompt_state": body.get("prompt_state") or None,
            "pipeline": _pipeline_name,
        })
    # ── /experimental rails ────────────────────────────────────────

    # Per-step timing instrumentation. Captures wall-clock deltas around
    # each major stage so we can see where time goes in a slow patch.
    # Logged as a single one-line summary at the end of the request.
    _timing: dict[str, float] = {}
    _t_request_start = time.perf_counter()
    _t_step_start = _t_request_start

    def _mark(step: str) -> None:
        nonlocal _t_step_start
        now = time.perf_counter()
        _timing[step] = now - _t_step_start
        _t_step_start = now

    # Stage A: model_hash + prompt_style plumbing. model_hash drives
    # arch-aware style template lookup (Stage B) and per-model
    # prompt_style fallback (Stage C). prompt_style drives the dual
    # tag-mode / natlang-mode output split (Stage C). Both are read here
    # so behavior keys can hang off them in subsequent phases without
    # threading the wires again.
    model_hash = (body.get("model_hash") or "").strip()
    grounding = _build_grounding(model_hash)
    arch = (grounding.get("architecture") or "").strip()
    prompt_style = (
        (body.get("prompt_style") or "").strip().lower()
        or (grounding.get("prompt_style") or "").strip().lower()
        or "tags"
    )

    # Stage B: deterministic style alias scan. When the user's request
    # phrasing matches a curated alias ("make it anime", "switch to
    # photography"), resolve to a canonical prompt-template id valid
    # for the current model. The scan returns at most one hit;
    # NEUTRAL_TEMPLATE_ID is a synthetic "no style" intent used to
    # suppress build-mode default injection (Phase B4).
    #
    # The full scope lookup (architecture + family + model_name +
    # model_hash) matters: many templates are model-scoped (Illustrious
    # XL, JANKU NoobAI) or family-scoped (Pony) and won't resolve from
    # architecture alone. Pass everything from grounding so the same
    # template list available to the patch flow is what alias scan checks against.
    #
    # No model-call involved — this fires before inference, in cold
    # boot, regardless of LLM availability. The model never sees the
    # style decision; the server injects the `// Style:` section after
    # post-pass filters run.
    style_template = None
    style_alias_hit = None
    arch_prompts: list[dict] = []
    # Style infrastructure runs in BOTH modes — templates are sectioned
    # text with prose-leaning bodies and tag-shaped negs (e.g. z-image's
    # `// 3D Styles - Cartoony\n<prose>\n\nNegative Prompt:\n<negs>`).
    # `_build_style_section` populates both `tokens` (tag-mode comma
    # output) and `body_text` (natlang prose output) so the same style
    # injection path works for either output shape.
    if arch:
        try:
            from . import prompts as _prompts
            arch_prompts = _prompts.list_prompts(
                architecture=arch,
                family=grounding.get("family"),
                model_name=(grounding.get("model_name")
                            or grounding.get("display_name")),
                model_hash=model_hash,
            )
        except Exception:
            logger.warning(
                "ai-patch: prompts.list_prompts failed for arch=%r",
                arch, exc_info=True,
            )
        valid_template_ids = {
            (p.get("id") or "").strip()
            for p in arch_prompts
            if p.get("id")
        }
        try:
            from . import style_search
            style_alias_hit = style_search.style_alias_scan(
                user_request, valid_template_ids,
            )
        except Exception:
            logger.warning("ai-patch: style alias scan failed", exc_info=True)
        if style_alias_hit and not style_alias_hit.get("is_neutral"):
            template_id = style_alias_hit["template_id"]
            if style_alias_hit.get("is_default"):
                # Synthetic _default sentinel — resolve to whatever this
                # model declares as its default_prompt_id. Lets users say
                # "default style" / "use default" on any arch without
                # per-arch alias curation.
                resolved = (grounding.get("default_prompt_id") or "").strip()
                if resolved:
                    style_template = _find_prompt_by_id(arch_prompts, resolved)
                    if style_template is None:
                        logger.warning(
                            "ai-patch: default_prompt_id=%r not in resolved "
                            "prompt list for arch=%r (scope mismatch?)",
                            resolved, arch,
                        )
                else:
                    logger.info(
                        "ai-patch: _default style hit but model has no "
                        "default_prompt_id configured — skipping injection",
                    )
            else:
                style_template = _find_prompt_by_id(arch_prompts, template_id)
                if style_template is None:
                    logger.warning(
                        "ai-patch: style template id=%r in alias seed but "
                        "not in resolved prompt list (scope mismatch?)",
                        template_id,
                    )

    is_neutral_suppress = bool(
        style_alias_hit and style_alias_hit.get("is_neutral")
    )

    # Fallback: name word-overlap match. When the alias scan didn't hit
    # but the user's request shares significant words with a template's
    # `name`, pick the best-scoring template across arch_prompts.
    #
    # Char-weighted overlap, not just word count: "hyperrealistic anime"
    # should pick `Hyperrealistic` (14-char overlap) over `90s Anime`
    # (5-char overlap) — both share exactly one word with the user's
    # request, but the longer match is more specific and more likely
    # the intended template. Word-count scoring tied 1:1 and picked
    # whichever appeared first in arch_prompts.
    #
    # Filter words to length >= 4 to avoid noise (`a`, `the`, `to`, etc.
    # don't carry intent; `cel` is short but rare enough to skip too).
    #
    # Minimum score threshold of 4.0 prevents weakly-matching templates
    # from getting picked when the user's request doesn't actually
    # describe any available template — e.g. "unreal engine 3d style"
    # with no template named anything close. Falls through to grounding
    # default-style or no style.
    if (style_template is None
            and not is_neutral_suppress
            and arch_prompts):
        user_words = set(re.findall(r"\w{4,}", user_request.lower()))
        if user_words:
            best = None
            best_score = 0.0
            for p in arch_prompts:
                name = (p.get("name") or "").lower()
                name_words = set(re.findall(r"\w{4,}", name))
                if not name_words:
                    continue
                overlap = name_words & user_words
                if not overlap:
                    continue
                overlap_chars = sum(len(w) for w in overlap)
                name_chars = sum(len(w) for w in name_words)
                if name_chars == 0:
                    continue
                adj = (overlap_chars ** 2) / name_chars
                if adj > best_score:
                    best = p
                    best_score = adj
            if best and best_score >= 4.0:
                style_template = best
                dbg.info(
                    "ai-patch[%s] style matched by name overlap: "
                    "id=%s name=%r score=%.2f",
                    request_id,
                    best.get("id"),
                    best.get("name"),
                    best_score,
                )
            elif best:
                dbg.info(
                    "ai-patch[%s] style name overlap below threshold: "
                    "best=%r score=%.2f (cutoff=4.0); falling through",
                    request_id, best.get("name"), best_score,
                )

    # Stage B4: build-mode default style injection. When the user is on
    # a standalone main PromptChain node (no children, output not
    # feeding another PromptChain) AND has an empty editor AND didn't
    # name a specific style AND didn't say "no style", auto-seed the
    # model's default_prompt_id as the style — fresh nodes get the
    # canonical default for their checkpoint.
    #
    # Skip explicitly when:
    #   - Frontend says is_standalone_main=False (child prompt or chain
    #     parent — styles belong on the final compiled prompt only).
    #   - node_prompt is non-empty (this isn't a fresh node).
    #   - User already triggered an alias hit (handled above).
    #   - Name fuzzy-match resolved a template (handled above).
    #   - User triggered _neutral ("no style please") — suppression.
    #   - No default_prompt_id configured for this checkpoint.
    is_standalone_main = bool(body.get("is_standalone_main"))
    if (style_template is None
            and is_standalone_main
            and not node_prompt
            and not is_neutral_suppress
            and arch_prompts):
        default_id = (grounding.get("default_prompt_id") or "").strip()
        if default_id:
            style_template = _find_prompt_by_id(arch_prompts, default_id)
            if style_template is not None:
                dbg.info(
                    "ai-patch[%s] build-mode default style auto-seeded: "
                    "id=%s (standalone main with empty prompt)",
                    request_id, default_id,
                )

    # Server-side outfit pre-resolution. Two deterministic passes
    # mutate the bio's default_outfit before the LLM sees it:
    #   1. Color swaps — "orange leotard" replaces the bio's leotard
    #      slot color; original phrase recorded as displaced.
    #   2. Modifier clears — [APPLIES] modifiers like `barefoot` with
    #      clears=[footwear, legwear] drop matching slots; phrases
    #      recorded as displaced.
    # Smaller backing models (qwen3-vl:8b-instruct) often skip these
    # rules; doing them in code makes the contracts self-enforcing
    # regardless of model capability.
    bios = _resolve_color_swaps(bios, user_request)
    bios = _resolve_modifier_clears(bios, user_request)
    _mark("setup")

    config = _load_config()
    provider = body.get("provider") or config.get("provider")
    if not provider:
        return error_response("no AI provider configured", 400)

    def _status(msg: str) -> None:
        _emit(request_id, "status", content=msg)

    # Pre-flight: cold-load detection. If Ollama is the local provider
    # and the target model isn't currently in VRAM, surface a 'Loading
    # model' status and force the load synchronously before the first
    # inference call. Without this, the user sees 'Thinking about
    # request' for the entire 10-30s VRAM populate, which misattributes
    # the wait to the model itself.
    if provider == "local":
        local = config.get("local") or {}
        local_base_url = (local.get("base_url") or "").strip().rstrip("/")
        local_model = (local.get("model") or "").strip()
        if local_base_url and local_model:
            ollama_root = _ollama_root(local_base_url)
            if (await _is_ollama(ollama_root)
                    and not await _is_ollama_model_loaded(ollama_root, local_model)):
                _status("Loading model")
                await _warmup_ollama_model(ollama_root, local_model)
    _mark("warmup")

    # Step A: decompose the user request into atomic sub-intents (LLM
    # call). Failure falls back to a single-intent list. This is a
    # separate Qwen call from the main pick step — adds ~5-15s latency
    # but makes per-intent retrieval focused, dramatically improving
    # tag-wiki RAG coverage on multi-intent prompts.
    #
    # Skipped in natlang mode — `_patch_user_message_natlang` doesn't
    # surface sub_intents to the model (they're tag-shaped). The full
    # decompose round-trip is wasted work in prose mode.
    if prompt_style == "natural":
        sub_intents = [{"text": user_request, "section": ""}]
    else:
        _status("Thinking about request")
        # Decompose ONLY the current turn's message, not the multi-turn
        # extract. `latest_user_text` (the multi-turn _extract_recent_text)
        # was used here briefly to recover intents the agent's distilled
        # paraphrase dropped — but it concatenates the last 6 messages,
        # so prior-turn intents (cyberpunk, thunder, cowboy outfit, etc.)
        # leak into the current turn's decompose and the patch flow
        # treats them as fresh user intent. `current_user_text` is the
        # most recent user message verbatim — exactly the right scope.
        # Falls back to user_request when current_user_text is missing
        # or trivially short (bare 'yes' / 'ok' confirmations where the
        # agent's distilled request carries the actual intent).
        decompose_input = current_user_text
        if not decompose_input or len(decompose_input.strip()) < 10:
            decompose_input = user_request
        sub_intents = await _decompose_user_request(
            request_id, provider, config, decompose_input
        )
        sub_intents = _filter_franchise_setting_style(
            sub_intents, decompose_input, request_id,
        )
        if sub_intents and len(sub_intents) > 1:
            dbg.info(
                "ai-patch[%s] decomposed into %d sub-intents:\n%s",
                request_id, len(sub_intents),
                "\n".join(f"  [{si['section']}] {si['text']}" for si in sub_intents),
            )
    _mark("decompose")

    # Outfit-borrow attribution (Tier 2 of outfit resolution). When the
    # agent dispatched a donor character via character_queries (e.g.
    # "cammy in chun-li's outfit" → queries=[cammy_white, chun-li]),
    # both bios load. Without this pass, multi_char fires and the model
    # emits 2girls. _resolve_bio_roles detects the donor (no character:
    # intent + possessive mention in an outfit:/strip: intent), copies
    # its outfit to the subject's user_requested_outfit, and drops the
    # donor from the bios list.
    bios = _resolve_bio_roles(
        bios, sub_intents or [], request_id, user_request=user_request,
    )

    # MODIFY mode: user is asking to alter the existing outfit, not
    # replace it with a stock one ('bikini version of the maid', 'make
    # it sexier', etc.). Skip tier-3 DB lookup (no auto-attach) and
    # node_prompt rewrite (no pre-resolved body). Always set the strip
    # flag so the bio.default_outfit doesn't pollute patch_user. A
    # modify_outfit_hint string is built and surfaced to the patch
    # model as an explicit reasoning task.
    modify_intent = _detect_modify_outfit_intent(current_user_text)
    modify_outfit_hint = ""
    if modify_intent:
        existing_outfit_match = _NODE_OUTFIT_HEADER_RE.search(node_prompt or "")
        existing_name = existing_outfit_match.group(1).strip() if existing_outfit_match else ""
        # Pull the existing outfit body lines straight out of node_prompt
        # so the model has the precise base to modify.
        existing_body = ""
        if existing_outfit_match:
            tail = (node_prompt or "")[existing_outfit_match.end():]
            body_lines = []
            for line in tail.lstrip("\n").splitlines():
                stripped = line.strip()
                if stripped.startswith("//"):
                    break
                if re.match(r"^\s*Negative\s+Prompt\s*:", line, re.IGNORECASE):
                    break
                if stripped:
                    body_lines.append(stripped)
            existing_body = ", ".join(body_lines)
        # Slot-aware classification: split the existing outfit's tokens
        # into (body_garments, preserve_others). Body garments are
        # what the modifier replaces; the rest must survive verbatim.
        # When the existing outfit matches a generic_outfits DB entry,
        # use the DB's slot decomposition — it lets us exclude tokens
        # from the `modifiers` slot. Modifiers are descriptive (e.g.
        # frilly_apron describes how the apron looks), not separate
        # physical items; surfacing them as preserve tokens leads the
        # model to emit both the base item AND its modifier-form, which
        # render as conflicting layers (frilly_apron renders as a
        # dress-shape over a bikini). Falls back to suffix-matching on
        # raw tokens for custom outfits the DB doesn't know about.
        body_tokens = [t.strip() for t in existing_body.split(",") if t.strip()]
        from . import tag_builder as _tb_modify
        db_outfit = _tb_modify.find_generic_outfit(existing_name) if existing_name else None
        if db_outfit and db_outfit.get("slots"):
            body_garments: list[str] = []
            preserved_tokens: list[str] = []
            db_phrases: set[str] = set()
            for slot in db_outfit["slots"]:
                slot_kind = (slot.get("slot") or "").strip().lower()
                item = (slot.get("item") or "").strip().lower()
                phrase = (slot.get("source_phrase") or "").strip()
                if not phrase:
                    continue
                db_phrases.add(phrase.lower())
                if slot_kind == "modifiers":
                    continue
                if item in set(_BODY_GARMENT_SUFFIXES):
                    body_garments.append(phrase)
                else:
                    preserved_tokens.append(phrase)
            # Catch user-added tokens not in the DB seed — classify via
            # suffix fallback so they don't silently disappear.
            for t in body_tokens:
                bare = re.sub(r"\s*:\s*[\d.]+\s*\)?\s*$", "", t.strip("()").strip()).strip().lower()
                if bare in db_phrases:
                    continue
                if _is_body_garment_token(t):
                    body_garments.append(t)
                else:
                    preserved_tokens.append(t)
        else:
            body_garments, preserved_tokens = _classify_outfit_tokens_for_modify(body_tokens)
        preserve_list = ", ".join(preserved_tokens) if preserved_tokens else "(none)"
        replace_list = ", ".join(body_garments) if body_garments else "(none — outfit has no body garment to swap)"
        modifier_phrase = modify_intent["phrase"]
        # Extract the principal modifier noun from the phrase (best-effort).
        # 'make it a bikini version' -> 'bikini'. 'goth variant of' -> 'goth'.
        # Used to make the replacement instruction concrete.
        principal_match = re.search(
            r"\b(bikini|skimpy|topless|nude|naked|casual|formal|fancy|sexy|"
            r"kinky|slutty|cute|edgy|gothic|goth|punk|tomboy|girly|lewd|"
            r"alternate|alt|leather|latex|wet|sheer|transparent|short|"
            r"shorter|longer|tighter|looser|sexier|cuter|edgier|skimpier|"
            r"\w{3,}er)\b",
            modifier_phrase, re.IGNORECASE,
        )
        principal_modifier = (
            principal_match.group(1).lower() if principal_match else "modified"
        )
        # Build a concrete replacement-tag suggestion list keyed off the
        # principal modifier. Concrete suggestions reduce the model's
        # ambiguity tax — it stops asking 'what should I emit' and just
        # picks from the list.
        replacement_examples = {
            "bikini": "bikini_top, bikini_bottom, OR a single bikini token "
                      "themed to the base aesthetic (e.g., frilly_bikini, "
                      "black_bikini with maid frills, white_lace_bikini)",
            "topless": "(drop the body garment with no replacement — "
                       "topless is the rare case where REMOVE means just "
                       "remove)",
            "nude": "(drop the body garment with no replacement)",
            "naked": "(drop the body garment with no replacement)",
            "leather": "leather_dress, leather_pants, leather_jacket, OR "
                       "the leather equivalent of the base body garment",
            "latex": "latex_dress, latex_bodysuit, OR latex equivalent",
            "goth": "black_dress, gothic_dress, dark_lace, OR dark "
                    "version of the base body garment",
            "casual": "t-shirt, jeans, OR casual equivalents",
            "formal": "evening_dress, ballgown, suit, OR formal equivalents",
        }
        replacement_hint = replacement_examples.get(
            principal_modifier,
            f"a {principal_modifier}-themed version of the base body garment "
            f"(use Danbooru tag forms, underscored, lowercase)",
        )
        is_topless_class = principal_modifier in {"topless", "nude", "naked"}
        modify_outfit_hint = (
            f"OUTFIT MODIFY MODE — apply a delta to the existing outfit\n\n"
            f"The user is modifying the existing // Outfit. Your job:\n"
            f"  (1) KEEP the base outfit's accessories/headwear/footwear "
            f"verbatim.\n"
            f"  (2) {'REMOVE' if is_topless_class else 'REPLACE'} the body "
            f"garment{'(s)' if not is_topless_class else ''} listed below "
            f"{'(no replacement — modifier means strip)' if is_topless_class else 'with the modifier-themed version'}.\n"
            f"  (3) Keep the base aesthetic — color palette, "
            f"frills/lace/ribbons if present.\n\n"
            f"  Base outfit name: {existing_name or '(none)'}\n"
            f"  User said: \"{modifier_phrase}\"\n"
            f"  Principal modifier: {principal_modifier}\n\n"
            f"KEEP (verbatim — must appear in your output // Outfit body):\n"
            f"  {preserve_list}\n\n"
            f"{'REMOVE' if is_topless_class else 'REPLACE'} (these are the "
            f"body garments {'to drop' if is_topless_class else 'to swap'}):\n"
            f"  {replace_list}\n\n"
            + (
                ""
                if is_topless_class
                else f"ADD (the new body garment to take their place):\n"
                     f"  {replacement_hint}\n\n"
            ) +
            f"OUTPUT FORMAT for // Outfit body — emit a comma-separated tag "
            f"line containing:\n"
            f"  - All KEEP tokens listed above (verbatim)\n"
            + (
                f"  - (no body garment — '{principal_modifier}' means the body is bare)\n"
                if is_topless_class else
                f"  - At least one ADD token replacing the body garment\n"
            ) +
            f"\n"
            f"CRITICAL: do NOT omit any KEEP token. "
            + (
                f"Do NOT add a body garment — '{principal_modifier}' "
                f"explicitly means there isn't one."
                if is_topless_class else
                f"You MUST add at least one body-garment-shaped token "
                f"to replace what was removed — the user wants the outfit "
                f"transformed, not stripped."
            )
        )
        dbg.info(
            "ai-patch[%s] MODIFY mode: phrase=%r base=%r preserve=%d replace=%d",
            request_id, modify_intent["phrase"], existing_name or "(none)",
            len(preserved_tokens), len(body_garments),
        )
        # Inject [strip] so user_strips_outfit fires; suppresses bio's
        # default-outfit slot list in patch_user.
        sub_intents = _ensure_strip_for_named_outfit_phrase(
            sub_intents, current_user_text, request_id,
        )
        if not any((si.get("section") or "").lower() == "strip"
                   for si in (sub_intents or [])):
            sub_intents = list(sub_intents or [])
            sub_intents.append({"section": "strip", "text": "modify-mode"})
    else:
        # Outfit resolution Tier 3: generic-outfit KB lookup. When the user
        # named a curated archetype (cowboy/maid/pirate/...) and tier 1+2
        # haven't already attached a user_requested_outfit, pull the slot
        # decomposition from tag_builder.generic_outfits and attach it to
        # the bio. Avoids canonical_resolver pulling scene-adjacent tags
        # (horse, gun) from "cowboy outfit" via bge embedding neighbors.
        sub_intents, bios = _apply_generic_outfit_to_bios(
            sub_intents, bios, current_user_text, request_id,
        )

        # Tier 4 strip-flag fallback: when the user named an outfit aesthetic
        # the generic-outfits DB doesn't recognize ('in a steampunk-cyberpunk
        # hybrid outfit'), tier-3 scan returns no match — but the user clearly
        # wants a fresh outfit, not bio defaults stacked with the LLM-vibed
        # additions. Inject a [strip] intent so user_strips_outfit fires and
        # bio.default_outfit slots get suppressed in patch_user. The model
        # then vibes the aesthetic from world knowledge alone.
        sub_intents = _ensure_strip_for_named_outfit_phrase(
            sub_intents, current_user_text, request_id,
        )

    # Step B: build the bio_known + modifier_canon sets so retrieval
    # doesn't surface tags already covered. Also detect which modifiers
    # are firing so retrieval can apply modifier-conflict filtering
    # (e.g. drop gestures-group tags when presenting_foot is firing).
    all_modifiers = _load_slot_modifiers()
    bio_known: set[str] = set()
    for b in bios or []:
        for t in (b.get("base_tags") or "").split(","):
            t = t.strip().lstrip("(").split(":", 1)[0]
            if t:
                bio_known.add(t.lower())
    modifier_canon = {m["canonical_tag"].lower() for m in all_modifiers}
    detected_modifiers = _detect_modifiers_in_text(user_request)
    applies_by_tag = {d["canonical_tag"]: d for d in detected_modifiers}

    # Step C: per-sub-intent retrieval. Two layers, additive:
    #
    # C1. Canonical resolver — LLM proposes Danbooru tags per sub-intent,
    #     validates against danbooru_tags + body-part variant swap.
    #     Solves the bge bare-form ranking bias where compound tags
    #     (`sitting_on_arm`) outrank bare canonicals (`sitting`) and
    #     the model picks user-verbatim phrasal forms over canonical.
    #     ~86% resolution rate on the test corpus vs ~18% for bge alone.
    #     Skipped sections: character (bio system handles), clear
    #     (no tag), style (template path).
    #
    # C2. bge / alias / literal-anchor retrieval (legacy) — fallback
    #     when the resolver returns empty or section is out-of-scope.
    #     Lands in "Related candidates" block below resolver hits.
    #
    # Skipped in natlang mode — tag candidates are tag-shaped and the
    # natlang user message doesn't include them.
    if prompt_style == "natural":
        tag_candidates = []
    else:
        # Filter out intents that were pre-resolved upstream (tier-3
        # generic-outfit lookup attaches the curated outfit directly to
        # the bio; running canonical_resolver / bge on the same intent
        # text would pull scene-adjacent tags like horse and gun back
        # into the candidate pool).
        retrieval_intents = [
            si for si in (sub_intents or []) if not si.get("pre_resolved")
        ]
        from . import canonical_resolver
        resolved_candidates = await canonical_resolver.resolve_intents_parallel(
            retrieval_intents, request_id, on_status=_status,
        )
        # Tags already covered by bio / modifier sets — surfacing them
        # in the candidate menu is noise. Resolver has no view of those
        # sets, so filter here.
        resolved_candidates = [
            c for c in resolved_candidates
            if (c.get("tag") or "").lower() not in bio_known
            and (c.get("tag") or "").lower() not in modifier_canon
        ]

        _status("Analyzing tag database")
        bge_candidates = _retrieve_tag_candidates(
            retrieval_intents, bio_known, modifier_canon,
            applies_by_tag=applies_by_tag,
            on_status=_status,
        )

        # Merge: resolved first (anchor block), then bge (filtered to
        # skip tags the resolver already surfaced).
        resolved_tags = {(c.get("tag") or "").lower() for c in resolved_candidates}
        merged_bge = [
            c for c in bge_candidates
            if (c.get("tag") or "").lower() not in resolved_tags
        ]
        tag_candidates = list(resolved_candidates) + merged_bge

        if tag_candidates:
            dbg.info(
                "ai-patch[%s] %d tag candidates "
                "(resolver=%d, bge=%d): %s",
                request_id, len(tag_candidates),
                len(resolved_candidates), len(merged_bge),
                ", ".join(
                    f"{c['tag']}({c['matched_via']}<-{c['matched_intent']!r})"
                    for c in tag_candidates[:10]
                ),
            )

        # Rewrite sub_intents text → resolved canonicals. The patch user
        # message's "Decomposed visual intents" block defaults to showing
        # the user's literal phrasing ([pose] focus_on_feet). Combined
        # with the user_request line and the literal phrasing pre-
        # underscored by the chat agent, that put `focus_on_feet` in
        # front of the patch model in three places vs `foot_focus` in
        # one, and the model echoed the literal — trace-check then
        # dropped it as untraceable, so the canonical never landed.
        # Replace the sub-intent text with its resolved canonical(s) so
        # the decomposed-intents block reinforces the canonical instead
        # of the literal. Sub-intents whose phrase didn't resolve keep
        # their original text.
        phrase_to_canonicals: dict[str, list[str]] = {}
        for c in resolved_candidates:
            intent = c.get("matched_intent") or ""
            tag = c.get("tag") or ""
            if intent and tag:
                phrase_to_canonicals.setdefault(intent, []).append(tag)
        if phrase_to_canonicals and sub_intents:
            rewritten: list[dict] = []
            for si in sub_intents:
                text = (si.get("text") or "").strip()
                canonicals = phrase_to_canonicals.get(text)
                if canonicals:
                    rewritten.append({**si, "text": ", ".join(canonicals)})
                else:
                    rewritten.append(si)
            sub_intents = rewritten
    _mark("retrieve")

    # Server-side outfit-section delta. When a bio's user_requested_outfit
    # differs from node_prompt's existing // Outfit section, rewrite the
    # section so patch-mode preservation honors the swap. Without this,
    # the model sees both 'preserve // Outfit: <old>' and 'use bio's
    # user_requested_outfit (<new>)' and chooses the preserved-old path,
    # causing the swap to silently no-op on multi-turn flows. Skipped
    # in MODIFY mode (no user_requested_outfit was attached; the model
    # is reasoning about the delta, not consuming a pre-resolved one).
    if not modify_intent:
        node_prompt = _rewrite_node_prompt_outfit_for_user_requested(
            node_prompt, bios, request_id,
        )

    # Server-side handling for `[clear] X` intents. Decompose emits these
    # for 'reset scene' / 'remove pose' / etc., but the patch model
    # would preserve the targeted section per patch-mode rules. Strip
    # the section from node_prompt server-side so 'clear' actually
    # clears.
    node_prompt = _apply_clear_intents_to_node_prompt(
        node_prompt, sub_intents, request_id,
    )

    system = _patch_system_prompt(
        bios=bios, has_node_prompt=bool(node_prompt),
        prompt_style=prompt_style,
    )
    user = _patch_user_message(
        node_prompt, user_request, bios=bios,
        sub_intents=sub_intents, tag_candidates=tag_candidates,
        prompt_style=prompt_style,
        modify_outfit_hint=modify_outfit_hint,
    )

    dbg.info(
        "ai-patch[%s] request: provider=%s node_chars=%d req_chars=%d bios=%d "
        "arch=%s prompt_style=%s style_alias=%s",
        request_id, provider, len(node_prompt), len(user_request), len(bios),
        arch or "(none)", prompt_style,
        (style_alias_hit and style_alias_hit.get("template_id")) or "(none)",
    )
    _dump(request_id, "patch_system", system)
    _dump(request_id, "patch_user", user)
    _mark("build_prompt")

    # ── Natlang render-mode short-circuit ───────────────────────────
    # Replace the patch-mode prose-surgery model call with deterministic
    # server-side composition. Server owns the structure (PromptState
    # + slot data); model is bypassed entirely for natlang mode. The
    # existing post-pass (style injection, neg merge, preserve_existing
    # _negatives) runs unchanged on the rendered sections — those are
    # section-level operations and don't care if the body came from a
    # composer or a model.
    if prompt_style == "natural":
        return await _run_natlang_v2(
            request_id, body, user_request, bios, node_prompt,
            style_template, provider, config, _t_request_start, _timing,
            _mark, _status, _dump, arch_prompts=arch_prompts,
        )

    _status("Building prompt")
    # Up to 3 attempts. The retry exists for the "thought hard then
    # emitted nothing" failure mode where reasoning_chars > 0 but body
    # is empty — common to abliterated/thinking-mode variants. Each
    # retry appends an output-forcing tail to the user message to nudge
    # past the reasoning step.
    max_attempts = 3
    raw = ""
    last_attempt = 0
    for attempt in range(1, max_attempts + 1):
        last_attempt = attempt
        attempt_user = user
        if attempt > 1:
            _status(f"Retrying ({attempt}/{max_attempts})")
            attempt_user = (
                user
                + "\n\nOutput the prompt body now. "
                "Do not reason. Begin with the first `// Section:` line."
            )
        # Reset reasoning counter per attempt so the failure-mode check
        # only reflects this attempt, not stale state from an earlier one.
        _request_reasoning_chars.pop(request_id, None)
        try:
            raw = await _run_generation(
                request_id, provider, config, system, attempt_user, [],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("ai-patch[%s] provider failed", request_id)
            return error_response(str(e) or "provider call failed", 500)

        raw = (raw or "").strip()
        reasoning_chars = _request_reasoning_chars.get(request_id, 0)
        if raw:
            break
        # Empty body. If reasoning was non-empty, it's the thinking-only
        # failure mode and a retry is justified. If reasoning was also
        # empty, the model genuinely returned nothing — retrying with the
        # same setup likely won't help, so bail.
        if reasoning_chars <= 0:
            logger.warning(
                "ai-patch[%s] empty body and empty reasoning on attempt %d — not retrying",
                request_id, attempt,
            )
            break
        logger.info(
            "ai-patch[%s] attempt %d: empty body, reasoning_chars=%d — will retry",
            request_id, attempt, reasoning_chars,
        )

    if not raw:
        reasoning_chars = _request_reasoning_chars.get(request_id, 0)
        if reasoning_chars > 0:
            return error_response(
                "Model emitted only reasoning, no output. "
                "Try a non-thinking model variant or `ollama stop` and reload.",
                502,
            )
        return error_response("empty response from model", 502)
    if last_attempt > 1:
        logger.info("ai-patch[%s] succeeded on attempt %d/%d",
                    request_id, last_attempt, max_attempts)

    _dump(request_id, "patch_raw", raw)
    _mark("inference")

    sections = _parse_sectioned_output(raw)

    # Provenance set: tokens the user already had in node_prompt.
    node_pos_set, _ = _node_prompt_token_sets(node_prompt)

    # Backstop: drop any // section whose name isn't one of the schema-
    # allowed ones AND contains no node_prompt tokens. Section-level —
    # works in both modes because canonical headers (Character/Outfit/
    # Pose/Style) match the prefix whitelist regardless of body shape.
    sections = _filter_allowed_sections(sections, node_pos=node_pos_set)

    # Character-swap enforcement: when sub_intents say the user wants
    # to swap out the existing character, drop any // Character: <X>
    # section whose canonical isn't in the current bios. The 8B patch
    # model occasionally appends the new character's section instead
    # of replacing the prior one — leaves two // Character blocks with
    # the wrong identity tags applied to the swap target.
    sections = _enforce_character_swap(
        sections, bios, sub_intents, request_id,
    )

    if prompt_style != "natural":
        # Tag-mode-only token-level filters. Each operates on the
        # comma-split `tokens` list inside each section — meaningless on
        # prose section bodies, where the body is one paragraph and
        # comma-fragments aren't independently droppable concepts.

        # Drop sections the user didn't ask for AND that weren't in the
        # input node_prompt. Catches qwen3-vl:8b's habit of speculating
        # // Pose/// Setting/// Quality on build-mode bare requests.
        sections = _filter_unrequested_sections(
            sections, sub_intents, node_prompt, bios, request_id,
        )

        # Outfit borrow: if a bio is marked _outfit_source_only, the
        # patch model is supposed to rewrite // Outfit using that bio's
        # slots. PATCH MODE preservation + OUTFIT HEADER rule fight the
        # rename — model emits old header `// Outfit: Delta Red` with
        # new body. Server-side rewrite makes the borrow deterministic.
        sections = _apply_outfit_borrow_overwrite(sections, bios, request_id)

        # Same-character-multi-outfit expansion: if bios has duplicate
        # tag entries (same canonical character with different
        # user_requested_outfit picks), duplicate the // Character
        # section per instance with distinct outfit names. Runs BEFORE
        # the outfit-auto-inject so the duplicates are in place when
        # the outfit-auto-inject scans for missing outfits.
        sections = _ensure_same_char_multi_outfit_sections(
            sections, bios, request_id,
        )

        # Auto-inject missing outfits. Multi-char-build failure mode:
        # qwen3-vl:8b emits both // Character sections but skips both
        # // Outfit sections, leaving SDXL to freelance clothing. Run
        # AFTER the outfit-borrow rewrite so the borrow case is already
        # handled and we only inject for true subject characters.
        sections = _ensure_bio_outfits_emitted(sections, bios, request_id)

        sections = _enforce_applies_modifiers(sections, applies_by_tag, request_id)

        # Modifier slot-clear post-pass. Runs AFTER
        # _enforce_applies_modifiers (which adds the modifier canonical
        # to the right section) so the drop-pass sees the modifier
        # canonical-token already in place and won't drop it as a stray
        # legwear/footwear fill. `wearing only X` strip semantics are
        # handled by the patch system prompt directly — no server-side
        # narrowing pass.
        sections = _apply_modifier_clear_post_pass(
            sections, user_request, bios, request_id,
        )
        sections = _apply_pose_anchor_override_post_pass(
            sections, user_request, node_prompt, request_id,
        )

        displaced_modifiers = _resolve_slot_displacements(
            user_request, node_prompt=node_prompt,
        )
        sections = _drop_displaced_modifiers(sections, displaced_modifiers, request_id)

        sections = _resolve_posture_conflicts(
            sections, user_request, node_prompt, request_id,
        )

        sections = _drop_untraceable_tokens(
            sections, user_request, bios, _load_slot_modifiers(), request_id,
            node_prompt=node_prompt,
        )

        sections = _drop_misplaced_tokens(sections, request_id)

        sections = _restore_weighted_parens(sections)

        sections = _enforce_default_outfit_negation(sections, bios)

        # Multi-char structural reorder: pair each `// Outfit: <canon>`
        # with its `// Character: <canon>` so the BREAK insertion
        # below lands at correct chunk boundaries. The 8B patch model
        # sometimes emits all character sections first then all outfit
        # sections — without this, sagat's chunk pulls in ryu's outfit.
        sections = _reorder_multi_char_sections(sections, bios, request_id)

        # Multi-char deterministic composition: replace per-character
        # subject counts with the canonical aggregate computed from
        # bios (1boy + 1girl -> `1boy, 1girl`, not `2girls`), drop
        # `solo` tokens that contradict multi-character composition,
        # and insert `BREAK` chunk separators between character
        # blocks. No-op for single-char output (bios < 2).
        sections = _enforce_multi_char_composition(sections, bios, request_id)

        # Dedup AFTER the composer: composer re-injects `(canon:1.1)`
        # into character sections. If a swap left that token in negs,
        # dedup must run after the re-injection to catch it.
        sections = _dedupe_negatives_from_positives(sections)

        # Belt-and-suspenders franchise strip — patch model echoes
        # franchise names into // Setting / Scene and // Style from
        # its world knowledge even after we franchise-strip the
        # user_request line. Drop deterministically.
        sections = _strip_franchise_tokens_from_scene_style(sections, request_id)

    # Section-level: preserve user's existing negative tokens (negs are
    # tag-shaped in both modes, so this works either way).
    sections = _preserve_existing_negatives(sections, node_prompt)

    # Character-swap cleanup: drop preserved negatives that match the
    # PRIOR character's default-outfit slot phrases. Without this, a
    # cammy -> mythra swap leaves cammy's `green leotard, red gloves,
    # ...` negs persisting forever (auto-added by an earlier turn's
    # default-outfit negation, no longer relevant). The new character's
    # default negs were already added by `_enforce_default_outfit_negation`
    # above so they survive this scrub.
    sections = _scrub_prior_character_default_negs(
        sections, bios, node_prompt, request_id,
    )

    # Stage B3/B5: server-side `// Style:` section injection + template
    # negative merge. Section-level — works in both modes because the
    # injected style section carries both `tokens` (for tag mode joined
    # comma-output) and `body_text` (for natlang prose-shaped output).
    if style_template:
        new_style = _build_style_section(style_template)
        if new_style:
            sections = _replace_or_append_style_section(sections, new_style)
            before_neg_count = sum(
                len(s.get("tokens") or [])
                for s in sections if s.get("is_negative")
            )
            sections = _merge_template_negatives(sections, style_template)
            after_neg_count = sum(
                len(s.get("tokens") or [])
                for s in sections if s.get("is_negative")
            )
            dbg.info(
                "ai-patch[%s] style injected: id=%s name=%s tokens=%d "
                "matched_alias=%r negs_added=%d",
                request_id,
                style_template.get("id"),
                style_template.get("name"),
                len(new_style["tokens"]),
                (style_alias_hit or {}).get("matched_alias"),
                max(0, after_neg_count - before_neg_count),
            )

    # Note on // Style / // Quality preservation: the patch system
    # prompt now allows the LLM to emit these sections directly and
    # tells it to preserve them verbatim across patch turns. We no
    # longer band-aid with `_preserve_existing_style_section` here —
    # block-then-restore was a fragile pattern (when restore failed,
    # content vanished silently). The LLM handles preservation as
    # part of its patch-mode rule; server-side `_replace_or_append_
    # style_section` above still OVERRIDES Style on alias / template
    # hit, which is the correct deterministic concern.

    tag_format = (body.get("tag_format") or "spaces").strip().lower()
    if tag_format == "spaces":
        for s in sections:
            s["tokens"] = [_format_output(t, tag_format) for t in s["tokens"]]
    for s in sections:
        s["tokens"] = [_strip_articles_from_phrasal(t) for t in s["tokens"]]

    output_text = "\n\n".join(
        f"{s['header']}\n{', '.join(s['tokens'])}" for s in sections
    )

    full_sections = sections
    sections = _drop_unchanged_positives(sections, node_prompt)
    sections = _add_positive_removal_chips(sections, full_sections, node_prompt)
    # `body_text` was populated by `_parse_sectioned_output` from the
    # raw LLM output and never updated when post-passes mutated
    # `tokens` (modifier-clear dropped `brown_boots`, strip narrowed,
    # etc.). The frontend chip view prefers `body_text` for prose
    # rendering when set — which leaks the LLM's pre-post-pass body
    # into the diff display, showing dropped tokens that aren't
    # actually in the final output. Tag-mode chips should always
    # render from `tokens`; clearing `body_text` forces the
    # frontend's fallback branch to fire.
    for s in sections:
        if not s.get("is_negative"):
            s["body_text"] = ""

    _mark("post")

    dbg.info(
        "ai-patch[%s] response: sections=%d tokens=%d output_chars=%d",
        request_id, len(sections),
        sum(len(s["tokens"]) for s in sections),
        len(output_text),
    )

    total = time.perf_counter() - _t_request_start
    dbg.info(
        "ai-patch[%s] timing: setup=%.2fs warmup=%.2fs decompose=%.2fs "
        "retrieve=%.2fs build_prompt=%.2fs inference=%.2fs post=%.2fs "
        "total=%.2fs",
        request_id,
        _timing.get("setup", 0.0),
        _timing.get("warmup", 0.0),
        _timing.get("decompose", 0.0),
        _timing.get("retrieve", 0.0),
        _timing.get("build_prompt", 0.0),
        _timing.get("inference", 0.0),
        _timing.get("post", 0.0),
        total,
    )

    # Phase 1 of render-flow plan: accept and echo prompt_state. The
    # parser/composer/render mutations land in subsequent phases. For now
    # the frontend can persist what it sends; nothing structural changes.
    incoming_prompt_state = body.get("prompt_state") or None

    return web.json_response({
        "request_id": request_id,
        "output_text": output_text,
        "sections": sections,
        "raw": raw,
        "prompt_state": incoming_prompt_state,
    })
