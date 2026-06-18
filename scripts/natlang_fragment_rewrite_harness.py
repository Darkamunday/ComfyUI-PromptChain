"""Fragment-level rewrite harness — INVESTIGATIVE.

Strategy under test (per user spec):
  1. Atomize the prompt by \\n, ., , — strip section headers; keep
     each comma-separated item or sentence as its own fragment.
  2. Hand ALL fragments to the AI in ONE call, numbered. The AI marks
     each fragment as either UNCHANGED or rewrites it.
  3. Splice rewrites back into the original prompt at the exact
     character positions the fragments occupied.

The AI is the filter AND the rewriter in a single pass. Python's job
is mechanical: atomize, splice.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_fragment_rewrite_harness.py              # all fixtures
  python scripts/natlang_fragment_rewrite_harness.py feet_big_socks
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import sys
import types
from dataclasses import dataclass


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
        instance=types.SimpleNamespace(routes=_S(),
                                       send_sync=lambda *a, **k: None))),
)


from core import ai_api  # noqa: E402


PROVIDER = "local"
CONFIG = {"local": {"base_url": "http://localhost:11434/v1",
                    "model": "qwen3-vl:8b-instruct"}}


# ── Atomizer ──────────────────────────────────────────────────────


@dataclass
class Atom:
    text: str       # the content of the fragment (no leading/trailing ws)
    start: int      # absolute char offset in the original prompt
    end: int        # absolute char offset (exclusive)
    section: str = ""   # lowercase section name (character/outfit/pose/style/negative)


_HEADER_LINE = re.compile(r"^\s*(//\s*\w+:.*|Negative Prompt:)\s*$")
_HEADER_NAME = re.compile(r"^\s*//\s*(\w+):", re.IGNORECASE)

# Sections where comma-separated items are independent slots/tokens
# that deserve fragment-level treatment. In other sections, commas are
# grammatical (clauses inside a sentence) and splitting on them
# fragments coherent prose — keep those sections at sentence level.
_COMMA_SPLIT_SECTIONS = {"outfit", "negative"}


def _section_name(line: str) -> str:
    """Return lowercase section name ('character', 'outfit', etc.)
    for a header line. 'Negative Prompt:' maps to 'negative'."""
    m = _HEADER_NAME.match(line)
    if m:
        return m.group(1).lower()
    if line.strip() == "Negative Prompt:":
        return "negative"
    return ""


def atomize(prompt: str) -> list[Atom]:
    """Split the prompt into atoms with absolute char positions.

    Splitting rules:
      - Section headers (// X: ... and Negative Prompt:) are excluded
        from atom output but tracked to determine the current section.
      - Inside `// Outfit:` and `Negative Prompt:` sections, split on
        both period and comma (each comma-separated item is its own
        slot / token).
      - Inside other sections (character, pose, style, etc.), split on
        period only — commas inside those sentences are grammatical,
        not semantic boundaries.
    """
    atoms: list[Atom] = []
    n = len(prompt)
    i = 0
    current_section = ""
    while i < n:
        # Skip leading whitespace on this line
        while i < n and prompt[i] in " \t":
            i += 1
        if i >= n:
            break
        line_end = prompt.find("\n", i)
        if line_end == -1:
            line_end = n
        line = prompt[i:line_end]
        if _HEADER_LINE.match(line):
            current_section = _section_name(line)
            i = line_end + 1
            continue
        if not line.strip():
            i = line_end + 1
            continue

        split_on_comma = current_section in _COMMA_SPLIT_SECTIONS
        seg_start = i
        j = i
        while j <= line_end:
            ch = prompt[j] if j < line_end else ""
            is_split = False
            if j < line_end:
                if ch == ".":
                    is_split = j + 1 >= line_end or prompt[j + 1] in " \t\n"
                elif ch == "," and split_on_comma:
                    is_split = j + 1 >= line_end or prompt[j + 1] in " \t\n"
            if is_split or j == line_end:
                frag = prompt[seg_start:j].strip()
                if frag:
                    raw = prompt[seg_start:j]
                    lstrip_offset = len(raw) - len(raw.lstrip())
                    rstrip_offset = len(raw) - len(raw.rstrip())
                    atoms.append(Atom(
                        text=frag,
                        start=seg_start + lstrip_offset,
                        end=j - rstrip_offset,
                        section=current_section,
                    ))
                seg_start = j + 1
            j += 1
        i = line_end + 1
    return atoms


def splice(prompt: str, atoms: list[Atom], rewrites: dict[int, str]) -> str:
    """Rebuild the prompt with rewrites substituted at each atom's
    char range. Atoms with no entry in `rewrites` keep their original
    text. Walks atoms in order, copying inter-atom characters
    (separators, headers, whitespace) verbatim."""
    out: list[str] = []
    cursor = 0
    for idx, atom in enumerate(atoms):
        out.append(prompt[cursor:atom.start])
        out.append(rewrites.get(idx, atom.text))
        cursor = atom.end
    out.append(prompt[cursor:])
    return "".join(out)


# ── Two-pass LLM: classify, then rewrite ─────────────────────────


CLASSIFY_SYSTEM = """You are reading a natural-language image-generation prompt that has been split into NUMBERED FRAGMENTS.

The user has requested a single modification.

Your only job: identify which fragment NUMBERS are directly affected by the modification.

A fragment is AFFECTED when it:
  - Names or describes the body part the user is modifying.
  - Is a garment ON that exact body part (socks/shoes/boots for FEET, gloves/gauntlets for HANDS, hats for HEAD). The garment must directly sit on the body part — not merely cover an adjacent region.
  - Is an action or pose that explicitly shows / points at / involves that body part.
  - Is a body-state modifier in the affected region (barefoot, bareheaded, topless).

A fragment is NOT affected just because:
  - It's a garment that COVERS the same general body area but doesn't sit on the affected part. A leotard covers a torso including the chest, but the leotard's MOCK-NECK, FABRIC, CUT, and SHAPE are not affected by "bigger breasts" — only a fragment naming the bust/chest itself, or directly clothing on it, would be.
  - It mentions something near, beside, or adjacent to the affected part.
  - It's about a different body part entirely.

A fragment is NEVER affected when it talks about:
  - Rendering / style language (lighting, scattering, focus, anime aesthetic) — UNLESS the style fragment literally names the affected body part (e.g. style fragment says "expressive anime eyes" and the request is about eyes).
  - Negative-prompt tokens (the don't-render list).

Output ONLY a comma-separated list of integer fragment numbers, like:
  15, 21

If no fragments are affected, output the literal token NONE.

No commentary. No explanation. Just the numbers."""


REWRITE_SYSTEM = """You are inspecting a natural language image generation prompt. The user has requested a modification on the snippet below. Return the new snippet that reflects the modification — or the literal token UNCHANGED if the modification doesn't apply to this snippet.

Guidance:
  - The modification might be a qualifier (size, length, color), a slot replacement, or a removal. Apply whichever makes sense for this snippet.
  - For a qualifier, weave it into the existing words. Don't append a new clause.
  - For a slot replacement, swap the old content for the new content — don't keep both. If qualifiers (size, color, fit) were attached to the old content and still apply to the new content, carry them forward into the new phrasing.
  - Don't invent details the user didn't ask for.

Return ONLY the new snippet text, or the token UNCHANGED. No quotes, no commentary."""


async def llm_classify(user_request: str,
                       atoms: list[Atom]) -> tuple[list[int], str]:
    """Pass 1: return list of 0-based atom indices the model thinks
    are affected by the modification."""
    if not atoms:
        return [], ""
    numbered = "\n".join(f"{i + 1}. {a.text}" for i, a in enumerate(atoms))
    user_msg = (
        f"USER REQUEST: {user_request}\n\n"
        f"FRAGMENTS:\n{numbered}\n\n"
        f"Which fragment numbers are affected? (comma-separated, or NONE)"
    )
    raw = await ai_api._run_generation(
        f"frag-classify-{abs(hash((user_request, numbered))) % 10000}",
        PROVIDER, CONFIG,
        CLASSIFY_SYSTEM, user_msg, [],
    )
    cleaned = (raw or "").strip()
    if cleaned.upper().startswith("NONE"):
        return [], (raw or "")
    indices: list[int] = []
    for tok in re.findall(r"\d+", cleaned):
        n = int(tok) - 1
        if 0 <= n < len(atoms) and n not in indices:
            indices.append(n)
    return indices, (raw or "")


async def llm_rewrite_one(user_request: str, fragment: str) -> tuple[str, str]:
    """Pass 2 per atom: tight rewrite of a single fragment."""
    user_msg = (
        f"USER REQUEST: {user_request}\n\n"
        f"FRAGMENT:\n{fragment}\n\n"
        f"Rewritten fragment:"
    )
    raw = await ai_api._run_generation(
        f"frag-rewriteone-{abs(hash((user_request, fragment))) % 10000}",
        PROVIDER, CONFIG,
        REWRITE_SYSTEM, user_msg, [],
    )
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"^(SNIPPET|Output|Result|Rewritten|Fragment|->|→):\s*",
                     "", cleaned, flags=re.IGNORECASE)
    if (cleaned.startswith('"') and cleaned.endswith('"')) \
            or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()
    # Single-line only — model sometimes adds commentary on subsequent lines.
    cleaned = cleaned.split("\n", 1)[0].strip()
    if cleaned.upper().strip().rstrip(".") == "UNCHANGED":
        return "", (raw or "")
    return cleaned, (raw or "")


async def run_modification(prompt: str, user_request: str) -> dict:
    atoms = atomize(prompt)
    affected, classify_raw = await llm_classify(user_request, atoms)
    # HARD GUARD — negative-prompt tokens are NEVER editable. The
    # classify system prompt says so but the model occasionally
    # overrides it (especially when the request contains body-part
    # words that also appear in negatives like "bad hands"). Drop any
    # classified atom whose section is `negative` before rewriting.
    affected = [i for i in affected
                if atoms[i].section.lower() != "negative"]
    # Parallel rewrite of affected atoms — each is its own LLM call
    rewrite_tasks = [
        llm_rewrite_one(user_request, atoms[i].text) for i in affected
    ]
    rewrite_results = await asyncio.gather(*rewrite_tasks)
    rewrites: dict[int, str] = {}
    for atom_idx, (text, _raw) in zip(affected, rewrite_results):
        if text and text != atoms[atom_idx].text:
            rewrites[atom_idx] = text
    final = splice(prompt, atoms, rewrites)
    return {
        "atoms": atoms,
        "affected_indices": affected,
        "classify_raw": classify_raw,
        "rewrites": rewrites,
        "final_prompt": final,
    }


# ── Fixtures ──────────────────────────────────────────────────────


CAMMY_RED_SOCKS = """// Character: Cammy White (Street Fighter)
Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar.

// Outfit: Killer Bee (Cammy White)
light blue sleeveless thong leotard with stiff tight mock-neck, thick sweater fabric material with ribbed texture on leotard, high-cut leotard exposing upper thigh and open upper back exposing shoulder blades, red socks, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs.

// Pose:
Sitting with legs up pointing red socks at viewer.

// Style: Hyperrealistic
Hyperrealistic anime style blending photorealism with anime aesthetics. Realistic skin texture with subsurface scattering, expressive anime eyes with realistic reflections. Cinematic studio lighting, detailed hair strands, depth of field. High fidelity rendering, sharp focus.

Negative Prompt:
blurry, low quality, jpeg artifacts, watermark, text, logo, bad anatomy, deformed, distorted, flat colors, flat lighting, pure cartoon, chibi, super deformed, sketchy, unfinished"""


CAMMY_BAREFOOT = """// Character: Cammy White (Street Fighter)
Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar.

// Outfit: Killer Bee (Cammy White)
light blue sleeveless thong leotard with stiff tight mock-neck, thick sweater fabric material with ribbed texture on leotard, high-cut leotard exposing upper thigh and open upper back exposing shoulder blades, barefoot, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs.

// Pose:
Sitting with legs up pointing feet at viewer.

// Style: Hyperrealistic
Hyperrealistic anime style blending photorealism with anime aesthetics. Realistic skin texture with subsurface scattering, expressive anime eyes with realistic reflections. Cinematic studio lighting, detailed hair strands, depth of field. High fidelity rendering, sharp focus.

Negative Prompt:
blurry, low quality, jpeg artifacts, watermark, text, logo, bad anatomy, deformed, distorted, flat colors, flat lighting, pure cartoon, chibi, super deformed, sketchy, unfinished"""


CAMMY_BOOTS = """// Character: Cammy White (Street Fighter)
Cammy White from Street Fighter, female, blonde hair, twin braids, sidelocks, long hair, blue eyes, toned athletic female body, moderate average bust size, a single extremely faint vertical old wound on lower jaw with no blood, a scar.

// Outfit: Killer Bee (Cammy White)
light blue sleeveless thong leotard with stiff tight mock-neck, thick sweater fabric material with ribbed texture on leotard, high-cut leotard exposing upper thigh and open upper back exposing shoulder blades, knee-high brown leather boots, red fingerless gauntlets, wearing a small blue garrison cap on head, miniature yellow necktie, black armband, blue lightning bolt paint designs on bare thighs.

// Pose:
Standing in a confident fighting stance, feet planted shoulder-width apart.

// Style: Hyperrealistic
Hyperrealistic anime style blending photorealism with anime aesthetics. Realistic skin texture with subsurface scattering, expressive anime eyes with realistic reflections. Cinematic studio lighting, detailed hair strands, depth of field. High fidelity rendering, sharp focus.

Negative Prompt:
blurry, low quality, jpeg artifacts, watermark, text, logo, bad anatomy, deformed, distorted, flat colors, flat lighting, pure cartoon, chibi, super deformed, sketchy, unfinished"""


FIXTURES = [
    {"name": "feet_big_socks",     "prompt": CAMMY_RED_SOCKS, "request": "make her feet big"},
    {"name": "feet_big_barefoot",  "prompt": CAMMY_BAREFOOT,  "request": "make her feet big"},
    {"name": "feet_big_boots",     "prompt": CAMMY_BOOTS,     "request": "make her feet big"},
    {"name": "breasts_big",        "prompt": CAMMY_RED_SOCKS, "request": "give her bigger breasts"},
    {"name": "hands_big",          "prompt": CAMMY_RED_SOCKS, "request": "give her bigger hands"},
    {"name": "eyes_big",           "prompt": CAMMY_RED_SOCKS, "request": "make her eyes bigger"},
    {"name": "longer_hair",        "prompt": CAMMY_RED_SOCKS, "request": "give her longer hair"},
]


def _hr(s):
    print("\n" + "=" * 78 + f"\n{s}\n" + "=" * 78)


async def run_fixture(fx: dict) -> None:
    _hr(f"FIXTURE: {fx['name']}    REQUEST: {fx['request']!r}")
    result = await run_modification(fx["prompt"], fx["request"])
    atoms = result["atoms"]
    rewrites = result["rewrites"]

    print(f"\n  atoms: {len(atoms)}   rewrites: {len(rewrites)}\n")
    for i, atom in enumerate(atoms):
        if i in rewrites:
            print(f"  *** [{i+1:2d}] REWROTE  {atom.text!r}")
            print(f"           -> {rewrites[i]!r}")
        else:
            print(f"      [{i+1:2d}] same     {atom.text[:80]!r}")

    diff = list(difflib.unified_diff(
        fx["prompt"].splitlines(), result["final_prompt"].splitlines(),
        fromfile="before", tofile="after", lineterm="",
    ))
    if diff:
        print("\n  --- DIFF ---")
        for line in diff:
            print("  " + line)
    else:
        print("\n  (no diff)")


async def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES
                if not name_filter or name_filter in f["name"]]
    if not selected:
        print(f"No fixture matched filter {name_filter!r}")
        return 1
    for fx in selected:
        await run_fixture(fx)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
