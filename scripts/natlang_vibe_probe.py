"""Vibe probe — Stage 2.

When resolve misses on a sentence-shaped concept (pose / scene /
expression / style) and the user's intent text is terse, run one
narrow LLM call to polish that intent into a proper one-sentence
prose description.

The vibe step is opt-in via a short-text gate — if the user already
typed a full descriptive sentence (e.g. "sitting with legs up
presenting feet at viewer"), we don't need vibe and skip it to save
latency. Only fires when text is short and terse.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_vibe_probe.py
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


VIBE_SYSTEM = """You shape a brief user intent into ONE grammatical sentence — NOTHING MORE.

You receive a CONCEPT name and a SHORT INTENT. Output exactly one sentence containing the user's exact words. Capitalize the first letter, add a single trailing period, and add at most one short connective article or preposition ("a", "an", "in a", "rendered in", "with") if needed for grammar.

HARD RULES — read carefully, you fail if you break any:
  - DO NOT invent any new posture, action, direction, anatomy, garment, setting, lighting, mood, time of day, or stylistic detail. Anything the user did not type, you do not type.
  - DO NOT substitute synonyms. If the user said `legs up`, output `legs up`. If they said `wide stance`, output `wide stance`.
  - DO NOT elaborate. Adding "confidently" or "gracefully" or "with one hand on the hip" or any flavor descriptor is invention.
  - The user's original words must appear in your output, in their original order, unmodified.
  - The ONLY things you may add are: a leading article ("A", "An", "In a", "Rendered in"), capitalization, and a final period.
  - If the user's intent is already a grammatical sentence, return it with just capitalization + period adjustments. Add nothing.

Output ONE sentence only. No quoted block, no commentary, no markdown fences.

Generic examples (read these for SHAPE — note how nothing is added beyond grammar):

  CONCEPT: pose
  INTENT: wide stance
  → A wide stance.

  CONCEPT: scene
  INTENT: rainy alley
  → In a rainy alley.

  CONCEPT: expression
  INTENT: shocked
  → A shocked expression.

  CONCEPT: style
  INTENT: watercolor
  → Rendered in watercolor.

  CONCEPT: pose
  INTENT: arms folded
  → Arms folded.
"""


def _looks_terse(text: str) -> bool:
    """Heuristic: text is short enough that vibe likely helps.
    Already-prose-shaped descriptions are skipped so vibe doesn't
    substitute the user's specific posture/anatomy words with its
    own paraphrase.

    Terse when:
      - <= 20 chars (tag-shaped: "victory pose", "smug grin"), OR
      - <= 40 chars AND fewer than 5 content words (>= 3 letters)

    Anything else is treated as already-shaped prose — pass through
    as raw text and let format_replace handle cap+period.
    """
    s = (text or "").strip()
    if not s:
        return False
    if len(s) <= 20:
        return True
    content_words = [w for w in re.findall(r"[A-Za-z']{3,}", s)]
    if len(s) <= 40 and len(content_words) < 5:
        return True
    return False


def _clean_vibe_output(raw: str) -> str:
    """Strip code fences, surrounding quotes, leading bullets."""
    s = (raw or "").strip()
    if not s:
        return s
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Strip leading arrow / bullet
    s = re.sub(r"^[→•\-\*]\s*", "", s)
    # Strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    # Take only the first line — model sometimes adds explanations.
    s = s.split("\n", 1)[0].strip()
    return s


async def vibe(concept: str, text: str) -> tuple[str, str]:
    """Return (polished_sentence, raw_response). Empty polished
    sentence on parse failure — caller falls back to using raw text."""
    user_msg = (
        f"CONCEPT: {concept}\n"
        f"INTENT: {text}\n\n"
        f"Output one polished sentence:"
    )
    raw = await ai_api._run_generation(
        f"vibe-{abs(hash((concept, text))) % 10000}",
        PROVIDER, CONFIG,
        VIBE_SYSTEM, user_msg, [],
    )
    cleaned = _clean_vibe_output(raw)
    return cleaned, (raw or "")


# ── Tests ────────────────────────────────────────────────────────


FIXTURES = [
    {
        "name": "pose_victory",
        "concept": "pose", "text": "victory pose",
        "expect_terse": True,
        "expect_contains_any": ["victory", "triumph"],
    },
    {
        "name": "pose_casual_standing",
        "concept": "pose", "text": "casual standing",
        "expect_terse": True,
        "expect_contains_any": ["standing", "relax", "casual"],
    },
    {
        "name": "scene_beach_sunset",
        "concept": "scene", "text": "beach sunset",
        "expect_terse": True,
        "expect_contains_any": ["beach", "sunset", "shore"],
    },
    {
        "name": "expression_sultry",
        "concept": "expression", "text": "sultry",
        "expect_terse": True,
        "expect_contains_any": ["sultry"],
    },
    {
        "name": "style_oil_painting",
        "concept": "style", "text": "oil painting",
        "expect_terse": True,
        "expect_contains_any": ["oil", "paint"],
    },
    {
        "name": "already_long_pose",
        "concept": "pose",
        "text": "sitting with legs up presenting feet at viewer, hands resting on knees",
        "expect_terse": False,  # gate should skip vibe for already-prose text
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
        print(f"  concept: {fx['concept']}")
        print(f"  text:    {fx['text']!r}")
        terse = _looks_terse(fx["text"])
        print(f"  terse:   {terse}")
        failures = []
        if "expect_terse" in fx and terse != fx["expect_terse"]:
            failures.append(f"terse gate: got {terse} expected {fx['expect_terse']}")
        if terse:
            cleaned, raw = await vibe(fx["concept"], fx["text"])
            print(f"  RAW:     {raw!r}")
            print(f"  vibed:   {cleaned!r}")
            for w in fx.get("expect_contains_any", []):
                if w.lower() in cleaned.lower():
                    break
            else:
                if fx.get("expect_contains_any"):
                    failures.append(
                        f"none of {fx['expect_contains_any']} present in vibe output"
                    )
        else:
            print(f"  (gate skipped vibe — text not terse)")
        if failures:
            print(f"\n  [FAIL]")
            for f in failures:
                print(f"    ! {f}")
        else:
            print(f"\n  [PASS]")
            pass_count += 1
    print(f"\n=== {pass_count}/{len(selected)} ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
