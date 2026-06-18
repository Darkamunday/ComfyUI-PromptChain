"""Character router — LLM NER pre-pass for build mode.

ONE LLM call takes a free-text request and returns the verbatim
character-name phrases present in it. Replaces the regex/substring
scan in `_scan_chars_in_text` which suffered from substring false
positives ("bare" → Yukibare, "white" → Lily White, "of" → Warrior
Of Light) and required an ever-growing stopword list.

The returned phrases feed into `match_character()` per-phrase to
produce the profile list used by the planner branch.

Output schema: JSON array of strings.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_char_router_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
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


ROUTER_SYSTEM = """You receive a free-text image-generation request. Extract the proper-noun character-name phrases that appear in it. Return a JSON array of strings, verbatim from the input (preserve casing and punctuation as the user wrote them).

INCLUDE:
- Multi-word personal names (e.g. two-word capitalized names, hyphenated franchise names)
- Single-word recognizable character names when context makes the role clear (subject of a sentence, possessive of an outfit)

EXCLUDE:
- Color words (white, blue, red, pink, black, ...) — even though "Lily White" is a real character, a bare "white" inside "white bikini" is a color, not a character
- Clothing nouns (bikini, dress, leotard, gi, uniform, sundress, kimono, hoodie, ...)
- Body parts / states (feet, hands, hair, eyes, bare, topless, nude, sitting, kneeling, ...)
- Setting/location words (beach, rooftop, classroom, sunset, forest, alley, bedroom, ...)
- Action verbs (fighting, kissing, dancing, sitting, holding, ...)
- Outfit/costume names even when capitalized — "Killer Bee outfit" → the outfit is "Killer Bee" and is NOT a character. Same for canon-outfit names like "Delta Red", "SF6 Costume", "Original FF7".
- Articles, prepositions, conjunctions (a, an, the, in, on, with, and, or, of, at, ...)

POSSESSIVES:
- "X's outfit" → output "X" (drop the trailing 's)
- "X's friend Y" → output ["X", "Y"]

DUPLICATES:
- If the same character is named twice, output the phrase once.

OUTPUT FORMAT:
- A single JSON array, e.g. ["Cammy White", "Chun-Li"]
- For zero characters: []
- No markdown fences, no commentary, no surrounding prose.

EXAMPLES (placeholders, no fixture leakage):

Request: "a girl in a red dress sitting on a chair"
Output: []

Request: "<Name> in a blue bikini on a beach"
Output: ["<Name>"]

Request: "<Name1> and <Name2> fighting on a rooftop"
Output: ["<Name1>", "<Name2>"]

Request: "<Name1> in <Name2>'s outfit dancing"
Output: ["<Name1>", "<Name2>"]

Request: "<Name1> in her Killer Bee outfit"
Output: ["<Name1>"]                  # Killer Bee is an outfit, not a character

Request: "<Name> in a pink microbikini sitting with legs up showing bare feet"
Output: ["<Name>"]                   # "bare feet" is a body-part state, not a character

Output the JSON array only. Nothing before or after."""


def _strip_fences(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    if s.startswith("```"):
        ls = s.splitlines()
        if ls and ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        s = "\n".join(ls).strip()
    return s


def _parse_names(raw: str) -> list[str] | None:
    s = _strip_fences(raw)
    if not s:
        return None
    start = s.find("[")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(s)):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= start:
        return None
    body = s[start:end]
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        phrase = item.strip()
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
    return out


async def extract_character_names(
    user_request: str,
) -> tuple[list[str] | None, str]:
    """Returns (phrases, raw_response).

    - phrases is a list (possibly empty) when the router LLM responded
      AND its output parsed successfully — empty list means "no
      character names in this request" (legitimate finding).
    - phrases is None when the LLM call failed OR returned unparseable
      output. Callers should treat None as "router unavailable" and
      fall back to the regex scan.
    """
    if not (user_request or "").strip():
        return [], ""
    raw = await ai_api._run_generation(
        f"char-router-{abs(hash(user_request)) % 10000}",
        PROVIDER, CONFIG,
        ROUTER_SYSTEM, user_request, [],
    )
    if not (raw or "").strip():
        return None, (raw or "")
    parsed = _parse_names(raw)
    return parsed, raw


# ── Probes ────────────────────────────────────────────────────────


FIXTURES = [
    {
        "name": "solo_no_decoy",
        "request": "cammy white",
        "expect_exact_set": {"cammy white"},
    },
    {
        "name": "solo_with_outfit_and_setting",
        "request": "cammy white in a pink microbikini sitting with legs up showing bare feet",
        "expect_exact_set": {"cammy white"},
        # Anti-regression: must NOT include "bare" (Yukibare false-positive)
        "must_not_include_lc": {"bare", "yukibare", "suzuran"},
    },
    {
        "name": "multi_basic",
        "request": "cammy white and chun-li fighting on a rooftop",
        "expect_exact_set": {"cammy white", "chun-li"},
    },
    {
        "name": "multi_per_char_outfits",
        "request": "cammy white in a blue bikini and chun-li in a red sundress on a beach",
        "expect_exact_set": {"cammy white", "chun-li"},
        "must_not_include_lc": {"blue", "red", "white", "bikini", "sundress", "beach"},
    },
    {
        "name": "multi_cross_borrow",
        "request": "chun-li in cammy white's outfit fighting cammy white on a beach",
        "expect_exact_set": {"chun-li", "cammy white"},
    },
    {
        "name": "single_with_canon_outfit_name",
        "request": "cammy white in her killer bee outfit in hyper realistic anime style",
        "expect_exact_set": {"cammy white"},
        # Killer Bee is an outfit, not a character
        "must_not_include_lc": {"killer bee", "killer", "bee"},
    },
    {
        "name": "no_characters",
        "request": "a girl in a red dress sitting on a chair in a forest at sunset",
        "expect_exact_set": set(),
    },
    {
        "name": "three_char_shared_outfit",
        "request": "cammy white, chun-li and tifa lockhart all in school uniforms in a classroom",
        "expect_exact_set": {"cammy white", "chun-li", "tifa lockhart"},
        "must_not_include_lc": {"school", "uniform", "uniforms", "classroom"},
    },
]


def _norm_set(items: list[str]) -> set[str]:
    return {(s or "").strip().lower() for s in items if (s or "").strip()}


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    pass_count = 0
    for fx in selected:
        print(f"\n{'='*78}\n{fx['name']}\n{'='*78}")
        print(f"REQUEST: {fx['request']!r}")
        phrases, raw = await extract_character_names(fx["request"])
        print(f"\n--- RAW ---")
        print(raw[:400] + ("..." if len(raw) > 400 else ""))
        print(f"\n--- PARSED ---")
        print(phrases)
        got = _norm_set(phrases or [])
        want = fx["expect_exact_set"]
        failures = []
        if got != want:
            failures.append(f"set mismatch: got={sorted(got)} want={sorted(want)}")
        for forbid in fx.get("must_not_include_lc") or set():
            if forbid in got:
                failures.append(f"forbidden phrase present: {forbid!r}")
        if failures:
            print(f"\n[FAIL]")
            for f in failures:
                print(f"  ! {f}")
        else:
            print(f"\n[PASS]")
            pass_count += 1
    print(f"\n=== {pass_count}/{len(selected)} passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
