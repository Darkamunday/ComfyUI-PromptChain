"""Planner probe — multi-character build-mode planning turn.

ONE LLM call takes the user request + matched bios, emits a structured
per-character plan (outfit + pose per char, shared scene/style/
interaction). Replaces the brittle decompose → per-intent loop for
multi-char build mode where intents need coordinated assignment
(per-char outfits, cross-char borrow, shared bikini for both).

Plan schema (YAML-style for clarity, returned as parsed dict):

  cast:
    - tag: cammy_white
      display: Cammy White
  per_character:
    - tag: cammy_white
      outfit_text: "blue bikini"          # raw user phrase
      outfit_source: generic              # generic | canon:<name> | borrow:<src_tag>
      pose_text: "fighting chun-li"       # raw user phrase OR ""
  scene_text: "on a beach"
  interaction: "fighting"                  # verb between subjects, or ""
  style_text: ""                           # rendering aesthetic if specified
  lighting_text: ""                        # if specified

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_planner_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types
from typing import Any


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


PLANNER_SYSTEM = """You receive a free-text image-generation request mentioning one or more named characters, plus a list of CHARACTER PROFILES (canonical character tags + display names). You output a structured plan in JSON form describing each character's outfit + pose, plus the shared scene/interaction/style/lighting.

The plan is consumed by deterministic code that looks up outfits from a knowledge base and assembles a final prompt. Your job is COORDINATION: figure out who wears what and who does what, even when the user phrases it casually ("both in bikinis", "X in Y's outfit", "fighting on a beach").

OUTPUT FORMAT — emit a single JSON object, no markdown fences, no commentary, no surrounding prose:

{
  "cast": [
    {"tag": "<canonical_tag>", "display": "<Display Name>"}
  ],
  "per_character": [
    {
      "tag": "<canonical_tag>",
      "outfit_text": "<verbatim user-phrase for this char's outfit, or empty>",
      "outfit_source": "<generic | canon | borrow:<source_tag>>",
      "pose_text": "<this char's pose/action, or empty>"
    }
  ],
  "scene_text": "<setting/environment, or empty>",
  "interaction": "<verb describing what the cast is doing TO EACH OTHER, or empty>",
  "style_text": "<rendering aesthetic, or empty>",
  "lighting_text": "<lighting/mood, or empty>"
}

OUTFIT SOURCE — three values, generic-categorical:
  - "generic"          : user named a clothing TYPE not specific to a character (e.g. "blue bikini", "school uniform", "red sundress", "ballgown")
  - "canon"            : user named THIS character's own canon outfit (e.g. "Cammy in her Delta Red", "Tifa's FF7 Original")
  - "borrow:<src_tag>" : user wants THIS character to wear ANOTHER character's outfit (e.g. "Chun-Li in Cammy's outfit" → for chun-li: source=borrow:cammy_white; "Tifa wearing Killer Bee" since Killer Bee is Cammy's outfit → borrow:cammy_white)

RULES:
- Every character in cast MUST have a per_character entry. Don't drop anyone.
- The cast comes from the CHARACTER PROFILES list. Only use canonical tags that appear in that list.
- If the user didn't name an outfit for a character, leave outfit_text empty AND set outfit_source to "canon" (deterministic code will fill in that character's default outfit).
- If the user named ONE outfit for multiple subjects ("both wearing bikinis", "all in school uniforms"), copy the outfit_text + outfit_source to each per_character entry.
- If the user named DIFFERENT outfits per subject ("cammy in blue bikini and chun-li in red sundress"), each per_character entry gets its own.
- For interaction verbs (fighting, kissing, dancing, racing), set "interaction" to the verb and give each character a pose_text that names what THEY are doing in that interaction (e.g. for "fighting": "throwing a punch at chun-li" / "blocking cammy's strike"). For single-subject scenes leave interaction empty.
- "scene_text" is location/environment only ("on a beach", "in a fancy bedroom"). Lighting goes in "lighting_text" if the user specified it ("warm sunset glow"). Style goes in "style_text" ("hyperrealistic anime", "watercolor").

Examples:

User: "cammy white in killer bee outfit"
Profiles: cammy_white=Cammy White
Plan:
{"cast":[{"tag":"cammy_white","display":"Cammy White"}],"per_character":[{"tag":"cammy_white","outfit_text":"Killer Bee","outfit_source":"canon","pose_text":""}],"scene_text":"","interaction":"","style_text":"","lighting_text":""}

User: "cammy white and chun-li both wearing bikinis at the beach"
Profiles: cammy_white=Cammy White; chun-li=Chun-Li
Plan:
{"cast":[{"tag":"cammy_white","display":"Cammy White"},{"tag":"chun-li","display":"Chun-Li"}],"per_character":[{"tag":"cammy_white","outfit_text":"bikini","outfit_source":"generic","pose_text":""},{"tag":"chun-li","outfit_text":"bikini","outfit_source":"generic","pose_text":""}],"scene_text":"at the beach","interaction":"","style_text":"","lighting_text":""}

User: "chun-li in cammy's outfit fighting cammy on a rooftop"
Profiles: chun-li=Chun-Li; cammy_white=Cammy White
Plan:
{"cast":[{"tag":"chun-li","display":"Chun-Li"},{"tag":"cammy_white","display":"Cammy White"}],"per_character":[{"tag":"chun-li","outfit_text":"","outfit_source":"borrow:cammy_white","pose_text":"throwing a kick at cammy"},{"tag":"cammy_white","outfit_text":"","outfit_source":"canon","pose_text":"blocking chun-li's strike"}],"scene_text":"on a rooftop","interaction":"fighting","style_text":"","lighting_text":""}

User: "cammy white in a blue bikini and chun-li in cammy white's outfit fighting on a beach"
Profiles: cammy_white=Cammy White; chun-li=Chun-Li
Plan:
{"cast":[{"tag":"cammy_white","display":"Cammy White"},{"tag":"chun-li","display":"Chun-Li"}],"per_character":[{"tag":"cammy_white","outfit_text":"blue bikini","outfit_source":"generic","pose_text":"throwing a punch at chun-li"},{"tag":"chun-li","outfit_text":"","outfit_source":"borrow:cammy_white","pose_text":"blocking cammy's strike"}],"scene_text":"on a beach","interaction":"fighting","style_text":"","lighting_text":""}

Output the JSON only. Nothing before or after."""


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


def _parse_plan(raw: str) -> dict | None:
    s = _strip_fences(raw)
    if not s:
        return None
    # Some models put leading prose before the JSON. Grab the first
    # balanced { ... } block.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= start:
        return None
    body = s[start:end]
    try:
        return json.loads(body)
    except Exception:
        return None


async def plan(user_request: str, profiles: list[dict]) -> tuple[dict | None, str]:
    """profiles: [{tag, display}, ...] matched characters from the bio
    layer. Returns (plan_dict, raw_response)."""
    if not user_request.strip():
        return None, ""
    profile_lines = "\n".join(
        f"  - tag={p.get('tag')}; display={p.get('display')}"
        for p in profiles
    )
    user_msg = (
        f"CHARACTER PROFILES:\n{profile_lines}\n\n"
        f"USER REQUEST: {user_request}\n\n"
        f"Output the JSON plan:"
    )
    raw = await ai_api._run_generation(
        f"plan-{abs(hash(user_request)) % 10000}",
        PROVIDER, CONFIG,
        PLANNER_SYSTEM, user_msg, [],
    )
    return _parse_plan(raw), (raw or "")


# ── Probes ────────────────────────────────────────────────────────


FIXTURES = [
    {
        "name": "shared_bikinis",
        "request": "chun-li and cammy white both wearing bikinis at the beach",
        "profiles": [
            {"tag": "chun-li", "display": "Chun-Li"},
            {"tag": "cammy_white", "display": "Cammy White"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 2,
            lambda p: all((pc.get("outfit_source") == "generic"
                            and "bikini" in (pc.get("outfit_text") or "").lower())
                          for pc in p.get("per_character") or []),
            lambda p: "beach" in (p.get("scene_text") or "").lower(),
        ],
    },
    {
        "name": "per_char_outfits",
        "request": "cammy white in a blue bikini and chun-li in a red sundress on a beach",
        "profiles": [
            {"tag": "cammy_white", "display": "Cammy White"},
            {"tag": "chun-li", "display": "Chun-Li"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 2,
            lambda p: any(
                pc.get("tag") == "cammy_white"
                and "blue" in (pc.get("outfit_text") or "").lower()
                and "bikini" in (pc.get("outfit_text") or "").lower()
                for pc in p.get("per_character") or []),
            lambda p: any(
                pc.get("tag") == "chun-li"
                and "sundress" in (pc.get("outfit_text") or "").lower()
                for pc in p.get("per_character") or []),
            lambda p: "beach" in (p.get("scene_text") or "").lower(),
        ],
    },
    {
        "name": "cross_char_borrow",
        "request": "chun-li in cammy white's outfit on a rooftop",
        "profiles": [
            {"tag": "chun-li", "display": "Chun-Li"},
            {"tag": "cammy_white", "display": "Cammy White"},
        ],
        "assertions": [
            lambda p: any(
                pc.get("tag") == "chun-li"
                and (pc.get("outfit_source") or "").startswith("borrow:cammy_white")
                for pc in p.get("per_character") or []),
            lambda p: "rooftop" in (p.get("scene_text") or "").lower(),
        ],
    },
    {
        "name": "complex_per_char_borrow_action",
        "request": "cammy white in a blue bikini and chun-li in cammy white's outfit fighting on a beach",
        "profiles": [
            {"tag": "cammy_white", "display": "Cammy White"},
            {"tag": "chun-li", "display": "Chun-Li"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 2,
            lambda p: any(
                pc.get("tag") == "cammy_white"
                and "blue" in (pc.get("outfit_text") or "").lower()
                and "bikini" in (pc.get("outfit_text") or "").lower()
                and (pc.get("outfit_source") or "") == "generic"
                for pc in p.get("per_character") or []),
            lambda p: any(
                pc.get("tag") == "chun-li"
                and (pc.get("outfit_source") or "").startswith("borrow:cammy_white")
                for pc in p.get("per_character") or []),
            lambda p: "beach" in (p.get("scene_text") or "").lower(),
            lambda p: "fight" in (p.get("interaction") or "").lower(),
        ],
    },
    {
        "name": "single_char_canon_outfit",
        "request": "cammy white in killer bee outfit",
        "profiles": [
            {"tag": "cammy_white", "display": "Cammy White"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 1,
            lambda p: any(
                pc.get("tag") == "cammy_white"
                and "killer bee" in (pc.get("outfit_text") or "").lower()
                and (pc.get("outfit_source") or "") == "canon"
                for pc in p.get("per_character") or []),
        ],
    },
    {
        "name": "three_char_shared_outfit",
        "request": "cammy white, chun-li and tifa lockhart all in school uniforms in a classroom",
        "profiles": [
            {"tag": "cammy_white", "display": "Cammy White"},
            {"tag": "chun-li", "display": "Chun-Li"},
            {"tag": "tifa_lockhart", "display": "Tifa Lockhart"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 3,
            lambda p: all((pc.get("outfit_source") or "") == "generic"
                          and "school" in (pc.get("outfit_text") or "").lower()
                          for pc in p.get("per_character") or []),
            lambda p: "classroom" in (p.get("scene_text") or "").lower(),
        ],
    },
    {
        "name": "mixed_canon_and_generic",
        "request": "cammy white in killer bee and tifa lockhart in a wedding dress dancing in a ballroom",
        "profiles": [
            {"tag": "cammy_white", "display": "Cammy White"},
            {"tag": "tifa_lockhart", "display": "Tifa Lockhart"},
        ],
        "assertions": [
            lambda p: any(
                pc.get("tag") == "cammy_white"
                and (pc.get("outfit_source") or "") == "canon"
                and "killer bee" in (pc.get("outfit_text") or "").lower()
                for pc in p.get("per_character") or []),
            lambda p: any(
                pc.get("tag") == "tifa_lockhart"
                and (pc.get("outfit_source") or "") == "generic"
                and ("wedding" in (pc.get("outfit_text") or "").lower()
                     or "dress" in (pc.get("outfit_text") or "").lower())
                for pc in p.get("per_character") or []),
            lambda p: "ballroom" in (p.get("scene_text") or "").lower(),
            lambda p: "danc" in (p.get("interaction") or "").lower(),
        ],
    },
    {
        "name": "single_char_no_outfit_named",
        "request": "tifa lockhart in a forest at sunset",
        "profiles": [
            {"tag": "tifa_lockhart", "display": "Tifa Lockhart"},
        ],
        "assertions": [
            lambda p: len(p.get("cast") or []) == 1,
            lambda p: any(
                pc.get("tag") == "tifa_lockhart"
                # No outfit named → empty text OR canon source for default
                and ((pc.get("outfit_text") or "") == ""
                     or (pc.get("outfit_source") or "") == "canon")
                for pc in p.get("per_character") or []),
            lambda p: "forest" in (p.get("scene_text") or "").lower(),
        ],
    },
]


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    pass_count = 0
    for fx in selected:
        print(f"\n{'='*78}\n{fx['name']}\n{'='*78}")
        print(f"REQUEST: {fx['request']!r}")
        result, raw = await plan(fx["request"], fx["profiles"])
        print(f"\n--- RAW ---")
        print(raw[:800] + ("..." if len(raw) > 800 else ""))
        print(f"\n--- PARSED ---")
        if result is None:
            print("  (parse failed)")
        else:
            print(json.dumps(result, indent=2))
        failures = []
        for i, assertion in enumerate(fx["assertions"]):
            try:
                if not result or not assertion(result):
                    failures.append(f"assertion[{i}] failed")
            except Exception as e:
                failures.append(f"assertion[{i}] error: {e}")
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
