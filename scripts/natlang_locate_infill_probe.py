"""Locate-then-infill probe — step 2 of the railed-thinking pipeline.

Single LLM call per intent: given the full current prompt, a concept
slot, and the user's short intent text, output a SEARCH span (the text
in the prompt that holds that concept) and a REPLACE span (the new
text).

If the concept is not present in the prompt, output SEARCH: (not
present) and an INSERT_AFTER anchor for the apply step.

Apply is a literal string substitution. If the AI's SEARCH doesn't
match the prompt verbatim, the apply step fails and the model is asked
to try again — no fuzzy/regex fallbacks. Cleanup belongs in the AI's
REPLACE, not in Python post-processing.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_locate_infill_probe.py
  python scripts/natlang_locate_infill_probe.py <filter>
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


# ── System prompt ────────────────────────────────────────────────
# Generic medieval / fantasy examples — they teach SHAPE not ANSWERS.
# Litmus: if test fixtures used totally different characters, do these
# still teach the task? Yes.


LOCATE_INFILL_SYSTEM = """You are editing an image-generation prompt. You receive:
  - the CURRENT PROMPT (free-form prose)
  - a CONCEPT slot (e.g. footwear, hair, pose, expression, scene)
  - an OP (add, remove, modify, anatomy_mod, replace)
  - a TEXT (the new content / item / modifier)

Your job: find the existing span in the prompt that holds this concept (or signal that no such span exists), then produce the new span text per the OP.

OUTPUT FORMAT — exactly these labels, no commentary, no markdown fences. You emit EXACTLY ONE of the two shapes below — never both, never a mix:

SHAPE A (concept ALREADY exists in the prompt — substitute):
  SEARCH:
  <verbatim substring from the CURRENT PROMPT — the existing span that holds this concept>
  REPLACE:
  <new span text>

SHAPE B (concept is NOT in the prompt — insert):
  SEARCH:
  (not present)
  INSERT_AFTER:
  <one sentence from positive prose to anchor the insert>
  REPLACE:
  <new span text>

The SEARCH value in SHAPE B is the LITERAL 4-character-plus-parens string `(not present)`. Exactly those characters. NOT `(end)`, NOT `(empty)`, NOT `none`, NOT `null`, NOT `n/a`. The string is `(not present)` — copy it verbatim.

You must pick exactly one shape. Do NOT emit both SEARCH=<real text> and INSERT_AFTER together — that's a mix. If you write SEARCH=<real text>, omit INSERT_AFTER entirely. If you write SEARCH=(not present), you must include INSERT_AFTER.

DECISION RULE — answer this two-step question:

Q1: Does the CURRENT PROMPT contain a single sentence that describes the CONCEPT?
  - pose      = a sentence about body position / posture / action ("Sitting...", "Standing...", "Leaping forward")
  - scene     = a sentence about environment ("At a beach", "In a forest")
  - expression= a sentence about facial affect ("Smiling", "Looks solemn")
  - outfit slot (footwear/tops/etc) = the comma-listed item inside the outfit clause

  Character descriptions, outfit lists, and style/lighting prose are NOT pose/scene/expression sentences — they describe something else. The whole positive prose is NOT a single concept-sentence.

If yes → Shape A: SEARCH = that sentence. REPLACE = new content. No INSERT_AFTER.
If no  → Shape B: SEARCH = (not present). INSERT_AFTER = the sentence right BEFORE where the new content naturally goes (see Q2). REPLACE = new content.

Q2 (only if Q1 = no): Where does the new concept go in canonical order?
  pose         → before the style sentence (anchor = last sentence before style; usually the outfit clause)
  expression   → before the scene/style sentence
  scene/setting→ before the style sentence
  style        → at end of positive prose (anchor = last positive sentence, never Negative Prompt)
  quality      → at end of positive prose

Concept order: character → outfit → pose → expression → scene → style → quality → Negative Prompt.

CRITICAL RULES:
  - SEARCH must be a VERBATIM substring of the CURRENT PROMPT (or the literal "(not present)"). Do NOT paraphrase.
  - If you cannot find a span that semantically holds this concept, output SEARCH: (not present). Do NOT pick a random nearby line and overwrite it — that loses unrelated content.
  - REPLACE must contain ONLY the new span content. NEVER paste the INSERT_AFTER anchor text inside REPLACE — the apply step adds the anchor itself. If you write the anchor in REPLACE, you get a duplicated line.
  - NEGATIVE PROMPT IS A TERMINATOR. If the CURRENT PROMPT contains a `Negative Prompt:` block, that block marks the END of the positive prose. INSERT_AFTER anchors MUST point at a sentence INSIDE the positive prose. NEVER use `end` as INSERT_AFTER when a `Negative Prompt:` block is present. NEVER anchor at the negatives themselves.
  - INSERT_AFTER must be exactly ONE existing sentence from the positive prose. NOT the entire prose. NOT multiple sentences concatenated. Just one period-bounded sentence whose tail is where you want the new content to start.

OP BEHAVIOR — strict per op:

  op=add
    SEARCH is the existing span for this body region (an item OR a body-state modifier).

    **CRITICAL SLOT-EXCLUSIVITY RULE**: A body region (feet, head, hands, torso, legs, etc.) holds exactly ONE thing at a time. It is either DELIBERATELY EMPTY (a body-state modifier like "barefoot", "topless", "bareheaded", "bare-handed", "bottomless", "shirtless", "going commando", "nude", etc. — anything that says "no covering here") OR holds one or more items. The two are MUTUALLY EXCLUSIVE — adding an item NECESSARILY removes the bare-state, because the body region can't simultaneously be uncovered and covered.

      → If SEARCH is a body-state modifier: REPLACE = TEXT ONLY (the modifier is REMOVED). Never `<modifier>, <new item>`. Never comma-append. The modifier is GONE.
      → If SEARCH is one or more existing items in the same slot: REPLACE = SEARCH + ", " + TEXT (comma-append — both items coexist, e.g. boots + leg holster).

    If no span exists in this region: (not present) + INSERT_AFTER + REPLACE = TEXT.

    Reason: think about what a person physically looks like. They are barefoot, OR they are wearing socks. Never both at once. Adding socks means the bareness is no longer true. The prompt must reflect physical reality.

  op=remove
    SEARCH is the span naming the item to remove (with its adjectives, AND any leading/trailing comma + space so the surrounding list stays grammatical after deletion).
    REPLACE is truly empty — leave the line after `REPLACE:` blank. Do NOT write `(empty)` or any placeholder token; it will be taken literally.

  op=modify
    SEARCH is the existing span containing the property to change.
    REPLACE is the SEARCH span with ONLY the affected adjective/quality swapped — every other word preserved verbatim.

  op=anatomy_mod
    The TEXT is a body-part size/shape descriptor (NOT a garment).
    SEARCH is the same-region span (whatever currently covers that body part — an item, a body-state, anything).
    REPLACE fuses the descriptor INTO the existing span — DO NOT comma-append. Two cases:
      → SEARCH is a garment item: prepend the size adjective so it attaches to the wearable as a qualifier of the worn item.
      → SEARCH is a body-state modifier (the region is bare): qualify it with a connective phrase joining body-state and anatomy.
    Comma-appending the anatomy as a peer slot occupant creates slot-exclusivity contradictions in the prompt — DO NOT do this. The body region holds ONE thing, and the anatomy modifier sits inside or alongside it, not as a separate list entry.
    If no span exists for that region: (not present) + INSERT_AFTER + REPLACE = TEXT.

  op=replace
    SEARCH is the existing span for this concept (full clause/sentence for pose/scene/style; outfit item for footwear modifier-swaps).
    For pose/scene/expression: SEARCH is ONE sentence describing that aspect. NEVER the entire positive prose. NEVER multiple sentences. If no single sentence describes this concept, output SEARCH: (not present) — do NOT substitute a paragraph-wide span.
    REPLACE is the TEXT.
    If no span exists: (not present) + INSERT_AFTER + REPLACE = TEXT.

    **MULTI-ITEM BODY-STATE COLLAPSE**: When TEXT is a body-state modifier (`barefoot`, `topless`, `bareheaded`, `bare-handed`, `bottomless`, `shirtless`, `going commando`, `nude`, etc.) AND the body region currently holds MULTIPLE items (e.g. socks plus shoes, or stockings plus boots, or undershirt plus jacket), SEARCH must cover ALL of those items as ONE contiguous span — including any commas joining them. The whole region becomes bare; you cannot leave one covering item behind.

      Right: SEARCH=`long stockings, leather riding boots` → REPLACE=`barefoot`
      Wrong: SEARCH=`leather riding boots` → REPLACE=`barefoot` (stockings still there → contradiction)

    Apply the same rule to other body-state replacements: `topless` removes ALL torso items, `bareheaded` removes ALL headwear items, etc.

  CONCEPT=outfit (whole-outfit swap)
    SEARCH is the prose that names WORN ITEMS — clothing, footwear, gloves, hat, jewelry, body paint, belts. Start at the first worn-item word and end at the last worn-item word of the SAME clause/sentence. Do NOT extend SEARCH to body description (hair, eyes, body build, scars). Do NOT extend SEARCH to pose, scene, or style sentences. Do NOT include the surrounding sentence-terminal periods if the body/pose continues nearby.
    The body description sentence (subject + appearance) and the pose sentence MUST be left untouched.
    REPLACE is the TEXT (already the new outfit's full natlang).

Generic examples (different content from any test case — they teach SHAPE only):

EXAMPLE 1 (op=modify on existing item):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, steel gauntlets, knee-length greaves, mounted on a black destrier in a misty forest at dawn.
  CONCEPT: handwear
  OP: modify
  TEXT: leather

  SEARCH:
  steel gauntlets
  REPLACE:
  leather gauntlets


EXAMPLE 2 (op=replace, body-state modifier swap on a single item):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, steel gauntlets, knee-length greaves, mounted on a black destrier in a misty forest at dawn.
  CONCEPT: footwear
  OP: replace
  TEXT: barefoot

  SEARCH:
  knee-length greaves
  REPLACE:
  barefoot


EXAMPLE 2b (op=replace, body-state swap when the region holds MULTIPLE items — SEARCH spans all of them):
  CURRENT PROMPT:
  Court mage, embroidered velvet robe, silver pendant, long white stockings, soft leather slippers, holding a tome.
  CONCEPT: footwear
  OP: replace
  TEXT: barefoot

  SEARCH:
  long white stockings, soft leather slippers
  REPLACE:
  barefoot

  Reason: barefoot means NOTHING on the feet. Leaving the stockings behind would contradict the new state. Collapse all foot-region items into ONE SEARCH span and replace with the body-state word.


EXAMPLE 3 (op=add, concept not present, insert):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, steel gauntlets, knee-length greaves, mounted on a black destrier in a misty forest at dawn.
  CONCEPT: hair
  OP: add
  TEXT: long flowing silver hair

  SEARCH:
  (not present)
  INSERT_AFTER:
  Sir Galahad
  REPLACE:
  long flowing silver hair


EXAMPLE 6 (op=remove — include the leading comma so the list stays clean; REPLACE is BLANK):
  CURRENT PROMPT:
  Elven ranger, leather tunic, fingerless gloves, brown boots, bow drawn back, in a moonlit glade.
  CONCEPT: accessories
  OP: remove
  TEXT: bow

  SEARCH:
  , bow drawn back
  REPLACE:


EXAMPLE 7 (op=add, body-state modifier → drops modifier):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, bareheaded, leather wraps, kneeling in a stone chapel.
  CONCEPT: headwear
  OP: add
  TEXT: iron helm

  SEARCH:
  bareheaded
  REPLACE:
  iron helm


EXAMPLE 7b (op=add on a different body-state modifier — same rule, modifier is GONE):
  CURRENT PROMPT:
  Elven ranger, leather tunic, fingerless gloves, barefoot, bow drawn back, in a moonlit glade.
  CONCEPT: footwear
  OP: add
  TEXT: tall hiking boots

  SEARCH:
  barefoot
  REPLACE:
  tall hiking boots

  WRONG outputs to never produce here:
    REPLACE: barefoot, tall hiking boots   ← contradictory, both bare AND shod
    REPLACE: barefoot tall hiking boots    ← still contradictory
  The modifier disappears entirely. There is no comma-append for modifier-vs-item.


EXAMPLE 8 (op=replace, scene not present):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, steel gauntlets, kneeling in a stone chapel.
  CONCEPT: expression
  OP: replace
  TEXT: solemn

  SEARCH:
  (not present)
  INSERT_AFTER:
  end
  REPLACE:
  solemn


EXAMPLE 9 (concept not present, prompt has Negative Prompt block — anchor at last positive-prose line, NEVER at `end`):
  CURRENT PROMPT:
  Sir Galahad, full plate armor, steel gauntlets, kneeling in a stone chapel.

  Negative Prompt:
  blurry, deformed, watermark

  CONCEPT: expression
  OP: replace
  TEXT: solemn determination

  SEARCH:
  (not present)
  INSERT_AFTER:
  kneeling in a stone chapel.
  REPLACE:
  Solemn determination.


EXAMPLE 9b (pose concept not present, body has only character + outfit + style + Negative Prompt — insert as a new sentence between outfit and style, anchor on the outfit's last sentence):
  CURRENT PROMPT:
  A tall warrior, brown hair, brown eyes. Wearing red dragon-scale armor, steel gauntlets, a crimson cloak. Photorealistic cinematic lighting, sharp focus.

  Negative Prompt:
  blurry, deformed

  CONCEPT: pose
  OP: replace
  TEXT: leaping forward with sword raised

  SEARCH:
  (not present)
  INSERT_AFTER:
  Wearing red dragon-scale armor, steel gauntlets, a crimson cloak.
  REPLACE:
  Leaping forward with sword raised.


COUNTER-EXAMPLE (DO NOT DO THIS — this is the failure mode):
  CURRENT PROMPT:
  A tall warrior, brown hair, brown eyes. Wearing red dragon-scale armor, steel gauntlets, a crimson cloak. Photorealistic cinematic lighting, sharp focus.
  CONCEPT: pose
  OP: replace
  TEXT: leaping forward

  BAD OUTPUT:
    SEARCH:
    A tall warrior, brown hair, brown eyes. Wearing red dragon-scale armor, steel gauntlets, a crimson cloak. Photorealistic cinematic lighting, sharp focus.
    REPLACE:
    Leaping forward.

  Wrong because SEARCH grabbed the ENTIRE positive prose. None of those sentences describe body position — they describe character, outfit, and style. Pose is NOT PRESENT in this prompt. The correct output is SEARCH: (not present) with INSERT_AFTER pointing at a single sentence (see Example 9b above).


EXAMPLE 10 (CONCEPT=outfit whole-outfit swap — SEARCH only the worn-items prose, leave body and pose intact):
  CURRENT PROMPT:
  Sir Galahad, tall muscular knight, brown hair, blue eyes.
  full plate armor, steel gauntlets, knee-length greaves, white tabard with red cross, leather sword belt.
  Kneeling in a stone chapel, head bowed.
  CONCEPT: outfit
  OP: replace
  TEXT: rough peasant tunic, brown breeches, worn leather sandals, hempen rope belt.

  SEARCH:
  full plate armor, steel gauntlets, knee-length greaves, white tabard with red cross, leather sword belt.
  REPLACE:
  rough peasant tunic, brown breeches, worn leather sandals, hempen rope belt.
"""


# ── Parser ────────────────────────────────────────────────────────


def _parse_locate_infill(raw: str) -> dict:
    out = {
        "search": None,
        "insert_after": None,
        "replace": None,
        "parse_errors": [],
    }
    text = (raw or "").strip()
    if not text:
        out["parse_errors"].append("empty response")
        return out
    if text.startswith("```"):
        ls = text.splitlines()
        if ls and ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        text = "\n".join(ls).strip()

    # Walk the labeled blocks. Tolerate "SEARCH:" on its own line OR
    # "SEARCH: value" on one line.
    blocks: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*(SEARCH|INSERT_AFTER|REPLACE)\s*:\s*(.*)$",
                     line, re.IGNORECASE)
        if m:
            current = m.group(1).upper()
            tail = m.group(2)
            blocks[current] = []
            if tail.strip():
                blocks[current].append(tail)
        else:
            if current:
                blocks[current].append(line)
    for k, lines in blocks.items():
        v = "\n".join(lines).strip()
        if k == "SEARCH":
            out["search"] = v
        elif k == "INSERT_AFTER":
            out["insert_after"] = v
        elif k == "REPLACE":
            out["replace"] = v
    if out["search"] is None:
        out["parse_errors"].append("no SEARCH block")
    if out["replace"] is None:
        out["parse_errors"].append("no REPLACE block")
    return out


# ── Apply ─────────────────────────────────────────────────────────
# Thin string substitution. No body-state lists, no anchor-leak
# detector, no fuzzy span search, no marker whitelists. The locate
# step is supposed to produce SEARCH/REPLACE/INSERT_AFTER that already
# encode the right semantics; if it doesn't, we either re-ask the AI
# or improve the prompt — not paper over it here.


def _apply_edit(prompt: str, parsed: dict, concept: str = "",
                op: str = "") -> tuple[str, str]:
    search = (parsed.get("search") or "").strip()
    replace = parsed.get("replace") or ""
    insert_after = (parsed.get("insert_after") or "").strip()

    is_not_present = (search.lower() == "(not present)" or not search)

    if not is_not_present:
        if search in prompt:
            return prompt.replace(search, replace, 1), "exact"
        return prompt, f"failed: SEARCH not in prompt ({search!r})"

    # Insertion path.
    # Sectioned replacements (start with `// Section:`) need newline
    # separation; flat-prose continues on the same paragraph with a space.
    sectioned = replace.lstrip().startswith("// ")
    sep_before = "\n\n" if sectioned else " "
    sep_after = "\n\n" if sectioned else ""

    if insert_after.lower() == "end" or not insert_after:
        head = prompt.rstrip()
        sep = "\n\n" if (head and replace.lstrip().startswith("// ")) \
            else ("\n" if head else "")
        return head + sep + replace.strip(), "insert_at_end"
    if insert_after in prompt:
        idx = prompt.index(insert_after) + len(insert_after)
        tail = prompt[idx:]
        tail_lstripped = tail.lstrip()
        joined = (prompt[:idx] + sep_before + replace.strip()
                  + (sep_after if sep_after else "")
                  + (tail_lstripped if sep_after else tail))
        return joined, "insert_after"
    return prompt, f"failed: INSERT_AFTER not in prompt ({insert_after!r})"


# ── Probe API ─────────────────────────────────────────────────────


_BODY_STATE_WORDS = {
    "barefoot", "barefooted", "bareheaded", "bare-handed", "topless",
    "bottomless", "shirtless", "naked", "nude", "going commando",
}


async def locate_infill(prompt: str, concept: str, intent: str,
                        op: str = "replace") -> tuple[dict, str]:
    # When TEXT is a body-state modifier on op=replace/add, the model
    # tends to capture only ONE item in the affected slot even when
    # the slot holds multiple. Add an in-message instruction so the
    # multi-item collapse rule from the system prompt fires reliably.
    is_body_state = (intent or "").strip().lower() in _BODY_STATE_WORDS
    hint = ""
    if is_body_state and op in ("replace", "add"):
        hint = (
            f"\n\nNOTE: TEXT '{intent}' is a body-state modifier (nothing on this region). "
            f"Scan the CURRENT PROMPT for EVERY item in the {concept} slot — there may be "
            f"more than one (e.g. socks AND boots, or stockings AND shoes). SEARCH MUST cover "
            f"ALL of them as ONE contiguous comma-joined span. Replacing only one item would "
            f"leave a contradiction (e.g. 'barefoot' and 'socks' present at the same time)."
        )
    user_msg = (
        f"CURRENT PROMPT:\n{prompt}\n\n"
        f"CONCEPT: {concept}\n"
        f"OP: {op}\n"
        f"TEXT: {intent}{hint}\n\n"
        f"Output the SEARCH / REPLACE (and INSERT_AFTER if needed):"
    )
    raw = await ai_api._run_generation(
        f"locate-infill-{abs(hash((prompt, concept, intent, op))) % 10000}",
        PROVIDER, CONFIG,
        LOCATE_INFILL_SYSTEM, user_msg, [],
    )
    return _parse_locate_infill(raw), (raw or "")


# ── Test fixtures ─────────────────────────────────────────────────


CAMMY_PROMPT = """Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar.
light blue sleeveless thong leotard with stiff tight mock-neck, thick sweater fabric material with ribbed texture on leotard, high-cut leotard exposing upper thigh and open upper back exposing shoulder blades, knee-high brown leather boots, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs.
Sitting with her legs up presenting feet at viewer.
Hyperrealistic anime style blending photorealism with anime aesthetics."""


FIXTURES = [
    {
        "name": "barefoot_replace",
        "prompt": CAMMY_PROMPT,
        "concept": "footwear", "op": "replace", "intent": "barefoot",
        "expect_search_contains_lc": ["boots"],
        "expect_replace_contains_lc": ["barefoot"],
        "expect_applied_contains_lc": ["barefoot"],
        "expect_applied_not_contains_lc": ["knee-high brown leather boots"],
    },
    {
        "name": "big_feet_anatomy_on_boots",
        "prompt": CAMMY_PROMPT,
        "concept": "footwear", "op": "anatomy_mod", "intent": "big feet",
        "expect_search_contains_lc": ["boots"],
        "expect_replace_contains_lc": ["boots", "big feet"],
        "expect_applied_contains_lc": ["big feet", "boots"],
    },
    {
        "name": "big_feet_anatomy_on_barefoot",
        "prompt": CAMMY_PROMPT.replace(
            "knee-high brown leather boots", "barefoot"),
        "concept": "footwear", "op": "anatomy_mod", "intent": "big feet",
        # Critical: barefoot must be PRESERVED, anatomy appended.
        "expect_search_contains_lc": ["barefoot"],
        "expect_replace_contains_lc": ["barefoot", "big feet"],
        "expect_applied_contains_lc": ["barefoot", "big feet"],
    },
    {
        "name": "red_socks_add",
        "prompt": CAMMY_PROMPT,
        "concept": "footwear", "op": "add", "intent": "red socks",
        # add to a slot with existing item: comma-append
        "expect_search_contains_lc": ["boots"],
        "expect_replace_contains_lc": ["red socks"],
        "expect_applied_contains_lc": ["red socks", "boots"],
    },
    {
        "name": "boots_back_on_after_barefoot",
        "prompt": CAMMY_PROMPT.replace(
            "knee-high brown leather boots", "barefoot"),
        "concept": "footwear", "op": "add", "intent": "brown boots",
        # add to a body-state-modifier span: drop the modifier
        "expect_search_contains_lc": ["barefoot"],
        "expect_replace_contains_lc": ["brown boots"],
        "expect_replace_not_contains_lc": ["barefoot"],
        "expect_applied_contains_lc": ["brown boots"],
        "expect_applied_not_contains_lc": ["barefoot"],
    },
    {
        "name": "leotard_pink_modify",
        "prompt": CAMMY_PROMPT,
        "concept": "tops", "op": "modify", "intent": "pink",
        "expect_search_contains_lc": ["leotard"],
        "expect_replace_contains_lc": ["pink"],
        "expect_applied_contains_lc": ["pink"],
        "expect_applied_not_contains_lc": ["light blue sleeveless thong"],
    },
    {
        "name": "longer_hair_modify",
        "prompt": CAMMY_PROMPT,
        "concept": "hair", "op": "modify", "intent": "longer",
        "expect_search_contains_lc": ["hair"],
        "expect_replace_contains_lc": ["hair"],
        "expect_applied_contains_lc": ["longer"],
    },
    {
        "name": "switch_to_standing_replace",
        "prompt": CAMMY_PROMPT,
        "concept": "pose", "op": "replace", "intent": "standing",
        "expect_search_contains_lc": ["sitting"],
        "expect_replace_contains_lc": ["standing"],
        "expect_applied_contains_lc": ["standing"],
        "expect_applied_not_contains_lc": ["sitting with her legs up"],
    },
    {
        "name": "remove_necktie_remove",
        "prompt": CAMMY_PROMPT,
        "concept": "neckwear", "op": "remove", "intent": "necktie",
        "expect_search_contains_lc": ["necktie"],
        "expect_applied_not_contains_lc": ["miniature yellow necktie"],
    },
    {
        "name": "add_hair_to_minimal_prompt",
        "prompt": "young woman in a red dress on a beach",
        "concept": "hair", "op": "add", "intent": "long black hair",
        "expect_applied_contains_lc": ["long black hair"],
    },
    {
        "name": "expression_insert_no_overwrite",
        # Verify locate doesn't grab a random nearby line and overwrite.
        "prompt": "young woman, blue eyes, standing on a beach at sunset",
        "concept": "expression", "op": "replace", "intent": "smiling",
        # Expression is not present; the model must signal (not present),
        # NOT overwrite "standing" with "smiling".
        "expect_applied_contains_lc": ["smiling", "standing"],
    },
]


def _print_block(label: str, body: str) -> None:
    print(f"--- {label} ---")
    for line in body.splitlines():
        print(f"  {line}")
    print()


def _check(fx: dict, parsed: dict, applied: str, method: str) -> list[str]:
    failures = []
    s_lc = (parsed.get("search") or "").lower()
    r_lc = (parsed.get("replace") or "").lower()
    a_lc = applied.lower()
    for w in fx.get("expect_search_contains_lc", []):
        if w.lower() not in s_lc:
            failures.append(f"SEARCH missing {w!r}")
    for w in fx.get("expect_replace_contains_lc", []):
        if w.lower() not in r_lc:
            failures.append(f"REPLACE missing {w!r}")
    for w in fx.get("expect_replace_not_contains_lc", []):
        if w.lower() in r_lc:
            failures.append(f"REPLACE unexpectedly contains {w!r}")
    for w in fx.get("expect_applied_contains_lc", []):
        if w.lower() not in a_lc:
            failures.append(f"APPLIED missing {w!r}")
    for w in fx.get("expect_applied_not_contains_lc", []):
        if w.lower() in a_lc:
            failures.append(f"APPLIED unexpectedly contains {w!r}")
    if parsed.get("parse_errors"):
        failures.append(f"parse errors: {parsed['parse_errors']}")
    if method == "failed":
        failures.append("apply method = failed")
    return failures


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    print(f"locate-then-infill probe — {len(selected)} fixtures\n")
    pass_count = 0
    for fx in selected:
        op = fx.get("op", "replace")
        print(f"========== {fx['name']} ==========")
        print(f"  concept: {fx['concept']}")
        print(f"  op:      {op}")
        print(f"  intent:  {fx['intent']!r}")
        try:
            parsed, raw = await locate_infill(fx["prompt"], fx["concept"],
                                              fx["intent"], op)
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            continue
        _print_block("RAW", raw)
        print(f"  search:        {parsed.get('search')!r}")
        print(f"  insert_after:  {parsed.get('insert_after')!r}")
        print(f"  replace:       {parsed.get('replace')!r}")
        if parsed.get("parse_errors"):
            print(f"  parse_errors:  {parsed['parse_errors']}")
        applied, method = _apply_edit(fx["prompt"], parsed, fx["concept"], op)
        print(f"  apply method:  {method}")
        _print_block("APPLIED", applied)
        failures = _check(fx, parsed, applied, method)
        if failures:
            print(f"  [FAIL] {len(failures)}")
            for f in failures:
                print(f"    ! {f}")
        else:
            print(f"  [PASS]")
            pass_count += 1
        print()
    print(f"=== {pass_count}/{len(selected)} fixtures passed ===")
    return 0 if pass_count == len(selected) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
