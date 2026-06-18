"""Natlang v3 — LLM-authored body with candidate-hint patch_user.

v3 keeps v2's decompose (typed-action sub_intents) and v2's deterministic
modifier handling (alias_scan + slot clears + substitute_section), but
REPLACES v2's state-driven `render_all_sections` with a single LLM
authoring pass. The authoring LLM sees:

  - The character appearance reference (bio.base_natlang) so it can
    name body parts / preserve identity without inventing facts.
  - The current node_prompt (after deterministic modifier rewrites).
  - Per-intent CURATED CANDIDATES retrieved from the DB --
    chip via bucket_search, outfit/pose via bio name fuzzy match.
    Each candidate carries a verbatim header + body the LLM swaps in
    when the user's intent matches it.
  - The user's raw edit request.

Rule: when a candidate fits an intent, swap that section's header+body
to the candidate's text VERBATIM. When no candidate fits, edit the
section's prose minimally to fulfill the request -- free-form. The
rendering layer is no longer rigid; the chip/outfit/pose DB is a help,
not a rail. (Same shape tag mode uses.)

v2's state-driven path stays intact (the file imports v2 helpers but
does not modify them) so we can A/B at the natural-mode short-circuit
and roll back if v3 regresses.
"""
from __future__ import annotations

import re
from typing import Optional


V3_SYSTEM = """You edit anime/illustration prompts written in natural-language section form. The prompt is split into `// Section: <Name>` blocks: Character, Outfit, Pose, Expression, Scene/Setting, Style, plus an optional `Negative Prompt:` trailer at the end. Each block has a header line and a prose body.

You are given:
  - A character appearance reference (the character's base description from the bio DB).
  - The existing node_prompt (the body the user is editing).
  - The user's edit request in plain English.
  - Optionally, a list of curated candidates retrieved from the bio DB for this request. Each candidate has a header (e.g., `// Pose: <name>`) and a verbatim body.

How to produce output:
  1. START from the existing node_prompt. Your output MUST include EVERY section that appears in node_prompt -- including Style and the Negative Prompt trailer if present. Do NOT drop any section. Do NOT collapse multiple sections into one. Sections you weren't asked to touch must be copied verbatim from node_prompt, header and body unchanged, even if the body is long prose. Never abbreviate a section body to a placeholder like "[bio]" or "(unchanged)" -- always emit the full prose from node_prompt.
  2. For each section in node_prompt, decide:
     - Does the user's request touch this section? If NO, copy it exactly -- same header, same body.
     - If YES and a curated candidate exists for that section type (Pose, Outfit, Character), use the candidate's header AND body VERBATIM. Do not paraphrase the header. Do not paraphrase or expand the body. Do not combine the candidate with your own additions. The candidate IS the section's content -- copy it exactly, character-for-character.
     - If YES and no candidate exists for that section, edit the section's prose minimally to fulfill the request. Add descriptive prose freely if needed.
  3. Candidate hints describe ONE section each; they are never the full output. Other sections must still come from node_prompt. When a candidate is provided, the candidate's header is the section's exact header in the output -- never invent your own header phrasing.
  4. Never invent new characters. Never invent outfit/pose names that aren't in node_prompt or in the candidates.
  5. Anatomy/body modifications ("bigger feet", "longer hair", "more muscular", "broader shoulders", "narrower waist") ALWAYS attach to the Character section, not Outfit or Pose. This applies even when the Pose section already mentions related body parts -- a pose describing "presenting feet" does NOT satisfy a "bigger feet" anatomy request. The character's body shape is independent from what the character is doing. If the user mentions an anatomy modification, edit the Character section's prose to incorporate it; leave the Pose section alone.
  6. Clothing additions/removals/colors attach to the Outfit section.
  7. Posture/body-position changes attach to the Pose section.
  8. Facial affect (smile, frown, blush) attaches to the Expression section. Camera framing ("focus on feet", "at viewer") is a Pose concept, not Expression.
  9. Style and Negative Prompt sections are NEVER edited by user appearance/pose requests. Copy them verbatim from node_prompt unless the user explicitly asked to change them.
 10. NEVER invent sections that are not present in node_prompt. If node_prompt has no // Scene, no // Style, or no Negative Prompt, do NOT emit those sections at all. Do NOT emit placeholder text like "(unchanged)", "(no change)", "(see node_prompt)" -- if a section exists in node_prompt, output its full real prose; if it doesn't, omit it entirely.
 11. Output only the section blocks separated by blank lines. No commentary, no markdown fences."""


def _strip_leading_section_header(text: str) -> str:
    """Drop a leading `// Section: ...` line from text -- bio fields
    occasionally embed their own header; we add the header ourselves
    when assembling reference / candidate blocks."""
    if not text:
        return ""
    lines = text.splitlines()
    if lines and re.match(r"^\s*//\s*\w+:", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def build_patch_user(bios: list[dict],
                     node_prompt: str,
                     user_request: str,
                     candidates: list[dict]) -> str:
    """Assemble the v3 patch_user message. Layered:
      1. Character appearance reference (bio.base_natlang only -- no
         outfit/pose duplication; those come via candidates).
      2. Curated candidates with verbatim header+body, one per intent
         that retrieved a hit.
      3. Existing node_prompt the model edits in place.
      4. User's raw edit request.
    """
    parts: list[str] = []
    if bios:
        ref_lines = [
            "Character appearance reference (CONTEXT ONLY -- never paste "
            "or paraphrase this into the output, never use it as a "
            "placeholder like '[bio]'. The output's character section "
            "is already in node_prompt; copy it from there verbatim "
            "unless the user explicitly asked you to change the "
            "character's appearance):",
        ]
        for b in bios or []:
            if not b or not b.get("tag"):
                continue
            display = (b.get("display") or "").strip() or b["tag"]
            series = (b.get("series") or "").strip()
            head = f"// Character: {display}" + (f" ({series})" if series else "")
            base_nat = _strip_leading_section_header(
                (b.get("base_natlang") or "").strip()
            )
            ref_lines.append("")
            ref_lines.append(head)
            if base_nat:
                ref_lines.append(base_nat)
            else:
                base_tags = (b.get("base_tags") or "").strip()
                if base_tags:
                    ref_lines.append(
                        f"(no natlang available, derived from tags: {base_tags})"
                    )
        parts.append("\n".join(ref_lines))
    if candidates:
        cand_lines = [
            "Curated candidates from the bio DB for this request. When the "
            "user's intent matches one of these, swap the matching section "
            "to its header + body VERBATIM. When nothing matches an intent, "
            "edit the relevant section's prose directly to fulfill the "
            "request.",
        ]
        for c in candidates:
            intent = (c.get("intent") or "").strip()
            header = (c.get("header") or "").strip()
            body = (c.get("body") or "").strip()
            cand_lines.append("")
            cand_lines.append(f"[Candidate for intent: {intent}]")
            cand_lines.append(header)
            cand_lines.append(body)
        parts.append("\n".join(cand_lines))
    if node_prompt and node_prompt.strip():
        parts.append(
            "Existing node_prompt (modify this; preserve sections you "
            "weren't asked to change):\n" + node_prompt.strip()
        )
    parts.append("User edit request:\n" + (user_request or "").strip())
    return "\n\n".join(parts)


_PLACEHOLDER_BODY_RE = re.compile(
    r"^\s*[\[\(](?:no\s+\w+(?:\s+\w+){0,3}\s+specified|"
    r"unchanged(?:\s+from\s+\w+)?|no\s+change|see\s+\w+|"
    r"empty|none|n/a|tbd|placeholder)[\]\)]\s*$",
    re.IGNORECASE,
)


def enforce_candidate_substitutions(v3_body: str,
                                    candidates: list[dict]) -> str:
    """Force each candidate's header + body into v3's output, replacing
    whatever v3 LLM wrote for that section kind. The 8B class authoring
    LLM occasionally paraphrases a chip header or rewrites an outfit
    body even when explicitly told to use the candidate verbatim --
    candidates exist precisely BECAUSE v2 already resolved the user's
    intent, so v3 has no business re-resolving it. Programmatic
    substitution makes the candidate authoritative."""
    if not candidates or not v3_body:
        return v3_body
    out = v3_body
    for c in candidates:
        header = (c.get("header") or "").strip()
        body = (c.get("body") or "").strip()
        if not header or not body:
            continue
        m = re.match(r"^\s*//\s*(\w+)", header)
        if not m:
            continue
        kind = m.group(1).lower()
        # Match the WHOLE section of this kind: from `// Kind: ...` line
        # up to (but not including) the next `// SomethingElse:` header
        # or EOF. Multi-line dotall via [\s\S].
        pattern = re.compile(
            rf"(?m)^\s*//\s*{re.escape(kind)}\b[^\n]*\n(?:(?!^\s*//\s*\w+).)*",
            re.DOTALL | re.IGNORECASE,
        )
        replacement = f"{header}\n{body}\n\n"
        new_out, n = pattern.subn(replacement, out, count=1)
        if n > 0:
            out = new_out
    # Collapse any runs of 3+ newlines created by the substitution.
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _parse_section_kinds(body: str) -> set[str]:
    """Return the set of // Section kinds present in `body` (lower-case
    first word of each // header, e.g. {'character', 'outfit', 'pose'})."""
    kinds: set[str] = set()
    if not body:
        return kinds
    for m in re.finditer(r"(?m)^\s*//\s*(\w+)", body):
        kinds.add(m.group(1).lower())
    return kinds


def strip_invented_sections(v3_body: str, v2_body: str) -> str:
    """Drop sections present in `v3_body` that were NOT in `v2_body`.
    v3 LLM occasionally invents `// Pose:`, `// Scene:`, `// Negative
    Prompt:` etc. with placeholder bodies even when those sections
    weren't in the v2 baseline. v2's section set is the authoritative
    one (it reflects state + user deltas); v3 is only allowed to edit
    the bodies of existing sections, never to add new ones."""
    if not v3_body:
        return v3_body
    v2_kinds = _parse_section_kinds(v2_body)
    if not v2_kinds:
        return v3_body
    lines = v3_body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*//\s*(\w+)", line)
        if m:
            kind = m.group(1).lower()
            header = line
            body_lines: list[str] = []
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*//\s*\w+", lines[j]):
                body_lines.append(lines[j])
                j += 1
            if kind in v2_kinds:
                out.append(header)
                out.extend(body_lines)
            i = j
        else:
            out.append(line)
            i += 1
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_placeholder_sections(body: str) -> str:
    """v3 LLM occasionally invents sections with placeholder bodies like
    `// Pose: \n[no pose specified]` or `// Scene: \n(unchanged)` even
    when those sections weren't in node_prompt. Walk the output and
    drop any // Section block whose body is a bracketed placeholder.

    Preserves: sections with real prose, sections that begin with `[bio]`
    when it's a deliberate test placeholder in the input (only the auto-
    generated `[no X specified]` / `(unchanged)` patterns are stripped)."""
    if not body or "//" not in body:
        return body
    lines = body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*//\s*\w+", line):
            # Collect section body lines (until next // header or EOF).
            header = line
            body_lines: list[str] = []
            j = i + 1
            while j < len(lines) and not re.match(r"^\s*//\s*\w+", lines[j]):
                body_lines.append(lines[j])
                j += 1
            body_text = "\n".join(body_lines).strip()
            # Drop sections whose entire body is a recognized placeholder.
            if body_text and not _PLACEHOLDER_BODY_RE.match(body_text):
                out.append(header)
                out.extend(body_lines)
            i = j
        else:
            out.append(line)
            i += 1
    # Collapse runs of >2 blank lines that result from the drops.
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def candidate_from_chip(chip: dict, intent_text: str) -> dict:
    """Build a v3 candidate dict from a chip row returned by
    bucket_search.search_for_apply (or _llm_pick_pose_chip)."""
    name = (chip.get("display_name") or chip.get("item_tag") or "").strip()
    body = (chip.get("base_natlang") or "").strip()
    header = f"// Pose: {name}" if name else "// Pose:"
    return {
        "intent": intent_text,
        "header": header,
        "body": body,
        "source": "chip",
        "source_id": chip.get("item_tag") or chip.get("id"),
    }


def candidate_from_bio_outfit(outfit: dict, char_display: str,
                              intent_text: str) -> dict:
    """Build a v3 candidate dict from a bio.outfits row."""
    name = (outfit.get("name") or outfit.get("outfit_name") or "").strip()
    body = _strip_leading_section_header(
        (outfit.get("natlang") or outfit.get("outfit_natlang") or "").strip()
    )
    header = (
        f"// Outfit: {name} from Character: {char_display}"
        if name and char_display
        else (f"// Outfit: {name}" if name else "// Outfit:")
    )
    return {
        "intent": intent_text,
        "header": header,
        "body": body,
        "source": "bio_outfit",
        "source_id": outfit.get("id"),
    }


def candidate_from_bio_pose(pose: dict, char_display: str,
                            intent_text: str) -> dict:
    """Build a v3 candidate dict from a bio.poses row."""
    name = (pose.get("name") or pose.get("pose_name") or "").strip()
    body = _strip_leading_section_header(
        (pose.get("natlang") or pose.get("pose_natlang") or "").strip()
    )
    is_sig = bool(pose.get("is_signature"))
    sig_suffix = " (signature)" if is_sig else ""
    header = (
        f"// Pose: {name}{sig_suffix} from Character: {char_display}"
        if name and char_display
        else (f"// Pose: {name}{sig_suffix}" if name else "// Pose:")
    )
    return {
        "intent": intent_text,
        "header": header,
        "body": body,
        "source": "bio_pose",
        "source_id": pose.get("id"),
    }


def find_bio_outfit_by_name(text: str, bios: list[dict]) -> tuple[Optional[dict], Optional[str]]:
    """Fuzzy match `text` (an outfit-swap intent payload like 'killer bee')
    against ANY bio's all_outfits list. Returns (outfit_row, char_display)
    or (None, None). Cross-character borrow supported -- the matched
    outfit may belong to a different character than the primary.
    """
    if not text or not bios:
        return None, None
    needle = text.strip().lower()
    if not needle:
        return None, None
    # Pass 1: exact word match in outfit_name.
    for b in bios:
        display = (b.get("display") or "").strip()
        for o in (b.get("all_outfits") or []):
            name = (o.get("outfit_name") or o.get("name") or "").lower()
            if name and needle == name:
                return o, display
    # Pass 2: substring match (e.g. "killer bee" in "Killer Bee Signature").
    for b in bios:
        display = (b.get("display") or "").strip()
        for o in (b.get("all_outfits") or []):
            name = (o.get("outfit_name") or o.get("name") or "").lower()
            if name and (needle in name or name in needle):
                return o, display
    # Pass 3: user_requested_outfit / default_outfit fallback (already
    # matched by upstream bio loader). When all_outfits isn't populated
    # the loader's pick is the best signal we have.
    for b in bios:
        display = (b.get("display") or "").strip()
        cand = b.get("user_requested_outfit") or b.get("default_outfit")
        if cand:
            name = (cand.get("name") or cand.get("outfit_name") or "").lower()
            if name and (needle in name or name in needle):
                return cand, display
    return None, None


def candidates_from_v2_sections(rendered_sections: list[dict],
                                deltas: list) -> list[dict]:
    """For each section type touched by a swap-shape delta this turn,
    emit a candidate whose header + body come from v2's RENDERED section.

    Why v2's render and not the raw delta bio_natlang: v2's render
    already applied modifier slot clears, slot-fill overlays, and the
    pose-chip slot-context injection. The raw bio_natlang doesn't --
    it's just the curated DB row. Locking v3 to v2's render preserves
    those mutations through the LLM authoring step.

    Sections not touched by a swap delta reach the LLM only via the
    node_prompt + user_request; the LLM is free to edit their prose to
    fulfill any unaddressed intent (e.g. "bigger feet" on Character)."""
    from . import natlang_facts as _nlf

    swap_kinds: set[str] = set()
    for d in deltas or []:
        if isinstance(d, _nlf.ApplyPoseChipDelta):
            swap_kinds.add("pose")
        elif isinstance(d, _nlf.SwapPoseDelta):
            swap_kinds.add("pose")
        elif isinstance(d, _nlf.SwapOutfitDelta):
            swap_kinds.add("outfit")
        elif isinstance(d, _nlf.SwapCharacterDelta):
            swap_kinds.add("character")
    if not swap_kinds:
        return []

    out: list[dict] = []
    for s in rendered_sections or []:
        header = (s.get("header") or "").strip()
        m = re.match(r"^\s*//\s*(\w+)\b", header, re.IGNORECASE)
        if not m:
            continue
        kind = m.group(1).lower()
        if kind == "scene":
            kind = "setting"
        if kind not in swap_kinds:
            continue
        body = (s.get("body_text") or "").strip()
        if not body:
            continue
        out.append({
            "intent": f"{kind} swap (preserve v2 render)",
            "header": header,
            "body": body,
            "source": "v2_render",
            "source_id": kind,
        })
    return out


def candidates_from_deltas(deltas: list, bios: list[dict]) -> list[dict]:
    """Legacy: derive candidates from raw delta bio_natlang. Use
    candidates_from_v2_sections instead -- this version skips v2's
    modifier slot-clear render mutations and emits raw bio text.
    Retained for callers that don't have rendered_sections handy."""
    from . import natlang_facts as _nlf

    out: list[dict] = []
    primary_display = ""
    if bios:
        b0 = bios[0] or {}
        primary_display = (b0.get("display") or "").strip() or (b0.get("tag") or "")

    for d in deltas or []:
        if isinstance(d, _nlf.ApplyPoseChipDelta):
            name = (d.display_name or "").strip()
            body = (d.base_natlang or "").strip()
            if not body:
                continue
            out.append({
                "intent": f"pose chip: {name or d.chip_tag}",
                "header": f"// Pose: {name}" if name else "// Pose:",
                "body": body,
                "source": "chip",
                "source_id": d.chip_tag,
            })
        elif isinstance(d, _nlf.SwapOutfitDelta):
            name = (d.outfit_name or "").strip()
            body = (d.bio_natlang or "").strip()
            if not body:
                continue
            owner = (d.source_character_display or "").strip() or primary_display
            header = (
                f"// Outfit: {name} from Character: {owner}"
                if name and owner else (f"// Outfit: {name}" if name else "// Outfit:")
            )
            out.append({
                "intent": f"outfit swap: {name}",
                "header": header,
                "body": _strip_leading_section_header(body),
                "source": "bio_outfit",
                "source_id": d.outfit_id,
            })
        elif isinstance(d, _nlf.SwapPoseDelta):
            name = (d.pose_name or "").strip()
            body = (d.bio_natlang or "").strip()
            if not body:
                continue
            owner = (d.source_character_display or "").strip() or primary_display
            sig_suffix = " (signature)" if getattr(d, "is_signature", False) else ""
            header = (
                f"// Pose: {name}{sig_suffix} from Character: {owner}"
                if name and owner else (f"// Pose: {name}{sig_suffix}" if name else "// Pose:")
            )
            out.append({
                "intent": f"pose swap: {name}",
                "header": header,
                "body": _strip_leading_section_header(body),
                "source": "bio_pose",
                "source_id": d.pose_id,
            })
    return out


async def retrieve_candidates(sub_intents: list[dict],
                              bios: list[dict],
                              user_request: str,
                              pose_chip_picker_fn=None) -> list[dict]:
    """Per-sub-intent candidate lookup. For each typed-action line from
    `_decompose_user_request`, route to the right retrieval surface:

      pose: <text>          -> bucket_search + LLM pick -> chip candidate
                              (fall back to bio.matched_pose name match)
      outfit-swap: <name>   -> bio.all_outfits fuzzy name match
      pose-swap: <name>     -> bio.all_poses fuzzy name match
      (outfit-fill, outfit-remove, outfit-modifier, character,
       expression, setting, style)  -> no candidate, LLM free-form

    Returns at most one candidate per sub_intent. Same chip is not
    emitted twice (de-dup by source_id). Order matches sub_intents so
    the LLM sees candidates in user-mention order.
    """
    from . import natlang_facts as _nlf

    out: list[dict] = []
    seen_chip_tags: set[str] = set()
    seen_outfit_ids: set = set()
    seen_pose_ids: set = set()

    primary_display = ""
    if bios:
        b0 = bios[0] or {}
        primary_display = (b0.get("display") or "").strip() or (b0.get("tag") or "")

    for sub in sub_intents or []:
        section = (sub.get("section") or "").lower()
        action = (sub.get("action") or "").lower()
        text = (sub.get("text") or "").strip()
        if not text:
            continue

        if section == "outfit" and action == "swap":
            outfit_row, char_display = find_bio_outfit_by_name(text, bios)
            if outfit_row:
                oid = outfit_row.get("id")
                if oid in seen_outfit_ids:
                    continue
                seen_outfit_ids.add(oid)
                out.append(candidate_from_bio_outfit(
                    outfit_row, char_display or primary_display, text,
                ))
            continue

        if section == "pose":
            # Bio pose name match first -- a character pose is more
            # specific than a generic chip.
            pose_row, char_display = find_bio_pose_by_name(text, bios)
            if pose_row:
                pid = pose_row.get("id")
                if pid in seen_pose_ids:
                    continue
                seen_pose_ids.add(pid)
                out.append(candidate_from_bio_pose(
                    pose_row, char_display or primary_display, text,
                ))
                continue
            # Fall back to chip lookup via bucket_search + LLM pick.
            try:
                chip = await _nlf._lookup_pose_chip_llm_picked(
                    text, pose_chip_picker_fn, picker_context=user_request,
                )
            except Exception:
                chip = None
            if chip:
                tag = chip.get("item_tag") or ""
                if tag and tag in seen_chip_tags:
                    continue
                if tag:
                    seen_chip_tags.add(tag)
                out.append(candidate_from_chip(chip, text))
            continue

        # outfit-fill / outfit-remove / outfit-modifier / outfit-strip /
        # character / expression / setting / style / clear:
        # no candidate -- LLM authors free-form (or modifier pre-pass
        # rewrites node_prompt deterministically before this point).
        continue

    return out


def find_bio_pose_by_name(text: str, bios: list[dict]) -> tuple[Optional[dict], Optional[str]]:
    """Fuzzy match `text` against any bio's all_poses list. Same passes
    as `find_bio_outfit_by_name`."""
    if not text or not bios:
        return None, None
    needle = text.strip().lower()
    if not needle:
        return None, None
    for b in bios:
        display = (b.get("display") or "").strip()
        for p in (b.get("all_poses") or []):
            name = (p.get("pose_name") or p.get("name") or "").lower()
            if name and needle == name:
                return p, display
    for b in bios:
        display = (b.get("display") or "").strip()
        for p in (b.get("all_poses") or []):
            name = (p.get("pose_name") or p.get("name") or "").lower()
            if name and (needle in name or name in needle):
                return p, display
    for b in bios:
        display = (b.get("display") or "").strip()
        cand = b.get("matched_pose")
        if cand:
            name = (cand.get("name") or cand.get("pose_name") or "").lower()
            if name and (needle in name or name in needle):
                return cand, display
    return None, None
