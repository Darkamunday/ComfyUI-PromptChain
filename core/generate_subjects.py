"""generate_subjects — KB-grounded subject generation.

Sibling to inline_wildcard_populate (which FETCHES existing named entities).
This GENERATES novel subjects from a fuzzy theme by deterministically
SAMPLING the KB appearance vocabulary + outfits — not LLM invention. The
chat agent's only job is turning a request into a small recipe (count,
fixed constraints, vary axes, outfit policy); this module composes the
bodies. Same safety profile as the fetcher: scale is free, output is
grounded, format is deterministically validatable.

Output modes:
  - inline_wildcards: N entries as `::Label::body`, appended to the node
  - single: one subject body (no labels), for standalone generation

See dev-promptchain/docs/plans/generate-subjects-plan.md.
"""
from __future__ import annotations

import os
import random
import sqlite3

from .inline_wildcard_populate import (
    _default_db_path, _existing_labels, _format_entry,
)

# Global safety floor: appearance items whose tag contains any of these
# are NEVER sampled, regardless of theme. Youthful/minor descriptors.
_SAFETY_EXCLUDE = (
    "loli", "child", "toddler", "shota", "baby", "infant",
    "kindergarten", "preteen", "young_child",
)
# Axes varied across subjects by default for distinctness. body_type is
# intentionally NOT here — it mixes gendered/feature tags, so it's only
# varied when the recipe explicitly asks (and is usually a `fixed` theme
# constraint instead). body_marks is also opt-in: its 247 items include
# anatomical/niche tags that read as noise on a generic subject.
_DEFAULT_VARY = ("hair_color", "hair_style", "eye_color")
# Per-axis substring excludes layered on top of the global safety floor.
# hair_style mixes in facial-hair items (beard/mustache/...) that don't
# belong on a default (female/androgynous) generated subject.
_AXIS_EXCLUDE = {
    "hair_style": ("beard", "mustache", "moustache", "goatee", "stubble",
                   "sideburn", "facial_hair"),
}
# Per-axis allowlist (exact tags). When present, ONLY these are sampled —
# robust against new exotic items. eye_color mixes plain colors with
# pupil-shape/feature items (slit_pupils, flower-shaped_pupils, blank_eyes,
# heterochromia, ...); restrict to plain natural colors for realism.
_AXIS_ALLOWLIST = {
    "eye_color": {
        "aqua_eyes", "black_eyes", "blue_eyes", "brown_eyes", "green_eyes",
        "grey_eyes", "orange_eyes", "pink_eyes", "purple_eyes", "red_eyes",
        "yellow_eyes",
    },
}
# base_natlang above this length is an instructional sentence, not a
# clean appearance phrase — fall back to the display name instead.
_PHRASE_MAX = 60
# Sampled without replacement so subjects don't collide on the most
# visually salient axis.
_PRIMARY_AXIS = "hair_color"


def _safe(tag: str) -> bool:
    t = (tag or "").lower()
    return not any(s in t for s in _SAFETY_EXCLUDE)


def _phrase(row: dict) -> str:
    nat = (row.get("base_natlang") or "").strip()
    disp = (row.get("display_name") or "").strip()
    # Some items (esp. hair_style) store an instructional sentence in
    # base_natlang rather than a clean phrase — use the display name.
    if nat and len(nat) <= _PHRASE_MAX:
        return nat
    # Display fallback is title-case ("Covered Eyes"); lowercase it so it
    # reads as part of the comma-joined appearance phrase.
    return (disp or nat).lower()


def _load_axis(conn: sqlite3.Connection, axis: str) -> list[dict]:
    rows = conn.execute(
        "SELECT item_tag, display_name, base_natlang "
        "FROM appearance_items WHERE item_group = ?",
        (axis,),
    ).fetchall()
    axis_excl = _AXIS_EXCLUDE.get(axis, ())
    allow = _AXIS_ALLOWLIST.get(axis)
    out = []
    for r in rows:
        tag = (r["item_tag"] or "").lower()
        if not _safe(tag) or not _phrase(dict(r)):
            continue
        if allow is not None and tag not in allow:
            continue
        if any(x in tag for x in axis_excl):
            continue
        out.append(dict(r))
    return out


def _resolve_fixed(conn: sqlite3.Connection, axis: str, value: str,
                   fmt: str = "natlang") -> str:
    """Resolve a fixed constraint value (a tag or a natural word) to its
    KB representation. fmt='tags' -> the danbooru item_tag; 'natlang' ->
    the full natlang phrase. Falls back to the literal value if not found."""
    row = conn.execute(
        "SELECT item_tag, display_name, base_natlang FROM appearance_items "
        "WHERE item_group = ? AND ("
        "LOWER(item_tag) = LOWER(?) OR LOWER(display_name) = LOWER(?) "
        "OR LOWER(item_tag) LIKE ?) LIMIT 1",
        (axis, value, value, f"%{value.lower()}%"),
    ).fetchone()
    if row:
        if fmt == "tags":
            return (row["item_tag"] or value).strip()
        # Fixed constraints are the subject's lead descriptor — use the
        # full natlang (uncapped), unlike varied traits which want a
        # short phrase. mature_female's rich "adult woman with mature
        # features..." is exactly what we want to keep here.
        return (row["base_natlang"] or row["display_name"] or value).strip()
    return value


def _all_generic_outfits(conn: sqlite3.Connection,
                         fmt: str = "natlang") -> list[str]:
    col = "outfit_tags" if fmt == "tags" else "outfit_natlang"
    rows = conn.execute(
        f"SELECT {col} AS o FROM generic_outfits WHERE o IS NOT NULL AND o != ''",
    ).fetchall()
    return [r["o"].strip() for r in rows if r["o"]]


def _build_outfit_pool(conn: sqlite3.Connection, policy: str,
                       fmt: str = "natlang") -> tuple[list[str], str]:
    """Resolve an outfit policy to a pool of outfit strings (one sampled per
    subject) + a note. fmt picks the tag vs natlang column. Two-tier theme
    resolution:
      tier 1  generic_outfits (named themes: goth, cyberpunk, maid, bikini)
      tier 2  clothing_items grouped (categories: lingerie, swimwear, ...)
      tier 3  no-silent-drop fallback -> all generic_outfits, with a note
    'random'/'themed' (no theme) -> all generic_outfits. 'none' -> empty."""
    o_col = "outfit_tags" if fmt == "tags" else "outfit_natlang"
    c_col = "base_tags" if fmt == "tags" else "base_natlang"
    policy = (policy or "").strip().lower()
    if policy in ("none", ""):
        return [], ""
    theme = policy.split(":", 1)[1].strip() if policy.startswith("themed:") else ""
    if not theme:
        return _all_generic_outfits(conn, fmt), ""

    # tier 1: named outfit theme
    rows = conn.execute(
        f"SELECT {o_col} AS o FROM generic_outfits WHERE "
        "o IS NOT NULL AND o != '' AND "
        "(LOWER(aliases) LIKE ? OR LOWER(name) LIKE ?)",
        (f"%{theme}%", f"%{theme}%"),
    ).fetchall()
    pool = [r["o"].strip() for r in rows if r["o"]]
    if pool:
        return pool, ""

    # tier 2: clothing category (lingerie, swimwear, underwear, ...). The
    # lingerie/swimwear/dress groups are mostly complete one-piece looks,
    # so a single sampled item reads as a coherent outfit.
    rows = conn.execute(
        f"SELECT {c_col} AS o, display_name FROM clothing_items WHERE "
        "LOWER(item_group) = LOWER(?) OR LOWER(item_group) LIKE ?",
        (theme, f"{theme}%"),
    ).fetchall()
    pool = [(r["o"] or r["display_name"] or "").strip()
            for r in rows if (r["o"] or r["display_name"])]
    if pool:
        return pool, ""

    # tier 3: theme matched nothing — never drop the outfit silently.
    return _all_generic_outfits(conn, fmt), (
        f"no '{theme}' outfits in the KB — used random outfits instead"
    )


def _compose_body(subject_kind: str, fixed_vals: dict,
                  traits: dict, vary: list, outfit: str,
                  fmt: str = "natlang") -> str:
    parts: list[str] = []
    if fmt == "tags":
        # Tag mode: fixed tags + varied item tags + outfit tags, comma-joined.
        for axis, val in fixed_vals.items():
            if val:
                parts.append(val)
        for axis in vary:
            t = traits.get(axis)
            if t and t.get("item_tag"):
                parts.append(t["item_tag"])
        body = ", ".join(p for p in parts if p)
        if outfit:
            body = f"{body}, {outfit}" if body else outfit
        return body
    # natlang
    if "body_type" in fixed_vals:
        parts.append(fixed_vals["body_type"])
    else:
        parts.append(f"a {subject_kind}")
    for axis, nat in fixed_vals.items():
        if axis != "body_type" and nat:
            parts.append(nat)
    for axis in vary:
        t = traits.get(axis)
        if t:
            parts.append(_phrase(t))
    body = ", ".join(p for p in parts if p)
    if outfit:
        body += f", wearing {outfit}"
    return body


def _label(traits: dict, subject_kind: str) -> str:
    bits: list[str] = []
    hc = traits.get("hair_color")
    if hc:
        bits.append((hc.get("display_name") or "").replace(" Hair", "").strip())
    bits.append(subject_kind.strip().title() or "Subject")
    return " ".join(b for b in bits if b)


def generate_subjects(
    *,
    count: int = 1,
    subject_kind: str = "woman",
    fixed: dict | None = None,
    vary: list | None = None,
    outfit_policy: str = "random",
    mode: str = "inline_wildcards",
    existing_content: str = "",
    seed: int | None = None,
    db_path: str | None = None,
    fmt: str = "natlang",
) -> dict:
    """Generate `count` KB-grounded subjects.

    fixed: hard appearance constraints, e.g. {"body_type": "mature_female"}.
    vary: appearance axes randomized across subjects (default hair/eyes/
        style/marks); a fixed axis is never also varied.
    outfit_policy: "random" | "themed:<x>" | "none".
    mode: "inline_wildcards" (append ::Label:: entries) | "single" (one body).
    fmt: "natlang" (prose bodies) | "tags" (danbooru tag bodies) — match the
        node's prompt_style.
    seed: reproducibility; a fresh one is generated + surfaced if omitted.
    Returns {content, summary, subjects, added, skipped, mode, seed}."""
    fixed = dict(fixed or {})
    vary = [a for a in (list(vary) if vary else list(_DEFAULT_VARY))
            if a not in fixed]
    count = max(1, int(count))
    if seed is None:
        seed = random.randrange(2 ** 31)
    rng = random.Random(seed)

    conn = sqlite3.connect(db_path or _default_db_path())
    conn.row_factory = sqlite3.Row
    try:
        axis_pools = {a: _load_axis(conn, a) for a in vary}
        fixed_vals = {a: _resolve_fixed(conn, a, v, fmt)
                      for a, v in fixed.items()}
        # Outfit pool resolved ONCE (theme tiers), then sampled per subject.
        outfit_pool, outfit_note = _build_outfit_pool(conn, outfit_policy, fmt)

        subjects: list[dict] = []
        used_primary: set[str] = set()
        for _ in range(count):
            traits: dict[str, dict] = {}
            for axis in vary:
                pool = axis_pools.get(axis) or []
                if not pool:
                    continue
                if axis == _PRIMARY_AXIS:
                    avail = [p for p in pool
                             if p["item_tag"] not in used_primary] or pool
                    pick = rng.choice(avail)
                    used_primary.add(pick["item_tag"])
                else:
                    pick = rng.choice(pool)
                traits[axis] = pick
            outfit = rng.choice(outfit_pool) if outfit_pool else ""
            subjects.append({
                "label": _label(traits, subject_kind),
                "body": _compose_body(subject_kind, fixed_vals, traits,
                                      vary, outfit, fmt),
            })
    finally:
        conn.close()

    note_suffix = f" ({outfit_note})" if outfit_note else ""

    if mode == "single":
        s = subjects[0]
        return {
            "content": s["body"],
            "summary": f"Generated {s['label']}.{note_suffix}",
            "subjects": subjects[:1], "added": [s["label"]], "skipped": [],
            "mode": mode, "seed": seed, "outfit_note": outfit_note,
        }

    # inline_wildcards: dedup labels (incl. against existing node content),
    # append non-destructively.
    seen = _existing_labels(existing_content or "")
    added: list[str] = []
    skipped: list[str] = []
    blocks: list[str] = []
    for s in subjects:
        label = s["label"]
        # Collision fallback: descriptive labels can repeat; suffix an
        # index so switch/roll can address each distinctly.
        final = label
        n = 2
        while final.lower() in seen:
            final = f"{label} {n}"
            n += 1
        if final != label:
            # only treat as skip if it was a true duplicate body we don't
            # want; here we keep all generated subjects, just disambiguate.
            pass
        seen.add(final.lower())
        s["label"] = final
        blocks.append(_format_entry(final, s["body"]))
        added.append(final)

    appended = "\n".join(blocks)
    existing = existing_content or ""
    content = (existing.rstrip() + "\n" + appended) if existing.strip() else appended
    preview = ", ".join(added[:6]) + (
        f", +{len(added) - 6} more" if len(added) > 6 else ""
    )
    summary = (
        f"Generated {len(added)} subject{'' if len(added) == 1 else 's'} "
        f"as inline wildcards ({preview}); node now has {len(seen)} "
        f"entries.{note_suffix}"
    )
    return {
        "content": content, "summary": summary, "subjects": subjects,
        "added": added, "skipped": skipped, "mode": mode, "seed": seed,
        "outfit_note": outfit_note,
    }
