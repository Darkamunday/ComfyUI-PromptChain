"""
Semantic search over Tag Builder bucket items.

The Prompt Generator needs to map descriptive user phrases ("showing the
bottoms of her foot at the viewer", "spreading her ass") to curated
bundle rows like `pose_items.presenting_feet` whose `base_tags` carry
the canonical Danbooru tag set. Literal/stem matching can't bridge
synonym/paraphrase gaps; embeddings can.

Index shape: every row of pose_items + nsfw_action_items + action_items +
expression_items + scene_items gets a 384-dim embedding from
BAAI/bge-small-en-v1.5. Stored as a single torch tensor in memory; cosine
similarity is one matmul.

Hot-reload: every search() call cheaply checks (bucket, MAX(rowid),
COUNT(*)) per indexed table. If anything moved, the index rebuilds.
Means new entries the user adds via Tag Builder are searchable on the
next request without restarting the server.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from typing import Any


# Body-region awareness for retrieval. When the user mentions clothing
# that implies a specific body region, bundles that engage that region
# get a small cosine bump. Without this, "pointing red socks at viewer"
# matches hand-pointing bundles by literal-word similarity even though
# the user's foot-region focus (red socks) implies a foot-presentation
# pose. Conservative bump (+0.05) — enough to flip a ~0.03 cosine gap,
# not so much it overrides genuinely-better matches.

# Clothing keyword in the user text → body region tag.
_CLOTHING_REGION: dict[str, str] = {
    # feet/lower legs
    "sock": "feet", "socks": "feet", "stocking": "feet", "stockings": "feet",
    "boot": "feet", "boots": "feet", "shoe": "feet", "shoes": "feet",
    "sandal": "feet", "sandals": "feet", "heel": "feet", "heels": "feet",
    "slipper": "feet", "slippers": "feet", "footwear": "feet",
    # legs/thighs (above the foot)
    "pantyhose": "legs", "tights": "legs", "leggings": "legs",
    "garter": "legs", "thighhighs": "legs",
    # head
    "hat": "head", "cap": "head", "beret": "head", "headband": "head",
    "tiara": "head", "crown": "head", "hood": "head", "helmet": "head",
    # torso
    "bikini": "torso", "shirt": "torso", "blouse": "torso",
    "dress": "torso", "leotard": "torso", "swimsuit": "torso",
    "lingerie": "torso", "corset": "torso", "vest": "torso",
    "jacket": "torso", "coat": "torso", "sweater": "torso",
    "tank": "torso",
    # hands
    "glove": "hands", "gloves": "hands", "mitten": "hands", "mittens": "hands",
}

# Direct body-part mention in the user text → body region tag. The user
# might mention a body part for the POSE (e.g. 'arms up') even when the
# clothing covers a different region (leotard → torso). Without this,
# 'arms up wearing leotard' boosts torso/breast bundles ('Arm Under
# Breasts') and the arms-pose bundles ('Arms Up') stay buried.
_BODY_PART_REGION: dict[str, str] = {
    # arms
    "arm": "arms", "arms": "arms", "elbow": "arms", "elbows": "arms",
    "shoulder": "arms", "shoulders": "arms",
    # legs
    "leg": "legs", "legs": "legs", "thigh": "legs", "thighs": "legs",
    "knee": "legs", "knees": "legs",
    # feet
    "foot": "feet", "feet": "feet", "toe": "feet", "toes": "feet",
    "ankle": "feet", "ankles": "feet", "sole": "feet", "soles": "feet",
    # hands
    "hand": "hands", "hands": "hands", "finger": "hands", "fingers": "hands",
    "wrist": "hands", "wrists": "hands",
    # head
    "face": "head", "head": "head", "hair": "head",
    # torso
    "chest": "torso", "breast": "torso", "breasts": "torso",
    "stomach": "torso", "belly": "torso", "navel": "torso",
}

# Body region → keywords that must appear (whole-word) in a bundle's
# display_name + base_tags + item_group for the boost to apply.
# Narrow keyword sets: pick clothing-related and presentation-related
# tokens, NOT generic body-action tokens. Generic 'head' would boost
# 'Head on Pillow' for a hat query — not what we want.
_REGION_BUNDLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "feet": (
        "foot", "feet", "sole", "soles", "kneepit", "kneepits",
        "toe", "toes", "ankle", "ankles",
        "presenting_feet", "feet_up", "feet_only", "foot_up",
        "foot_focus", "feet_focus", "foot_dangle", "leg_up", "legs_up",
    ),
    "legs": (
        "leg", "legs", "thigh", "thighs", "knee", "knees",
        "kneeling", "spread_legs", "kneepit", "kneepits",
    ),
    "head": (
        # Hat-related bundles only — NOT generic 'head' actions like
        # 'head on pillow' that would boost noisily for a hat query.
        "hat", "headwear", "headband", "tiara", "crown", "beret", "cap",
        "helmet", "hood",
    ),
    "torso": (
        # Torso-presentation and torso-clothing bundles.
        "chest", "breast", "breasts", "torso", "stomach", "belly", "navel",
        "bikini", "shirt", "blouse", "dress", "leotard", "swimsuit",
        "lingerie", "corset", "vest",
    ),
    "hands": (
        "hand", "hands", "finger", "fingers", "wrist", "wrists",
        "glove", "gloves",
    ),
    "arms": (
        "arm", "arms", "elbow", "elbows", "shoulder", "shoulders",
        "arms_up", "arm_up", "arms_above_head", "arms_behind_head",
        "arm_raised", "arms_raised", "arm_above_head",
    ),
}


# Presentation verbs — interchangeable for retrieval. The user's
# wording is one of these; the curator's bundle name might use a
# different one. Embedding cosine treats them as similar but not
# identical, so 'pointing red socks' under-matches 'Presenting Feet'.
# Query-expand by emitting one variant per replacement verb and
# max-pooling the cosines against the bundle index — handles synonym
# blindness without telling the LLM about it.
_PRESENTATION_VERBS = (
    "pointing", "aiming", "showing", "displaying",
    "flashing", "presenting", "exposing", "exhibiting",
)


# When the user explicitly describes the gesture using a hand or
# finger, they mean a literal hand-pointing gesture — verb expansion
# would dilute the signal by adding presenting/showing/displaying
# variants that don't fit. Skip expansion in this case so the original
# 'pointing' query lands cleanly on hand-pointing bundles.
_EXPLICIT_HAND_GESTURE_RE = re.compile(
    r"\bwith\s+(?:her|his|their|the)?\s*"
    r"(?:finger|fingers|hand|hands|index_finger|pointer)\b",
    re.IGNORECASE,
)


def _expand_presentation_verbs(text: str) -> list[str]:
    """Return the original text plus verb-swapped variants when any
    presentation verb is present. Each variant replaces every matched
    verb with one of the alternatives, leaving everything else
    untouched. Always includes the original at index 0.

    Skipped when the user explicitly describes a hand/finger gesture —
    that case wants literal pointing, not presenting."""
    if not text:
        return [text] if text else []
    if _EXPLICIT_HAND_GESTURE_RE.search(text):
        return [text]
    text_lc = text.lower()
    matched = [v for v in _PRESENTATION_VERBS if re.search(rf"\b{v}\b", text_lc)]
    if not matched:
        return [text]
    variants = [text]
    for replacement in _PRESENTATION_VERBS:
        if replacement in matched:
            continue
        new_text = text
        for verb in matched:
            new_text = re.sub(
                rf"\b{verb}\b", replacement, new_text, flags=re.IGNORECASE,
            )
        if new_text != text:
            variants.append(new_text)
    return variants


def _detect_body_regions(user_text: str) -> set[str]:
    """Scan the user's text for clothing keywords AND direct body-part
    mentions that imply body regions. Returns the set of regions
    implied — usually 0-2.
      'wearing a hat and red socks' → {head, feet}
      'arms up wearing leotard'     → {arms, torso}
      'kneeling in pantyhose'       → {legs}  (leg+pantyhose both legs)"""
    if not user_text:
        return set()
    regions: set[str] = set()
    text = user_text.lower()
    for word, region in _CLOTHING_REGION.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            regions.add(region)
    for word, region in _BODY_PART_REGION.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            regions.add(region)
    return regions


def _bundle_introduces_unmentioned_body_part(entry: dict, user_text: str,
                                              implied_regions: set[str] | None = None) -> bool:
    """True if the bundle's display_name contains a body-part word
    (finger, hand, foot, etc.) that does NOT appear in the user's
    text AND whose region isn't implied by the user's clothing or
    other body-part mentions.

    Catches 'Finger Gun' surfacing for 'pointing a gun at viewer'
    (no hand/finger/glove in user text → demote). Allows 'Presenting
    Feet' for 'wearing red socks pointing them at viewer' (socks →
    feet region → bundle's 'feet' word maps to that same region →
    keep)."""
    if not entry or not user_text:
        return False
    name = (entry.get("display_name") or "").lower()
    if not name:
        return False
    user_lc = user_text.lower()
    if implied_regions is None:
        implied_regions = _detect_body_regions(user_text)
    name_words = re.findall(r"\b\w+\b", name)
    for word in name_words:
        if word in _BODY_PART_REGION:
            if re.search(rf"\b{re.escape(word)}\b", user_lc):
                continue  # User said this exact word — not a mismatch.
            bundle_word_region = _BODY_PART_REGION[word]
            if bundle_word_region not in implied_regions:
                return True  # User's text doesn't imply this region — demote.
    return False


def _bundle_matches_region(entry: dict, regions: set[str]) -> bool:
    """True if the bundle's display_name / base_tags / item_group
    contains any keyword for any of the implied regions. Word-boundary
    match so 'back' doesn't substring-match `white_background`."""
    if not regions:
        return False
    haystack = (
        f"{entry.get('display_name', '')} "
        f"{entry.get('base_tags', '')} "
        f"{entry.get('item_group', '')}"
    ).lower()
    for region in regions:
        for kw in _REGION_BUNDLE_KEYWORDS.get(region, ()):
            if re.search(rf"\b{re.escape(kw)}\b", haystack):
                return True
    return False

logger = logging.getLogger("promptchain.bucket_search")
_dbg = logging.getLogger("promptchain.ai.debug")

# Buckets we actually want to retrieve over. Cast/characters/clothing/
# appearance are intentionally omitted — characters get resolved by
# match-characters (deterministic), and clothing/appearance flow through
# the character bio block, not free-form description matching.
INDEXED_BUCKETS = (
    "pose_items",
    "nsfw_action_items",
    "action_items",
    "expression_items",
    "scene_items",
)

# Embedding model is loaded once and shared by bucket_search,
# modifier_search, and tag_search. See core/_embed_model.py.
from . import _embed_model

_lock = threading.RLock()
_state: dict[str, Any] = {
    "embeddings": None,      # torch.Tensor [N, 384]
    "rows": [],              # list[dict] aligned with embeddings
    "fingerprint": None,     # tuple of (bucket, max_rowid, count) — change → rebuild
}


def _ensure_model_loaded() -> bool:
    """Defers to the shared embed model loader."""
    return _embed_model.get() is not None


def _embed(texts: list[str]):
    """Return L2-normalized CLS embeddings for `texts` as a torch.Tensor."""
    return _embed_model.embed(texts, batch_size=64)


def _to_underscored_bundle(base_tags: str) -> str:
    """Convert space-form tag tokens in a curated bundle to Danbooru-
    canonical underscored form, preserving weighted-tag syntax. Bucket
    rows are curator-entered so a single bundle may mix `presenting feet`
    (space) with `green_leotard` (underscore). The system prompt rule is
    'always emit underscored canonical, output post-processor converts
    back to spaces for spaces-format models' — we feed canonical to the
    LLM here so the rule actually holds for bundle data too.

    Mirrors ai_api._to_underscored_tags but kept local to avoid the
    import cycle (bucket_search → tag_builder.get_db, ai_api → ...)."""
    if not base_tags:
        return base_tags
    out = []
    for raw in base_tags.split(","):
        tok = raw.strip()
        if not tok:
            continue
        # Whole-string replace handles both bare tags ('presenting feet'
        # → 'presenting_feet') and weighted forms ('(legs up:1.1)' →
        # '(legs_up:1.1)') uniformly. Parens/colon/number positions are
        # preserved by the structure of the input.
        out.append(tok.replace(" ", "_"))
    return ", ".join(out)


def _row_text(bucket: str, row: dict) -> str:
    """Build the text we embed for a row. Includes display_name, the
    bucket-as-context (so 'spread' in pose_items doesn't collide with
    'spread' in clothing), and the base_tags themselves — bge-small
    benefits from seeing the canonical tag tokens, not just the prose
    name."""
    bucket_label = bucket.removesuffix("_items").replace("_", " ")
    parts = [f"{bucket_label}:", row.get("display_name") or row.get("item_tag") or ""]
    base_tags = (row.get("base_tags") or "").strip()
    if base_tags:
        parts.append(base_tags)
    return " ".join(parts).strip()


def _fingerprint() -> tuple:
    """Cheap signature of the indexed tables. Changes when any row is
    added/removed; max(rowid) catches inserts and count catches deletes."""
    from .tag_builder import get_db
    db = get_db()
    sig = []
    for bucket in INDEXED_BUCKETS:
        try:
            row = db.execute(
                f"SELECT COALESCE(MAX(rowid), 0) AS m, COUNT(*) AS c FROM {bucket}"
            ).fetchone()
            sig.append((bucket, row["m"], row["c"]))
        except Exception:
            sig.append((bucket, 0, 0))
    # Props + prop_actions also affect retrieval — fingerprint includes
    # them so adding a new prop or action triggers a reindex.
    for table in ("props", "prop_actions"):
        try:
            row = db.execute(
                f"SELECT COALESCE(MAX(rowid), 0) AS m, COUNT(*) AS c FROM {table}"
            ).fetchone()
            sig.append((table, row["m"], row["c"]))
        except Exception:
            sig.append((table, 0, 0))
    return tuple(sig)


def _build_prop_bundles(db) -> list[tuple[dict, str]]:
    """Build prop bundles from the tag-builder props + prop_actions
    tables. Each prop becomes one bundle on its own (so 'cammy with a
    gun' surfaces the Gun prop), and each (prop, compatible_action)
    pair becomes a virtual bundle (so 'pointing a gun' surfaces 'Aiming
    Gun → aiming, gun'). compatible_categories on prop_actions is a
    JSON list of category names; we cross-join only when the prop's
    category is in that list. Returns [(entry_dict, embed_text), ...]."""
    import json
    out: list[tuple[dict, str]] = []
    try:
        prop_rows = db.execute(
            "SELECT prop_tag, display_name, category, base_tags FROM props ORDER BY rowid"
        ).fetchall()
    except Exception as e:
        logger.warning("bucket_search: skip props: %s", e)
        return out
    try:
        action_rows = db.execute(
            "SELECT action_tag, display_name, action_prefix_tags, compatible_categories "
            "FROM prop_actions ORDER BY rowid"
        ).fetchall()
    except Exception as e:
        logger.warning("bucket_search: skip prop_actions: %s", e)
        action_rows = []
    # Bare props — bucket = 'prop'. Lets the user surface a prop without
    # naming an action ('cammy with a gun', 'holding a sword').
    for r in prop_rows:
        base = (r["base_tags"] or "").strip()
        if not base and not (r["display_name"] or r["prop_tag"]):
            continue
        entry = {
            "bucket": "prop",
            "item_tag": r["prop_tag"] or "",
            "item_group": r["category"] or "",
            "display_name": r["display_name"] or r["prop_tag"] or "",
            "base_tags": base,
        }
        text = _row_text("props", dict(r))
        if not text:
            continue
        out.append((entry, text))
    # Action+prop virtual bundles — bucket = 'prop_action'. Cross-join
    # on category compatibility. compatible_categories is JSON-encoded
    # list of category names ('["weapons"]'); fall back to comma-split
    # if it's not valid JSON.
    for ar in action_rows:
        compat_raw = ar["compatible_categories"] or ""
        try:
            compat = json.loads(compat_raw)
            if not isinstance(compat, list):
                compat = [compat_raw]
        except Exception:
            compat = [c.strip() for c in compat_raw.split(",") if c.strip()]
        compat_set = {str(c).strip().lower() for c in compat}
        if not compat_set:
            continue
        action_prefix = (ar["action_prefix_tags"] or "").strip()
        if not action_prefix:
            continue
        for pr in prop_rows:
            if (pr["category"] or "").strip().lower() not in compat_set:
                continue
            prop_tags = (pr["base_tags"] or "").strip()
            if not prop_tags:
                continue
            display = f"{ar['display_name']} {pr['display_name']}".strip()
            combined_tags = f"{action_prefix}, {prop_tags}"
            entry = {
                "bucket": "prop_action",
                "item_tag": f"{ar['action_tag']}__{pr['prop_tag']}",
                "item_group": (pr["category"] or "").strip(),
                "display_name": display,
                "base_tags": combined_tags,
            }
            # Embed text combines display + tags so cosine catches both
            # the action name ('aiming') and the prop name ('gun').
            text = f"{(pr['category'] or 'prop').replace('_', ' ')}: {display} {combined_tags}".strip()
            out.append((entry, text))
    return out


def _rebuild_index() -> None:
    """Read every indexed bucket, embed every row, store as one tensor.
    Includes the curated *_items buckets plus props and prop+action
    virtual bundles."""
    from .tag_builder import get_db
    if not _ensure_model_loaded():
        return
    db = get_db()
    rows: list[dict] = []
    texts: list[str] = []
    for bucket in INDEXED_BUCKETS:
        try:
            cur = db.execute(
                f"SELECT item_tag, item_group, display_name, base_tags, "
                f"base_natlang FROM {bucket} ORDER BY rowid"
            )
        except Exception as e:
            logger.warning("bucket_search: skip %s: %s", bucket, e)
            continue
        for r in cur.fetchall():
            entry = {
                "bucket": bucket.removesuffix("_items"),
                "item_tag": r["item_tag"] or "",
                "item_group": r["item_group"] or "",
                "display_name": r["display_name"] or r["item_tag"] or "",
                "base_tags": (r["base_tags"] or "").strip(),
                "base_natlang": (r["base_natlang"] or "").strip(),
            }
            text = _row_text(bucket, dict(r))
            if not text:
                continue
            rows.append(entry)
            texts.append(text)
    # Props + prop_action virtual bundles.
    prop_count = 0
    action_count = 0
    for entry, text in _build_prop_bundles(db):
        rows.append(entry)
        texts.append(text)
        if entry["bucket"] == "prop":
            prop_count += 1
        else:
            action_count += 1
    if not rows:
        _state["embeddings"] = None
        _state["rows"] = []
        _state["fingerprint"] = _fingerprint()
        return
    # Batch to keep peak memory bounded; bge-small is light, but a 5k row
    # batch on CPU still allocates a chunk of RAM.
    import torch
    batch_size = 64
    all_emb = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        all_emb.append(_embed(chunk))
    embeddings = torch.cat(all_emb, dim=0)
    _state["embeddings"] = embeddings
    _state["rows"] = rows
    _state["fingerprint"] = _fingerprint()
    logger.info(
        "bucket_search: indexed %d rows (%d buckets + %d props + %d prop_actions)",
        len(rows), len(INDEXED_BUCKETS), prop_count, action_count,
    )


def _ensure_index_fresh() -> None:
    """Rebuild if the tag-builder DB has shifted since last index. Cheap
    metadata query first; rebuild only if shape changed."""
    if _state["fingerprint"] == _fingerprint() and _state["embeddings"] is not None:
        return
    _rebuild_index()


def warmup() -> None:
    """Best-effort eager load — call from a daemon thread on boot so the
    first match-buckets request doesn't pay the model-download latency.

    Retries a few times because the warmup thread races with torch's
    own cold-start imports (joblib/loky circular-init transitives).
    Sleeps between attempts so the import cycle has a chance to resolve.
    Hard ModuleNotFound failures bail immediately — no point retrying
    a missing pip install."""
    import time
    for attempt in range(3):
        with _lock:
            if _ensure_model_loaded():
                _ensure_index_fresh()
                return
            if _embed_model.get_load_error():
                return  # cached hard failure — retry won't help
        time.sleep(2.0 * (attempt + 1))


def search(user_text: str, top_k: int = 30) -> list[dict]:
    """Return top-k bucket rows by cosine similarity to user_text.

    Each entry: {bucket, item_tag, item_group, display_name, base_tags, score}.
    Empty list when the model failed to load (degrade gracefully).
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return []
    with _lock:
        if not _ensure_model_loaded():
            return []
        _ensure_index_fresh()
        embeddings = _state["embeddings"]
        rows = _state["rows"]
        if embeddings is None or not rows:
            return []
        import torch
        # Expand presentation verbs (pointing/showing/displaying/etc.)
        # and embed all variants. Take max cosine per bundle so we keep
        # the best literal-word match AND the best synonym-substituted
        # match — neither path suffers when the user phrases their
        # intent with a verb the bundle didn't use.
        variants = _expand_presentation_verbs(user_text)
        q = _embed(variants)  # [k, dim], one row per variant
        # Cosine == dot since both sides are L2-normalized.
        # scores_per_variant: [N, k]; we take max over k.
        scores_per_variant = embeddings @ q.T  # [N, k]
        scores = scores_per_variant.max(dim=1).values  # [N]
        # Literal-word vs synonym alignment: when expansion fired (i.e.
        # the user used a presentation verb), bundles whose original-
        # query cosine is meaningfully HIGHER than any variant's cosine
        # are aligned with literal pointing (Pointing at Viewer for
        # 'pointing X at viewer'). In presentation context, the user's
        # likely intent is the synonym/presenting interpretation, so
        # demote those to fall out of top-K. Threshold (>0.02 stronger
        # on original than variants) avoids false positives where the
        # cosines are roughly tied.
        if len(variants) > 1:
            orig = scores_per_variant[:, 0]  # [N] cosine to original
            variant_max = scores_per_variant[:, 1:].max(dim=1).values  # [N]
            literal_aligned = (orig - variant_max) > 0.02
            scores = scores + literal_aligned.float() * (-0.10)
        # Pull a wider net for the cosine cut, then rerank by adjusted
        # score (cosine + richness bonus). A bundle like
        # "Presenting Feet → (legs up:1.1), sitting, presenting feet,
        # soles, foot focus" should outrank a 1-tag stub like
        # "Presenting Foot → presenting_foot" when the cosine gap is
        # within tens of millis. The curator put the rich bundle there
        # for a reason; cosine alone treats foot/feet as tied.
        # Wide cosine pool so the diversity cap has enough non-dominant
        # bucket entries to draw from. With queries like 'fully nude
        # sitting with legs up' the embedding pulls hard toward NSFW
        # content — Presenting Feet (pure pose bundle) lands at rank
        # 50-80 cosine. Pool of top_k*2 missed it; widening to ~150
        # for top_k=30 catches the long-tail pose/scene bundles the
        # diversity cap can then surface back into top_k.
        cosine_pool = min(max(top_k * 5, top_k + 100), scores.shape[0])
        pool = torch.topk(scores, k=cosine_pool)
        # Body-region awareness: when the user's text mentions clothing
        # that implies a body region (e.g. 'red socks' → feet), boost
        # bundles that engage that region. Catches phrasings like
        # 'pointing red socks at viewer' where literal-word retrieval
        # surfaces hand-pointing bundles instead of foot-presentation.
        regions = _detect_body_regions(user_text)
        out = []
        for s, idx in zip(pool.values.tolist(), pool.indices.tolist()):
            entry = dict(rows[idx])
            entry["score"] = float(s)
            # log keeps the bonus modest: 1 tag → 0, 5 tags → 0.032,
            # 20 tags → 0.060. Enough to flip the order on a 0.01-0.05
            # cosine gap without overpowering genuinely better matches.
            n_tags = max(1, len([t for t in (entry.get("base_tags") or "").split(",") if t.strip()]))
            adjusted = float(s) + 0.02 * math.log(n_tags)
            if regions and _bundle_matches_region(entry, regions):
                adjusted += 0.05
            # Demote bundles whose display_name contains a body-part
            # word the user never mentioned AND whose region isn't
            # implied by user's clothing/body mentions. Catches 'Finger
            # Gun' for 'pointing a gun' (no hand/finger/glove implied),
            # but doesn't demote 'Presenting Feet' for 'wearing red
            # socks' (socks → feet region → match).
            if _bundle_introduces_unmentioned_body_part(entry, user_text, regions):
                adjusted -= 0.10
            entry["adjusted_score"] = adjusted
            # Normalize to underscored canonical form before returning —
            # bucket rows can be a mix of 'presenting feet' / 'green_leotard'.
            # The downstream LLM rule expects one canonical form throughout,
            # and the post-stream output formatter converts back to spaces
            # for spaces-format target models.
            entry["base_tags"] = _to_underscored_bundle(entry.get("base_tags") or "")
            out.append(entry)
        out.sort(key=lambda e: e["adjusted_score"], reverse=True)
        # Diversity cap: keywords like 'nude' pull bge-small embeddings
        # heavily toward NSFW bundles, drowning out pure pose / scene /
        # expression bundles that match the user's actual intent
        # (e.g. 'sitting with legs up pointing socks at viewer' should
        # surface Presenting Feet but loses cosine to Wide Spread Legs,
        # Cum on Legs, etc.). Cap any single bucket at half of top_k so
        # other buckets' best matches always reach the model. Within
        # the cap, cosine ordering is preserved.
        return _diversify_by_bucket(out, top_k)


def search_for_apply(user_text: str,
                     buckets: tuple[str, ...] = (),
                     top_k: int = 10) -> list[dict]:
    """Variant-free retrieval for the AI auto-apply path.

    Skips `_expand_presentation_verbs` and the literal-aligned penalty —
    when the natlang pipeline auto-applies a chip to a section, we WANT
    the chip whose authored prose literally matches the user phrase. The
    variant-expansion trick in `search()` is tuned for UI menus where a
    user picks from top-K and synonym broadening helps coverage; here it
    actively demotes the right chip (`presenting feet` query → bge
    cosine matches `presenting_feet` chip's natlang verbatim, then gets
    -0.10'd for being "too literal").

    Still applies: richness bonus (log(n_tags)), region bump
    (clothing/body-part → +0.05), unmentioned-body-part demotion. These
    keep retrieval grounded in the same signal the UI uses, just without
    the synonym-broadening overlay.

    Filters to `buckets` when provided (e.g. ("pose","nsfw_action") to
    skip scene/expression for pose-section lookup). Empty tuple = all
    indexed buckets."""
    user_text = (user_text or "").strip()
    if not user_text:
        return []
    with _lock:
        if not _ensure_model_loaded():
            return []
        _ensure_index_fresh()
        embeddings = _state["embeddings"]
        rows = _state["rows"]
        if embeddings is None or not rows:
            return []
        import torch
        q = _embed([user_text])  # [1, dim]
        scores = (embeddings @ q.T).squeeze(1)  # [N]
        cosine_pool = min(max(top_k * 5, top_k + 50), scores.shape[0])
        pool = torch.topk(scores, k=cosine_pool)
        regions = _detect_body_regions(user_text)
        out: list[dict] = []
        for s, idx in zip(pool.values.tolist(), pool.indices.tolist()):
            entry = dict(rows[idx])
            if buckets and entry.get("bucket") not in buckets:
                continue
            entry["score"] = float(s)
            n_tags = max(1, len([
                t for t in (entry.get("base_tags") or "").split(",") if t.strip()
            ]))
            adjusted = float(s) + 0.02 * math.log(n_tags)
            if regions and _bundle_matches_region(entry, regions):
                adjusted += 0.05
            if _bundle_introduces_unmentioned_body_part(entry, user_text, regions):
                adjusted -= 0.10
            entry["adjusted_score"] = adjusted
            entry["base_tags"] = _to_underscored_bundle(entry.get("base_tags") or "")
            out.append(entry)
        out.sort(key=lambda e: e["adjusted_score"], reverse=True)
        return out[:top_k]


def _diversify_by_bucket(results: list[dict], top_k: int) -> list[dict]:
    """Greedy cap on per-bucket count within top_k. First pass adds
    each result if its bucket has room; second pass fills any
    remaining slots from the cosine-ordered overflow. Preserves
    cosine ranking inside each bucket."""
    cap_per_bucket = max(3, top_k // 2)
    bucket_count: dict[str, int] = {}
    primary: list[dict] = []
    overflow: list[dict] = []
    for r in results:
        bucket = r.get("bucket", "")
        if bucket_count.get(bucket, 0) < cap_per_bucket:
            bucket_count[bucket] = bucket_count.get(bucket, 0) + 1
            primary.append(r)
        else:
            overflow.append(r)
        if len(primary) >= top_k:
            break
    if len(primary) < top_k:
        primary.extend(overflow[: top_k - len(primary)])
    return primary[:top_k]
