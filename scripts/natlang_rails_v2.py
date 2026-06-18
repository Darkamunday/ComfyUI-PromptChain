"""Rails v2 — decompose -> scan -> dispatch -> apply.

The single 'locate-infill' step has been split:

  Top-level sentence concepts go through Python dispatch using scan
  output + canonical order. No 'is it present?' decision for the LLM
  to flub.

  Sub-slot concepts (footwear within outfit, hair within character)
  still use locate-infill, but scoped to the parent sentence rather
  than the full prompt. Much tighter target.

The apply step is unchanged — exact substring substitute or insert at
anchor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import types


logger = logging.getLogger("promptchain.ai.rails")


def _preview(s: str | None, n: int = 100) -> str:
    s = (s or "").replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"...({len(s)}c)"


_HEADER_LINE = re.compile(r"(?m)^//\s*\w+:")


def _prompt_has_headers(prompt: str) -> bool:
    """Detect `// Section:` style headers in the input prompt. When
    present, dispatch emits matching headers on inserted sections;
    when absent (flat prose), inserts blend in as bare sentences."""
    return bool(_HEADER_LINE.search(prompt or ""))


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

from scripts.natlang_decompose_probe import decompose  # noqa: E402
from scripts.natlang_scan_probe import scan_prompt  # noqa: E402
from scripts.natlang_locate_infill_probe import (  # noqa: E402
    locate_infill, _apply_edit,
)
from scripts.natlang_dispatch import (  # noqa: E402
    dispatch, normalize_concept, format_section_header,
    CANONICAL_ORDER, SENTENCE_CONCEPTS,
    OUTFIT_SUBSLOTS, CHARACTER_SUBSLOTS,
)
try:
    from scripts.natlang_scene_composer import compose_scene_paragraph  # noqa: E402
except Exception:
    compose_scene_paragraph = None  # type: ignore
try:
    from scripts.natlang_planner_probe import plan as plan_multichar  # noqa: E402
    from scripts.natlang_compose_from_plan import compose_from_plan  # noqa: E402
except Exception:
    plan_multichar = None  # type: ignore
    compose_from_plan = None  # type: ignore


def _expand_search_to_header(prompt: str, search: str,
                             concept: str) -> tuple[str, str | None]:
    """If `prompt` has a `// <Concept>:[...]\\n` header line directly
    above the `search` body, return (expanded_search, old_header_line)
    so the apply step can rewrite the header AND body atomically.
    Otherwise return (search, None) — no header expansion possible.

    Matches case-insensitively on the concept name. Also matches the
    legacy `// Setting:` header for scene concepts.
    """
    if not search or search not in prompt:
        return search, None
    idx = prompt.index(search)
    if idx == 0:
        return search, None
    # The character just before `idx` should be a newline; the line
    # ending at that newline is the candidate header.
    pre = prompt[:idx]
    if not pre.endswith("\n"):
        return search, None
    header_end = idx - 1  # position of the trailing newline
    line_start = pre.rfind("\n", 0, header_end) + 1  # 0 if no prior newline
    header_line = prompt[line_start:header_end]
    concept_aliases = [concept]
    if concept == "scene":
        concept_aliases.append("setting")
    for name in concept_aliases:
        pat = re.compile(rf"^//\s*{re.escape(name)}\s*:", re.IGNORECASE)
        if pat.match(header_line):
            expanded = prompt[line_start:idx + len(search)]
            return expanded, header_line
    return search, None

try:
    from scripts.natlang_resolve_probe import resolve_intent
except Exception:
    resolve_intent = None

try:
    from scripts.natlang_harmonize_probe import harmonize, apply_corrections
except Exception:
    harmonize = None
    apply_corrections = None

try:
    from scripts.natlang_vibe_probe import vibe, _looks_terse
except Exception:
    vibe = None
    _looks_terse = None


_SENTENCE_SHAPED = {"pose", "scene", "expression", "style"}


# Default negative prompt block appended in build mode. Item 4 will
# replace this with style-template-driven negatives once the style
# resolve step lands.
_DEFAULT_NEGATIVE_BLOCK = (
    "Negative Prompt:\n"
    "blurry, low quality, jpeg artifacts, watermark, text, logo, "
    "bad anatomy, deformed, distorted, flat colors, flat lighting, "
    "pure cartoon, chibi, super deformed, sketchy, unfinished"
)


def _load_default_style_section(
    model_hash: str | None,
) -> tuple[str, list[str]] | None:
    """Look up the active model's default style template (configured
    via the model-settings UI as `default_prompt_id`). Returns
    (section_text, neg_tokens) when the model has one and its template
    has positive content. Otherwise None — caller falls through to
    the canned default negative block.

    The returned section_text is ready to splice in after the last
    structured section: a `// Style: <Name>` header + the template's
    positive tokens comma-joined as body. Negative tokens are
    surfaced separately so the caller can drive the Negative Prompt
    block from them instead of the canned default.
    """
    if not (model_hash or "").strip():
        return None
    try:
        from core import ai_api as _api
        from core import prompts as _prompts
    except Exception:
        return None
    try:
        grounding = _api._build_grounding(model_hash)
    except Exception:
        return None
    default_id = (grounding.get("default_prompt_id") or "").strip()
    if not default_id:
        return None
    try:
        tmpl = next(
            (p for p in _prompts.list_prompts()
             if (p.get("id") or "").strip() == default_id),
            None,
        )
    except Exception:
        return None
    if not tmpl:
        return None
    try:
        pos_tokens, neg_tokens = _api._parse_style_template_text(
            tmpl.get("text") or ""
        )
    except Exception:
        return None
    if not pos_tokens:
        return None
    name = (tmpl.get("name") or "").strip()
    header = f"// Style: {name}" if name else "// Style"
    section_text = header + "\n" + ", ".join(pos_tokens)
    return section_text, list(neg_tokens or [])


_SECTION_HEADER_RE = re.compile(r"^//\s*(\w+)\s*:", re.IGNORECASE)


def _scan_sections_from_prompt(prompt: str) -> dict:
    """Parse a sectioned prompt into {concept: body} pairs. Used in
    build mode to keep `scan` in sync after each intent — sub-slot
    intents modify the parent section's body without updating scan,
    which leaves later intents with stale insert_after anchors."""
    out: dict = {}
    cur_name: str | None = None
    cur_body: list[str] = []
    for line in (prompt or "").splitlines():
        m = _SECTION_HEADER_RE.match(line)
        if m:
            if cur_name is not None:
                out[cur_name] = "\n".join(cur_body).strip()
            cur_name = m.group(1).lower()
            cur_body = []
            continue
        if line.strip() == "Negative Prompt:":
            if cur_name is not None:
                out[cur_name] = "\n".join(cur_body).strip()
            cur_name = "negative"
            cur_body = []
            continue
        if cur_name is not None and line.strip():
            cur_body.append(line)
    if cur_name is not None:
        out[cur_name] = "\n".join(cur_body).strip()
    return out


def _scan_chars_in_text(text: str) -> list[dict]:
    """Find known character canonical entries that appear as substrings
    of `text`. Returns [{tag, display, series, i_start, i_end,
    matched_text}, ...] ordered by position. De-duplicated by tag.

    Used to split a multi-character subject intent like
    'cammy white fighting tifa lockhart' into its constituent
    characters so per-character resolve + outfit injection can run.

    Search strategy: walk word spans of length 3, 2, 1 (longest first
    so 'tifa lockhart' wins over bare 'tifa'). Skip positions already
    claimed by an earlier (longer) match.
    """
    if not (text or "").strip():
        return []
    try:
        from scripts.natlang_resolve_probe import (
            match_character, _open_db as _rdb,
        )
    except Exception:
        return []
    # Stopwords that must never anchor a 1-word character match — the
    # natlang_resolve_probe.match_character substring fallback otherwise
    # picks "of" → Warrior Of Light, "a" → A (Xenoblade), "white" →
    # Lily White, etc. Filter only applies to single-word spans;
    # multi-word spans like "of light" or "cammy white" still pass.
    _SCAN_STOPWORDS = {
        # Articles / prepositions / conjunctions
        "a", "an", "the", "and", "or", "but", "of", "in", "on", "at",
        "to", "for", "with", "by", "as", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "from", "into", "over", "under", "near", "next", "vs",
        "versus", "between", "among",
        # Pronouns
        "her", "his", "its", "their", "she", "he", "they", "we", "i",
        "you", "it", "this", "that", "these", "those",
        # Common action verbs (would otherwise match named "Running"/
        # "Sitting"/etc. characters)
        "fighting", "standing", "sitting", "walking", "running",
        "holding", "wearing", "kissing", "dancing", "hugging",
        # Color words (would match Lily White, Cure White, Red Mage,
        # Blue Heart, Pink Diamond, etc. when the user typed a color
        # as an outfit/item descriptor)
        "white", "black", "red", "blue", "green", "yellow", "pink",
        "purple", "orange", "brown", "gray", "grey", "gold", "silver",
        "violet", "cyan", "magenta", "indigo", "teal", "tan", "beige",
        "navy", "crimson", "scarlet", "lavender", "turquoise", "rose",
        # Common clothing nouns (would match e.g. "Bikini Killer",
        # "Hoodie", etc.)
        "bikini", "bikinis", "dress", "skirt", "shirt", "pants",
        "shoes", "boots", "gloves", "hat", "cap", "outfit", "clothes",
        "uniform", "kimono", "robe", "armor", "sundress",
    }
    # Normalize each word for matching: strip leading/trailing
    # punctuation (`"`, `'`, `,`, `.`, `!`, `?`, `:`, `;`) and trailing
    # possessive `'s`. Without this, a leading quote in the user_text
    # (`"chun-li ...`) tokenizes as `"chun-li` and never matches.
    # Apostrophe-s in `chun-li's` also breaks matching.
    raw_words = text.split()
    words = []
    for w in raw_words:
        # Strip outer punctuation chars
        stripped = w.strip("\"'`,.!?:;()[]{}")
        # Trim possessive 's only when preceded by a non-trivial stem
        if stripped.lower().endswith("'s") and len(stripped) > 3:
            stripped = stripped[:-2]
        elif stripped.lower().endswith("’s") and len(stripped) > 3:
            # Curly apostrophe (Unicode)
            stripped = stripped[:-2]
        words.append(stripped or w)

    found: list[dict] = []
    seen_tags: set[str] = set()
    c = _rdb()
    try:
        for span_len in (3, 2, 1):
            i = 0
            while i + span_len <= len(words):
                if any(f["i_start"] <= i < f["i_end"] for f in found):
                    i += 1
                    continue
                candidate = " ".join(words[i:i + span_len])
                # Skip single-word stopwords — they're never character
                # names and the substring matcher gives false positives.
                if (span_len == 1
                        and candidate.lower() in _SCAN_STOPWORDS):
                    i += 1
                    continue
                try:
                    match = match_character(c, candidate)
                except Exception:
                    match = None
                # Word-boundary validation for 1-word matches —
                # match_character's substring-on-display fallback
                # otherwise picks "bare" → Suzuran (Yukibare),
                # "of" → Warrior Of Light, etc. when the candidate is
                # buried INSIDE another word. Require the candidate to
                # appear as a whole word in display OR tag.
                if match and span_len == 1:
                    cand_lc = candidate.lower()
                    disp_lc = (match.get("display") or "").lower()
                    tag_lc = (match.get("tag") or "").lower()
                    pat = r"(?:^|\b|_)" + re.escape(cand_lc) + r"(?:\b|_|$)"
                    if not (re.search(pat, disp_lc) or re.search(pat, tag_lc)):
                        match = None
                if match and match["tag"] not in seen_tags:
                    seen_tags.add(match["tag"])
                    found.append({
                        **match,
                        "i_start": i,
                        "i_end": i + span_len,
                        "matched_text": candidate,
                    })
                i += 1
    finally:
        c.close()
    return sorted(found, key=lambda f: f["i_start"])


async def _router_scan_chars_in_text(text: str) -> list[dict]:
    """LLM-first character detection. One router call extracts proper-
    noun character-name phrases; each phrase is then resolved through
    `match_character` against the characters table.

    Returns the same list-of-dicts shape as `_scan_chars_in_text`
    ({tag, display, series, i_start, i_end, matched_text}) so the
    planner trigger and downstream code work unchanged.

    Falls back to the regex/substring scan when the router LLM call
    fails or its output is unparseable.
    """
    if not (text or "").strip():
        return []
    try:
        from scripts.natlang_char_router_probe import extract_character_names
        from scripts.natlang_resolve_probe import (
            match_character, _open_db as _rdb,
        )
    except Exception:
        return _scan_chars_in_text(text)

    try:
        phrases, _raw = await extract_character_names(text)
    except Exception:
        phrases = None
    if phrases is None:
        # Router unavailable / unparseable — fall back to regex scan
        logger.warning("rails-router: LLM router failed, regex fallback")
        return _scan_chars_in_text(text)

    if not phrases:
        return []

    found: list[dict] = []
    seen_tags: set[str] = set()
    c = _rdb()
    try:
        for idx, phrase in enumerate(phrases):
            try:
                match = match_character(c, phrase)
            except Exception:
                match = None
            if not match or match["tag"] in seen_tags:
                continue
            seen_tags.add(match["tag"])
            found.append({
                **match,
                "i_start": idx,
                "i_end": idx + 1,
                "matched_text": phrase,
            })
    finally:
        c.close()
    return found


def _maybe_split_multi_char_subject(
    intents: list[dict], user_request: str,
) -> list[dict]:
    """Build-mode helper: detect a subject intent whose text names 2+
    characters (e.g. 'cammy white fighting tifa lockhart') and split
    it into per-character subject intents. The connecting verb between
    the named characters (e.g. 'fighting') is captured and prepended to
    the scene intent's text, or — if no scene intent exists — surfaced
    as a new scene intent.

    Falls through (returns intents unchanged) when:
      - no subject intent
      - subject text has fewer than 2 detectable characters
      - subject text isn't multi-char-shaped (single name only)

    Mirrors tag-rails' multi-char build composition: each character
    becomes its own subject intent so the per-intent loop can resolve
    each bio + auto-inject each default outfit + emit a proper
    // Character section per character.
    """
    # Find the subject intent (concept normalizes to 'character')
    subject_idx = next(
        (i for i, it in enumerate(intents)
         if normalize_concept(it.get("concept", "")) == "character"),
        None,
    )
    if subject_idx is None:
        return intents

    subject = intents[subject_idx]
    subject_text = (subject.get("text") or "").strip()
    if not subject_text:
        return intents

    chars = _scan_chars_in_text(subject_text)
    if len(chars) < 2:
        return intents

    # Capture two kinds of leftover text from the subject text:
    #   connector: words BETWEEN matched character spans (e.g.
    #              'fighting' in 'cammy white fighting tifa lockhart').
    #              These are interaction verbs/preps → emit as pose.
    #   trailing:  words AFTER the last matched character span (e.g.
    #              'on top of a roof' in 'cammy white fighting tifa
    #              lockhart on top of a roof'). Often setting/scene
    #              info that decompose folded into the subject intent
    #              instead of emitting its own scene intent. Emit as
    #              scene (unless a scene intent already exists).
    words = subject_text.split()
    connector_spans: list[str] = []
    for prev, nxt in zip(chars, chars[1:]):
        gap = " ".join(words[prev["i_end"]:nxt["i_start"]]).strip()
        if gap:
            connector_spans.append(gap)
    connector = " ".join(connector_spans).strip()
    trailing = " ".join(words[chars[-1]["i_end"]:]).strip()

    # Heuristic: scene keywords ("on", "at", "in", "near", "above",
    # "outside") in the trailing text signal a setting/location. Otherwise
    # treat trailing as additional pose detail (e.g. "cammy fighting
    # tifa with a sword" — "with a sword" is pose/prop, not scene).
    trailing_looks_like_scene = bool(re.match(
        r"^(on|at|in|near|above|below|under|outside|inside|by|next to|"
        r"across from|behind|in front of|atop|aboard)\b",
        trailing.lower(),
    ))

    # Build replacement intents: one subject per matched char.
    # First subject inherits the original op (typically `replace` in
    # build mode = insert_at_end on empty prompt). Subsequent
    # subjects get op=add so dispatch routes them to insert_after
    # instead of replacing the first character section.
    new_subjects: list[dict] = []
    for idx, ch in enumerate(chars):
        new_op = subject.get("op") if idx == 0 else "add"
        new_subjects.append({
            **subject, "op": new_op, "text": ch["matched_text"],
        })

    # If the user has a generic outfit intent that applies to all
    # subjects (e.g. "both wearing bikinis"), duplicate the outfit
    # intent per character — without this, only the first char gets
    # the outfit and the second falls back to KB default. Same for
    # pose ("both standing"), expression ("both smiling").
    _SHARED_TRIGGERS = ("both", "all", "each", "everyone", "they")
    user_text_lower = (user_request or "").lower()
    apply_to_all = any(t in user_text_lower for t in _SHARED_TRIGGERS)

    # Connector → pose intent. Going into scene would pollute scene
    # KB matching ("fighting" gets picked up by match_scene as
    # "Fighting Arena" and overrides the user's actual setting).
    # Trailing → scene intent when it looks scene-shaped, else pose.
    merged_intents: list[dict] = []
    pose_seen = False
    scene_seen = False
    char_displays = [ch.get("display") or ch.get("matched_text") for ch in chars]
    for idx, it in enumerate(intents):
        if idx == subject_idx:
            merged_intents.extend(new_subjects)
            continue
        nc = normalize_concept(it.get("concept", ""))
        if nc == "pose":
            pose_seen = True
        if nc == "scene":
            scene_seen = True
        # Per-character duplication for outfit/pose/expression when
        # the user used a shared trigger ("both", "all", "each").
        # Scene/style/quality are scene-global so they only fire once.
        # Stamp each duplicate with the char display so per-intent
        # resolve in run_turn_v2 scopes the lookup to the right bio.
        if apply_to_all and nc in ("outfit", "pose", "expression"):
            for i_ch, disp in enumerate(char_displays):
                dup_op = it.get("op") if i_ch == 0 else "add"
                merged_intents.append({
                    **it, "op": dup_op,
                    "text": f"{disp} {it.get('text') or ''}".strip(),
                })
            continue
        merged_intents.append(it)

    extra_pose_text = connector
    extra_scene_text = ""
    if trailing:
        if trailing_looks_like_scene:
            extra_scene_text = trailing
        else:
            extra_pose_text = (extra_pose_text + " " + trailing).strip()

    # Pure-conjunction connectors ('and', 'with', 'vs') describe
    # grouping, not action. Don't emit them as a pose intent — that
    # leaks "// Pose: And." into the structured output and confuses
    # both the composer and the image model.
    _CONNECTOR_STOPWORDS = {
        "and", "with", "vs", "versus", "&", ",", "plus",
        "alongside", "beside", "next", "near",
    }
    if extra_pose_text:
        pose_words = [
            w for w in re.findall(r"[A-Za-z]+", extra_pose_text.lower())
            if w not in _CONNECTOR_STOPWORDS
        ]
        if not pose_words:
            extra_pose_text = ""

    if extra_pose_text and not pose_seen:
        # Mark connector-pose intents as `_skip_kb_resolve` so the
        # resolve step doesn't pick up an unrelated pose row (e.g.
        # the verb 'fighting' matching some character's named
        # "Fighting Stance" pose, whose static prose body ("in a
        # fighting stance, martial arts pose") then biases the
        # scene composer toward posed mannequins. The connector verb
        # is an interaction descriptor — let the composer expand it
        # as active motion between the named subjects.
        merged_intents.append({
            "concept": "pose",
            "op": "replace",
            "text": extra_pose_text,
            "_skip_kb_resolve": True,
        })
    if extra_scene_text and not scene_seen:
        merged_intents.append({
            "concept": "scene",
            "op": "replace",
            "text": extra_scene_text,
        })

    logger.info(
        "rails: multi-char subject split: %d chars matched (%s); "
        "connector=%r trailing=%r",
        len(chars),
        ", ".join(ch["matched_text"] for ch in chars),
        connector, trailing,
    )
    return merged_intents


def _maybe_inject_default_outfit(intents: list[dict]) -> list[dict]:
    """Build mode helper: for each character intent the user named, if
    no outfit was explicitly named, look up that character's default
    outfit row in the KB and inject a synthetic outfit intent paired
    with it. Mirrors TagBuilder v2's 'character match implies default
    outfit unless overridden' behavior.

    Handles multi-character build: when the user said
    'cammy white fighting tifa lockhart', after multi-char split we
    have TWO character intents. Each gets its own default outfit
    injected if a) it has a known default and b) the user didn't
    already name an outfit. The outfit text carries the character
    name as a hint (`Killer Bee from Cammy White`) so per-intent
    resolve in run_turn_v2 scopes to the right bio.
    """
    # Collect character intents
    char_texts: list[str] = []
    for it in intents:
        if normalize_concept(it.get("concept", "")) == "character":
            t = (it.get("text") or "").strip()
            if t:
                char_texts.append(t)
    if not char_texts:
        return intents

    # If the user named ANY outfit, defer to that (per-character outfit
    # naming is rare and would be a more advanced parse). A single
    # named outfit + multi-char will currently apply to whoever
    # resolve finds first; multi-outfit-per-char is a future
    # enhancement.
    has_outfit = any(
        normalize_concept(it.get("concept", "")) == "outfit"
        for it in intents
    )
    if has_outfit:
        return intents

    try:
        from scripts.natlang_resolve_probe import (
            match_character, _extract_subject_name, _open_db as _rdb,
        )
    except Exception as e:
        logger.warning("rails: default-outfit injection import failed: %s", e)
        return intents

    new_outfits: list[dict] = []
    c = _rdb()
    try:
        for char_text in char_texts:
            head = _extract_subject_name(char_text)
            if not head:
                continue
            ch = match_character(c, head)
            if not ch:
                continue
            row = c.execute(
                "SELECT outfit_name FROM outfits "
                "WHERE character_tag = ? AND is_default = 1 "
                "ORDER BY sort_order, id LIMIT 1",
                (ch["tag"],),
            ).fetchone()
            if not row:
                continue
            outfit_name = (row["outfit_name"] or "").strip()
            if not outfit_name:
                continue
            # Tag the outfit intent with the character display so per-
            # intent resolve in run_turn_v2 can scope its outfit lookup
            # to the right character — critical in multi-char build.
            # First injected outfit uses `replace` (which dispatches to
            # insert_after the matching character on first build).
            # Subsequent outfits get `add` so they don't overwrite the
            # first outfit section.
            # Format as "{char_display} {outfit_name}" so the foreign-
            # character outfit matcher in resolve_intent picks up the
            # character at the start of the text (its scan iterates
            # contiguous token windows; the first character match
            # wins). "Outfit from Character" form gets eaten by
            # false-positive substring matches (e.g. "delta" matching
            # a different character before "cammy white" is reached).
            char_display = ch.get("display") or head
            new_outfits.append({
                "concept": "outfit",
                "op": "replace" if not new_outfits else "add",
                "text": f"{char_display} {outfit_name}",
            })
            logger.info(
                "rails: build-mode default outfit injected: %r for %r",
                outfit_name, char_text,
            )
    finally:
        c.close()

    return list(intents) + new_outfits


def _canonical_sort_key(intent: dict) -> tuple[int, int]:
    """Order intents by canonical position so character lands before
    outfit lands before pose, etc. Sub-concepts sort with their parent
    section. Unknown concepts sort to the end (stable)."""
    raw = intent.get("concept", "")
    c = normalize_concept(raw)
    if c in SENTENCE_CONCEPTS:
        try:
            return (CANONICAL_ORDER.index(c), 0)
        except ValueError:
            pass
    if raw in OUTFIT_SUBSLOTS or c in OUTFIT_SUBSLOTS:
        return (CANONICAL_ORDER.index("outfit"), 1)
    if raw in CHARACTER_SUBSLOTS or c in CHARACTER_SUBSLOTS:
        return (CANONICAL_ORDER.index("character"), 1)
    return (len(CANONICAL_ORDER), 0)


async def _run_planner_build(
    user_request: str,
    chars_in_request: list[dict],
    model_hash: str | None,
    trace: dict,
) -> dict:
    """Build-mode planner path. Used when the request names 2+ known
    characters. One LLM call coordinates per-character outfit + pose
    assignment, then deterministic compose-from-plan + cinematic
    polish.
    """
    import os as _os
    # Find tag-builder.db from package layout.
    db_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "data", "tag-builder", "tag-builder.db",
    )

    profiles = [
        {"tag": ch["tag"], "display": ch.get("display") or ch["tag"]}
        for ch in chars_in_request
    ]
    logger.info(
        "rails-plan: multi-char build path, profiles=%s",
        [p["display"] for p in profiles],
    )

    plan_dict, plan_raw = await plan_multichar(user_request, profiles)
    if not plan_dict:
        logger.warning(
            "rails-plan: planner returned no parsable JSON — "
            "falling back to existing per-intent path. raw=%r",
            (plan_raw or "")[:200],
        )
        # Caller will run the normal path; signal by raising a sentinel?
        # Simpler: emit minimal trace and return prompt as-is. The agent
        # layer will see empty final_prompt and may retry.
        trace["final_prompt"] = ""
        trace["plan"] = None
        return trace

    # Sanity check: planner cast must be non-empty, every cast tag
    # must be one of the profiles (no hallucinated characters), and
    # every cast entry must have a matching per_character entry.
    # We DO NOT enforce "every profile must be in cast" — the scan
    # that produced profiles has false-positive substring matches
    # (e.g. random characters from common words); the planner
    # correctly filters those out and the cast may be a STRICT
    # SUBSET of profiles. That's fine.
    plan_cast_tags = {(c.get("tag") or "").lower()
                       for c in plan_dict.get("cast") or []}
    plan_per_char_tags = {(pc.get("tag") or "").lower()
                          for pc in plan_dict.get("per_character") or []}
    profile_tags = {p["tag"].lower() for p in profiles}
    if not plan_cast_tags:
        logger.warning("rails-plan: planner returned empty cast — fallback")
        trace["final_prompt"] = ""
        trace["plan"] = plan_dict
        return trace
    hallucinated = plan_cast_tags - profile_tags
    cast_without_pc = plan_cast_tags - plan_per_char_tags
    if hallucinated or cast_without_pc:
        logger.warning(
            "rails-plan: planner output bad: hallucinated_tags=%s "
            "cast_without_per_char=%s — fallback",
            sorted(hallucinated), sorted(cast_without_pc),
        )
        trace["final_prompt"] = ""
        trace["plan"] = plan_dict
        return trace

    trace["plan"] = plan_dict
    logger.info("rails-plan: plan cast=%s scene=%r interaction=%r",
                [c.get("display") for c in plan_dict.get("cast") or []],
                plan_dict.get("scene_text"),
                plan_dict.get("interaction"))

    # Load bios for the cast (display + series + base_natlang). The
    # tag scanner already returned dicts with tag/display/series; load
    # base_natlang for each from the DB.
    bios = []
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for cm in plan_dict.get("cast") or []:
            tag = (cm.get("tag") or "").lower()
            row = conn.execute(
                "SELECT tag, display, series, base_natlang "
                "FROM characters WHERE tag = ?", (tag,),
            ).fetchone()
            bios.append(dict(row) if row else {"tag": tag,
                                                "display": cm.get("display") or tag})
    finally:
        conn.close()

    structured = compose_from_plan(
        plan_dict, bios, db_path,
        default_negative_block=_DEFAULT_NEGATIVE_BLOCK,
    )
    logger.info("rails-plan: compose_from_plan %d chars", len(structured))

    # Final polish — same composer used by the existing build path.
    # Same sanity guard: if it drops a character display, fall back to
    # the structured form.
    positive = structured
    negative = ""
    if "\n\nNegative Prompt:" in structured:
        positive, _, negative = structured.partition("\n\nNegative Prompt:")
        negative = "\n\nNegative Prompt:" + negative
    elif structured.startswith("Negative Prompt:"):
        positive, negative = "", structured

    polished = ""
    cast_displays = [cm.get("display", "") for cm in plan_dict.get("cast") or []]

    async def _try_polish(payload: str) -> tuple[str, list[str]]:
        if compose_scene_paragraph is None or not payload.strip():
            return "", []
        try:
            out, _ = await compose_scene_paragraph(payload)
        except Exception:
            logger.exception("rails-plan: scene composer raised")
            return "", []
        dropped = [d for d in cast_displays
                   if d and d.lower() not in out.lower()]
        return out, dropped

    polished, dropped = await _try_polish(positive)
    if dropped and polished:
        # Retry once with explicit char-presence nudge prepended to
        # the structured input. The composer system prompt already
        # says "CHARACTER COUNT IS LOAD-BEARING" but LLMs still drop
        # ~10-20% of multi-char outputs. A direct in-payload reminder
        # often nudges the second try across.
        cast_list_str = ", ".join(cast_displays)
        nudge = (
            f"# CRITICAL: your output paragraph MUST include every "
            f"character by full name: {cast_list_str}. Dropping any "
            f"is the worst failure.\n\n"
        )
        logger.warning(
            "rails-plan: polish dropped %s on first try — retrying with nudge",
            dropped,
        )
        polished, dropped = await _try_polish(nudge + positive)

    if polished and not dropped:
        logger.info(
            "rails-plan: polished build output (%d -> %d chars)",
            len(positive), len(polished),
        )
        final = polished.rstrip() + negative
    elif polished:
        logger.warning(
            "rails-plan: polish still dropped %s after retry — keeping structured",
            dropped,
        )
        final = positive.rstrip() + negative
    else:
        logger.warning("rails-plan: polish returned empty — keeping structured")
        final = positive.rstrip() + negative

    trace["final_prompt"] = final
    trace["scan"] = None
    logger.info("rails-plan: turn done | final_chars=%d preview=%r",
                len(final), _preview(final, 200))
    return trace


async def run_turn_v2(prompt: str, user_request: str,
                      model_hash: str | None = None,
                      pre_decomposed: dict | None = None,
                      bios: list[dict] | None = None) -> dict:
    """End-to-end rails-v2 turn. Returns full trace.

    `model_hash` scopes style template lookup to the active checkpoint
    so model-specific templates filter correctly (e.g. z-image-only
    templates don't get masked by global aliases like 'anime style'
    pointing at illustrious-xl-anime).

    `bios` is the chat agent's preflight character-match list. Used
    in build mode to synthesize a subject intent when the user
    referred to a character by pronoun ('let's do her in a dojo')
    and decompose couldn't extract a name. Without this, the rails
    output ships a // Pose + // Scene but no // Character section.
    """
    is_build_mode = not (prompt or "").strip()
    # Build mode always emits sectioned output (fresh prompt = canonical
    # `// Section:` blocks). Edit mode follows the input's format.
    emit_headers = True if is_build_mode else _prompt_has_headers(prompt)
    logger.info(
        "rails: turn start | build_mode=%s emit_headers=%s "
        "model_hash=%s prompt_chars=%d user_request=%r",
        is_build_mode, emit_headers, model_hash or "(none)",
        len(prompt or ""), (user_request or "")[:200],
    )
    trace = {
        "user_request": user_request,
        "decompose": None,
        "scan": None,
        "intents": [],
        "final_prompt": prompt,
        "emit_headers": emit_headers,
        "build_mode": is_build_mode,
    }

    # MULTI-CHARACTER BUILD: when the request names 2+ known
    # characters, route through the planning turn instead of the
    # decompose → per-intent loop. The planner coordinates per-char
    # outfit/pose assignment (handles "X in Y's outfit", "both in
    # bikinis", "X in blue and Y in red", interaction verbs) which the
    # per-intent loop can't do because each intent is solved
    # independently.
    if (is_build_mode and plan_multichar is not None
            and compose_from_plan is not None):
        chars_in_request = await _router_scan_chars_in_text(user_request)
        if len(chars_in_request) >= 2:
            planner_trace = await _run_planner_build(
                user_request, chars_in_request, model_hash, trace,
            )
            # If planner returned a real final prompt, use it. If it
            # bailed (empty final_prompt = planner JSON parse failed),
            # fall through to the per-intent loop as a safety net.
            if (planner_trace.get("final_prompt") or "").strip():
                return planner_trace
            logger.warning(
                "rails-plan: planner path returned empty — "
                "falling through to per-intent loop"
            )

    # Build mode starts from empty prose so scan would have nothing to
    # classify — skip the LLM call and synthesize an empty scan dict.
    # Edit mode runs decompose+scan in parallel.
    # `pre_decomposed`: caller supplies intents directly (hybrid uses
    # this to dedupe multi-intent same-concept turns before rails sees
    # them). When provided, skip the decompose LLM call.
    if is_build_mode:
        if pre_decomposed is not None:
            decomposed = pre_decomposed
        else:
            decomposed, _ = await decompose(user_request)
        scan = {c: None for c in (
            "character", "outfit", "pose", "expression",
            "scene", "style", "quality", "negative",
        )}
    else:
        if pre_decomposed is not None:
            decomposed = pre_decomposed
            scan, _ = await scan_prompt(prompt)
        else:
            (decomposed, _), (scan, _) = await asyncio.gather(
                decompose(user_request),
                scan_prompt(prompt),
            )
    trace["decompose"] = decomposed
    trace["scan"] = scan
    logger.info("rails: decompose intents=%d", len(decomposed.get("intents") or []))
    for i, it in enumerate(decomposed.get("intents") or []):
        logger.info(
            "rails:   intent[%d] concept=%s op=%s text=%r",
            i, it.get("concept"), it.get("op"), _preview(it.get("text"), 120),
        )
    if scan:
        present = [k for k, v in scan.items() if (v or "").strip()]
        absent = [k for k, v in scan.items() if not (v or "").strip()]
        logger.info("rails: scan present=%s absent=%s", present, absent)
        for k in present:
            logger.info("rails:   scan[%s]=%r", k, _preview(scan.get(k), 120))
    if not decomposed["intents"]:
        return trace

    # Build mode runs the intents in canonical order so the prompt
    # assembles from character → outfit → pose → ... left-to-right.
    # Edit mode preserves the user's stated intent order.
    intents = list(decomposed["intents"])
    if is_build_mode:
        # Bio-driven subject injection: when the chat agent matched a
        # character via preflight (e.g. user said "let's do her in a
        # dojo" referring to a character from earlier conversation) but
        # decompose only saw the pronoun and emitted pose/scene intents,
        # synthesize a subject intent for each matched bio. Without
        # this, the build ships a // Pose + // Scene but the // Character
        # section is missing — the agent knew who they meant, rails
        # didn't get the signal.
        has_subject_intent = any(
            normalize_concept(it.get("concept", "")) == "character"
            for it in intents
        )
        if not has_subject_intent and bios:
            for b in bios:
                display = (b.get("display") or b.get("tag") or "").strip()
                if not display:
                    continue
                intents.insert(0, {
                    "concept": "subject",
                    "op": "replace",
                    "text": display,
                })
            logger.info(
                "rails: bios subject-injection added %d subject intent(s) "
                "(decompose missed character; user likely used pronoun)",
                sum(1 for it in intents
                    if normalize_concept(it.get("concept", "")) == "character"),
            )

        # Multi-char subject split — turn `cammy white fighting tifa
        # lockhart` into two subject intents + merged scene action.
        # Runs BEFORE default-outfit injection so each split subject
        # gets its own outfit pairing.
        intents = _maybe_split_multi_char_subject(intents, user_request)
        intents = _maybe_inject_default_outfit(intents)
        intents.sort(key=_canonical_sort_key)

    # Track the character resolved so far. The first `subject` intent
    # sets it; subsequent outfit/pose/scene intents scope DB lookups
    # to that character so "killer bee outfit" finds Cammy's row
    # instead of guessing.
    current_character_tag: str | None = None
    # Negatives composed from the matched style template (Item 4). When
    # the style intent resolves to a real template, its neg_tokens
    # drive the Negative Prompt block instead of the canned default.
    style_neg_tokens: list[str] | None = None

    # In edit mode, derive the character from the existing prompt's
    # character sentence so outfit/pose intents inside an edit also
    # get the right scope.
    if not is_build_mode and scan and scan.get("character"):
        try:
            from scripts.natlang_resolve_probe import (
                match_character, _extract_subject_name, _open_db as _rdb,
            )
            head = _extract_subject_name(scan["character"])
            if head:
                _c = _rdb()
                try:
                    ch = match_character(_c, head)
                    if ch:
                        current_character_tag = ch["tag"]
                finally:
                    _c.close()
        except Exception:
            pass

    current = prompt
    for i, intent in enumerate(intents):
        concept = intent["concept"]
        op = intent.get("op") or "replace"
        text = intent["text"]
        logger.info(
            "rails: intent[%d/%d] start concept=%s op=%s text=%r",
            i + 1, len(intents), concept, op, _preview(text, 120),
        )

        # Resolve before dispatch if available. Pass scan so resolve
        # can derive implicit character context (e.g. "victory pose"
        # → scan['character'] tells resolve which character's pose to
        # look up). Also pass the rolling current_character_tag from
        # earlier intents in this turn so outfit/pose look-ups scope
        # to the right character even when scan is empty (build mode).
        # `_skip_kb_resolve` lets a producer (e.g. the multi-char
        # subject splitter) opt this intent out — the synthesized
        # connector-verb pose isn't a named signature pose, so KB
        # lookup would pull an unrelated static "stance" prose body.
        resolved_info = None
        skip_resolve = bool(intent.get("_skip_kb_resolve"))
        if resolve_intent is not None and not skip_resolve:
            r = resolve_intent(
                {"concept": concept, "op": op, "text": text},
                scan=scan,
                character_tag=current_character_tag,
                model_hash=model_hash,
            )
            logger.info(
                "rails:   resolve source=%s match=%s text=%r",
                r.get("resolved_source"),
                ((r.get("resolved_match") or {}).get("name")
                 or (r.get("resolved_match") or {}).get("tag")
                 or (r.get("resolved_match") or {}).get("outfit_name")
                 or (r.get("resolved_match") or {}).get("pose_name")),
                _preview(r.get("resolved_text"), 120),
            )
            if r.get("resolved_text"):
                resolved_info = r
                text = r["resolved_text"]
                # The subject intent's resolved character becomes the
                # scope for everything that follows in this turn.
                if (r.get("resolved_source") == "character"
                        and isinstance(r.get("resolved_match"), dict)):
                    current_character_tag = (
                        r["resolved_match"].get("tag")
                        or current_character_tag
                    )
                # Capture neg_tokens from the matched style template
                # so the build-mode Negative Prompt block uses them
                # at the end of this turn.
                if (r.get("resolved_source") == "style"
                        and isinstance(r.get("resolved_match"), dict)):
                    neg = r["resolved_match"].get("neg_tokens") or []
                    if neg:
                        style_neg_tokens = list(neg)

        # Vibe fallback — when resolve misses on a sentence-shaped
        # concept and the user's text is terse (a brief tag, not a
        # full sentence), polish it into proper prose with one
        # narrow LLM call. Skip when resolve already hit, when text
        # is already prose, or when concept isn't sentence-shaped.
        vibe_info = None
        if (resolved_info is None
                and vibe is not None
                and _looks_terse is not None
                and concept in _SENTENCE_SHAPED
                and op in ("replace", "add", "")
                and _looks_terse(text)):
            try:
                vibed, vibe_raw = await vibe(concept, text)
            except Exception as e:
                vibed, vibe_raw = "", f"error: {e}"
            if vibed:
                vibe_info = {"source": "vibe", "raw": vibe_raw}
                text = vibed
                logger.info(
                    "rails:   vibe polished concept=%s -> %r",
                    concept, _preview(vibed, 120),
                )

        intent_with_text = {**intent, "resolved_text": text}

        # Plumb resolve metadata so format_section_header can build a
        # `// Section: <Name> (<Series>)` line for newly-inserted sections.
        resolved_match = (resolved_info or {}).get("resolved_match")
        resolved_source = (resolved_info or {}).get("resolved_source")
        # For outfit/pose matches, splice in the character display so the
        # `from Character: <Display>` suffix renders.
        if (resolved_source in ("outfit", "pose")
                and resolved_match and scan
                and not resolved_match.get("character_display")):
            char_sentence = (scan.get("character") or "").strip()
            if char_sentence:
                head = char_sentence.split(",", 1)[0].strip()
                head = head.split(" from ", 1)[0].strip()
                if head:
                    resolved_match = {**resolved_match,
                                      "character_display": head}

        dis = dispatch(intent_with_text, scan,
                       resolved_match=resolved_match,
                       resolved_source=resolved_source,
                       emit_headers=emit_headers)
        trace_intent = {
            "concept": concept,
            "op": op,
            "text": text,
            "resolved_source": (resolved_info or {}).get("resolved_source"),
            # Surface the resolved match's display name so a post-pass
            # (e.g. ai_api's full-template style swap) can re-look up
            # the template without re-running the resolver.
            "resolved_match_name": (
                ((resolved_info or {}).get("resolved_match") or {}).get("name")
                or ((resolved_info or {}).get("resolved_match") or {}).get("tag")
                or ((resolved_info or {}).get("resolved_match") or {}).get("outfit_name")
                or ((resolved_info or {}).get("resolved_match") or {}).get("pose_name")
            ),
            "vibed": vibe_info is not None,
            "dispatch_kind": dis["kind"],
        }

        if dis["kind"] == "sentence":
            parsed = dis["parsed"]
            # On a REPLACE in sectioned mode, also rewrite the
            # `// Concept:` header line above the matched body so it
            # reflects the new resolve match (or drops to a bare header
            # when the new body is vibe/raw text without a KB row).
            normalized = normalize_concept(concept)
            if (emit_headers
                    and parsed.get("search")
                    and parsed.get("search") != "(not present)"
                    and normalized in SENTENCE_CONCEPTS):
                expanded_search, old_header = _expand_search_to_header(
                    current, parsed["search"], normalized,
                )
                if old_header is not None:
                    new_header = format_section_header(
                        normalized, resolved_match, resolved_source,
                    )
                    if not new_header:
                        new_header = f"// {normalized.capitalize()}:"
                    parsed = {
                        **parsed,
                        "search": expanded_search,
                        "replace": f"{new_header}\n{parsed['replace']}",
                    }
                    logger.info(
                        "rails:   header rewrite old=%r new=%r",
                        old_header, new_header,
                    )
            applied, method = _apply_edit(current, parsed, concept, op)
            logger.info(
                "rails:   dispatch=sentence method=%s search=%r insert_after=%r replace=%r",
                method,
                _preview(parsed.get("search"), 80),
                _preview(parsed.get("insert_after"), 80),
                _preview(parsed.get("replace"), 120),
            )
            if applied == current:
                logger.warning(
                    "rails:   apply produced NO CHANGE (method=%s) — likely failed substitution",
                    method,
                )
            trace_intent.update({
                "search": parsed["search"],
                "replace": parsed["replace"],
                "insert_after": parsed["insert_after"],
                "method": method,
                "before": current,
                "after": applied,
            })
            current = applied
            # In build mode, register the inserted concept body in scan
            # so the next intent's canonical_anchor finds this section.
            # Use the body without any `// Section:` header line.
            if is_build_mode:
                body = parsed["replace"]
                if body.lstrip().startswith("// "):
                    body = body.split("\n", 1)[1] if "\n" in body else ""
                normalized = normalize_concept(concept)
                if body.strip() and normalized in SENTENCE_CONCEPTS:
                    scan[normalized] = body.strip()
        else:
            # Sub-slot — scope locate-infill to the parent sentence.
            parent_sentence = dis.get("parent_sentence") or current
            li_parsed, _ = await locate_infill(
                parent_sentence, concept, text, op,
            )
            # Safety net for locate-infill returning empty REPLACE on
            # op=replace/add. Without this guard the apply step would
            # delete SEARCH and substitute nothing — boots vanish but
            # `barefoot` never lands. The intent's own text is the
            # canonical content for this op, so fall back to it.
            if op in ("replace", "add") and not (li_parsed.get("replace") or "").strip():
                if (text or "").strip():
                    logger.warning(
                        "rails:   locate-infill returned empty REPLACE on op=%s; "
                        "filling from intent.text=%r",
                        op, _preview(text, 80),
                    )
                    li_parsed = {**li_parsed, "replace": text.strip()}
            # The LLM produced SEARCH within the parent sentence; the
            # apply step still works on the full prompt because
            # parent_sentence is a substring of `current`.
            applied, method = _apply_edit(current, li_parsed, concept, op)
            logger.info(
                "rails:   dispatch=subslot parent=%s method=%s search=%r replace=%r",
                dis.get("parent"), method,
                _preview(li_parsed.get("search"), 80),
                _preview(li_parsed.get("replace"), 120),
            )
            if applied == current:
                logger.warning(
                    "rails:   subslot apply produced NO CHANGE (method=%s)",
                    method,
                )
            trace_intent.update({
                "parent": dis.get("parent"),
                "parent_sentence_preview": (parent_sentence or "")[:120],
                "search": li_parsed.get("search"),
                "replace": li_parsed.get("replace"),
                "insert_after": li_parsed.get("insert_after"),
                "method": method,
                "before": current,
                "after": applied,
            })
            current = applied

        # Build mode: keep scan in sync with the actual prompt so the
        # next intent's canonical_anchor lookup matches what's there.
        # Sub-slot intents modify the parent section without updating
        # scan; without this resync, later intents try to insert_after
        # the OLD section body and fail with "INSERT_AFTER not in prompt".
        if is_build_mode:
            fresh = _scan_sections_from_prompt(current)
            for k, v in fresh.items():
                if v:
                    scan[k] = v

        # Harmonize pass — only on pose intents that successfully
        # inserted/replaced content. The 9B may flag at most 2
        # gaze/direction contradictions in other sentences. Conservative
        # by design: items/identity stay regardless of view.
        if (concept == "pose"
                and harmonize is not None
                and applied != trace_intent.get("before")
                and (text or "").strip()):
            try:
                corrections, harmonize_raw = await harmonize(
                    current, text, concept,
                )
            except Exception as e:
                corrections, harmonize_raw = [], f"error: {e}"
            if corrections:
                logger.info(
                    "rails:   harmonize corrections=%d %s",
                    len(corrections), corrections,
                )
                harmonized, harmonize_methods = apply_corrections(
                    current, corrections, new_content=text,
                )
                trace_intent["harmonize"] = {
                    "corrections": corrections,
                    "methods": harmonize_methods,
                    "before_harmonize": current,
                    "after_harmonize": harmonized,
                }
                current = harmonized

        trace["intents"].append(trace_intent)

    # Build-mode style auto-seed: when the user did not name a style,
    # pull the active model's `default_prompt_id` and append it as a
    # `// Style: <Name>` section. The template's neg_tokens become the
    # Negative Prompt block source so the model's curated negatives
    # ship instead of the canned default. Matches tag_rails_v1's
    # _build_style_section path so SDXL/Z-Image/etc. all get their
    # configured base aesthetic + negatives without the user having
    # to type the style name every turn.
    if (is_build_mode and current.strip()
            and "// Style" not in current):
        seed = _load_default_style_section(model_hash)
        if seed:
            section_text, seed_neg = seed
            current = current.rstrip() + "\n\n" + section_text
            if not style_neg_tokens and seed_neg:
                style_neg_tokens = list(seed_neg)
            logger.info(
                "rails: style auto-seed from model default (neg_tokens=%d)",
                len(seed_neg),
            )

    # Build mode tacks on a Negative Prompt block. When the style
    # intent matched a real template, that template's neg_tokens
    # drive the block (matches legacy /ai/patch's _build_negative_*
    # path); otherwise fall back to a canned default.
    if is_build_mode and current.strip() and "Negative Prompt:" not in current:
        if style_neg_tokens:
            neg_block = "Negative Prompt:\n" + ", ".join(style_neg_tokens)
        else:
            neg_block = _DEFAULT_NEGATIVE_BLOCK
        current = current.rstrip() + "\n\n" + neg_block

    # Build-mode final polish: convert structured `// Section:` output
    # into one cinematic paragraph. Only runs for multi-character builds
    # (2+ // Character sections) — the planner branch handles those when
    # detected up front, but the per-intent loop is the safety net when
    # the planner bails. Single-character builds keep the structured
    # form because it's KB-rich (canonical outfit/pose/style headers
    # like "Killer Bee from Character: Cammy White") and that fidelity
    # is lost when the LLM polish rewrites everything as prose.
    char_section_count = sum(
        1 for line in current.splitlines()
        if line.strip().lower().startswith("// character:")
    )
    if (is_build_mode and current.strip()
            and compose_scene_paragraph is not None
            and char_section_count >= 2):
        # Split off the Negative Prompt block (the polish step is
        # paragraph-only; the neg block is preserved verbatim and
        # re-attached below).
        if "\n\nNegative Prompt:" in current:
            positive, _, negative = current.partition("\n\nNegative Prompt:")
        elif current.startswith("Negative Prompt:"):
            positive, negative = "", current[len("Negative Prompt:"):]
        else:
            positive, negative = current, ""
        # Extract identifying character names from the structured
        # input — used as a sanity check on the polished output. If
        # the polish drops a character (LLM nondeterminism), fall
        # back to the structured form rather than ship a broken
        # multi-char paragraph.
        char_names: list[str] = []
        for line in positive.splitlines():
            ll = line.strip().lower()
            if ll.startswith("// character:"):
                name = line.split(":", 1)[1].strip()
                # Strip "(Series)" suffix and outer whitespace
                name = re.sub(r"\s*\([^)]+\)\s*$", "", name).strip()
                if name:
                    char_names.append(name)
        try:
            paragraph, raw = await compose_scene_paragraph(positive)
        except Exception as e:
            logger.warning("rails: scene composer failed: %s", e)
            paragraph = ""
        # Sanity check: every character named in the structured form
        # must still appear in the polished paragraph. If any are
        # missing, the polish dropped content — retain the structured
        # form instead.
        dropped = [
            name for name in char_names
            if name and name.lower() not in paragraph.lower()
        ]
        if paragraph and not dropped:
            logger.info(
                "rails: scene composer polished build output "
                "(positive %d -> %d chars)",
                len(positive), len(paragraph),
            )
            if negative.strip():
                current = paragraph.rstrip() + "\n\nNegative Prompt:" + negative
            else:
                current = paragraph
        elif paragraph and dropped:
            logger.warning(
                "rails: scene composer dropped character(s) %s — "
                "keeping structured output",
                dropped,
            )
        else:
            logger.warning(
                "rails: scene composer returned empty — keeping structured output"
            )

    trace["final_prompt"] = current
    logger.info(
        "rails: turn done | intents=%d final_chars=%d preview=%r",
        len(trace.get("intents") or []), len(current), _preview(current, 200),
    )
    return trace
