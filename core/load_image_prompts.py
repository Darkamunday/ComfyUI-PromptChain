# Indexed variants (__LoadImagePositive_1__, _2__, …) pick the Nth LoadImage
# node sorted by node id; the un-indexed form picks the one upstream of the
# KSampler's latent_image input — without that distinction, branches that mix
# multiple LoadImages couldn't address them deterministically.

import json
import os
import re

_LOAD_IMAGE_TYPES = {"LoadImage", "LoadImageOutput"}
_SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "FaceDetailer",
                  "FaceDetailerPipe", "SamplerCustomAdvanced", "UltimateSDUpscale"}

_KEYWORD_RE = re.compile(r"__LoadImage(Positive|Negative)(?:_(\d+))?__")


def resolve_load_image_keywords(prompt_text: str, execution_prompt: dict | None) -> dict | None:
    if not prompt_text or not execution_prompt:
        return None
    if "__LoadImage" not in prompt_text:
        return None

    matches = list(_KEYWORD_RE.finditer(prompt_text))
    if not matches:
        return None

    # collect all unique indices referenced (None = un-indexed)
    indices = set()
    for m in matches:
        indices.add(int(m.group(2)) if m.group(2) else None)

    load_images = _find_all_load_images(execution_prompt)
    if not load_images:
        return {m.group(0): "" for m in matches}

    # cache extracted prompts per image path to avoid re-reading
    _cache: dict[str, tuple[str, str]] = {}

    def _get_prompts(path: str) -> tuple[str, str]:
        if path not in _cache:
            _cache[path] = extract_prompts_from_file(path) if path else ("", "")
        return _cache[path]

    replacements = {}
    for m in matches:
        keyword = m.group(0)
        polarity = m.group(1)  # "Positive" or "Negative"
        idx = int(m.group(2)) if m.group(2) else None

        if idx is not None:
            # indexed: pick the Nth LoadImage (1-based)
            image_path = load_images[idx - 1] if 1 <= idx <= len(load_images) else None
        else:
            # un-indexed: pick the first (primary) LoadImage
            image_path = load_images[0] if load_images else None

        pos, neg = _get_prompts(image_path) if image_path else ("", "")
        replacements[keyword] = pos if polarity == "Positive" else neg

    return replacements


def _find_all_load_images(execution_prompt: dict) -> list[str]:
    # KSampler.latent_image's upstream LoadImage is the "primary" — keyword
    # resolution puts it first so an un-indexed __LoadImagePositive__ refers
    # to the image the user is actually sampling from, not whichever node
    # happens to have the lowest id.
    primary_path = None
    primary_nid = None
    for nid, node in execution_prompt.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type", "") not in _SAMPLER_TYPES:
            continue
        latent_ref = node.get("inputs", {}).get("latent_image")
        if isinstance(latent_ref, list) and len(latent_ref) == 2:
            primary_nid, primary_path = _trace_upstream_load_image(
                execution_prompt, str(latent_ref[0]))
            break

    # collect all LoadImage nodes, sorted by node id for stable ordering
    all_nodes = []
    for nid, node in execution_prompt.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type", "") not in _LOAD_IMAGE_TYPES:
            continue
        image_name = node.get("inputs", {}).get("image", "")
        if not image_name:
            continue
        path = _resolve_image_path(image_name)
        if path:
            all_nodes.append((nid, path))

    # Sort by leading digit prefix, then alphabetically.  Without the
    # string tiebreaker, all non-numeric IDs hashed to 0 and their order
    # was arbitrary — which mattered for "which image is primary" logic
    # below.
    def _sort_key(nid):
        digits = "".join(c for c in nid if c.isdigit())
        return (int(digits) if digits else 0, nid)
    all_nodes.sort(key=lambda x: _sort_key(x[0]))

    # put primary first if found
    if primary_path and primary_nid:
        result = [primary_path]
        for nid, path in all_nodes:
            if nid != primary_nid:
                result.append(path)
        return result

    return [path for _, path in all_nodes]


def _trace_upstream_load_image(prompt: dict, node_id: str) -> tuple[str | None, str | None]:
    visited = set()
    queue = [node_id]
    while queue:
        nid = queue.pop(0)
        if nid in visited:
            continue
        visited.add(nid)
        node = prompt.get(nid)
        if not isinstance(node, dict):
            continue
        if node.get("class_type", "") in _LOAD_IMAGE_TYPES:
            image_name = node.get("inputs", {}).get("image", "")
            if image_name:
                return nid, _resolve_image_path(image_name)
        for val in node.get("inputs", {}).values():
            if isinstance(val, list) and len(val) == 2:
                queue.append(str(val[0]))
    return None, None


def _resolve_image_path(image_name: str) -> str | None:
    try:
        import folder_paths
        return folder_paths.get_annotated_filepath(image_name)
    except Exception:
        return None


def extract_prompts_from_image(img) -> tuple[str, str]:
    """Read pos/neg prompts from an open PIL.Image's ComfyUI metadata."""
    raw_prompt = img.info.get("prompt", "")
    raw_workflow = img.info.get("workflow", "")
    if not raw_prompt:
        return "", ""
    try:
        prompt_data = json.loads(raw_prompt)
        workflow_data = json.loads(raw_workflow) if raw_workflow else None
    except Exception:
        return "", ""
    return _trace_prompts(prompt_data, workflow_data)


def extract_prompts_from_file(image_path: str) -> tuple[str, str]:
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return extract_prompts_from_image(img)
    except Exception:
        return "", ""


def extract_prompts_from_bytes(raw: bytes) -> tuple[str, str]:
    if not raw:
        return "", ""
    try:
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(raw)) as img:
            return extract_prompts_from_image(img)
    except Exception:
        return "", ""


def _trace_prompts(prompt_data: dict, workflow_data: dict | None) -> tuple[str, str]:
    wf_props = {}
    if workflow_data:
        for wf_node in workflow_data.get("nodes", []):
            wf_props[str(wf_node.get("id", ""))] = wf_node.get("properties", {})

    sampler = None
    for nid, node in prompt_data.items():
        ct = node.get("class_type", "")
        if "Sampler" in ct or "sampler" in ct:
            sampler = node
            break

    if not sampler:
        return "", ""

    def resolve_text(node_id, slot, depth=0):
        if depth > 20:
            return ""
        node_id = str(node_id)
        node = prompt_data.get(node_id)
        if not node:
            return ""

        ct = node.get("class_type", "")

        if "PromptChain" in ct:
            props = wf_props.get(node_id, {})
            if slot == 1 and props.get("pcrCompiledOutput"):
                return props["pcrCompiledOutput"]
            elif slot == 2 and props.get("pcrCompiledNegOutput"):
                return props["pcrCompiledNegOutput"]
            # fallback for older images without compiled outputs:
            # use the raw prompt text and split on Negative Prompt:
            raw = node.get("inputs", {}).get("prompt", "")
            if raw:
                import re
                parts = re.split(r"Negative Prompt:", raw, maxsplit=1, flags=re.IGNORECASE)
                pos_part = parts[0].strip() if parts else ""
                neg_part = parts[1].strip() if len(parts) > 1 else ""
                return pos_part if slot == 1 else neg_part

        # CLIP-like encode nodes — check "text" and "prompt" fields.
        # Covers CLIPTextEncode, TextEncodeQwenImageEditPlus, etc.
        if "Encode" in ct or "CLIP" in ct:
            for field in ("text", "prompt"):
                text_val = node.get("inputs", {}).get(field)
                if text_val is None:
                    continue
                if isinstance(text_val, str):
                    return text_val
                if isinstance(text_val, list) and len(text_val) == 2:
                    return resolve_text(text_val[0], text_val[1], depth + 1)
            return ""

        # Unknown-class node between sampler and encode (FluxGuidance,
        # ConditioningZeroOut, ConditioningCombine, etc.). Recurse
        # through link-typed inputs, preferring conditioning-ish names,
        # before falling back to string scrape. Without this, a bare
        # string input on a passthrough node (e.g. a scheduler's
        # "index_timestep_zero" mode) gets returned as the prompt.
        inputs = node.get("inputs", {})
        preferred, other = [], []
        for name, val in inputs.items():
            if not (isinstance(val, list) and len(val) == 2):
                continue
            low = name.lower()
            if "cond" in low or low in ("positive", "negative", "prompt", "text"):
                preferred.append(val)
            else:
                other.append(val)
        for ref in preferred + other:
            text = resolve_text(ref[0], ref[1], depth + 1)
            if text:
                return text

        # Bare-string fallback for nodes that don't expose conditioning
        # links but still carry a raw prompt string (custom text-primitive
        # nodes, etc.). Require a space so we reject mode/parameter
        # identifiers like "index_timestep_zero" that would otherwise
        # surface as the negative prompt when the graph goes through a
        # non-text conditioning node.
        for val in inputs.values():
            if isinstance(val, str) and len(val) > 5 and " " in val:
                return val

        return ""

    inputs = sampler.get("inputs", {})
    positive = negative = ""

    pos_ref = inputs.get("positive")
    neg_ref = inputs.get("negative")

    if isinstance(pos_ref, list) and len(pos_ref) == 2:
        positive = resolve_text(pos_ref[0], pos_ref[1])
    if isinstance(neg_ref, list) and len(neg_ref) == 2:
        negative = resolve_text(neg_ref[0], neg_ref[1])

    return positive, negative
