import sys
import os

# Vendored into PromptChain from ssitu/ComfyUI_UltimateSDUpscale. The upstream
# repositories/ultimate_sd_upscale/ is a git submodule that the original
# __init__ downloaded from Coyote-A's master.zip on first run if empty; we ship
# that script pre-filled (see repositories/ultimate_sd_upscale/scripts/), so the
# network fetch is removed — the installer never reaches out for node code.

# The shim below is upstream's own import isolation: nodes.py uses bare absolute
# imports (`from utils import …`, `import modules.shared`, `from usdu_patch import
# usdu`), so we put this package's dir on sys.path, stash any conflicting
# top-level `modules`/`utils`, import, then restore everything. Non-destructive.

# Remove other custom_node paths from sys.path to avoid conflicts
custom_node_paths = [path for path in sys.path if "custom_node" in path]
original_sys_path = sys.path.copy()
for path in custom_node_paths:
    sys.path.remove(path)

# Add this repository's path to sys.path for third-party imports
repo_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, repo_dir)
original_modules = sys.modules.copy()

# Place aside potentially conflicting modules
modules_used = [
    "modules",
    "modules.devices",
    "modules.images",
    "modules.processing",
    "modules.scripts",
    "modules.shared",
    "modules.upscaler",
    "utils",
]
original_imported_modules = {}
for module in modules_used:
    if module in sys.modules:
        original_imported_modules[module] = sys.modules.pop(module)

# The bundled Auto1111 script (repositories/.../ultimate-upscale.py) does
# `import gradio as gr` at module top for its A1111 UI — the ui() method ComfyUI
# never calls (all gr.* uses live there). ComfyUI doesn't ship gradio, so on a
# clean install that bare import would crash the whole pack. Install a no-op
# stub for the import ONLY when gradio isn't actually present; the cleanup below
# removes it (it post-dates the original_modules snapshot), so a real gradio is
# never touched and the stub never lingers.
import importlib.util as _ilu
if "gradio" not in sys.modules and _ilu.find_spec("gradio") is None:
    class _GradioStub:
        def __getattr__(self, _name):
            return _GradioStub()
        def __call__(self, *args, **kwargs):
            return _GradioStub()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
    sys.modules["gradio"] = _GradioStub()

# Proceed with node setup
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

# Clean up imports
# Remove any new modules
modules_to_remove = []
for module in sys.modules:
    if module not in original_modules:
        modules_to_remove.append(module)
for module in modules_to_remove:
    del sys.modules[module]

# Restore original modules
sys.modules.update(original_imported_modules)

# Restore original sys.path
sys.path = original_sys_path
