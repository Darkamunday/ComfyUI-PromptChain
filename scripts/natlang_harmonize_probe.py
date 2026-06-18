"""Harmonize probe — Stage 3.

Single AI call after a sentence-shaped concept change (pose / scene /
expression / style) lands in the prompt. Asks the model to identify
which existing sentences are now visually inconsistent with the new
content and propose minor rewrites or deletions.

This is a narrow classification + rewrite task. The model is given:
  - the candidate prompt (after dispatch + apply produced it)
  - the new content that was just inserted (or replaced)
  - the concept that was changed

It outputs zero or more SEARCH/REPLACE corrections. A blank REPLACE
deletes the matched span.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_harmonize_probe.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _S:
    def _p(self, _):
        def w(f): return f
        return w
    post = get = put = delete = patch = head = options = _p


sys.modules.setdefault(
    "folder_paths",
    types.SimpleNamespace(folder_names_and_paths={}, get_folder_paths=lambda x: [],
                          get_full_path=lambda *a, **k: None,
                          models_dir="/tmp", get_user_directory=lambda: "/tmp",
                          base_path="/tmp"),
)
sys.modules.setdefault(
    "server",
    types.SimpleNamespace(PromptServer=types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=_S(), send_sync=lambda *a, **k: None))),
)

from core import ai_api  # noqa: E402


PROVIDER = "local"
CONFIG = {"local": {"base_url": "http://localhost:11434/v1",
                    "model": "qwen3-vl:8b-instruct"}}


HARMONIZE_SYSTEM = """You check a newly-inserted pose/scene/expression sentence against the rest of an image-generation prompt and identify ONLY direct gaze/direction CONTRADICTIONS with existing sentences. Nothing else.

WHAT TO FLAG (and only this):
  - The new pose specifies a gaze direction (looking back, looking down, looking up, profile, side view, rear view, from behind, face down, head turned) AND the existing prompt has a contradicting gaze token ("looking at viewer", "facing forward", "smiling at camera", "facing the camera", "eye contact with viewer", etc.).
  - The new pose specifies face-buried / face-obscured / mouth-covered AND the existing prompt has a visible facial-expression token ("smiling at viewer", "grinning brightly", "wide-eyed").

WHAT NOT TO FLAG (do NOT delete or rewrite these — they stay on the subject regardless of camera angle):
  - Character identity (name, hair color, eye color, scars, marks) — these describe the SUBJECT, not the view.
  - Body description (build, height, bust, skin tone, muscle tone) — same.
  - Outfit items / accessories / paint — they remain ON the body even if not visible from this angle. Leave every clothing item, glove, hat, necktie, paint design, etc. UNTOUCHED.
  - Camera framing tokens, depth of field, lighting, style — unrelated to subject visibility.

Items NOT being visible from a given angle is NOT a contradiction. The token still describes the subject and z-image will render coherently from the new angle. Do not strip items.

OUTPUT FORMAT — zero or more CORRECTION blocks, each exactly like this:

  CORRECTION:
  SEARCH: <verbatim text from the candidate prompt — one comma-clause or one sentence>
  REPLACE: <revised text, OR leave the line blank to delete the matched span>

If no gaze/direction contradictions exist, output exactly:
  CORRECTIONS_NONE

You may output AT MOST 2 CORRECTION blocks. If you find more than 2 candidates, your scope is too wide — reconsider and pick only the strongest 1-2 gaze/direction contradictions, or output CORRECTIONS_NONE.

Generic examples (abstract — different content from any specific case):

Example A (rear view contradicts facing-viewer gaze):
  NEW CONTENT: Posterior view from behind, looking over her shoulder.
  CANDIDATE PROMPT:
  A knight in chainmail with a red cross painted on her chestplate. Looking at viewer with a grin. Mounted on a black destrier.

  CORRECTION:
  SEARCH: Looking at viewer with a grin
  REPLACE:

(The red cross on chestplate STAYS — it's still painted there, just not visible from rear. The destrier mount STAYS. Only the gaze contradiction is flagged.)

Example B (face-down contradicts smiling-at-viewer):
  NEW CONTENT: Lying face-down on a pillow, head buried in fabric.
  CANDIDATE PROMPT:
  A young scholar, brown hair, glasses. Smiling brightly at viewer.

  CORRECTION:
  SEARCH: Smiling brightly at viewer
  REPLACE:

Example C (no gaze contradiction, pose change is harmless):
  NEW CONTENT: Standing in a fighting stance.
  CANDIDATE PROMPT:
  Sir Galahad, full plate armor, steel gauntlets. Photorealistic style.

  CORRECTIONS_NONE
"""


def _parse_corrections(raw: str) -> list[dict]:
    """Parse zero or more CORRECTION blocks.

    `CORRECTIONS_NONE` standalone → empty list. But if the model emits
    CORRECTION blocks AND a trailing CORRECTIONS_NONE, the blocks win
    (treat the marker as a stray footer).
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        ls = text.splitlines()
        if ls and ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        text = "\n".join(ls).strip()
    corrections: list[dict] = []
    parts = re.split(r"^\s*CORRECTION\s*:\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        sm = re.search(r"^\s*SEARCH\s*:\s*(.*)$", part, re.MULTILINE | re.IGNORECASE)
        rm = re.search(r"^\s*REPLACE\s*:\s*(.*)$", part, re.MULTILINE | re.IGNORECASE)
        if not sm:
            continue
        search = sm.group(1).strip()
        replace = (rm.group(1).strip() if rm else "")
        if not search:
            continue
        # Drop the marker tokens if the model wrote them verbatim
        if search.lower() in ("(not present)", "(empty)", "(none)"):
            continue
        corrections.append({"search": search, "replace": replace})
    # Only honor CORRECTIONS_NONE if no real CORRECTION blocks parsed.
    if not corrections and re.search(r"\bCORRECTIONS?_NONE\b", text, re.IGNORECASE):
        return []
    # The system prompt caps at 2. If the model ignored that and emitted
    # more, drop the whole list — the model is over-scoping and we
    # shouldn't trust ANY of its picks. Bail to CORRECTIONS_NONE.
    if len(corrections) > 2:
        return []
    return corrections


async def harmonize(candidate_prompt: str,
                    new_content: str,
                    concept: str) -> tuple[list[dict], str]:
    user_msg = (
        f"NEW CONTENT (just inserted as a `{concept}` sentence):\n"
        f"{new_content}\n\n"
        f"CANDIDATE PROMPT (full text after the insert):\n"
        f"{candidate_prompt}\n\n"
        f"Output CORRECTION blocks for any existing sentences that are now "
        f"visually inconsistent with the new content. If nothing needs to "
        f"change, output CORRECTIONS_NONE."
    )
    raw = await ai_api._run_generation(
        f"harmonize-{abs(hash((candidate_prompt, new_content, concept))) % 10000}",
        PROVIDER, CONFIG,
        HARMONIZE_SYSTEM, user_msg, [],
    )
    return _parse_corrections(raw), (raw or "")


def apply_corrections(prompt: str, corrections: list[dict],
                      new_content: str = "") -> tuple[str, list[str]]:
    """Apply each correction's SEARCH/REPLACE on the prompt. Skips any
    correction whose SEARCH overlaps the new_content — the harmonize
    step's job is to fix OTHER sentences in the prompt, not edit the
    content that was just inserted."""
    methods = []
    current = prompt
    nc = (new_content or "").strip()
    for c in corrections:
        search = c["search"]
        replace = c["replace"]
        if not search:
            methods.append("empty_search")
            continue
        # Don't let harmonize edit the new content itself.
        if nc and search in nc:
            methods.append(f"skip_self({search[:40]!r})")
            continue
        if search not in current:
            methods.append(f"miss({search[:40]!r})")
            continue
        if not replace.strip():
            # Delete: strip a surrounding ", " or ". " if present so the
            # list/sentence flow stays clean.
            for pattern in (f", {search}", f"{search}, ", f". {search}", search):
                if pattern in current:
                    current = current.replace(pattern, "", 1)
                    methods.append(f"delete({pattern[:40]!r})")
                    break
        else:
            current = current.replace(search, replace, 1)
            methods.append(f"replace({search[:40]!r}->{replace[:40]!r})")
    return current, methods


# ── Test fixtures ─────────────────────────────────────────────────


VICTORY_POSE_BODY = "Posterior view from behind showing her back and rear, Looking back with head turned in profile, standing confidently, looking out of the corner of her eye with confidence, Frame image to her large ass."

CAMMY_WITH_VICTORY = """Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, pale light skin, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar. light blue sleeveless thong leotard with stiff tight mock-neck, knee-high brown leather boots, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs. Posterior view from behind showing her back and rear, Looking back with head turned in profile, standing confidently, looking out of the corner of her eye with confidence, Frame image to her large ass. Hyperrealistic anime style.

Negative Prompt:
blurry"""


NO_CONFLICT_PROMPT = """A young woman, brown hair, brown eyes. Wearing red dress, black flats. Standing in a fighting stance with one hand raised. Photorealistic anime style.

Negative Prompt:
blurry"""


FIXTURES = [
    {
        "name": "cammy_victory_pose_rear_view",
        "prompt": CAMMY_WITH_VICTORY,
        "new_content": VICTORY_POSE_BODY,
        "concept": "pose",
        # Inspect — model may flag necktie, lightning bolt paint designs
        # on bare thighs (front-facing), or facing-viewer tokens. Expect
        # at least one correction.
        "expect_corrections_min": 1,
    },
    {
        "name": "no_real_conflict",
        "prompt": NO_CONFLICT_PROMPT,
        "new_content": "Standing in a fighting stance with one hand raised.",
        "concept": "pose",
        "expect_corrections_max": 0,
    },
    {
        "name": "rear_view_vs_facing_viewer",
        # Real gaze contradiction the conservative rule SHOULD catch.
        "prompt": """A young woman, brown hair, brown eyes, athletic build. Wearing red leotard, white boots. Smiling brightly at the viewer with eye contact. Posterior view from behind, looking over her shoulder. Photorealistic.

Negative Prompt:
blurry""",
        "new_content": "Posterior view from behind, looking over her shoulder.",
        "concept": "pose",
        "expect_corrections_min": 1,
        "expect_corrections_max": 2,
    },
]


def _hr(label):
    bar = "=" * 78
    print(f"\n{bar}\n{label}\n{bar}")


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    pass_count = 0
    for fx in selected:
        _hr(fx["name"])
        print(f"new_content (concept={fx['concept']}):")
        print(f"  {fx['new_content']}")
        try:
            corrections, raw = await harmonize(
                fx["prompt"], fx["new_content"], fx["concept"],
            )
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            continue
        print(f"\nRAW:\n{raw}\n")
        print(f"parsed corrections ({len(corrections)}):")
        for c in corrections:
            print(f"  SEARCH: {c['search']!r}")
            print(f"  REPLACE: {c['replace']!r}")
        if corrections:
            applied, methods = apply_corrections(
                fx["prompt"], corrections, new_content=fx["new_content"],
            )
            print(f"\napply methods: {methods}")
            print(f"\nAPPLIED PROMPT:")
            for line in applied.splitlines():
                print(f"  {line}")
        failures = []
        if "expect_corrections_min" in fx:
            if len(corrections) < fx["expect_corrections_min"]:
                failures.append(
                    f"expected >= {fx['expect_corrections_min']} corrections, got {len(corrections)}"
                )
        if "expect_corrections_max" in fx:
            if len(corrections) > fx["expect_corrections_max"]:
                failures.append(
                    f"expected <= {fx['expect_corrections_max']} corrections, got {len(corrections)}"
                )
        if failures:
            print(f"\n[FAIL]")
            for f in failures:
                print(f"  ! {f}")
        else:
            print(f"\n[PASS]")
            pass_count += 1
    print(f"\n=== {pass_count}/{len(selected)} ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
