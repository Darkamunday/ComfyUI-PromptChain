# Kept separate from per-system manifests (tags, models, etc.) so onboarding
# state and global prefs survive when those are cleared or rebuilt.
from __future__ import annotations

import json
import logging
from pathlib import Path

import folder_paths

from .api_utils import atomic_write_json

_logger = logging.getLogger("promptchain.config")


def _config_path() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain" / "config.json"


def load() -> dict:
    path = _config_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _logger.warning("PromptChain config parse failed at %s: %s", path, e)
    return {}


def save(config: dict):
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, config)


def is_onboarded() -> bool:
    return load().get("onboarded", False)


def set_onboarded():
    config = load()
    config["onboarded"] = True
    save(config)


# Distinct from `onboarded` so a user who skips AI setup during the splash
# still gets re-prompted when they open the AI Assistant — until they
# explicitly dismiss it there.
def is_ai_setup_dismissed() -> bool:
    return load().get("ai_setup_dismissed", False)


def set_ai_setup_dismissed():
    config = load()
    config["ai_setup_dismissed"] = True
    save(config)
