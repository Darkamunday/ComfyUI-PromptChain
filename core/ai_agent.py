"""
AI chat agent loop — wraps the existing /promptchain/ai/patch endpoint as a
tool callable from a streaming chat agent.

Provider routing mirrors /ai/patch:
- cloud=claude  → Anthropic /v1/messages tool-use stream
- cloud=other   → OpenAI-compat /v1/chat/completions tool-use stream
- local         → OpenAI-compat /v1/chat/completions tool-use stream
                  (Qwen via Ollama works here — Ollama 0.4+ emits OpenAI-
                  shape tool_calls in streaming deltas)

When the model emits a tool_use for `apply_prompt_patch`, this module
dispatches to /promptchain/ai/patch over localhost — same endpoint the
legacy single-shot panel hits — so the patch flow's system prompt /
post-passes / model call remain byte-equivalent and zero-regression.

WS events on `promptchain_ai_stream`:
  - agent_text         : streamed text deltas from the chat agent
  - agent_tool_call    : tool_use detected (proposal_id, name, input)
  - agent_tool_result  : /ai/patch returned (proposal_id, applied)
  - agent_done         : end_turn or hop limit reached
  - error              : fatal (HTTP error, parse fail)

Internal block shape is Anthropic-style {type, text|id|name|input}; the
OpenAI streamer normalizes its tool_calls deltas to that shape so the loop
is provider-agnostic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

import aiohttp
from aiohttp import web
import server

from .api_utils import error_response, parse_json
from .shared import send_ws, HASH_RE
from . import ai_api  # reuse _load_config, _emit, _active_requests, _cleanup_request

logger = logging.getLogger("promptchain.ai.agent")
routes = server.PromptServer.instance.routes


# ── tool surface ──────────────────────────────────────────────────────

_AGENT_TOOLS = [
    {
        "name": "list_model_styles",
        "description": (
            "List or look up the style templates configured for the "
            "user's active model checkpoint. Returns the actual "
            "templates in their ComfyUI installation — NOT generic "
            "anime-style trivia from training data. Two use cases:\n"
            "  1. Browse: 'what styles does this model have', 'what "
            "anime styles are there', 'list cinematic ones' — pass "
            "`filter` with the category word or leave empty.\n"
            "  2. Identify a specific style by name: 'what's "
            "hyperrealistic?', 'tell me about the cinematic style', "
            "'does it have studio ghibli?' — pass `filter` with the "
            "style name. When the filter matches a single template the "
            "result includes its description text.\n"
            "The `filter` param is a case-insensitive substring match "
            "against BOTH the template's name AND its category, so "
            "'hyperrealistic' matches the Hyperrealistic template (it's "
            "in the Anime category, not a category itself). The tool "
            "returns pre-formatted markdown that you MUST echo verbatim. "
            "Do NOT call this to APPLY a style (that's "
            "apply_prompt_patch)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter. "
                        "Matches against BOTH name and category (e.g. "
                        "'anime' → all templates in Anime category; "
                        "'hyperrealistic' → the Hyperrealistic "
                        "template; 'cinematic' → matches both the "
                        "Cinematic category and any template named "
                        "Cinematic). Leave empty to list every template."
                    ),
                },
            },
        },
    },
    {
        "name": "apply_prompt_patch",
        "description": (
            "Modify the user's current Stable-Diffusion prompt. Pass a short, "
            "imperative natural-language `request` describing the change "
            "(examples: 'add red socks', 'remove blur', 'increase red_socks "
            "weight by 20%', 'switch outfit to delta red'). Returns the "
            "patched prompt sections. Call this whenever the user asks for "
            "any prompt change — adding, removing, reweighting, swapping a "
            "character/outfit/pose/style. Do not call it for pure questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "An imperative description of EVERY prompt change "
                        "the user asked for in this turn. Include ALL "
                        "intents — outfit, pose, scene, character, "
                        "expression, style. If the user said 'tifa in a "
                        "cowboy outfit, leaning against a bar, expand "
                        "the bar scene', the request must cover the "
                        "cowboy outfit, the leaning pose, AND the scene "
                        "expansion — never drop an intent because it's a "
                        "different section than the focal one.\n\n"
                        "RULES:\n"
                        "1. DO NOT INVENT REPLACE / SWAP / REMOVE "
                        "OPERATIONS THE USER DID NOT NAME. 'add X' is "
                        "'add X', not 'add X, replace Y with X'. The "
                        "patch flow preserves existing section tokens by "
                        "default. Only emit a removal when the user "
                        "explicitly said 'remove Y' or 'replace Y with X'.\n"
                        "2. For SHORT user requests (1-6 words like "
                        "'spreading toes', 'barefoot', 'red socks', "
                        "'lying on a beach'), the request should be "
                        "'add <user's exact phrase> to // <section>'. "
                        "No swap clause, no interpretation, no "
                        "elaboration. Trust the user's literal words.\n"
                        "3. Character names use Danbooru canonical "
                        "first_last form (`cammy_white`, `tifa_lockhart`, "
                        "`chun-li`, `yuuki_(sao)`). Don't use franchise-"
                        "only forms like `cammy_(street_fighter)`.\n"
                        "4. Pose / scene / setting / expression concepts: "
                        "write as natural English phrases. Don't "
                        "underscore-join multi-word concepts you invent "
                        "— the patch flow canonicalizes phrases against "
                        "the full Danbooru tag database."
                    ),
                },
                "character_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "REQUIRED whenever any character is in scope. "
                        "Canonical Danbooru first_last tags for every "
                        "character relevant to this turn — including "
                        "outfit-borrow sources. The downstream matcher "
                        "uses this to load each character's bio (curated "
                        "outfit, pose, anatomy slots). Without it, the "
                        "matcher has no bio loaded and outfit/pose "
                        "lookups by name silently fail.\n\n"
                        "Pass `[]` ONLY when no character is in scope "
                        "(generic scene, model question, conversational "
                        "turn).\n\n"
                        "Examples:\n"
                        "  'set up Cammy' → ['cammy_white']\n"
                        "  'cammy white sitting with legs up, "
                        "barefoot, hyperrealistic anime style' → "
                        "['cammy_white']  (character is Cammy)\n"
                        "  'cammy in pyra's outfit' → "
                        "['cammy_white', 'pyra_(xenoblade)']\n"
                        "  'cammy and tifa fighting' → "
                        "['cammy_white', 'tifa_lockhart']\n"
                        "  node_prompt has '// Character: cammy_white', "
                        "user says 'switch to killer bee outfit' → "
                        "['cammy_white']\n"
                        "  'add red socks' (no character mentioned, "
                        "no // Character: in node_prompt) → []\n\n"
                        "WORDS LIKE 'killer bee', 'delta red', 'cannon "
                        "spike' are OUTFIT/POSE names — they belong "
                        "in `request` text, NEVER in character_queries."
                    ),
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "populate_inline_wildcards",
        "description": (
            "Fill the CURRENT node with a SET of options as inline "
            "wildcards (the `::Label::body` switch/roll format), pulled "
            "from the local character database or the model's style "
            "templates. Use this whenever the user asks to add MANY items "
            "at once by category or attribute rather than one specific "
            "thing:\n"
            "  'add all anime styles', 'fill this with photorealistic "
            "styles' → source='styles', category=<the style category>.\n"
            "  'add every street fighter character', 'add the blonde "
            "street fighter girls as inline wildcards', 'add all "
            "characters from nier' → source='characters' with the "
            "matching filters.\n"
            "Entries are APPENDED non-destructively to whatever is "
            "already in the node, deduped by name. Do NOT use this to add "
            "ONE named character or style — that is apply_prompt_patch. "
            "Attribute filters map to database columns; leave a field "
            "empty to not filter on it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["characters", "styles"],
                    "description": (
                        "'characters' to pull from the character database "
                        "(filter by series + appearance attributes); "
                        "'styles' to pull the model's style templates "
                        "(filter by category)."
                    ),
                },
                "series": {
                    "type": "string",
                    "description": (
                        "source=characters only. Franchise/series filter, "
                        "natural form: 'street fighter', 'final fantasy', "
                        "'nier'. Substring match. Empty = all series."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "source=styles only. Style category: 'anime', "
                        "'photorealistic', '3d styles', 'drawing', "
                        "'watercolor', 'lighting', etc. Empty = all."
                    ),
                },
                "hair_color": {
                    "type": "string",
                    "description": (
                        "source=characters. e.g. 'blonde', 'black', "
                        "'red'. Empty = any."
                    ),
                },
                "eye_color": {"type": "string", "description": (
                    "source=characters. e.g. 'blue', 'green'. Empty = any.")},
                "hair_style": {"type": "string", "description": (
                    "source=characters. e.g. 'ponytail', 'twin_braids', "
                    "'bob'. Empty = any.")},
                "body_type": {"type": "string", "description": (
                    "source=characters. BUILD only — 'muscular', 'slim', "
                    "'curvy', 'petite'. NEVER put gender here. Empty = any.")},
                "gender": {"type": "string", "description": (
                    "source=characters. 'female' or 'male' — filters the "
                    "roster by character gender. Use this for 'the female "
                    "X characters' / 'male X'. Do NOT use body_type for "
                    "gender. Empty = both.")},
                "include_outfit": {"type": "boolean", "description": (
                    "source=characters. Set TRUE when the user asks for "
                    "characters WITH their (default) outfits — appends "
                    "each character's canonical default outfit to the "
                    "body. Default false = appearance only.")},
            },
            "required": ["source"],
        },
    },
    {
        "name": "generate_subjects",
        "description": (
            "GENERATE novel, original subjects from a theme — NOT named "
            "characters from the database. Use when the user asks to "
            "invent/create/make-up people by a vibe or description rather "
            "than pull existing ones:\n"
            "  'give me 10 inline wildcards for 10 mature women with "
            "random outfits', 'fill this with 6 cyberpunk mercenaries', "
            "'make me an elegant noblewoman'.\n"
            "Subjects are composed by sampling the appearance KB (hair, "
            "eyes, build, marks) + outfits, grounded and varied. "
            "Contrast: populate_inline_wildcards FETCHES named DB "
            "characters/styles; apply_prompt_patch edits ONE thing; "
            "generate_subjects INVENTS a set (or one) from a theme.\n"
            "Translate the user's theme into a recipe: hard constraints "
            "go in `fixed` (e.g. a 'mature woman' theme → "
            "fixed={'body_type':'mature_female'}); the count and outfit "
            "wish set `count` / `outfit_policy`. Default SFW — only touch "
            "explicit content when the user explicitly asks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "How many subjects to generate (default 1).",
                },
                "subject_kind": {
                    "type": "string",
                    "description": (
                        "The noun for the subject: 'woman', 'man', "
                        "'warrior', 'mercenary'. Default 'woman'."
                    ),
                },
                "fixed": {
                    "type": "object",
                    "description": (
                        "Hard appearance constraints every subject shares, "
                        "as {axis: value}. Axes: body_type, hair_color, "
                        "eye_color, hair_style, skin. e.g. a 'mature "
                        "woman' theme → {'body_type': 'mature_female'}; "
                        "'redheads' → {'hair_color': 'red'}. Leave empty "
                        "for a fully random theme."
                    ),
                },
                "outfit_policy": {
                    "type": "string",
                    "description": (
                        "How to dress each subject. RULE: if the user "
                        "names ANY outfit type/aesthetic/category, use "
                        "'themed:<that word>' — even if they also say "
                        "'random'. 'random' only means 'pick freely' when "
                        "NO outfit word is given; it does NOT cancel a "
                        "named theme. Examples:\n"
                        "  'random lingerie outfits' → 'themed:lingerie' "
                        "(random selection WITHIN lingerie)\n"
                        "  'in goth outfits' → 'themed:goth'\n"
                        "  'wearing swimsuits' → 'themed:swimsuit'\n"
                        "  'with random outfits' / 'with outfits' → "
                        "'random' (no specific type named)\n"
                        "  outfits not mentioned at all → 'random' "
                        "(the default)\n"
                        "  'no outfit' / 'just the face' → 'none'.\n"
                        "The theme word is passed straight through to a KB "
                        "lookup — use the user's word (lingerie, goth, "
                        "kimono, military, bikini, cyberpunk, ...)."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["inline_wildcards", "single"],
                    "description": (
                        "'inline_wildcards' = append N ::Label:: entries "
                        "to the node (use when the user says 'inline "
                        "wildcards' or asks for several). 'single' = one "
                        "subject as a plain prompt. Default "
                        "'inline_wildcards' when count>1, else 'single'."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_inline_wildcards",
        "description": (
            "Fill the CURRENT node with a list of THINGS (not people) from "
            "the knowledge base as inline wildcards: clothing, shoes, "
            "lingerie, swimwear, hats, dresses, poses, expressions / "
            "emotions, scenes, backgrounds, lighting, weather, actions, "
            "and so on. Use for 'a list of N <things>' / 'N types of "
            "<things>' / 'all the <things>' where the wildcard entries "
            "themselves are the things.\n"
            "Examples:\n"
            "  'list of 10 lingerie outfits' -> what='lingerie'\n"
            "  '10 types of shoes' -> what='shoes'\n"
            "  'add some angry expressions' -> what='angry expressions'\n"
            "  'fill this with rooftop backgrounds' -> what='backgrounds'\n"
            "  'a bunch of dynamic poses' -> what='poses'\n"
            "DECISION RULE — what are the wildcard entries?\n"
            "  THINGS (clothes/poses/expressions/scenes/actions) -> this "
            "tool.\n"
            "  Named characters (Cammy, every Street Fighter char) -> "
            "populate_inline_wildcards (source=characters).\n"
            "  Style templates -> populate_inline_wildcards (source="
            "styles).\n"
            "  PEOPLE invented from a theme (10 mature women) -> "
            "generate_subjects.\n"
            "Pass the category in the user's own words; a KB catalog "
            "resolves it deterministically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "description": (
                        "What to list. For a plain category, the user's "
                        "word: 'shoes', 'lingerie', 'backgrounds', "
                        "'weather'. For a SPECIFIC described item, pass "
                        "the user's FULL description verbatim — do NOT "
                        "compress it to a category. The KB is ranked-"
                        "searched against these words, so detail picks the "
                        "right item.\n"
                        "  'a pose sitting with legs up showing bare feet, "
                        "focus on feet' -> what='pose sitting legs up "
                        "showing bare feet focus on feet' (NOT 'foot "
                        "poses').\n"
                        "  'an angry open-mouth shouting expression' -> "
                        "what='angry open-mouth shouting expression'.\n"
                        "Keep the descriptive words; they are the signal."
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": (
                        "How many entries to produce. Set it to the number "
                        "the user asked for: '5 poses' -> 5, 'a pose that "
                        "is X' / 'one foot pose' -> 1 (the single best "
                        "match). Omit it when no number is implied ('some "
                        "lingerie', 'foot poses') to get a default set."
                    ),
                },
            },
            "required": ["what"],
        },
    },
]


_AGENT_MACRO_VERBS_BLOCK = (
    "Macro-verb shortcuts — when the user uses one of these verbs, "
    "translate it into a focused tool `request` (don't refuse or ask "
    "for clarification; pick a sensible default scope):\n"
    "- 'expand' / 'elaborate' / 'add detail' on a section: tool call "
    "with 'add 4-6 more canonical tags adjacent to the existing items "
    "in // <section>; preserve the existing tags verbatim'.\n"
    "- 'condense' / 'tighten' / 'simplify' / 'trim' a section: tool "
    "call with 'reduce // <section> to its 3-5 most essential canonical "
    "tags; drop redundant near-duplicates'.\n"
    "- 'reword' / 'rephrase' / 'say it differently' a section: tool "
    "call with 'replace the tokens in // <section> with semantically-"
    "equivalent canonical tags conveying the same intent'.\n"
    "- 'enrich' / 'add atmosphere' / 'polish': tool call with 'add "
    "3-5 atmospheric tokens to // Style (e.g. cinematic_lighting, "
    "volumetric_fog, rim_light, dramatic_shadows, moody_atmosphere) "
    "AND 3-5 quality tokens to // Quality (e.g. masterpiece, "
    "best_quality, highres, sharp_focus, detailed); preserve other "
    "sections'.\n"
    "- 'vary' / 'give me variations' / 'options for X' / 'show me "
    "<n> takes': DO NOT call the tool. Instead, reply in chat with a "
    "short numbered list (3 options unless the user named a different "
    "count). For each option, give it a brief name and the 4-8 tags "
    "you'd put in the section. The user will pick one in a follow-up "
    "turn (e.g. 'go with option 1') — that follow-up IS when you call "
    "the tool with the chosen option's tags. Do not modify the prompt "
    "until the user picks.\n"
    "  Example reply for 'give me 3 sexy variations on a bunny outfit':\n"
    "  '1. **Classic Playboy** — black bunny suit, fishnet stockings, "
    "bow tie, bunny ears, cuffs.\n"
    "  2. **Latex Couture** — latex bunny suit, thigh high boots, "
    "long gloves, choker, bunny ears.\n"
    "  3. **Cute & Frilly** — pastel bunny dress, frilly tutu, bunny "
    "ears headband, ribbon, knee socks.\n"
    "  Which one would you like to try?'\n\n"
)


# Question-shape detection — same set of phrases the agent system
# prompt uses to decide "answer in text vs call the tool". Bio preload
# only fires when the message looks like a question; on edit / setup
# requests, the bio block is unhelpful noise that shifts the agent's
# paraphrasing and regresses multi-character scenarios.
_QUESTION_OPENERS = (
    "what is", "what's", "what are", "what color", "what kind",
    "who is", "who's", "who are",
    "do you know", "tell me about", "describe",
    "which", "how", "why", "when", "where", "is",
)


def _looks_like_question(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return any(t.startswith(opener) for opener in _QUESTION_OPENERS)


def _filter_bios_for_agent_preload(bios: list[dict],
                                    user_text: str) -> list[dict]:
    """Strict filter on match-characters output for agent-side
    knowledge preload. Drops single-token-prefix matches (e.g.
    `night` -> `night_angel`) that polluted the system prompt in the
    pre-T4 raw-text preflight era.

    Keeps a bio only when its canonical tag's bare form (parens
    stripped) appears as a multi-word phrase in the user message OR
    its display name appears as a multi-word phrase. Single-word
    matches survive only when the canonical tag itself is single-
    token (e.g. `mythra` matches a tag whose bare form is `mythra`).

    Filters input: bios already deduped and matched by the matcher;
    we just trim noise."""
    if not bios or not user_text:
        return bios
    text_lc = user_text.lower()

    def _multi_token_in_text(name: str) -> bool:
        n = (name or "").lower().strip()
        if not n:
            return False
        # Remove parenthetical disambiguation suffix.
        n = re.sub(r"\s*\([^)]*\)", "", n).strip()
        if not n:
            return False
        # Normalize separators to space then check phrase membership.
        phrase = re.sub(r"[\s\-_]+", " ", n).strip()
        if not phrase:
            return False
        if " " in phrase:
            return phrase in re.sub(r"[\s\-_]+", " ", text_lc)
        # Single-token canonical (e.g. `mythra`) — accept word-bounded
        # match in user text.
        return bool(re.search(
            r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])",
            text_lc,
        ))

    out: list[dict] = []
    for b in bios:
        tag = (b.get("tag") or "").strip()
        display = (b.get("display") or "").strip()
        if _multi_token_in_text(tag) or _multi_token_in_text(display):
            out.append(b)
    return out


def _build_agent_bio_block(bios: list[dict]) -> str:
    """Render filtered bios as a 'Character knowledge' system-prompt
    block. APPEARANCE FACTS ONLY — base_tags (hair, eyes, build,
    distinguishing features). Outfits are intentionally NOT surfaced
    here: the patch flow already gets them via `character_queries`
    when the agent dispatches a tool call, AND surfacing both
    characters' outfits in the agent prompt during borrow scenarios
    bloats the system prompt and shifts paraphrasing behavior, which
    regressed the borrow harness.

    Empty string when bios is empty so the prompt stays lean on
    edit / generic-chat turns."""
    if not bios:
        return ""
    lines = [
        "Character knowledge (from local database — use as the "
        "AUTHORITATIVE source for any factual question about these "
        "characters' APPEARANCE; do NOT contradict these facts with "
        "model world knowledge):",
    ]
    for b in bios[:5]:  # cap to 5 to avoid system-prompt bloat
        tag = (b.get("tag") or "").strip()
        display = (b.get("display") or "").strip() or tag
        series = (b.get("series") or "").strip()
        base_tags = (b.get("base_tags") or "").strip()
        line = f"- {display}"
        if series:
            line += f" ({series})"
        line += f" [canonical: {tag}]"
        if base_tags:
            line += f": {base_tags}"
        lines.append(line)
    return "\n".join(lines) + "\n\n"


def _agent_system_prompt(node_ctx: dict, bios: list[dict] | None = None) -> str:
    node_prompt = (node_ctx or {}).get("node_prompt") or ""
    # extra_verbs gates the macro-verb shortcuts block. Off by default so
    # the simple-edit user gets a leaner system prompt — the 8B Qwen
    # showed measurable noise increase when the macro block was always
    # on, and most users only need add/remove/swap verbs anyway.
    extra_verbs = bool((node_ctx or {}).get("extra_verbs"))
    macro_block = _AGENT_MACRO_VERBS_BLOCK if extra_verbs else ""

    # Character knowledge block — surfaces bio facts for any character
    # the user mentioned in this turn. Lets the agent answer factual
    # questions ("what color hair does cammy have?") from the local DB
    # instead of model world knowledge, which is unreliable on 8B.
    # Only fires when at least one bio matched — no noise on edits or
    # generic chat. For tool calls, the agent's `character_queries`
    # field still drives bio resolution in `_call_patch_internal`; this
    # block exists for the text-response path.
    bio_block = _build_agent_bio_block(bios or [])

    # /no_think disables Qwen3.x reasoning blocks for the chat-agent role.
    # Tool-routing decisions are simple — we don't need <think> traces
    # for them, and reasoning models burn 2-8K tokens per call on those
    # blocks, dominating turn latency. The patch model still gets its
    # own /no_think directive in ai_api._patch_system_prompt.
    return (
        "/no_think\n"
        + bio_block
        + "You are an in-editor AI assistant helping a user iterate on "
        "Stable-Diffusion prompts inside ComfyUI's PromptChain node. "
        "Your job is to read the user's natural-language requests and, "
        "when they want a prompt change, call the `apply_prompt_patch` "
        "tool with a focused imperative `request` string distilled from "
        "their message.\n\n"
        "Conversational rules:\n"
        "- Reply in 1-2 short sentences, never a wall of text.\n"
        "- After a tool call, summarize what changed in plain English. Do "
        "NOT re-list the diff — the UI shows it. One sentence is enough.\n"
        "- If the user's message ends with `?` or starts with explicit "
        "question phrases (`what is`, `who is`, `do you know`, `tell me "
        "about`, `which`, `how`, `why`), reply in text without calling "
        "the `apply_prompt_patch` tool. Otherwise default to CALLING "
        "`apply_prompt_patch` — the user is almost always trying to "
        "edit the prompt.\n"
        "- EXCEPTION: questions about MODEL STYLES go to "
        "`list_model_styles` (which fetches the user's actual "
        "configured templates), not plain-text replies. Triggers: "
        "'what styles does this model have', 'what anime styles are "
        "there', 'list available styles', 'any cinematic ones', "
        "'what's hyperrealistic', 'tell me about studio ghibli'. The "
        "tool's `filter` param does case-insensitive substring match "
        "against BOTH name AND category, so pass whatever style word "
        "the user mentioned — 'anime' (category match), 'hyperrealistic' "
        "(name match), 'cinematic' (matches both). The tool returns "
        "PRE-FORMATTED markdown that you MUST echo verbatim — keep "
        "the `- ` dashes and `**bold**` markers, do not strip them; "
        "they render as real bullets in the chat. Do not add training-"
        "data style names (Shoujo, Seinen, Slice of Life are not real "
        "templates), do not drop entries, do not reorder. 'apply the "
        "X style' is an edit, not a query — that still goes to "
        "apply_prompt_patch.\n"
        "- A short message containing a character name + any setup verb "
        "(`set up X`, `give me X`, `make X`, `mythra`, `cammy in delta "
        "red`) IS a tool-call request. Do not ask 'do you mean X' or "
        "'would you like me to' — just call the tool. The downstream "
        "matcher validates names against the character database; you "
        "don't need to verify them yourself.\n"
        "- If a name is genuinely ambiguous (multiple known characters "
        "share it AND the user gave no franchise context), THEN ask. "
        "Otherwise prefer action over caution.\n\n"
        "Tool-call rules:\n"
        "- Call `apply_prompt_patch` AT MOST ONCE per turn.\n"
        "- The `request` you pass is fed to a separate, narrow tag-editing "
        "model. Phrase it as an imperative the way a junior prompt editor "
        "would understand: 'add red socks', 'increase red_socks weight by "
        "20%', 'remove blur, motion_blur', 'switch character to "
        "cammy_white'. Do NOT pass open-ended phrasings like 'make it "
        "better' or 'fix the issues'.\n"
        "- When naming a character, use the Danbooru canonical full-name "
        "tag form: first_last (`cammy_white`, `tifa_lockhart`, `chun-li`, "
        "`yuuki_(sao)`). Do NOT use franchise-only forms like "
        "`cammy_(street_fighter)` — those rarely match the character "
        "database. If you don't know the canonical form, use the most "
        "specific name you do know plus the franchise.\n"
        "- Pass the user's request through to the tool's `request` field "
        "in natural language. Don't rewrite it into a `setting: X; style: "
        "Y` template — that template loses verbs and modifiers that don't "
        "fit the slot names. The patch flow handles natural language "
        "directly.\n"
        "- For pose, action, scene, setting, expression, gaze, framing, "
        "and any non-character concept where you are NOT certain a real "
        "Danbooru tag exists with that exact spelling, write the concept "
        "as a natural English phrase. Do NOT join multi-word concepts "
        "with underscores to make them look tag-shaped — the downstream "
        "tag-editing model has the full Danbooru tag database and "
        "canonicalizes phrases against it. An underscore-joined "
        "fake-tag-shape you invent gets echoed verbatim and then dropped "
        "as untraceable, which means the real canonical never lands in "
        "the output. Canonical character tags and weighted forms stay in "
        "canonical form per the rules above; everything else is natural "
        "language.\n"
        "- Pass `character_queries` whenever a CHARACTER is in scope — "
        "either named directly by the user OR present in node_prompt's "
        "`// Character:` header. The matcher needs the bio loaded to "
        "fuzzy-match outfit/pose names against that character's curated "
        "options. Without it, 'switch to killer bee outfit' has no bio "
        "to find Killer Bee in.\n"
        "  Examples:\n"
        "    'set up Cammy' → ['cammy_white']\n"
        "    'cammy in pyra's outfit' (borrow) → ['cammy_white', "
        "'pyra_(xenoblade)']\n"
        "    'cammy and tifa fighting' → ['cammy_white', 'tifa_lockhart']\n"
        "    node_prompt has '// Character: cammy_white', user says "
        "'switch to killer bee outfit' → ['cammy_white'] (existing "
        "character stays in scope so matcher can find her Killer Bee "
        "outfit). Pass the canonical from the `// Character:` header.\n"
        "    Words like 'killer bee', 'delta red', 'cannon spike' are "
        "OUTFIT/POSE names — they belong in `request` text, NEVER in "
        "character_queries.\n"
        "  Skip character_queries only when no character is in scope at "
        "all (fresh prompt with no character + user describing a scene "
        "without naming anyone, or pure-question turns).\n"
        "- Never call the tool a second time within one turn unless the "
        "user explicitly asked for two distinct changes.\n\n"
        + macro_block
        + f"Current prompt in the node:\n<node_prompt>\n{node_prompt}\n</node_prompt>"
    )


def _router_system_prompt(node_ctx: dict) -> str:
    """Prompt for the tool-routing pass. Tight focus: decide which tool
    (if any) to call and with what arguments. No narration guidance —
    that belongs to `_assistant_system_prompt`. Bios are intentionally
    omitted; the router doesn't need character knowledge to pick a tool,
    and the patch flow runs its own preflight on character_queries."""
    node_prompt = (node_ctx or {}).get("node_prompt") or ""
    extra_verbs = bool((node_ctx or {}).get("extra_verbs"))
    macro_block = _AGENT_MACRO_VERBS_BLOCK if extra_verbs else ""
    return (
        "/no_think\n"
        "You are the tool-router for an in-editor AI assistant helping a "
        "user iterate on Stable-Diffusion prompts inside ComfyUI's "
        "PromptChain node. Your job: read the user's latest message in "
        "context of the chat history, decide whether to call a tool, "
        "and emit the tool call with focused arguments. If no tool is "
        "needed (general conversational message, factual question about "
        "a character's appearance), emit a brief 1-2 sentence text reply "
        "instead.\n\n"
        "Routing rules:\n"
        "- Edit requests (add, remove, swap, reweight, switch character / "
        "outfit / pose / scene / style) → apply_prompt_patch.\n"
        "- 'apply / try / use / go with / pick / let's do X' is an EDIT "
        "→ apply_prompt_patch, NOT list_model_styles. Even when X is a "
        "style name the user just looked up, the user now wants to "
        "APPLY it.\n"
        "- Questions about MODEL STYLES ('what styles does this model "
        "have', 'what anime styles are there', 'what's hyperrealistic', "
        "'tell me about studio ghibli') → list_model_styles. The tool's "
        "`filter` param does case-insensitive substring match against "
        "BOTH name AND category; pass whatever style word the user "
        "mentioned.\n"
        "- Generic chat or factual questions about a character's "
        "appearance ('what color hair does cammy have') → no tool, "
        "short text reply.\n"
        "- A short message containing a character name + setup verb "
        "(`set up X`, `give me X`, `mythra`, `cammy in delta red`) IS "
        "a tool-call request. Don't ask for clarification — call "
        "apply_prompt_patch. The downstream matcher validates names; "
        "you don't need to verify them yourself.\n"
        "- Deictic references ('that one', 'it', 'lets try that', "
        "'use that', 'apply that') after discussing a specific style / "
        "character / outfit MUST be resolved against the chat history "
        "AND treated as an APPLY request → apply_prompt_patch. The "
        "`request` field must NAME the referent explicitly (e.g. "
        "'switch style to Hyperrealistic'), never echo the literal "
        "deictic.\n"
        "- When in doubt between a tool and just chatting, prefer "
        "apply_prompt_patch — most user messages in this editor are "
        "edit requests.\n\n"
        "Args-construction rules for apply_prompt_patch are in the tool's "
        "schema description — follow them. Two additional routing rules:\n"
        "- Call apply_prompt_patch AT MOST ONCE per turn unless the user "
        "explicitly asked for two distinct changes.\n"
        "- When calling apply_prompt_patch, you MUST populate "
        "`character_queries` with every named character in the user's "
        "request AND every character already present in the node prompt's "
        "`// Character:` header. Missing characters break outfit/pose "
        "matching downstream. Only pass `[]` when there's genuinely no "
        "character in scope (generic edits like 'add red socks' to an "
        "anonymous prompt). Examples are in the tool schema.\n\n"
        + macro_block
        + f"Current prompt in the node:\n<node_prompt>\n{node_prompt}\n</node_prompt>"
    )


def _combined_system_prompt(node_ctx: dict,
                            bios: list[dict] | None = None,
                            history_summary: str = "",
                            has_image: bool = False) -> str:
    """Single-persona prompt for the A/B comparison against router+assistant.
    Mirrors the Copilot/Continue.dev pattern: one system prompt covers
    BOTH the routing decision (which tool, if any) AND the narration of
    the tool result in the same conversation. Tools are attached to
    every call; the model decides tool-vs-text on the fly.

    Args-construction rules for apply_prompt_patch live in the tool's
    schema (the model sees them via function-call spec)."""
    node_prompt = (node_ctx or {}).get("node_prompt") or ""
    extra_verbs = bool((node_ctx or {}).get("extra_verbs"))
    macro_block = _AGENT_MACRO_VERBS_BLOCK if extra_verbs else ""
    bio_block = _build_agent_bio_block(bios or [])
    summary_block = ""
    if history_summary:
        summary_block = (
            "Conversation so far (older turns, condensed — these edits are "
            "ALREADY reflected in the current prompt, do not re-apply them):\n"
            + history_summary
            + "\n\n"
        )
    image_block = ""
    if has_image:
        image_block = (
            "IMAGE ATTACHED — the user has attached an image to this turn.\n"
            "- If they asked a question about it ('who is this?', 'what "
            "character/outfit/style is this?'), answer in chat. Name the "
            "character AND franchise if you recognize them; say plainly if "
            "you are unsure rather than guessing confidently.\n"
            "- If they asked you to recreate / make a prompt for it (or "
            "attached it with no instruction), call apply_prompt_patch with "
            "a `request` that DESCRIBES what you see: the character by name "
            "and franchise if recognized (e.g. 'Cammy from Street Fighter'), "
            "otherwise plain visual description (hair, body, clothing, pose, "
            "setting, art style). Naming a known character lets the system "
            "ground it to canonical tags; do NOT invent canonical tags "
            "yourself — describe in natural language and let the patch step "
            "resolve them.\n"
            "- If they use the image as a REFERENCE for one specific aspect "
            "('do her in this pose', 'use this angle/composition/lighting/"
            "framing', 'same pose as this'), extract ONLY that named aspect "
            "from the image and put just that in the request. Do NOT copy the "
            "reference person's identity, face, hair, eyes, or clothing — the "
            "subject stays whoever the user is already working on.\n"
            "- If the user refers to a subject by pronoun ('her', 'him', "
            "'them') but the current prompt has no character and none was "
            "established earlier in the chat, ASK who they mean instead of "
            "inventing one.\n\n"
        )
    return (
        "/no_think\n"
        + bio_block
        + summary_block
        + image_block
        + "You are an in-editor AI assistant helping a user iterate on "
        "Stable-Diffusion prompts inside ComfyUI's PromptChain node. "
        "Reply in 1-2 short sentences, friendly and concise. Use "
        "markdown when listing items (`- **Name**` bullets, `**bold**` "
        "for emphasis).\n\n"
        "Routing — when to call which tool vs chat directly:\n"
        "- Edit requests (add, remove, swap, reweight, switch "
        "character/outfit/pose/scene/style) → apply_prompt_patch.\n"
        "- 'apply / try / use / go with / pick / let's do X' is an "
        "EDIT → apply_prompt_patch, NOT list_model_styles, even when "
        "X is a style name the user just looked up.\n"
        "- Questions about MODEL STYLES ('what styles does this model "
        "have', 'what anime styles', 'what's hyperrealistic', 'tell "
        "me about studio ghibli') → list_model_styles. Pass `filter` "
        "with whatever style word the user mentioned.\n"
        "- Requests to add MANY items at once by category/attribute → "
        "populate_inline_wildcards. 'add all anime styles', 'fill this "
        "with photorealistic styles' → source='styles'. 'add every "
        "street fighter character', 'add the blonde street fighter "
        "girls', 'add all characters from nier' → source='characters' "
        "with series + appearance filters. This is for SETS; a single "
        "named character/style is still apply_prompt_patch.\n"
        "- Requests to INVENT original subjects from a theme (not named "
        "DB characters) → generate_subjects. 'give me 10 inline "
        "wildcards for mature women with random outfits', 'make me a "
        "cyberpunk mercenary', 'fill this with 6 elegant noblewomen'. "
        "Map the theme to `fixed` constraints (e.g. mature woman → "
        "fixed={'body_type':'mature_female'}), set count + "
        "outfit_policy. KEY DISTINCTION: populate_inline_wildcards "
        "FETCHES existing NAMED characters/styles; generate_subjects "
        "INVENTS new ones from a vibe. 'street fighter characters' = "
        "populate; 'mature women' / 'cyberpunk mercenaries' = generate.\n"
        "- ANY request to add/make an INLINE WILDCARD of a THING (pose, "
        "expression, scene, background, lighting, weather, action, "
        "clothing, shoes, ...) → list_inline_wildcards. The words "
        "'inline', 'inline wildcard', 'another inline', 'a second "
        "inline', 'wildcard for X' ALL route here — even for ONE entry. "
        "Pass `what`=the user's description verbatim and (if they named a "
        "number) `count`. DO NOT use apply_prompt_patch for this and DO "
        "NOT hand-write a `::Label::body` string yourself — that's the "
        "tool's job; apply_prompt_patch cannot insert wildcard entries "
        "and silently no-ops.\n"
        "  'add a second inline for presenting a foot while standing, leg "
        "extended, foot focus' → list_inline_wildcards(what='presenting a "
        "foot while standing, leg extended, foot focus', count=1).\n"
        "  '10 lingerie outfits' / 'some angry expressions' / 'rooftop "
        "backgrounds' / 'dynamic poses' → list_inline_wildcards too.\n"
        "KEY: THINGS → list_inline_wildcards; PEOPLE → generate_subjects "
        "(invent) or populate_inline_wildcards (named chars).\n"
        "- Factual questions about a character's appearance ('what "
        "color hair does cammy have') → answer directly using the "
        "Character knowledge block, no tool.\n"
        "- Generic chat → reply directly, no tool.\n"
        "- A short message containing a character name + setup verb "
        "(`set up X`, `give me X`, `mythra`, `cammy in delta red`) "
        "IS a tool-call request — call apply_prompt_patch without "
        "asking for clarification.\n"
        "- Deictic references ('that one', 'it', 'lets try that') "
        "after discussing a specific style/character MUST be resolved "
        "against chat history AND treated as an APPLY request. The "
        "`request` field must NAME the referent explicitly.\n"
        "- When in doubt, prefer apply_prompt_patch — most user "
        "messages in this editor are edit requests.\n\n"
        "Narrating tool_result blocks (look at the most recent in the "
        "conversation):\n"
        "- list_model_styles with MULTIPLE styles: introduce briefly "
        "(`This model has N styles:`), then a bulleted list of just "
        "the `name` fields as bold. Don't add descriptions per item, "
        "don't add style names that aren't in the result, don't "
        "reorder. End with one short line offering to apply one.\n"
        "- list_model_styles with a SINGLE style: introduce by name "
        "in bold, then ONE sentence describing what the style does "
        "using the `description` field. Don't invent details.\n"
        "- list_model_styles EMPTY: say no matching styles are "
        "configured.\n"
        "- apply_prompt_patch result: summarize what changed in ONE "
        "sentence. Don't re-list the diff — the UI shows it.\n\n"
        "Args-construction rules for apply_prompt_patch are in its "
        "tool schema. Two more rules:\n"
        "- Call apply_prompt_patch AT MOST ONCE per turn unless the "
        "user explicitly asked for two distinct changes.\n"
        "- When calling apply_prompt_patch, you MUST populate "
        "`character_queries` with every named character in the user's "
        "request AND every character already present in the node "
        "prompt's `// Character:` header. Missing characters break "
        "outfit/pose matching downstream. Only pass `[]` when there's "
        "genuinely no character in scope (generic edits like 'add red "
        "socks' to an anonymous prompt).\n\n"
        + macro_block
        + f"Current prompt in the node:\n<node_prompt>\n{node_prompt}\n</node_prompt>"
    )


def _assistant_system_prompt(node_ctx: dict,
                             bios: list[dict] | None = None) -> str:
    """Prompt for the narration pass. The assistant has NO tools and
    receives the user message + the router's `tool_use` + the `tool_result`
    as canonical Anthropic-shaped blocks in `history` (matches what
    Qwen3 / Claude / OpenAI all expect for tool exchanges). Bios are
    surfaced here (not in the router) because narration is where the
    assistant needs to answer factual questions about characters; the
    patch flow already resolves bios for tool dispatch."""
    node_prompt = (node_ctx or {}).get("node_prompt") or ""
    bio_block = _build_agent_bio_block(bios or [])
    return (
        "/no_think\n"
        + bio_block
        + "You are an in-editor AI assistant helping a user iterate on "
        "Stable-Diffusion prompts inside ComfyUI's PromptChain node. "
        "Reply in 1-2 short sentences, friendly and concise. Use "
        "markdown when listing items (e.g. `- **Name**` bullets, "
        "`**bold**` for emphasis).\n\n"
        "Narrating tool results (look at the most recent tool_result "
        "block in the conversation):\n"
        "- list_model_styles with MULTIPLE styles: introduce briefly "
        "(`This model has N styles:`), then a bulleted list of just "
        "the `name` fields as bold (`- **90s Anime**`). Do NOT add "
        "descriptions per item, do NOT add style names that aren't in "
        "the result, do NOT reorder. End with one short line offering "
        "to apply one.\n"
        "- list_model_styles with a SINGLE style: introduce by name in "
        "bold, then ONE sentence describing what the style does using "
        "the `description` field. Use the description verbatim or "
        "summarize from it — do NOT invent details from outside the "
        "result.\n"
        "- list_model_styles with EMPTY array: say no matching styles "
        "are configured for this model.\n"
        "- apply_prompt_patch result: summarize what changed in ONE "
        "sentence. Don't re-list the diff — the UI shows it."
        + f"\n\nCurrent prompt in the node:\n<node_prompt>\n{node_prompt}\n</node_prompt>"
    )


# ── loop config ───────────────────────────────────────────────────────

_AGENT_MAX_HOPS = 6
_AGENT_MAX_TOKENS = 2048   # chat replies are short; reasoning is the patch model's job
_AGENT_CONNECT_TIMEOUT = 10
_AGENT_READ_TIMEOUT = 300

# ── history compaction ────────────────────────────────────────────────
# A long chat ferries its entire canonical message list (text + every
# tool_use/tool_result block) back to the model each turn (see
# _run_agent_loop). Left unbounded it eventually overruns the model
# window — Ollama runs at num_ctx=32768 (see _stream_openai_compat_with_tools),
# so the local case is the binding constraint. We watermark: below budget,
# send verbatim; above it, drop the OLDEST turns and fold them into a
# mechanical summary carried in the system prompt. Safe to drop the old
# tool bodies because their effect is already baked into node_prompt, which
# is rebuilt and sent fresh every turn.
_HISTORY_BUDGET_LOCAL = 16000     # ~half of Ollama's 32768, leaving system+output room
_HISTORY_BUDGET_CLOUD = 120000    # bounds cost on large-window cloud models
_HISTORY_RECENT_TARGET = 0.55     # trim recent-tail down to this fraction of budget
_HISTORY_SUMMARY_MAX_CHARS = 3000 # cap the running summary itself


def _emit_agent(request_id: str, event: str, **payload):
    """Mirror ai_api._emit but with arbitrary payload keys (proposal_id,
    name, input, etc.). Goes through the same WS channel."""
    msg = {
        "request_id": request_id,
        "event": event,
        "t": time.time(),
        **payload,
    }
    send_ws("promptchain_ai_stream", msg)


# ── message + tool shape converters ───────────────────────────────────
# Internal history is Anthropic-shaped (assistant content is a list of
# {type:"text"|"tool_use"|"tool_result", ...}). The OpenAI tool-use API
# wants role=assistant with `tool_calls` and role=tool with `tool_call_id`.
# Convert at the wire boundary so the loop stays shape-agnostic.

def _to_openai_messages(system: str, anthropic_messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in anthropic_messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": ""})
            continue

        if role == "user":
            text_parts: list[str] = []
            tool_results: list[dict] = []
            image_parts: list[dict] = []
            for block in content:
                bt = block.get("type")
                if bt == "text":
                    text_parts.append(block.get("text") or "")
                elif bt == "tool_result":
                    tool_results.append(block)
                elif bt == "image":
                    # Anthropic base64 image → OpenAI image_url data-URL.
                    src = block.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
                            },
                        })
            for tr in tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id") or "",
                    "content": tr.get("content") or "",
                })
            joined = "\n".join(p for p in text_parts if p)
            if image_parts:
                # Multimodal user turn: OpenAI wants content as a parts list.
                parts: list[dict] = []
                if joined:
                    parts.append({"type": "text", "text": joined})
                parts.extend(image_parts)
                out.append({"role": "user", "content": parts})
            elif joined:
                out.append({"role": "user", "content": joined})
            elif not tool_results:
                out.append({"role": "user", "content": ""})
            continue

        if role == "assistant":
            text_parts = []
            tool_calls: list[dict] = []
            for block in content:
                bt = block.get("type")
                if bt == "text":
                    text_parts.append(block.get("text") or "")
                elif bt == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
            entry: dict = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else ""}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        # fall-through (system rare, etc.)
        out.append({"role": role, "content": json.dumps(content)})
    return out


def _tools_for_openai(anthropic_tools: list[dict]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": t.get("name"),
            "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object"},
        },
    } for t in anthropic_tools]


# ── streaming with tools (Anthropic) ──────────────────────────────────

async def _stream_claude_with_tools(
    request_id: str, api_key: str, model: str,
    system: str, messages: list[dict],
    *, tools: list[dict] | None = None,
) -> dict:
    """Stream a single Claude turn with optional tool support. Returns:
        {"stop_reason": str, "content": [...content_blocks...]}
    `content` is the assembled list of text + tool_use blocks in order.
    Streams text deltas to WS as `agent_text` events live.

    `tools` defaults to the module's _AGENT_TOOLS. Pass `[]` for a
    narration-only call with no tool surface (assistant pass)."""
    if tools is None:
        tools = _AGENT_TOOLS
    payload = {
        "model": model,
        "max_tokens": _AGENT_MAX_TOKENS,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools

    blocks_by_index: dict[int, dict] = {}  # index -> {type, _text|_input_json, ...}
    final_blocks: list[dict] = []
    stop_reason: str | None = None
    parse_failures = 0

    timeout = aiohttp.ClientTimeout(connect=_AGENT_CONNECT_TIMEOUT, sock_read=_AGENT_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        ) as resp:
            logger.info("agent_stream[%s] HTTP %d", request_id, resp.status)
            if resp.status != 200:
                err_body = await resp.text()
                raise RuntimeError(f"Claude HTTP {resp.status}: {err_body[:300]}")

            # readline() is line-delimited; iterating resp.content yields
            # arbitrary chunks that can split or merge SSE events.
            while True:
                raw = await resp.content.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except Exception:
                    parse_failures += 1
                    continue
                evt_type = evt.get("type")

                if evt_type == "content_block_start":
                    idx = evt.get("index", 0)
                    block = evt.get("content_block") or {}
                    blocks_by_index[idx] = {
                        "type": block.get("type"),
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "_text": "",
                        "_input_json": "",
                    }

                elif evt_type == "content_block_delta":
                    idx = evt.get("index", 0)
                    delta = evt.get("delta") or {}
                    blk = blocks_by_index.get(idx)
                    if blk is None:
                        continue
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        blk["_text"] += text
                        _emit_agent(request_id, "agent_text", content=text, partial=True)
                    elif delta.get("type") == "input_json_delta":
                        blk["_input_json"] += delta.get("partial_json", "")

                elif evt_type == "content_block_stop":
                    idx = evt.get("index", 0)
                    blk = blocks_by_index.pop(idx, None)
                    if blk is None:
                        continue
                    if blk["type"] == "text":
                        final_blocks.append({"type": "text", "text": blk["_text"]})
                    elif blk["type"] == "tool_use":
                        try:
                            parsed_input = json.loads(blk["_input_json"] or "{}")
                        except Exception as e:
                            logger.warning(
                                "agent_stream[%s] tool_use input parse fail: %s | raw=%r",
                                request_id, e, blk["_input_json"][:200],
                            )
                            parsed_input = {}
                        final_blocks.append({
                            "type": "tool_use",
                            "id": blk["id"],
                            "name": blk["name"],
                            "input": parsed_input,
                        })
                        _emit_agent(
                            request_id, "agent_tool_call",
                            proposal_id=blk["id"], name=blk["name"], input=parsed_input,
                        )

                elif evt_type == "message_delta":
                    sr = (evt.get("delta") or {}).get("stop_reason")
                    if sr:
                        stop_reason = sr

                elif evt_type == "message_stop":
                    return {"stop_reason": stop_reason, "content": final_blocks}

                elif evt_type == "error":
                    err_msg = (evt.get("error") or {}).get("message") or "stream error"
                    raise RuntimeError(err_msg)

    if parse_failures:
        logger.info("agent_stream[%s] %d parse failures", request_id, parse_failures)
    return {"stop_reason": stop_reason or "end_turn", "content": final_blocks}


# ── streaming with tools (OpenAI-compat / Ollama) ─────────────────────

async def _stream_openai_compat_with_tools(
    request_id: str, base_url: str, model: str,
    system: str, messages: list[dict],
    *, api_key: str | None = None, is_ollama: bool = False,
    tools: list[dict] | None = None,
) -> dict:
    """Stream a single tool-aware turn via OpenAI /v1/chat/completions.
    Used for cloud-non-claude AND local Ollama (which serves OpenAI-shape
    tool_calls deltas as of 0.4+). `messages` is in Anthropic shape; we
    convert at the wire boundary. Returns Anthropic-shaped result so the
    agent loop stays provider-agnostic.

    Tool-call streaming: deltas come on `choices[0].delta.tool_calls[]`
    with stable `index` values; the first chunk for an index carries
    {id, type, function: {name}}, subsequent chunks carry partial
    `function.arguments`. We accumulate per-index then JSON-parse on
    finish_reason."""
    if tools is None:
        tools = _AGENT_TOOLS
    payload = {
        "model": model,
        "messages": _to_openai_messages(system, messages),
        "stream": True,
        "max_tokens": _AGENT_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = _tools_for_openai(tools)
    if is_ollama:
        # Disable Ollama-side reasoning blocks for the chat-agent role —
        # tool-routing decisions don't need <think>, and reasoning models
        # often emit tool_calls AFTER long think blocks which inflates
        # latency. The patch flow's separate Ollama call still gets
        # whatever think setting the patch path uses.
        payload["think"] = False
        payload["options"] = {"num_ctx": 32768}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    text_buffer: list[str] = []
    # Per-index tool-call accumulator: {index: {id, name, args_json}}.
    tool_calls_by_index: dict[int, dict] = {}
    finish_reason: str | None = None
    parse_failures = 0

    timeout = aiohttp.ClientTimeout(connect=_AGENT_CONNECT_TIMEOUT, sock_read=_AGENT_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{base_url}/chat/completions"
        async with session.post(url, json=payload, headers=headers) as resp:
            logger.info("agent_stream_openai[%s] HTTP %d url=%s", request_id, resp.status, url)
            if resp.status != 200:
                err_body = await resp.text()
                raise RuntimeError(f"chat-agent HTTP {resp.status}: {err_body[:300]}")

            # readline() is line-delimited; iterating resp.content yields
            # arbitrary chunks that can split or merge SSE events.
            while True:
                raw = await resp.content.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except Exception:
                    parse_failures += 1
                    continue

                choices = evt.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                text_delta = delta.get("content") or ""
                if text_delta:
                    text_buffer.append(text_delta)
                    _emit_agent(request_id, "agent_text", content=text_delta, partial=True)

                tcs = delta.get("tool_calls") or []
                for tc in tcs:
                    idx = tc.get("index", 0)
                    slot = tool_calls_by_index.setdefault(idx, {
                        "id": "", "name": "", "args_json": "",
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args_json"] += fn["arguments"]

                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr

    if parse_failures:
        logger.info("agent_stream_openai[%s] %d parse failures", request_id, parse_failures)

    # Assemble final blocks in Anthropic shape, ordered text-then-tool_use
    # per OpenAI's wire convention.
    final_blocks: list[dict] = []
    if text_buffer:
        final_blocks.append({"type": "text", "text": "".join(text_buffer)})
    for idx in sorted(tool_calls_by_index.keys()):
        slot = tool_calls_by_index[idx]
        try:
            parsed_input = json.loads(slot["args_json"] or "{}")
        except Exception as e:
            logger.warning(
                "agent_stream_openai[%s] tool args parse fail: %s | raw=%r",
                request_id, e, slot["args_json"][:200],
            )
            parsed_input = {}
        proposal_id = slot["id"] or f"toolu_{uuid.uuid4().hex[:12]}"
        final_blocks.append({
            "type": "tool_use",
            "id": proposal_id,
            "name": slot["name"],
            "input": parsed_input,
        })
        _emit_agent(
            request_id, "agent_tool_call",
            proposal_id=proposal_id, name=slot["name"], input=parsed_input,
        )

    # Full raw-response dump on the shared debug channel — the build/patch
    # paths get rich `dbg` traces in ai_api, but this agent tool-stream
    # path only had an 80-char preview (agent_hop). When the model
    # misroutes (wrong tool, empty/garbage args, or a leaked <think> block
    # from an Ollama that ignored think:False), that preview hides the
    # WHY. Dumping the full text + each tool call's raw args here makes a
    # single re-run conclusive regardless of where the turn derails.
    raw_text = "".join(text_buffer)
    if ai_api._THINK_OPEN_RE.search(raw_text):
        ai_api.dbg.info(
            "agent[%s] LEAKED <think> block in agent response content — "
            "think:False not honored by this Ollama build", request_id,
        )
    tool_dump = "; ".join(
        f"{tool_calls_by_index[i].get('name') or '?'}("
        f"{ai_api._trunc(tool_calls_by_index[i].get('args_json') or '', 600)})"
        for i in sorted(tool_calls_by_index.keys())
    ) or "(none)"
    ai_api.dbg.info(
        "agent[%s] raw response: finish_reason=%s | tools=%s | text=%s",
        request_id, finish_reason, tool_dump,
        ai_api._trunc(raw_text, 2000) or "(empty)",
    )

    # Map OpenAI finish_reason → Anthropic stop_reason.
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "stop":
        stop_reason = "end_turn"
    else:
        stop_reason = finish_reason or "end_turn"

    return {"stop_reason": stop_reason, "content": final_blocks}


# ── n-gram extraction for server-side bios preflight ──────────────────
# Python port of svelte-src/lib/ai-patch-helpers.js (extractNGrams +
# synthesizeParenVariants). Behavior must stay aligned with the client
# version — the legacy single-shot path runs preflight client-side; the
# chat agent paraphrases the user's intent so we must run preflight
# again, server-side, on the agent's distilled `request` string.

_WEIGHT_RE = re.compile(r"\(\s*([^():]+?)\s*:\s*[\d.]+\s*\)")
_POSSESSIVE_RE = re.compile(r"'s\b", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w\s()'\\-]")
_PREP_RE = re.compile(r"\s+(?:from|in|of|for|as)\s+", re.IGNORECASE)


def _synthesize_paren_variants(text: str) -> list[str]:
    if not text:
        return []
    variants: list[str] = []
    for m in _PREP_RE.finditer(text):
        left_text = text[:m.start()].strip()
        right_text = text[m.end():].strip()
        if not left_text or not right_text:
            continue
        left_words = [w for w in re.split(r"\s+", re.sub(r"[^\w\s'\\-]", " ", left_text)) if w]
        right_words = [w for w in re.split(r"\s+", re.sub(r"[^\w\s'\\-]", " ", right_text)) if w]
        for nl in range(1, min(3, len(left_words)) + 1):
            name = " ".join(left_words[-nl:])
            for rl in range(1, min(3, len(right_words)) + 1):
                series = " ".join(right_words[:rl])
                variants.append(f"{name} ({series})")
    return variants


def _extract_ngrams(text: str) -> list[str]:
    if not text:
        return []
    cleaned = _WEIGHT_RE.sub(r"\1", text)
    cleaned = _POSSESSIVE_RE.sub("", cleaned)
    cleaned = _NON_WORD_RE.sub(" ", cleaned)
    words = [w for w in re.split(r"\s+", cleaned) if w]
    out: set[str] = set()
    for n in range(1, 5):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n]).strip()
            if not phrase:
                continue
            if n == 1 and len(phrase) < 3:
                continue
            out.add(phrase)
    for variant in _synthesize_paren_variants(text):
        out.add(variant)
    return list(out)


async def _preflight_bios(
    request: web.Request, agent_request: str, node_prompt: str,
    *, latest_user_text: str = "", user_text_override: str | None = None,
    franchise_hint_text: str = "",
) -> list[dict]:
    """Run match-characters server-side. N-grams come from THREE sources
    so character mentions are caught regardless of where they appear:
      - agent_request: the chat agent's distilled tool input (often has
        canonical `cammy_(street_fighter)` form when the agent inferred
        the character)
      - latest_user_text: the user's most recent message verbatim (catches
        names like 'cammy white' that the agent may have paraphrased
        away — e.g. interpreting 'white' as a color modifier)
      - node_prompt: existing characters already in the prompt
    Union'd so a hit in any source flows the curated bio into the patch.

    `user_text_override`: when set, sent as the `user_text` field in the
    match-characters payload (drives the OUTFIT picker / POSE picker
    server-side) regardless of what fed the n-grams. Used by the
    character_queries-driven path so character matching stays focused
    on the agent's queries while outfit/pose picking still sees the
    user's full natural-language request."""
    sources = [agent_request, latest_user_text or "", node_prompt or ""]
    all_ngrams: set[str] = set()
    for src in sources:
        for ng in _extract_ngrams(src):
            all_ngrams.add(ng)
    ngrams = list(all_ngrams)
    if not ngrams:
        return []
    host = request.url.host or "127.0.0.1"
    port = request.url.port or 8188
    url = f"http://{host}:{port}/promptchain/tag-builder/match-characters"
    payload = {
        "tokens": ngrams,
        "user_text": (user_text_override if user_text_override is not None else agent_request),
        "node_prompt": node_prompt or "",
        # Franchise hint text — used ONLY by the matcher's stage-3/4
        # franchise-preference tiebreaker. Distinct from n-gram source
        # so it doesn't expand the matcher's token set (which would
        # cause bare names like `rin`/`miku`/`kagamine` from the user's
        # full message to over-match unrelated characters). Distinct
        # from `user_text` (which is scoped to the agent's distilled
        # request to avoid outfit-picker bleed).
        "latest_user_text": franchise_hint_text or "",
    }
    timeout = aiohttp.ClientTimeout(connect=_AGENT_CONNECT_TIMEOUT, sock_read=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("preflight_bios HTTP %d", resp.status)
                    return []
                data = await resp.json()
                matched = data.get("matched") or []
                return matched if isinstance(matched, list) else []
    except Exception as e:
        logger.warning("preflight_bios failed: %s", e)
        return []


# ── /ai/patch internal dispatch ───────────────────────────────────────

async def _call_patch_internal(
    request: web.Request,
    node_ctx: dict,
    user_request: str,
    chat_request_id: str,
    *,
    latest_user_text: str = "",
    current_user_text: str = "",
    character_queries: list[str] | None = None,
) -> dict:
    """POST to /promptchain/ai/patch over localhost so the patch endpoint
    sees the same request shape it does from the panel.

    Bios resolution: when the agent provided `character_queries`, run
    match-characters over the joined query string. When it didn't, no
    bio is loaded — the patch flow's no-bio path takes over. We do NOT
    fall back to scanning raw user text: the LLM is the character
    resolver, and a regex/prefix-match fallback re-creates exactly the
    false positives the character_queries pivot was meant to eliminate
    (e.g. 'micro bikini' → micro_uzi_(girls'_frontline) prefix-match)."""
    node_prompt = node_ctx.get("node_prompt") or ""
    t_pre = time.perf_counter()
    cleaned_queries = [q.strip() for q in (character_queries or []) if q and q.strip()]
    if cleaned_queries:
        # LLM-driven path: agent listed every character relevant to this
        # turn. Match all queries via a single joined-string preflight so
        # the existing matcher (n-grams + prefix-match) resolves each
        # name independently.
        #
        # user_text_override carries ONLY the current turn's request
        # (the agent's paraphrased imperative) so the OUTFIT picker
        # sees in-turn phrases like 'in her sf6 outfit' but does NOT
        # see prior-turn history that could fuzzy-match to outfit
        # names ('what street fighter...' → 'Street Fighter 6' outfit).
        joined = " | ".join(cleaned_queries)
        agent_bios = await _preflight_bios(
            request, joined, "", latest_user_text="",
            user_text_override=user_request,
            franchise_hint_text=latest_user_text,
        )
        logger.info(
            "agent_patch[%s] bios_preflight=%.2fs hits=%d source=character_queries=%r",
            chat_request_id, time.perf_counter() - t_pre,
            len(agent_bios), cleaned_queries,
        )
    else:
        # Agent didn't dispatch character_queries — no bio. The patch
        # flow's no-bio path handles the request (model uses its own
        # knowledge for the character). If a bio actually was needed,
        # this surfaces as a quality drop in the harness rather than
        # silently mis-injecting a phantom character via prefix match.
        agent_bios = []
        logger.info(
            "agent_patch[%s] bios_preflight=%.2fs hits=0 source=no_queries "
            "(agent did not pass character_queries; no fallback)",
            chat_request_id, time.perf_counter() - t_pre,
        )

    # When the agent provided character_queries, that list is
    # authoritative — the LLM read the user's intent. Don't union with
    # the client-side raw-text preflight (node_ctx.bios), because that
    # path is regex/prefix-driven and false-positives on garment words
    # ("micro bikini" → micro_uzi_(girls'_frontline)). Without queries,
    # we fall back to client-side bios as before.

    # Outfit-borrow augmentation: when the user request mentions
    # `<name>'s outfit|clothes|attire|...`, that character is an OUTFIT
    # SOURCE, not a subject. The agent typically (and correctly) leaves
    # such names out of character_queries — they aren't appearing in
    # the image. But the patch flow needs the source character's
    # outfit slot data to perform the borrow. Pull missing source bios
    # from node_ctx.bios (the client-side raw-text preflight) when
    # their name matches a borrow pattern AND they aren't already in
    # agent_bios. Narrowly scoped — only fires on explicit `'s outfit`
    # phrasing, not bare name mentions, so the false-positive risk that
    # justified killing client-side bios union is sidestepped.
    if cleaned_queries:
        # Pattern captures the words PRECEDING a possessive `'s <outfit-
        # word>`. Greedy match would grab too much (e.g. `change outfit
        # to chun-li` for "change outfit to chun-li's outfit"), so we
        # instead scan suffix windows of 1-3 tokens against client_bios
        # — same approach the natlang side's
        # `_match_foreign_character_outfit` uses.
        _borrow_pattern = re.compile(
            r"([A-Za-z][\w\s\-]*?)['’]s\s+"
            r"(outfit|clothes|attire|costume|getup|gear|uniform|"
            r"look|set|wardrobe|garb|wear)\b",
            re.IGNORECASE,
        )
        borrow_candidates: list[str] = []
        for m in _borrow_pattern.finditer(user_request or ""):
            phrase = m.group(1).strip().lower()
            tokens = re.findall(r"[\w\-]+", phrase)
            # Try last 1, 2, 3 tokens (chun-li / to chun-li /
            # outfit to chun-li). Last-token-first because character
            # names tend to be the rightmost in `<verb> ... <name>'s`.
            for window in (1, 2, 3):
                if window > len(tokens):
                    break
                candidate = " ".join(tokens[-window:])
                if len(candidate) >= 2:
                    borrow_candidates.append(candidate)
        if borrow_candidates:
            existing_tags = {(b or {}).get("tag", "").lower() for b in agent_bios}
            client_bios = node_ctx.get("bios") or []
            for cb in client_bios:
                if not isinstance(cb, dict):
                    continue
                tag = (cb.get("tag") or "").lower()
                if not tag or tag in existing_tags:
                    continue
                display = (cb.get("display") or "").lower()
                tag_norm = re.sub(r"[\s_\-]+", " ", tag).strip()
                display_norm = re.sub(r"[\s_\-]+", " ", display).strip()
                matched_via: str | None = None
                for cand in borrow_candidates:
                    cand_norm = re.sub(r"[\s_\-]+", " ", cand).strip()
                    if not cand_norm:
                        continue
                    if (cand_norm == tag_norm or
                            cand_norm == display_norm or
                            (len(cand_norm) >= 3 and
                             (cand_norm in tag_norm or
                              cand_norm in display_norm))):
                        matched_via = cand
                        break
                if matched_via:
                    augmented = dict(cb)
                    augmented["_outfit_source_only"] = True
                    agent_bios.append(augmented)
                    existing_tags.add(tag)
                    logger.info(
                        "agent_patch[%s] outfit-borrow: added %s as "
                        "outfit-source bio (matched name %r in request)",
                        chat_request_id, tag, matched_via,
                    )

    seen: set[str] = set()
    bios: list[dict] = []
    sources = agent_bios if cleaned_queries else (
        agent_bios + (node_ctx.get("bios") or [])
    )
    for b in sources:
        tag = (b or {}).get("tag") or ""
        if tag in seen:
            continue
        seen.add(tag)
        bios.append(b)

    payload = {
        "request_id": f"{chat_request_id}-patch-{uuid.uuid4().hex[:6]}",
        "node_prompt": node_prompt,
        "user_request": user_request,
        # Original user message — patch flow uses this to recover intents
        # the agent's paraphrasing may have dropped (cowboy outfit, leaning
        # pose, etc.). user_request is the agent's distilled imperative;
        # latest_user_text is the source of truth for what the user asked.
        "latest_user_text": latest_user_text or "",
        # Current-turn user message ONLY (no multi-turn concat). Used for
        # outfit-name scan and other current-turn intent classification
        # where prior-turn phrases would pollute matching ('cowboy outfit'
        # from last turn matching against this turn's 'french maid').
        "current_user_text": current_user_text or latest_user_text or "",
        "bios": bios,
        # Plumb character_queries through so the patch endpoint can
        # detect multi-character ADD transitions (2+ queries while the
        # existing node_prompt has 0-1 // Character: sections) and
        # route through a dedicated compose path that bypasses
        # hybrid/rails dispatch. Without this signal the patch flow
        # has to infer chars from the agent's prose request, which
        # gets unreliable on multi-char edits.
        "character_queries": cleaned_queries or [],
        "tag_format": node_ctx.get("tag_format") or "spaces",
        "model_hash": node_ctx.get("model_hash") or "",
        "prompt_style": node_ctx.get("prompt_style") or "tags",
        "is_standalone_main": bool(node_ctx.get("is_standalone_main")),
        "prompt_state": node_ctx.get("prompt_state"),
    }
    host = request.url.host or "127.0.0.1"
    port = request.url.port or 8188
    url = f"http://{host}:{port}/promptchain/ai/patch"
    timeout = aiohttp.ClientTimeout(connect=_AGENT_CONNECT_TIMEOUT, sock_read=_AGENT_READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            # On non-200 the patch endpoint may return text/plain (default
            # aiohttp 500 page) instead of JSON. Read the body as text
            # first and parse JSON only when content-type allows; surface
            # whichever shape we got as a useful error instead of crashing
            # with ContentTypeError on a 500.
            raw_body = await resp.text()
            if resp.status != 200:
                snippet = (raw_body or "").strip()[:300] or f"HTTP {resp.status}"
                raise RuntimeError(f"patch HTTP {resp.status}: {snippet}")
            try:
                data = json.loads(raw_body)
            except Exception as e:
                raise RuntimeError(f"patch returned non-JSON body: {e}: {raw_body[:200]}")
            return data


def _dispatch_list_model_styles(tool_input: dict, node_ctx: dict,
                                *, grounding: dict | None = None) -> dict:
    """Resolve a list_model_styles tool call against the user's configured
    style templates. Returns:
        {
          summary: str,    # legacy pre-rendered markdown (combined-call path)
          data: {          # structured shape (router+assistant path)
              styles: [{name, category, id, description}, ...],
              filter, count, arch,
          },
          count, filter, arch,
        }
    Caller chooses which shape to feed back as tool_result content.
    Caller owns WS emission + logging.

    `grounding`: pass when the caller already resolved arch/family (the CLI
    e2e harness needs this — `ai_api._build_grounding` can't see model
    config without folder_paths). Production omits it and lets us compute.
    """
    from . import prompts as _prompts
    model_hash = (node_ctx or {}).get("model_hash") or ""
    if grounding is None:
        grounding = ai_api._build_grounding(model_hash) if model_hash else {}
    arch = (grounding.get("architecture") or "").strip() or None
    family = (grounding.get("family") or "").strip() or None
    templates = _prompts.list_prompts(
        architecture=arch, family=family,
        model_hash=model_hash or None,
    )
    raw_rows = [p for p in templates if (p.get("name") or "").strip()]
    # Accept the legacy `category` field in case cached tool-use blocks
    # still carry it — same case-insensitive substring match against
    # BOTH name AND category, so 'hyperrealistic' matches the
    # Hyperrealistic template (which is in the Anime category).
    raw_filter = tool_input.get("filter") or tool_input.get("category") or ""
    flt = raw_filter.strip().lower()
    if flt:
        raw_rows = [
            p for p in raw_rows
            if flt in (p.get("name") or "").lower()
            or flt in (p.get("category") or "").lower()
        ]
    rows = [
        {
            "name": (p.get("name") or "").strip(),
            "category": (p.get("category") or "").strip(),
            "id": (p.get("id") or "").strip(),
            "text": p.get("text") or "",
        }
        for p in raw_rows
    ]
    # Build structured `data` rows alongside the legacy summary so the
    # router+assistant path (Stage 3) can consume clean JSON without
    # having to re-parse the markdown summary.
    data_styles: list[dict] = []
    for r in rows:
        try:
            pos_tokens, _neg = ai_api._parse_style_template_text(r["text"])
        except Exception:
            pos_tokens = []
        data_styles.append({
            "name": r["name"],
            "category": r["category"],
            "id": r["id"],
            "description": ", ".join(pos_tokens),
        })
    if not rows:
        header = "No matching style templates configured"
        if flt:
            header += f" for '{flt}'"
        header += f" on this model (arch={arch or 'unknown'})."
        summary = header
    elif len(rows) == 1:
        r = rows[0]
        try:
            pos_tokens, _neg = ai_api._parse_style_template_text(r["text"])
        except Exception:
            pos_tokens = []
        desc = ", ".join(pos_tokens) if pos_tokens else (
            "No description text available."
        )
        cat_str = (
            f" (in the **{r['category']}** category)" if r["category"] else ""
        )
        intro = (
            f"**{r['name']}**{cat_str} — yes, this template is "
            f"configured for your model."
        )
        outro = (
            "Reply with the intro line above, then a one-paragraph "
            "plain-English summary of what the style does based on this "
            "description (do not list the raw tokens verbatim): "
            f'"{desc}"'
        )
        summary = intro + "\n\n[" + outro + "]"
    else:
        scope = f"{flt} style" if flt else "style"
        bullets = [
            f"- **{r['name']}**"
            + (f" ({r['category']})" if r.get("category") and not flt else "")
            for r in rows
        ]
        intro = (
            f"This model has {len(rows)} {scope} "
            f"template{'' if len(rows) == 1 else 's'}:"
        )
        outro = (
            "Reply to the user with the intro line above followed by EXACTLY "
            "this bullet list, VERBATIM markdown (keep the `- ` dashes and "
            "the `**bold**` markers — they render as a real bulleted list "
            "in the chat UI). Do not add, remove, reorder, or paraphrase "
            "names. End with a one-line offer to apply one."
        )
        summary = intro + "\n" + "\n".join(bullets) + "\n\n[" + outro + "]"
    return {
        "summary": summary,
        "data": {
            "styles": data_styles,
            "filter": flt,
            "count": len(rows),
            "arch": arch,
        },
        "count": len(rows),
        "filter": flt,
        "arch": arch,
    }


_POPULATE_FILTER_KEYS = (
    "series", "hair_color", "eye_color", "hair_style",
    "body_type", "breast_size", "ass_size", "category",
    "gender", "sex",
)


def _dispatch_populate_inline_wildcards(tool_input: dict, node_ctx: dict,
                                        *, grounding: dict | None = None) -> dict:
    """Resolve a populate_inline_wildcards tool call against the current
    node's content. Deterministic — no LLM. Returns the populate() result
    dict: {content, summary, added, skipped, total_entries, source}.

    `grounding`: pass when arch/family already resolved (CLI harness); prod
    omits it so the populate core computes it from model_hash."""
    from . import inline_wildcard_populate as _iwp
    source = (tool_input.get("source") or "").strip()
    filters = {
        k: tool_input[k].strip()
        for k in _POPULATE_FILTER_KEYS
        if isinstance(tool_input.get(k), str) and tool_input[k].strip()
    }
    existing = (node_ctx or {}).get("node_prompt") or ""
    model_hash = (node_ctx or {}).get("model_hash") or ""
    include_outfit = bool(tool_input.get("include_outfit"))
    # The node's prompt_style drives the body format: "natural" -> prose,
    # anything else ("tags") -> danbooru tag bodies. Auto-detected; no AI
    # input needed.
    fmt = "natlang" if (node_ctx or {}).get("prompt_style") == "natural" else "tags"
    return _iwp.populate(
        source, existing_content=existing, filters=filters,
        model_hash=model_hash, grounding=grounding,
        include_outfit=include_outfit, fmt=fmt,
    )


def _dispatch_generate_subjects(tool_input: dict, node_ctx: dict) -> dict:
    """Resolve a generate_subjects tool call. Deterministic KB sampling —
    no LLM here (the agent already produced the recipe). Returns the
    generate_subjects() result dict."""
    from . import generate_subjects as _gs
    count = tool_input.get("count")
    try:
        count = int(count) if count is not None else 1
    except (TypeError, ValueError):
        count = 1
    mode = (tool_input.get("mode") or "").strip()
    if mode not in ("inline_wildcards", "single"):
        mode = "single" if count <= 1 else "inline_wildcards"
    fixed = tool_input.get("fixed")
    if not isinstance(fixed, dict):
        fixed = {}
    fmt = "natlang" if (node_ctx or {}).get("prompt_style") == "natural" else "tags"
    return _gs.generate_subjects(
        count=count,
        subject_kind=(tool_input.get("subject_kind") or "woman").strip() or "woman",
        fixed={k: str(v) for k, v in fixed.items() if v},
        outfit_policy=(tool_input.get("outfit_policy") or "random").strip(),
        mode=mode,
        existing_content=(node_ctx or {}).get("node_prompt") or "",
        fmt=fmt,
    )


def _dispatch_list_inline_wildcards(tool_input: dict, node_ctx: dict) -> dict:
    """Resolve a list_inline_wildcards call against the KB catalog. Pure
    deterministic resolve+emit; the agent only passed a category phrase."""
    from . import kb_list as _kb
    what = (tool_input.get("what") or "").strip()
    count = tool_input.get("count")
    try:
        count = int(count) if count not in (None, "") else None
    except (TypeError, ValueError):
        count = None
    existing = (node_ctx or {}).get("node_prompt") or ""
    fmt = "natlang" if (node_ctx or {}).get("prompt_style") == "natural" else "tags"
    return _kb.list_items(what, count=count, fmt=fmt, existing_content=existing)


def _format_tool_result_summary(patch_resp: dict) -> str:
    """Compact summary of the patch result. The chat agent only needs
    enough to write a 1-sentence reply to the user; dumping the whole
    sections JSON wastes context."""
    sections = patch_resp.get("sections") or []
    out_chars = len(patch_resp.get("output_text") or "")
    if not sections:
        # Empty sections + a real output = a from-scratch build, NOT a no-op.
        # Saying "no-op" here makes the agent narrate failure to the user.
        if out_chars > 0:
            return (f"Patch applied; prompt rebuilt ({out_chars} chars). "
                    "The change succeeded.")
        return "Patch applied; no changes were needed."
    lines = ["Patch applied. Sections:"]
    for s in sections:
        header = s.get("header") or "(unnamed)"
        polarity = "negative" if s.get("is_negative") else "positive"
        action = "removed" if s.get("is_removal") else "changed"
        if s.get("body_text"):
            preview = s["body_text"][:140]
            lines.append(f"- {header} ({polarity}, {action}): {preview}")
        else:
            tokens = s.get("tokens") or []
            sample = ", ".join(tokens[:6]) + (" …" if len(tokens) > 6 else "")
            lines.append(f"- {header} ({polarity}, {action}, {len(tokens)} tokens): {sample}")
    out_chars = len(patch_resp.get("output_text") or "")
    lines.append(f"Total prompt length: {out_chars} chars.")
    return "\n".join(lines)


# ── agent loop ────────────────────────────────────────────────────────

def _extract_current_user_text(history: list[dict]) -> str:
    """Return the text of the most recent user-role message in history.
    Used for current-turn intent classification (outfit-name scan etc.)
    where multi-turn _extract_recent_text would pollute with stale
    phrases from prior turns ('cowboy outfit' from a previous request
    matching against this turn's 'french maid')."""
    for msg in reversed(history or []):
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if t:
                        chunks.append(t)
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
    return ""


def _extract_recent_text(history: list[dict], max_messages: int = 6) -> str:
    """Pull text from the last N messages (any role) for bios preflight
    n-gram extraction. Multi-turn lookup-then-confirm flows hide the
    canonical character name in the assistant's prior reply (e.g. agent
    answered 'Cammy (Street Fighter) has blonde pigtails' on turn 1) —
    when the user then says 'yes', we need to scan the assistant text
    too so the bios preflight still catches Cammy.

    Skips tool_result blocks (they're patch dispatch responses, not
    human-meaningful text). Returns text joined newline-separated."""
    chunks: list[str] = []
    n = 0
    for msg in reversed(history or []):
        if n >= max_messages:
            break
        n += 1
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    chunks.append(t)
            # tool_use input's `request` field is also a useful signal —
            # it's what the agent decided to send to the patch model
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                req = inp.get("request") or ""
                if req:
                    chunks.append(req)
    return "\n".join(chunks)


def _estimate_message_tokens(msg: dict) -> int:
    """Rough token count for one canonical message. chars/4 is the usual
    English-text heuristic; exactness doesn't matter here — we only need a
    stable signal for the compaction watermark."""
    content = msg.get("content")
    if isinstance(content, str):
        return len(content) // 4
    if not isinstance(content, list):
        return 0
    chars = 0
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            chars += len(b.get("text") or "")
        elif bt == "tool_use":
            chars += len(json.dumps(b.get("input") or {}))
        elif bt == "tool_result":
            c = b.get("content")
            chars += len(c if isinstance(c, str) else json.dumps(c))
    return chars // 4


def _summarize_dropped_messages(msgs: list[dict]) -> list[str]:
    """Mechanical (non-LLM) recap of turns being dropped from the model's
    context. Keeps only the load-bearing signal: what the user asked and
    which edits got applied. Narration, tool_result bodies, and lookup
    calls are omitted — they carry no state the rebuilt node_prompt doesn't
    already convey."""
    lines: list[str] = []
    for m in msgs:
        role = m.get("role") or ""
        content = m.get("content")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and role == "user":
                txt = (b.get("text") or "").strip().replace("\n", " ")
                if txt:
                    lines.append(f'- You: "{txt[:140]}"')
            elif bt == "tool_use" and b.get("name") == "apply_prompt_patch":
                req = ((b.get("input") or {}).get("request") or "").strip().replace("\n", " ")
                if req:
                    lines.append(f'- Applied edit: "{req[:140]}"')
    return lines


def _compact_history(
    history: list[dict], summary: str, prov: dict,
) -> tuple[list[dict], str]:
    """Watermark compaction. Returns (history_to_send, updated_summary).

    Below budget: history unchanged, summary unchanged. Above budget: drop
    the oldest turns up to a clean user-text boundary (so the retained tail
    is a self-contained, role-alternating conversation with no orphaned
    tool_use/tool_result), and append a mechanical recap of the dropped
    turns to the running summary. The summary is injected into the system
    prompt by the caller, NOT back into history — that keeps role
    alternation and tool pairing intact."""
    if not history:
        return history, summary
    # Keyless providers (local Ollama / llama.cpp) get the tight budget;
    # only cloud providers with a key have the large window.
    is_local = bool(prov.get("is_ollama")) or not prov.get("api_key")
    budget = _HISTORY_BUDGET_LOCAL if is_local else _HISTORY_BUDGET_CLOUD
    total = sum(_estimate_message_tokens(m) for m in history)
    if total <= budget:
        return history, summary

    # User-text boundaries are the only safe cut points: a real user prompt,
    # never a tool_result-carrying user turn.
    bounds = []
    for i, m in enumerate(history):
        if (m.get("role") or "") != "user":
            continue
        c = m.get("content")
        has_text = isinstance(c, str) or (
            isinstance(c, list)
            and any(isinstance(b, dict) and b.get("type") == "text" for b in c)
        )
        if has_text:
            bounds.append(i)
    if len(bounds) <= 1:
        # Only the current turn exists as a boundary — nothing safe to drop.
        return history, summary

    target = int(budget * _HISTORY_RECENT_TARGET)
    bound_set = set(bounds)
    cut = bounds[-1]  # worst case: keep only the current user turn onward
    acc = 0
    for i in range(len(history) - 1, -1, -1):
        acc += _estimate_message_tokens(history[i])
        if i in bound_set and acc <= target:
            cut = i  # keep as much as fits; loops downward → smallest qualifying i
    if cut <= 0:
        return history, summary

    dropped, kept = history[:cut], history[cut:]
    new_lines = _summarize_dropped_messages(dropped)
    if new_lines:
        joined = "\n".join(new_lines)
        summary = f"{summary}\n{joined}".strip() if summary else joined
        if len(summary) > _HISTORY_SUMMARY_MAX_CHARS:
            summary = summary[-_HISTORY_SUMMARY_MAX_CHARS:]
            nl = summary.find("\n")  # drop the now-partial leading line
            if nl != -1:
                summary = summary[nl + 1:]
    return kept, summary


# ── image-turn helpers ─────────────────────────────────────────────

_IMAGE_REVERSE_PROMPT_INSTRUCTION = (
    "Create a Stable-Diffusion prompt that recreates this image. Identify "
    "the character and franchise by name if you recognize them, then call "
    "apply_prompt_patch describing the subject, outfit, pose, setting, and "
    "art style."
)


def _set_last_user_text(history: list[dict], text: str) -> None:
    """Replace (or insert) the text of the most recent user message."""
    for msg in reversed(history):
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = text
            return
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    b["text"] = text
                    return
            content.insert(0, {"type": "text", "text": text})
        return


def _attach_images_to_last_user(history: list[dict], image_blocks: list[dict]):
    """Append Anthropic image blocks to the most recent user message's
    content (for this model call only). Returns the mutated message so the
    caller can strip the blocks back out before persisting, or None."""
    for msg in reversed(history):
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}] if content else []
            msg["content"] = content
        if isinstance(content, list):
            content.extend(image_blocks)
            return msg
        return None
    return None


def _extract_doc_from_uploads(image_hashes: list[str]) -> str | None:
    """Recover the COMPILED prompt that actually rendered the first uploaded
    image carrying ComfyUI metadata. `extract_prompts_from_file` traces the
    sampler's conditioning back to the PromptChain node feeding it and reads
    its pcrCompiledOutput — i.e. the resolved prompt, not the editable source.

    The compiled output is what we want: a node's raw `inputs.prompt` may hold
    unresolved inline-wildcard libraries (`::Style::…`) or be one piece of a
    multi-node chain, neither of which is what produced the image."""
    from . import chat_uploads
    from .load_image_prompts import extract_prompts_from_file
    for h in image_hashes:
        p = chat_uploads.resolve_upload_path(h)
        if not p:
            continue
        pos, neg = extract_prompts_from_file(str(p))
        if pos:
            return f"{pos}\n\nNegative Prompt:\n{neg}" if neg else pos
    return None


def _doc_to_sections(doc: str) -> list[dict]:
    """Render a recovered prompt doc as proposal-card diff sections: the
    positive body as prose, the negative as removable-style chips (matching
    how the patch flow shapes negatives for the card)."""
    parts = re.split(r"(?im)^\s*Negative Prompt:\s*$", doc, maxsplit=1)
    pos = parts[0].strip()
    neg = parts[1].strip() if len(parts) > 1 else ""
    sections: list[dict] = []
    if pos:
        sections.append({
            "header": "From image", "body_text": pos,
            "is_negative": False, "is_removal": False,
        })
    if neg:
        tokens = [t.strip() for t in re.split(r"[,\n]", neg) if t.strip()]
        sections.append({
            "header": "Negative", "tokens": tokens,
            "is_negative": True, "is_removal": False,
        })
    return sections


def _metadata_prompt_response(request_id: str, history: list[dict],
                              history_summary: str, doc: str,
                              mode: str) -> dict:
    """Build the chat response for the metadata short-circuit: an assistant
    turn carrying a proposal whose output_text IS the recovered prompt. No
    model call — this is exact data lifted from the image."""
    now_ms = int(time.time() * 1000)
    pid = f"meta_{uuid.uuid4().hex[:12]}"
    # auto / auto-run apply immediately (frontend honors status=="applied");
    # ask waits for the user's Accept.
    status = "applied" if mode in ("auto", "auto-run") else "pending"
    proposal = {
        "tool_input": {"request": "recover prompt from image metadata"},
        "tool_result": {"output_text": doc, "sections": _doc_to_sections(doc),
                        "prompt_state": None},
        "status": status,
        "createdAt": now_ms,
    }
    text = ("This image was generated with PromptChain — here's its prompt, "
            "ready to load.")
    turn = {"role": "assistant", "text": text, "proposalId": pid,
            "timestamp": now_ms}
    history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    _emit_agent(request_id, "agent_done")
    return {
        "request_id": request_id,
        "new_turns": [turn],
        "new_proposals": {pid: proposal},
        "history_for_persistence": history,
        "history_summary": history_summary,
    }


# ── image aspect-extraction rails ──────────────────────────────────
# The patch/rails pipeline that builds the prompt is TEXT-ONLY — it never
# sees the image. So when the user says "use this pose / outfit", the chat
# agent can't just write "this pose" deictically (the downstream pipeline
# has nothing to resolve). We extract concrete text from the image FIRST
# via decomposed per-region vision sub-calls — focused questions beat one
# omnibus prompt (an 8B glosses/hallucinates on "describe the whole pose").
# Validated: 8B+rail ≈ 30B quality, far better than 8B single-shot.

_ASPECT_SYS = (
    "/no_think\nYou are a precise visual describer. Answer ONLY what is "
    "asked, in plain words, no preamble, no markdown."
)

# Pose = pure body posture: NO camera/framing and NO furniture/surface
# (see feedback_poses_pure_body_posture — users pick framing per-render).
_POSE_RAIL = [
    ("posture", "In one short clause, the subject's overall body posture "
     "(sitting / standing / kneeling / lying / crouching). Do NOT mention "
     "furniture, the floor, or what they rest on."),
    ("legs", "Describe ONLY the legs and feet position in one short clause "
     "(bent / extended / raised / crossed; are the soles toward the viewer?). "
     "Ignore everything else."),
    ("arms", "Describe ONLY the arms and hands position in one short clause "
     "(resting where, holding what, raised / lowered). Ignore everything else."),
    ("torso_head", "Describe ONLY the torso lean and the head tilt and gaze "
     "direction in one short clause (e.g. 'leaning back, head tilted down, "
     "looking at the viewer'). Do NOT mention eye colour, face, or hair."),
]

_OUTFIT_RAIL = [
    ("garment", "Describe ONLY the clothing the subject wears: garment type, "
     "cut, neckline, straps, length, any slits, material, and colour, in one "
     "sentence. Ignore worn accessories, pose, body, and background."),
    ("footwear", "Describe ONLY the footwear (shoes, boots, sandals, socks, "
     "stockings). If the feet are bare with NO footwear, reply 'barefoot'. "
     "One short phrase."),
    ("accessories", "Name the specific worn accessories you can see (for "
     "example: eyepatch, gold earrings, hair ornament, pink nail polish). "
     "Output the actual items, NOT category words like 'jewelry' or "
     "'eyewear'. Do not mention footwear here. Reply 'none' if there are none."),
]

_STYLE_RAIL = [
    ("medium", "In one short phrase, the overall art / rendering style and "
     "medium (e.g. anime, photorealistic, 3D render, oil painting, "
     "cel-shaded, watercolour). Ignore the subject, clothing, and pose."),
    ("lighting", "In one short phrase, the lighting and render quality (e.g. "
     "cinematic lighting, soft studio light, high detail, depth of field, "
     "film grain). Ignore the subject, clothing, and pose."),
]

_POSE_REF_RE = re.compile(
    r"\b(pose|posture|posing|positioned|stance)\b", re.IGNORECASE)
_OUTFIT_REF_RE = re.compile(
    r"\b(outfit|clothes|clothing|wearing|attire|costume|wardrobe)\b",
    re.IGNORECASE)
_STYLE_REF_RE = re.compile(
    r"\b(style|aesthetic|rendering|cel.?shad\w*|painterly|art.?style)\b",
    re.IGNORECASE)


def _detect_reference_aspects(text: str) -> dict:
    """Which image aspects the user asked to borrow ('use this pose', 'and the
    outfit', 'this style'). Empty dict → no specific aspect (full recreate)."""
    text = text or ""
    return {
        "pose": bool(_POSE_REF_RE.search(text)),
        "outfit": bool(_OUTFIT_REF_RE.search(text)),
        "style": bool(_STYLE_REF_RE.search(text)),
    }


async def _run_aspect_rail(request_id: str, image_datas: list[dict],
                           config: dict, provider: str,
                           rail: list[tuple], sub: str) -> str:
    """Run a decomposed extraction rail: one focused vision call per facet,
    assembled comma-separated. Empty/'none' facets are dropped."""
    pieces: list[str] = []
    for key, question in rail:
        try:
            ans = await ai_api._call_provider_complete(
                f"{request_id}-{sub}-{key}", provider, config,
                _ASPECT_SYS, question, image_datas,
            )
        except Exception:
            logger.warning("agent[%s] aspect-rail %s/%s failed",
                           request_id, sub, key, exc_info=True)
            ans = ""
        ans = " ".join((ans or "").split()).strip().rstrip(".")
        if ans and ans.lower() != "none":
            pieces.append(ans)
    return ", ".join(pieces)


# Text-extraction over a ComfyUI/PromptChain render's EMBEDDED prompt. This
# is ground truth (the authored pose/outfit, not a pixel re-interpretation),
# it's faster than vision, and the exclusion clause is how we avoid pulling
# the reference image's character onto the user's subject ("Cammy, not Juri").
_TEXT_ASPECT_SYS = (
    "/no_think\nYou extract one aspect from an image-generation prompt. "
    "Return only a short comma-separated phrase, no preamble, no markdown."
)
_TEXT_POSE_INSTR = (
    "From the prompt below, extract ONLY the body pose and limb/feet "
    "position. EXCLUDE the character name, identity, body type, hair, eyes, "
    "face, art/render style, and camera framing. Reply 'none' if there is no "
    "pose."
)
_TEXT_OUTFIT_INSTR = (
    "From the prompt below, extract ONLY the clothing/outfit and footwear "
    "(say 'barefoot' if the feet are bare). EXCLUDE the character name, "
    "identity, body type, hair, eyes, face, and art/render style. Reply "
    "'none' if there is no clothing."
)
_TEXT_STYLE_INSTR = (
    "From the prompt below, extract ONLY the art / rendering style, medium, "
    "lighting, and render-quality phrases (e.g. 'hyperrealistic anime, "
    "cinematic lighting, sharp focus'). EXCLUDE the character name, identity, "
    "body, clothing, and pose. Reply 'none' if there is no style."
)


async def _text_extract_aspect(request_id: str, doc: str, sub: str,
                               instruction: str, config: dict,
                               provider: str) -> str:
    try:
        ans = await ai_api._call_provider_complete(
            f"{request_id}-meta-{sub}", provider, config,
            _TEXT_ASPECT_SYS, f"{instruction}\n\nPROMPT: {doc}", [],
        )
    except Exception:
        logger.warning("agent[%s] metadata aspect %s failed",
                       request_id, sub, exc_info=True)
        return ""
    ans = " ".join((ans or "").split()).strip().rstrip(".")
    return "" if ans.lower() == "none" else ans


async def _extract_reference_aspects(
    request_id: str, image_hashes: list[str], aspects: dict,
    config: dict, provider: str,
) -> dict:
    """Extract the requested pose/outfit aspects from the first uploaded image.
    Prefers TEXT extraction over a ComfyUI render's embedded prompt (ground
    truth, character excluded); falls back to the decomposed VISION rails for
    external images with no metadata. Returns {pose?, outfit?}."""
    out: dict = {}

    doc = _extract_doc_from_uploads(image_hashes)
    if doc:
        if aspects.get("pose"):
            txt = await _text_extract_aspect(request_id, doc, "pose",
                                             _TEXT_POSE_INSTR, config, provider)
            if txt:
                out["pose"] = txt
        if aspects.get("outfit"):
            txt = await _text_extract_aspect(request_id, doc, "outfit",
                                             _TEXT_OUTFIT_INSTR, config, provider)
            if txt:
                out["outfit"] = txt
        if aspects.get("style"):
            txt = await _text_extract_aspect(request_id, doc, "style",
                                             _TEXT_STYLE_INSTR, config, provider)
            if txt:
                out["style"] = txt
        if out:
            logger.info("agent[%s] extracted aspects from image METADATA "
                        "(no vision)", request_id)
            return out

    # No metadata (external image) — fall back to decomposed vision rails.
    from . import chat_uploads
    image_datas = [
        d for h in image_hashes if (d := chat_uploads.load_image_data(h))
    ]
    if not image_datas:
        return {}
    if aspects.get("pose"):
        txt = await _run_aspect_rail(request_id, image_datas, config, provider,
                                     _POSE_RAIL, "pose")
        if txt:
            out["pose"] = txt
    if aspects.get("outfit"):
        txt = await _run_aspect_rail(request_id, image_datas, config, provider,
                                     _OUTFIT_RAIL, "outfit")
        if txt:
            out["outfit"] = txt
    if aspects.get("style"):
        txt = await _run_aspect_rail(request_id, image_datas, config, provider,
                                     _STYLE_RAIL, "style")
        if txt:
            out["style"] = txt
    if out:
        logger.info("agent[%s] extracted aspects via VISION rails", request_id)
    return out


def _compose_reference_instruction(original: str, extracted: dict) -> str:
    """Fold concrete extracted aspects back into the user's instruction so the
    text-only patch pipeline gets real descriptions instead of deixis ('this
    pose'). Keeps the original (which names the subject) and appends the
    resolved outfit/pose."""
    parts = [original.strip().rstrip(".")]
    if extracted.get("outfit"):
        parts.append(f"Use this exact outfit from the reference image: "
                     f"{extracted['outfit']}")
    if extracted.get("pose"):
        parts.append(f"Use this exact body pose from the reference image: "
                     f"{extracted['pose']}")
    if extracted.get("style"):
        parts.append(f"Use this exact art style from the reference image: "
                     f"{extracted['style']}")
    return ". ".join(parts) + "."


async def _resolve_provider(config: dict) -> dict:
    """Decide which streaming path to use based on the user's AI config.
    Mirrors the dispatch in ai_api._run_generation. Returns:
        {kind: "claude"|"openai", api_key, model, base_url?, is_ollama?}
    or raises RuntimeError with a user-facing message."""
    provider = (config.get("provider") or "").strip()
    if provider == "cloud":
        cloud = config.get("cloud") or {}
        service = (cloud.get("service") or "claude").strip()
        api_key = (cloud.get("api_key") or "").strip()
        model = (cloud.get("model") or "").strip()
        if not api_key:
            raise RuntimeError("Cloud API key not configured.")
        if not model:
            raise RuntimeError("Cloud model not configured.")
        if service == "claude":
            return {"kind": "claude", "api_key": api_key, "model": model}
        base_url = ai_api._cloud_base_url(cloud)
        if not base_url:
            raise RuntimeError("Cloud base URL missing.")
        return {
            "kind": "openai", "api_key": api_key, "model": model,
            "base_url": base_url, "is_ollama": False,
        }
    if provider == "local":
        local = config.get("local") or {}
        base_url = (local.get("base_url") or "").strip().rstrip("/")
        model = (local.get("model") or "").strip()
        if not base_url:
            raise RuntimeError("Local base URL not configured.")
        if not model:
            raise RuntimeError("Local model not configured.")
        ollama_root = ai_api._ollama_root(base_url)
        is_ollama = await ai_api._is_ollama(ollama_root)
        return {
            "kind": "openai", "api_key": None, "model": model,
            "base_url": base_url, "is_ollama": is_ollama,
        }
    raise RuntimeError(f"Unknown provider {provider!r}. Configure cloud or local in AI settings.")


async def _stream_turn(request_id: str, prov: dict,
                       system_prompt: str, history: list[dict],
                       *, tools: list[dict] | None = None) -> dict:
    """Single turn against the configured provider. `tools=None` uses the
    default `_AGENT_TOOLS` surface; pass `tools=[]` for a narration-only
    call (assistant pass — no tool selection)."""
    if prov["kind"] == "claude":
        return await _stream_claude_with_tools(
            request_id, prov["api_key"], prov["model"], system_prompt, history,
            tools=tools,
        )
    return await _stream_openai_compat_with_tools(
        request_id, prov["base_url"], prov["model"], system_prompt, history,
        api_key=prov.get("api_key"), is_ollama=bool(prov.get("is_ollama")),
        tools=tools,
    )


async def _run_agent_loop(request: web.Request, body: dict) -> dict:
    request_id = body.get("request_id") or uuid.uuid4().hex
    mode = (body.get("mode") or "ask").strip()
    node_ctx = body.get("node_ctx") or {}
    history: list[dict] = list(body.get("history") or [])
    history_summary: str = (body.get("history_summary") or "").strip()

    # Per-stage timing dict — emitted as a one-line summary at the end so
    # we can see where time goes without parsing the whole log. Pattern
    # mirrors ai_api._api_patch's `_timing` collector.
    timing: dict[str, float] = {}
    t_total_start = time.perf_counter()

    config = ai_api._load_config()
    try:
        prov = await _resolve_provider(config)
    except RuntimeError as e:
        return {"error": str(e)}
    timing["setup"] = time.perf_counter() - t_total_start

    # Bound the model's context: drop the oldest turns past the budget and
    # fold them into history_summary (injected into the system prompt below).
    # No-op for short conversations.
    _pre_len = len(history)
    history, history_summary = _compact_history(history, history_summary, prov)
    if len(history) != _pre_len:
        logger.info(
            "agent[%s] compacted history: %d→%d msgs, summary=%dch",
            request_id, _pre_len, len(history), len(history_summary),
        )

    # Recent text from the last few history messages (user + assistant)
    # for bios preflight n-gram extraction. Used in every patch dispatch
    # so character mentions land bios regardless of WHICH turn introduced
    # them — important for lookup-then-confirm flows where turn 1's
    # assistant reply identifies the character and turn 2's user message
    # is just 'yes'.
    latest_user_text = _extract_recent_text(history, max_messages=6)
    current_user_text = _extract_current_user_text(history)

    # ── Uploaded images (current turn) ──────────────────────────────
    # The frontend sends just-uploaded image(s) as {hash} refs in `images`;
    # pixels live on disk under the user folder, never in the workflow JSON.
    # We rehydrate them into the LAST user message for THIS model call only
    # (caption-once: older turns never re-send bytes, and
    # history_for_persistence is stripped back to text below).
    image_hashes = [
        im["hash"] for im in (body.get("images") or [])
        if isinstance(im, dict) and im.get("hash")
    ]
    injected_image_msg = None
    has_image = False
    if image_hashes:
        # Metadata pre-pass: a ComfyUI/PromptChain render embeds its own
        # prompt — recover it exactly (no vision call) and offer it as a
        # proposal. Only when the user added no text of their own; if they
        # asked something ("who is this?"), fall through to the vision path.
        if not current_user_text.strip():
            meta_doc = _extract_doc_from_uploads(image_hashes)
            if meta_doc:
                logger.info("agent[%s] recovered prompt from image metadata "
                            "(%d chars), skipping vision", request_id, len(meta_doc))
                return _metadata_prompt_response(
                    request_id, history, history_summary, meta_doc, mode,
                )

        # Aspect-reference ("use this pose / outfit"): the patch pipeline is
        # text-only, so extract concrete pose/outfit text from the image NOW
        # via the decomposed rails, fold it into the instruction, and consume
        # the image — the agent then runs a normal text edit with real
        # descriptions instead of deferring with "this pose".
        image_consumed = False
        aspects = _detect_reference_aspects(current_user_text)
        if current_user_text.strip() and (aspects["pose"] or aspects["outfit"]):
            # Carry the reference's style by default on any image-reference
            # build. Without a // Style section the rails auto-seed the
            # model's default (often photorealistic), which clashes with an
            # anime reference. The reference's own style is the coherent
            # default; the user can still override ('...but photorealistic').
            aspects["style"] = True
            extracted = await _extract_reference_aspects(
                request_id, image_hashes, aspects,
                config, config.get("provider") or "",
            )
            if extracted:
                rewritten = _compose_reference_instruction(current_user_text, extracted)
                _set_last_user_text(history, rewritten)
                current_user_text = rewritten
                image_consumed = True
                logger.info("agent[%s] extracted image aspects %s; rewrote "
                            "instruction to %r", request_id,
                            list(extracted.keys()), rewritten[:200])

        if not image_consumed:
            from . import chat_uploads
            image_blocks = [
                blk for h in image_hashes
                if (blk := chat_uploads.load_image_block(h))
            ]
            if image_blocks:
                # Image with no instruction = "make me a prompt for this". An
                # 8B left alone narrates instead of calling the tool, so make
                # the reverse-prompt intent explicit.
                if not current_user_text.strip():
                    _set_last_user_text(history, _IMAGE_REVERSE_PROMPT_INSTRUCTION)
                    current_user_text = _IMAGE_REVERSE_PROMPT_INSTRUCTION
                injected_image_msg = _attach_images_to_last_user(history, image_blocks)
                has_image = injected_image_msg is not None
                logger.info("agent[%s] attached %d image(s) for vision",
                            request_id, len(image_blocks))

    # Log the user's most recent literal message so debugging "why
    # didn't the agent dispatch?" doesn't require guessing what they
    # typed. Truncated for log volume.
    _user_only_text = _extract_recent_text(history, max_messages=1)
    if _user_only_text:
        logger.info(
            "agent[%s] user_text=%r",
            request_id, _user_only_text[:200],
        )

    # Knowledge preload: when the latest user message LOOKS LIKE A
    # QUESTION (ends with `?` or starts with `what / who / how / which
    # / tell me about / describe`), run a strict-filtered match-
    # characters preflight and surface any matched bios in the system
    # prompt as authoritative character knowledge. Solves the failure
    # mode where 8B answers "what color hair does cammy have?" from
    # world knowledge and gets it wrong.
    #
    # GATED on question-shape (not always-on): the bio block adds
    # ~200-500 tokens per matched bio to the system prompt. Always-on
    # caused multi-character scenarios (cammy + chun-li borrow) to
    # regress because both bios got surfaced and shifted the agent's
    # paraphrasing behavior. For tool calls, the agent's
    # `character_queries` field still drives bio resolution
    # downstream — this block only feeds the text-response path,
    # which is exactly the path question-shape detection identifies.
    #
    # The strict filter (`_filter_bios_for_agent_preload`) drops
    # single-word prefix matches (`night→night_angel` etc.) that
    # polluted the pre-T4 raw-text preflight.
    preload_bios: list[dict] = []
    if _user_only_text and _looks_like_question(_user_only_text):
        try:
            raw_bios = await _preflight_bios(
                request, _user_only_text, "",
            )
            preload_bios = _filter_bios_for_agent_preload(
                raw_bios, _user_only_text,
            )
            if preload_bios:
                logger.info(
                    "agent[%s] knowledge preload: bios=%d after filter "
                    "(raw=%d): %s",
                    request_id, len(preload_bios), len(raw_bios),
                    ", ".join((b.get("tag") or "?") for b in preload_bios),
                )
        except Exception:
            logger.warning("agent[%s] knowledge preload failed",
                           request_id, exc_info=True)
    new_proposals: dict[str, dict] = {}
    new_assistant_blocks: list[dict] = []

    # Single-pass agent loop (Copilot / Continue.dev pattern). One system
    # prompt covers routing + narration. Tools attached every call. Model
    # decides tool-vs-text on the fly; we loop until it stops emitting
    # tool_use blocks. The A/B harness (_harness/chat_e2e_probe.py) showed this
    # pattern is ~16% faster than the prior two-pass split with identical
    # pass rate across 8 fixtures — primarily because Ollama can reuse
    # the KV cache across calls within a turn (same system prompt both
    # times). The two-pass _router_system_prompt / _assistant_system_prompt
    # helpers are kept for that harness only.
    combined_prompt = _combined_system_prompt(
        node_ctx, preload_bios, history_summary, has_image=has_image,
    )
    for hop in range(_AGENT_MAX_HOPS):
        t_hop = time.perf_counter()
        result = await _stream_turn(
            request_id, prov, combined_prompt, history,
        )
        timing[f"hop{hop+1}_model"] = time.perf_counter() - t_hop

        tool_blocks = [
            b for b in result["content"] if b.get("type") == "tool_use"
        ]

        # One-line decision telemetry per hop.
        if tool_blocks:
            for b in tool_blocks:
                inp = b.get("input") or {}
                arg_preview = (
                    (inp.get("request") or inp.get("filter") or "")[:80]
                )
                logger.info(
                    "agent_hop[%s] hop=%d tool=%s args=%r",
                    request_id, hop + 1, b.get("name"), arg_preview,
                )
        else:
            text_preview = "".join(
                b.get("text", "") for b in result["content"]
                if b.get("type") == "text"
            )[:80]
            logger.info(
                "agent_hop[%s] hop=%d text=%r",
                request_id, hop + 1, text_preview,
            )

        # Persist this hop's assistant turn in history regardless.
        if result["content"]:
            history.append({"role": "assistant", "content": result["content"]})
        new_assistant_blocks.extend(result["content"])

        if not tool_blocks:
            # Terminal hop: model emitted text only. That's the reply.
            break

        # Dispatch tool calls and build Anthropic-canonical tool_result
        # blocks. `_to_openai_messages` translates these to OpenAI
        # role:tool / tool_call_id shape at the wire boundary.
        tool_result_blocks: list[dict] = []
        for block in tool_blocks:
            proposal_id = block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
            tool_name = block.get("name")
            tool_input = block.get("input") or {}

            if tool_name == "list_model_styles":
                try:
                    res = _dispatch_list_model_styles(tool_input, node_ctx)
                    logger.info(
                        "agent[%s] list_model_styles arch=%r filter=%r "
                        "returned=%d",
                        request_id, res["arch"],
                        res["filter"] or None, res["count"],
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": json.dumps(res["data"], indent=2),
                    })
                    _emit_agent(
                        request_id, "agent_tool_result",
                        proposal_id=proposal_id,
                        tool_name="list_model_styles",
                        count=res["count"],
                    )
                except Exception as e:
                    logger.exception(
                        "agent[%s] list_model_styles failed", request_id,
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": f"list_model_styles failed: {e}",
                        "is_error": True,
                    })
                continue

            if tool_name == "populate_inline_wildcards":
                now_ms = int(time.time() * 1000)
                try:
                    res = _dispatch_populate_inline_wildcards(
                        tool_input, node_ctx,
                    )
                    logger.info(
                        "agent[%s] populate_inline_wildcards source=%r "
                        "added=%d skipped=%d total=%d",
                        request_id, res.get("source"),
                        len(res.get("added") or []),
                        len(res.get("skipped") or []),
                        res.get("total_entries", 0),
                    )
                    # Carry the new node content as output_text so the
                    # frontend applies it the same way it applies an
                    # apply_prompt_patch proposal. No diff sections — the
                    # change is an append, summarized in `summary`.
                    proposal = {
                        "tool_input": tool_input,
                        "tool_result": {
                            "output_text": res["content"],
                            "sections": [],
                            "pipeline": "inline-wildcard-populate",
                            "added": res["added"],
                            "skipped": res["skipped"],
                            "summary": res["summary"],
                        },
                        "status": "pending" if mode == "ask" else "applied",
                        "createdAt": now_ms,
                    }
                    if proposal["status"] == "applied":
                        proposal["appliedAt"] = now_ms
                    new_proposals[proposal_id] = proposal
                    _emit_agent(
                        request_id, "agent_tool_result",
                        proposal_id=proposal_id,
                        added=len(res.get("added") or []),
                        applied=(proposal["status"] == "applied"),
                    )
                    # Only the compact summary goes back to the agent —
                    # the full content (could be dozens of entries) stays
                    # in the proposal, keeping narration context small.
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": res["summary"],
                    })
                except Exception as e:
                    logger.exception(
                        "agent[%s] populate_inline_wildcards failed",
                        request_id,
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": f"populate_inline_wildcards failed: {e}",
                        "is_error": True,
                    })
                continue

            if tool_name == "generate_subjects":
                now_ms = int(time.time() * 1000)
                try:
                    res = _dispatch_generate_subjects(tool_input, node_ctx)
                    logger.info(
                        "agent[%s] generate_subjects mode=%r count=%d seed=%s",
                        request_id, res.get("mode"),
                        len(res.get("added") or []), res.get("seed"),
                    )
                    proposal = {
                        "tool_input": tool_input,
                        "tool_result": {
                            "output_text": res["content"],
                            "sections": [],
                            "pipeline": "generate-subjects",
                            "mode": res.get("mode"),
                            "added": res.get("added"),
                            "seed": res.get("seed"),
                            "summary": res["summary"],
                        },
                        "status": "pending" if mode == "ask" else "applied",
                        "createdAt": now_ms,
                    }
                    if proposal["status"] == "applied":
                        proposal["appliedAt"] = now_ms
                    new_proposals[proposal_id] = proposal
                    _emit_agent(
                        request_id, "agent_tool_result",
                        proposal_id=proposal_id,
                        added=len(res.get("added") or []),
                        applied=(proposal["status"] == "applied"),
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": res["summary"],
                    })
                except Exception as e:
                    logger.exception(
                        "agent[%s] generate_subjects failed", request_id,
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": f"generate_subjects failed: {e}",
                        "is_error": True,
                    })
                continue

            if tool_name == "list_inline_wildcards":
                now_ms = int(time.time() * 1000)
                try:
                    res = _dispatch_list_inline_wildcards(tool_input, node_ctx)
                    logger.info(
                        "agent[%s] list_inline_wildcards what=%r -> %s/%s "
                        "added=%d",
                        request_id, tool_input.get("what"),
                        res.get("domain"), res.get("group"),
                        len(res.get("added") or []),
                    )
                    if res.get("added"):
                        proposal = {
                            "tool_input": tool_input,
                            "tool_result": {
                                "output_text": res["content"],
                                "sections": [],
                                "pipeline": "kb-list",
                                "domain": res.get("domain"),
                                "group": res.get("group"),
                                "added": res["added"],
                                "summary": res["summary"],
                            },
                            "status": "pending" if mode == "ask" else "applied",
                            "createdAt": now_ms,
                        }
                        if proposal["status"] == "applied":
                            proposal["appliedAt"] = now_ms
                        new_proposals[proposal_id] = proposal
                        _emit_agent(
                            request_id, "agent_tool_result",
                            proposal_id=proposal_id,
                            added=len(res["added"]),
                            applied=(proposal["status"] == "applied"),
                        )
                    # No-match (or all-present): no proposal, just the note
                    # back so the agent can tell the user.
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": res["summary"],
                    })
                except Exception as e:
                    logger.exception(
                        "agent[%s] list_inline_wildcards failed", request_id,
                    )
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": proposal_id,
                        "content": f"list_inline_wildcards failed: {e}",
                        "is_error": True,
                    })
                continue

            if tool_name != "apply_prompt_patch":
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": proposal_id,
                    "content": f"Unknown tool '{tool_name}' — not invoked.",
                    "is_error": True,
                })
                continue

            patch_request = (tool_input.get("request") or "").strip()
            if not patch_request:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": proposal_id,
                    "content": "apply_prompt_patch rejected: empty `request` field.",
                    "is_error": True,
                })
                continue

            now_ms = int(time.time() * 1000)
            try:
                t_patch = time.perf_counter()
                raw_cq = tool_input.get("character_queries")
                if isinstance(raw_cq, str):
                    raw_cq = [raw_cq]
                if not isinstance(raw_cq, list):
                    raw_cq = None
                patch_resp = await _call_patch_internal(
                    request, node_ctx, patch_request, request_id,
                    latest_user_text=latest_user_text,
                    current_user_text=current_user_text,
                    character_queries=raw_cq,
                )
                timing[f"hop{hop+1}_patch"] = time.perf_counter() - t_patch
                patch_summary = _format_tool_result_summary(patch_resp)
                proposal = {
                    "tool_input": tool_input,
                    "tool_result": patch_resp,
                    "status": "pending" if mode == "ask" else "applied",
                    "createdAt": now_ms,
                }
                if proposal["status"] == "applied":
                    proposal["appliedAt"] = now_ms
                new_proposals[proposal_id] = proposal
                _emit_agent(
                    request_id, "agent_tool_result",
                    proposal_id=proposal_id,
                    sections_count=len(patch_resp.get("sections") or []),
                    applied=(proposal["status"] == "applied"),
                )
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": proposal_id,
                    "content": "apply_prompt_patch result:\n" + patch_summary,
                })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "agent[%s] patch dispatch failed", request_id,
                )
                new_proposals[proposal_id] = {
                    "tool_input": tool_input,
                    "tool_result": {"error": str(e)},
                    "status": "failed",
                    "createdAt": now_ms,
                }
                _emit_agent(
                    request_id, "agent_tool_result",
                    proposal_id=proposal_id, applied=False, error=str(e),
                )
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": proposal_id,
                    "content": f"apply_prompt_patch failed: {e}",
                    "is_error": True,
                })

        # Append the tool_results as a user-turn block. Next hop's call
        # sees the full canonical exchange in history; the model loops
        # back to narrate or call another tool.
        history.append({"role": "user", "content": tool_result_blocks})
    else:
        # for-else: hop limit hit without a final text turn.
        logger.warning(
            "agent[%s] hit max_hops=%d without final text reply",
            request_id, _AGENT_MAX_HOPS,
        )
        _emit_agent(
            request_id, "agent_text",
            content="(stopped: tool-call hop limit reached)", partial=False,
        )
        new_assistant_blocks.append({
            "type": "text",
            "text": "(stopped: tool-call hop limit reached)",
        })

    _emit_agent(request_id, "agent_done")

    # One-line timing summary so we can see where each turn's wall-clock
    # went without parsing the full log. router_model / assistant_model =
    # chat-agent inference; patch = nested /ai/patch round-trip.
    timing["total"] = time.perf_counter() - t_total_start
    model_total = sum(v for k, v in timing.items() if k.endswith("_model"))
    patch_total = timing.get("patch", 0.0)
    summary = " ".join(f"{k}={v:.2f}s" for k, v in timing.items())
    logger.info(
        "agent[%s] proposals=%d agent_model=%.2fs patch=%.2fs total=%.2fs | %s",
        request_id, len(new_proposals), model_total, patch_total,
        timing["total"], summary,
    )

    # Strip the rehydrated image blocks back out so the persisted history
    # stays text-only (caption-once): the pixels were for this turn's model
    # call, not for re-sending on every future turn.
    if injected_image_msg is not None:
        injected_image_msg["content"] = [
            b for b in injected_image_msg["content"] if b.get("type") != "image"
        ]

    new_turns = _project_blocks_to_turns(new_assistant_blocks, new_proposals)
    return {
        "request_id": request_id,
        "new_turns": new_turns,
        "new_proposals": new_proposals,
        "history_for_persistence": history,
        "history_summary": history_summary,
    }


def _project_blocks_to_turns(blocks: list[dict], proposals: dict) -> list[dict]:
    """Project the canonical assistant content blocks into UI turn rows.
    A text block becomes a turn with `text`. A tool_use block becomes a
    turn with `proposalId` (text empty unless paired with the most recent
    text block, in which case the text is on the prior turn already).

    Read-only tools (list_model_styles etc.) don't add an entry to
    `proposals` — their result is folded into the next narration turn.
    Emitting a tool_use turn for them would render as an empty 'AI'
    bubble in the chat, so skip the turn entirely. Any pending text
    is preserved by flushing it into the standalone text turn that
    follows the tool dispatch."""
    turns: list[dict] = []
    pending_text = ""
    for block in blocks:
        if block.get("type") == "text":
            t = block.get("text") or ""
            if t.strip():
                if pending_text:
                    turns.append({
                        "role": "assistant",
                        "text": pending_text,
                        "timestamp": int(time.time() * 1000),
                    })
                pending_text = t
        elif block.get("type") == "tool_use":
            proposal_id = block.get("id")
            # Read-only tools have no proposal — skip the row to avoid
            # an empty 'AI' label. Pending text (if any) carries forward
            # to the next text turn.
            if proposal_id not in proposals:
                continue
            turns.append({
                "role": "assistant",
                "text": pending_text,
                "proposalId": proposal_id,
                "timestamp": int(time.time() * 1000),
            })
            pending_text = ""
    if pending_text:
        turns.append({
            "role": "assistant",
            "text": pending_text,
            "timestamp": int(time.time() * 1000),
        })
    return turns


# ── route ─────────────────────────────────────────────────────────────

@routes.post("/promptchain/ai/upload-image")
async def _api_upload_image(request):
    """Persist a chat-uploaded image to the user folder; return its hash +
    serve URL + dimensions. The frontend keeps only the hash/URL in chat
    state — pixels never enter the workflow JSON."""
    body, err = await parse_json(request)
    if err:
        return err
    from . import chat_uploads
    meta = chat_uploads.save_upload(body.get("data") or "", body.get("media_type"))
    if not meta:
        return error_response("invalid or unsupported image", 400)
    width = height = None
    try:
        from PIL import Image
        with Image.open(chat_uploads.resolve_upload_path(meta["hash"])) as im:
            width, height = im.size
    except Exception:
        logger.debug("upload dimension probe failed", exc_info=True)
    return web.json_response({
        "hash": meta["hash"],
        "url": f"/promptchain/ai/upload/{meta['hash']}",
        "media_type": meta["media_type"],
        "width": width,
        "height": height,
    })


@routes.get("/promptchain/ai/upload/{hash}")
async def _api_serve_upload(request):
    image_hash = request.match_info.get("hash", "")
    if not HASH_RE.match(image_hash):
        return error_response("invalid hash")
    from . import chat_uploads
    path = chat_uploads.resolve_upload_path(image_hash)
    if not path:
        return error_response("not found", 404)
    import mimetypes as mt
    mime, _ = mt.guess_type(str(path))
    return web.FileResponse(path, headers={
        "Content-Type": mime or "application/octet-stream",
        "Cache-Control": "public, max-age=31536000",
    })


@routes.post("/promptchain/ai/chat")
async def _api_chat(request):
    body, err = await parse_json(request)
    if err:
        return err

    request_id = (body.get("request_id") or "").strip() or uuid.uuid4().hex
    body["request_id"] = request_id

    task = asyncio.create_task(_run_agent_loop(request, body))
    ai_api._active_requests[request_id] = task
    try:
        result = await task
    except asyncio.CancelledError:
        ai_api._emit(request_id, "cancelled")
        # Frontend treats agent_done as the terminal signal; without it
        # after a cancel mid-stream the panel shows partial deltas with
        # no end marker.
        _emit_agent(request_id, "agent_done")
        return error_response("cancelled", 499)
    except Exception as e:
        logger.exception("ai_chat failed")
        msg = _friendly_chat_error(e)
        ai_api._emit(request_id, "error", error=msg)
        _emit_agent(request_id, "agent_done")
        return error_response(msg, 500)
    finally:
        ai_api._cleanup_request(request_id)

    if isinstance(result, dict) and result.get("error"):
        return error_response(result["error"], 400)
    return web.json_response(result)


def _friendly_chat_error(e: Exception) -> str:
    """Rewrite aiohttp connection refusals into something an end-user can
    act on. Looks up the configured provider so the message names what's
    actually unreachable (Ollama vs cloud API)."""
    raw = str(e)
    is_conn = isinstance(e, aiohttp.ClientConnectorError) or "Cannot connect to host" in raw
    if not is_conn:
        return raw
    cfg = ai_api._load_config()
    provider = cfg.get("provider")
    if provider == "local":
        base = ((cfg.get("local") or {}).get("base_url") or "").strip().rstrip("/")
        target = base or "localhost:11434"
        return f"Can't reach Ollama at {target}. Start Ollama and try again."
    if provider == "cloud":
        service = ((cfg.get("cloud") or {}).get("service") or "claude")
        return f"Can't reach the {service} API. Check your internet connection."
    return f"Can't reach the AI provider: {raw}"
