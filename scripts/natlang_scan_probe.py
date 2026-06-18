"""Scan probe — classify each sentence in a prompt by concept.

One narrow LLM call per turn (not per intent). Takes the full prompt
text and asks the model to label each sentence with one of the
canonical concepts (or omit). Output is a flat mapping that downstream
dispatch reads to decide where edits go.

This is the missing step between decompose and locate-infill. The
9B can do classification reliably; what it can't do reliably is
combined `is-it-present? AND what's-the-delta?` in one call.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_scan_probe.py
  python scripts/natlang_scan_probe.py <filter>
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _StubRoutes:
    def _passthrough(self, _p):
        def w(f): return f
        return w
    post = get = put = delete = patch = head = options = _passthrough


sys.modules.setdefault(
    "folder_paths",
    types.SimpleNamespace(
        folder_names_and_paths={},
        get_folder_paths=lambda x: [],
        get_full_path=lambda *a, **k: None,
        models_dir="/tmp",
        get_user_directory=lambda: os.path.join(ROOT, "..", "..", "user"),
        base_path=os.path.join(ROOT, "..", ".."),
    ),
)
sys.modules.setdefault(
    "server",
    types.SimpleNamespace(PromptServer=types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=_StubRoutes(),
                                       send_sync=lambda *a, **k: None),
    )),
)

from core import ai_api  # noqa: E402


PROVIDER = "local"
CONFIG = {"local": {"base_url": "http://localhost:11434/v1",
                    "model": "qwen3-vl:8b-instruct"}}


# Concepts the scan classifies into. Same vocabulary as decompose so
# downstream dispatch can look up by name.
SCAN_CONCEPTS = (
    "character", "outfit", "pose", "expression",
    "scene", "style", "quality", "negative",
)


SCAN_SYSTEM = """You are a structural classifier. You receive a free-prose image-generation prompt and label which concept each sentence (or comma-clause) belongs to.

The classification vocabulary — these are the ONLY labels you may use:
  character   — named subject + physical traits (hair, eyes, build, scars, etc.)
  outfit      — clothing, footwear, gloves, hat, accessories, body paint, body-state modifiers (barefoot/topless/etc.)
  pose        — body position, action, gesture, gaze direction, what they're doing
  expression  — facial affect (smiling, frowning, sultry, neutral)
  scene       — environment, location, background, time-of-day, weather, lighting tied to the scene
  style       — rendering aesthetic (anime, photorealism, oil painting, cinematic)
  quality     — quality tokens (sharp focus, masterpiece, depth of field, high fidelity)
  negative    — content inside a `Negative Prompt:` block

Important distinctions:
  - "Cinematic studio lighting" is STYLE (rendering aesthetic), not scene.
  - "At a beach at sunset" is SCENE.
  - "Standing in a fighting stance" is POSE.
  - "Smiling softly" is EXPRESSION.
  - Body-state words like `barefoot` go with OUTFIT (they belong to the worn-state region), not pose.

Output format — ONE LINE per concept that appears in the prompt. Verbatim text from the prompt, no paraphrasing. Concepts that aren't present are simply omitted (do NOT write `<concept>: (not present)` or similar — just leave them out).

  character: <verbatim sentence(s) from the prompt>
  outfit: <verbatim sentence(s) from the prompt>
  pose: <verbatim sentence(s) from the prompt>
  expression: <verbatim sentence(s) from the prompt>
  scene: <verbatim sentence(s) from the prompt>
  style: <verbatim sentence(s) from the prompt>
  quality: <verbatim sentence(s) from the prompt>
  negative: <verbatim body of the Negative Prompt block>

Multi-sentence concepts: if several consecutive sentences all describe the same concept (e.g. three sentences of style/quality prose all about rendering), join them with a space in the value. Keep them verbatim.

Concepts that are NOT in the prompt MUST be omitted. Don't invent them. Don't write a placeholder.

No commentary, no markdown fences. Just the labeled lines."""


def _parse_scan(raw: str) -> dict:
    """Parse `concept: sentence` lines into a dict. Missing concepts
    map to None."""
    text = (raw or "").strip()
    out = {c: None for c in SCAN_CONCEPTS}
    if not text:
        return out
    if text.startswith("```"):
        ls = text.splitlines()
        if ls and ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        text = "\n".join(ls).strip()
    current = None
    buf: list[str] = []

    def _flush():
        if current is not None:
            v = "\n".join(buf).strip()
            # Accept either an omitted line or an explicit absence marker
            # as "absent". Some sampling produces "(not present)" / "(none)"
            # / "(absent)" / "n/a" even when told to omit.
            v_norm = re.sub(r"[\(\)\s]+", "", v.lower())
            if v and v_norm not in ("notpresent", "absent", "none",
                                     "null", "na", "nothing", "empty"):
                out[current] = v
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z]+)\s*:\s*(.*)$", line)
        if m and m.group(1).strip().lower() in SCAN_CONCEPTS:
            _flush()
            current = m.group(1).strip().lower()
            buf = [m.group(2)] if m.group(2).strip() else []
        else:
            if current is not None:
                buf.append(line)
    _flush()
    return out


async def scan_prompt(prompt: str) -> tuple[dict, str]:
    raw = await ai_api._run_generation(
        f"scan-{abs(hash(prompt)) % 10000}", PROVIDER, CONFIG,
        SCAN_SYSTEM, f"Prompt to classify:\n{prompt}", [],
    )
    return _parse_scan(raw), (raw or "")


# ── Test fixtures ─────────────────────────────────────────────────


FLAT_CAMMY = """Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, pale light skin, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar. light blue sleeveless thong leotard with stiff tight mock-neck, thick sweater fabric material with ribbed texture on leotard, high-cut leotard exposing upper thigh and open upper back exposing shoulder blades, knee-high brown leather boots, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs. Hyperrealistic anime style blending photorealism with anime aesthetics. Realistic skin texture with subsurface scattering, expressive anime eyes with realistic reflections. Cinematic studio lighting, detailed hair strands, depth of field. High fidelity rendering, sharp focus.

Negative Prompt:
blurry, low quality, jpeg artifacts, watermark, text, logo, bad anatomy, deformed, distorted, flat colors, flat lighting, pure cartoon, chibi, super deformed, sketchy, unfinished"""


FLAT_CAMMY_WITH_POSE = """Cammy White from Street Fighter, female, blonde hair, blue eyes, athletic body. Wearing a blue leotard and brown boots. Sitting on the floor with her legs crossed. Hyperrealistic anime style.

Negative Prompt:
blurry"""


SIMPLE = """A tall warrior, brown hair. Wearing red armor. Photorealistic cinematic style.

Negative Prompt:
blurry"""


FIXTURES = [
    {
        "name": "flat_cammy_no_pose",
        "prompt": FLAT_CAMMY,
        "expect_present": ["character", "outfit", "style", "negative"],
        "expect_absent": ["pose", "expression", "scene"],
        "expect_contains": {
            "character": ["Cammy White", "blonde hair", "scar"],
            "outfit": ["leotard", "knee-high brown leather boots", "gauntlets"],
            "style": ["Hyperrealistic anime style"],
            "negative": ["blurry", "watermark"],
        },
    },
    {
        "name": "flat_cammy_with_pose",
        "prompt": FLAT_CAMMY_WITH_POSE,
        "expect_present": ["character", "outfit", "pose", "style", "negative"],
        "expect_absent": ["expression", "scene"],
        "expect_contains": {
            "character": ["Cammy White"],
            "pose": ["Sitting on the floor"],
        },
    },
    {
        "name": "simple_warrior",
        "prompt": SIMPLE,
        "expect_present": ["character", "outfit", "style", "negative"],
        "expect_absent": ["pose", "expression", "scene"],
        "expect_contains": {
            "character": ["tall warrior", "brown hair"],
            "outfit": ["red armor"],
            "style": ["Photorealistic"],
        },
    },
]


def _print_block(label: str, body: str) -> None:
    print(f"\n--- {label} ---")
    for line in (body or "").splitlines():
        print(f"  {line}")
    if not body:
        print("  (empty)")


def _check(fx: dict, scan: dict) -> list[str]:
    failures = []
    for c in fx.get("expect_present", []):
        if not (scan.get(c) or "").strip():
            failures.append(f"expected {c} present, got None")
    for c in fx.get("expect_absent", []):
        if (scan.get(c) or "").strip():
            failures.append(f"expected {c} ABSENT, got {scan[c]!r}")
    for c, words in fx.get("expect_contains", {}).items():
        val = (scan.get(c) or "").lower()
        for w in words:
            if w.lower() not in val:
                failures.append(f"{c} missing word {w!r}")
    return failures


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    print(f"scan probe — {len(selected)} fixtures\n")
    pass_count = 0
    for fx in selected:
        print(f"========== {fx['name']} ==========")
        _print_block("PROMPT", fx["prompt"])
        try:
            scan, raw = await scan_prompt(fx["prompt"])
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            continue
        _print_block("RAW", raw)
        print(f"\n  scan result:")
        for c in SCAN_CONCEPTS:
            v = scan.get(c)
            if v:
                preview = v if len(v) <= 100 else v[:100] + "..."
                print(f"    {c}: {preview!r}")
            else:
                print(f"    {c}: (absent)")
        failures = _check(fx, scan)
        if failures:
            print(f"\n  [FAIL] {len(failures)}")
            for f in failures:
                print(f"    ! {f}")
        else:
            print(f"\n  [PASS]")
            pass_count += 1
        print()
    print(f"=== {pass_count}/{len(selected)} fixtures passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
