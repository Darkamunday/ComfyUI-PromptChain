# Shared utilities used across multiple route modules.

import logging
import re
import server

logger = logging.getLogger("promptchain.shared")

# Accepts either the 16-char quick_hash used by the fingerprint cache
# or a full 64-char SHA256.  Narrow per-endpoint if mixing the two
# becomes risky — callers currently treat the hash opaquely.
HASH_RE = re.compile(r"^[0-9a-f]{16,64}$")


def send_ws(event: str, data: dict):
    """Thread-safe WebSocket broadcast."""
    try:
        server.PromptServer.instance.send_sync(event, data)
    except Exception:
        logger.debug("WebSocket send failed for %s", event, exc_info=True)
