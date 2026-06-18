"""Isolated vendored slice of comfyui_controlnet_aux (DepthAnything-V2 + Canny only).

Renamed `custom_controlnet_aux` -> `pcr_cnaux` so importing PromptChain's native
preprocessor nodes can NEVER shadow or get shadowed by a real comfyui_controlnet_aux
install. See _VENDORED.txt for what was extracted and why.

The depth/canny submodules use bare absolute imports (`from pcr_cnaux.util import …`,
`from pcr_cnaux.depth_anything_v2.dpt import …`). For those to resolve consistently
we load the whole slice through the top-level `pcr_cnaux` name: this file (imported
by PromptChain as `…vendor.pcr_cnaux`) scope-inserts the parent `vendor/` dir onto
sys.path — the same non-destructive trick vendor/usdu/__init__.py uses — imports
`pcr_cnaux.wrappers` absolutely so every internal `pcr_cnaux.*` import binds to the
identical package objects, then restores sys.path. The host environment is left
untouched: `import custom_controlnet_aux` still resolves to the genuine install.
"""
import sys
import os
import importlib

_repo_dir = os.path.dirname(os.path.realpath(__file__))      # …/vendor/pcr_cnaux
_vendor_dir = os.path.dirname(_repo_dir)                     # …/vendor

_original_sys_path = sys.path.copy()
_path_was_inserted = _vendor_dir not in sys.path
if _path_was_inserted:
    sys.path.insert(0, _vendor_dir)

try:
    _wrappers = importlib.import_module("pcr_cnaux.wrappers")
    NODE_CLASS_MAPPINGS = _wrappers.NODE_CLASS_MAPPINGS
    NODE_DISPLAY_NAME_MAPPINGS = _wrappers.NODE_DISPLAY_NAME_MAPPINGS
finally:
    # Restore sys.path so no host import path is polluted. The imported pcr_cnaux.*
    # modules stay in sys.modules (cheap, and keeps the live class references valid),
    # but they were never reachable without `vendor/` on the path, so the genuine
    # custom_controlnet_aux is unaffected.
    if _path_was_inserted:
        sys.path = _original_sys_path

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
