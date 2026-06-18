# Inpaint/edit transient input files.
#
# The viewer's inpaint + Edit→Inpaint flows upload two kinds of throwaway file
# into input/promptchain_inpaint: the painted mask (promptchain_mask_<hash>.png,
# read by LoadImageMask) and, for the composite + condition references, a source
# image (promptchain_ref_<hash>.<ext>, read by LoadImage). They are scoped to a
# subfolder — not the input ROOT — so they never pollute LoadImage's combo and
# can be swept as a group. Content-addressed names dedupe identical bytes.
#
# A render that actually CONSUMES one pins it with a `<file>.keep` (refs in
# history_db._resolve_and_register_parent, masks in edit_api.save_temp_image), so
# saved outputs stay re-applicable and lineage parents stay resolvable. Abandoned
# attempts (modal closed without saving) are collected by an age sweep at server
# start — the same model as input/promptchain_pose (see pose_api).

import glob
import os
import time

import folder_paths

SUBFOLDER = "promptchain_inpaint"
_SWEEP_MAX_AGE_DAYS = 30
_TRANSIENT_PREFIXES = ("promptchain_mask_", "promptchain_ref_")


def inpaint_dir() -> str:
    return os.path.join(folder_paths.get_input_directory(), SUBFOLDER)


def _scoped_path(value: str) -> str | None:
    """Absolute path for a LoadImage(-Mask) widget value IF it names a file in our
    subfolder, else None — so pinning can never touch a file outside the scoped
    namespace. Accepts only a bare 'foo.png' or our own 'promptchain_inpaint/foo.png'
    (any other directory component means the value isn't ours)."""
    dirpart, base = os.path.split((value or "").replace("\\", "/"))
    if not base or dirpart not in ("", SUBFOLDER):
        return None
    root = os.path.normpath(inpaint_dir())
    path = os.path.normpath(os.path.join(root, base))
    return path if path.startswith(root + os.sep) else None


def pin(abspath: str) -> None:
    """Drop a `<file>.keep` beside an absolute path so the age sweep spares it —
    no-op unless the path is a real file inside the scoped subfolder."""
    root = os.path.normpath(inpaint_dir())
    path = os.path.normpath(abspath or "")
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return
    try:
        open(path + ".keep", "a").close()
    except OSError:
        pass


def pin_value(value: str) -> None:
    """Pin by widget value (mask/ref filename, possibly subfolder-prefixed)."""
    path = _scoped_path(value)
    if path and os.path.isfile(path):
        pin(path)


def _sweep() -> None:
    d = inpaint_dir()
    if not os.path.isdir(d):
        return
    cutoff = time.time() - _SWEEP_MAX_AGE_DAYS * 86400
    swept = 0
    for prefix in _TRANSIENT_PREFIXES:
        for path in glob.glob(os.path.join(glob.escape(d), prefix + "*")):
            if path.endswith(".keep") or os.path.isfile(path + ".keep"):
                continue  # pinned by a saved render — keep indefinitely
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    swept += 1
            except OSError:
                pass
    # Drop orphaned pins whose file is already gone (hand-deleted, etc.). Scoped
    # to our own prefixes so a future, unrelated .keep producer here is left alone.
    for prefix in _TRANSIENT_PREFIXES:
        for marker in glob.glob(os.path.join(glob.escape(d), prefix + "*.keep")):
            if not os.path.exists(marker[:-5]):
                try:
                    os.remove(marker)
                except OSError:
                    pass
    if swept:
        print(f"[PromptChain] swept {swept} aged inpaint transient(s) older than {_SWEEP_MAX_AGE_DAYS} days")


_sweep()
