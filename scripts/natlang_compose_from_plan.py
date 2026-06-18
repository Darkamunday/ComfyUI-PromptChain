"""Deterministic compose-from-plan — turn a planner output into a
structured `// Section:` prompt body.

Takes the JSON plan from natlang_planner_probe.plan() plus the list of
matched bios (cammy_white_bio, chun-li_bio, etc.) and assembles:

  // Character: <Display> (<Series>)
  <character base_natlang>

  // Outfit: <Outfit Name> from Character: <Display>
  <outfit body — from KB row or generic_outfits or literal text>

  // Pose:
  <per-character pose sentences joined>

  // Scene:
  <plan.scene_text>

  // Style:
  <plan.style_text if any>

The polish step runs after this to convert to a cinematic paragraph.

No silent fallbacks: if the planner said outfit_source=generic and the
generic_outfits lookup misses, we use the literal outfit_text. We
NEVER replace user-named outfits with a character's canon default.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _lookup_canon_outfit(
    conn: sqlite3.Connection, char_tag: str, outfit_text: str,
) -> Optional[dict]:
    """Lookup a canon outfit row for this character by name. Returns
    {name, natlang} or None. NEVER falls back to the character's
    default outfit — the planner is authoritative."""
    if not outfit_text:
        return None
    needle = outfit_text.strip().lower()
    row = conn.execute(
        "SELECT outfit_name, outfit_natlang FROM outfits "
        "WHERE character_tag = ? AND LOWER(outfit_name) LIKE ? "
        "ORDER BY is_default DESC LIMIT 1",
        (char_tag, f"%{needle}%"),
    ).fetchone()
    if row:
        return {"name": row["outfit_name"], "natlang": row["outfit_natlang"]}
    return None


def _lookup_default_outfit(
    conn: sqlite3.Connection, char_tag: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT outfit_name, outfit_natlang FROM outfits "
        "WHERE character_tag = ? AND is_default = 1 LIMIT 1",
        (char_tag,),
    ).fetchone()
    if row:
        return {"name": row["outfit_name"], "natlang": row["outfit_natlang"]}
    return None


def _lookup_generic_outfit(
    conn: sqlite3.Connection, outfit_text: str,
) -> Optional[dict]:
    """Lookup the generic_outfits table for clothing types like
    'bikini', 'school uniform', 'sundress'. Tries exact, plural-
    stripped, and alias substring."""
    if not outfit_text:
        return None
    needle = outfit_text.strip().lower()
    singular = needle.rstrip("s") if needle.endswith("s") else needle
    row = conn.execute(
        "SELECT name, aliases, outfit_natlang FROM generic_outfits "
        "WHERE LOWER(name) = ? "
        "OR LOWER(name) = ? "
        "OR LOWER(name) LIKE ? "
        "OR LOWER(aliases) LIKE ? "
        "LIMIT 1",
        (needle, singular, f"%{needle}%", f"%{needle}%"),
    ).fetchone()
    if row:
        return {"name": row["name"], "natlang": row["outfit_natlang"]}
    return None


def _resolve_outfit_for_char(
    conn: sqlite3.Connection,
    char_tag: str,
    char_display: str,
    outfit_text: str,
    outfit_source: str,
    bios_by_tag: dict[str, dict],
) -> dict:
    """Resolve a planner per_character outfit entry into a section
    body. Returns {header_outfit_name, body, source_char_display}.

    outfit_source values:
      'generic'         : generic_outfits lookup → literal fallback
      'canon'           : this char's canon outfit by name → default
      'borrow:<src>'    : source character's outfit row
      'literal'         : caller-supplied body verbatim, no DB lookup
    """
    src = (outfit_source or "").lower()
    if src == "literal":
        # Caller already has the exact outfit text (e.g. preserved from
        # an existing prompt's // Outfit: section during a multi-char
        # add). Use it verbatim — NEVER override a user's existing
        # outfit with a DB canon/default lookup.
        return {
            "header_outfit_name": "",
            "body": (outfit_text or "").strip(),
            "source_char_display": "",
        }
    if src.startswith("borrow:"):
        source_tag = src.split(":", 1)[1].strip()
        # Borrow outfit_text from source's canon-name match (e.g.
        # "Chun-Li in Cammy's Killer Bee") or default outfit.
        if outfit_text:
            row = _lookup_canon_outfit(conn, source_tag, outfit_text)
            if row:
                source_display = (
                    bios_by_tag.get(source_tag, {}).get("display")
                    or source_tag.replace("_", " ").title()
                )
                return {
                    "header_outfit_name": row["name"],
                    "body": row["natlang"] or outfit_text,
                    "source_char_display": source_display,
                }
        # No specific outfit named or canon lookup missed → source's
        # default outfit.
        row = _lookup_default_outfit(conn, source_tag)
        if row:
            source_display = (
                bios_by_tag.get(source_tag, {}).get("display")
                or source_tag.replace("_", " ").title()
            )
            return {
                "header_outfit_name": row["name"],
                "body": row["natlang"] or "",
                "source_char_display": source_display,
            }
        # Borrow source has no outfit row at all → use literal text.
        return {
            "header_outfit_name": "",
            "body": outfit_text or "(unknown outfit)",
            "source_char_display": "",
        }

    if src == "generic":
        # User-named clothing type. Try generic_outfits first; fall
        # through to literal text if no match.
        row = _lookup_generic_outfit(conn, outfit_text)
        if row:
            return {
                "header_outfit_name": row["name"],
                "body": row["natlang"] or outfit_text,
                "source_char_display": "",
            }
        return {
            "header_outfit_name": "",
            "body": (outfit_text or "").strip(),
            "source_char_display": "",
        }

    # canon source — this character's own outfit, by name or default.
    if outfit_text:
        row = _lookup_canon_outfit(conn, char_tag, outfit_text)
        if row:
            return {
                "header_outfit_name": row["name"],
                "body": row["natlang"] or "",
                "source_char_display": char_display,
            }
    row = _lookup_default_outfit(conn, char_tag)
    if row:
        return {
            "header_outfit_name": row["name"],
            "body": row["natlang"] or "",
            "source_char_display": char_display,
        }
    return {
        "header_outfit_name": "",
        "body": (outfit_text or "").strip(),
        "source_char_display": "",
    }


def compose_from_plan(
    plan_dict: dict,
    bios: list[dict],
    db_path: str,
    *,
    default_negative_block: str = "",
) -> str:
    """Build a structured `// Section:` prompt body from the planner
    output. Polish step runs after this to convert to prose.

    bios: list of bio dicts keyed by tag; used for character display +
    series + base_natlang.
    db_path: path to tag-builder.db for outfit lookups.
    """
    bios_by_tag = {(b.get("tag") or "").lower(): b for b in bios if b}
    cast = plan_dict.get("cast") or []
    per_character = plan_dict.get("per_character") or []
    per_char_by_tag = {(pc.get("tag") or "").lower(): pc for pc in per_character}

    conn = _open_db(db_path)
    try:
        sections: list[str] = []

        # 1. // Character sections — one per cast member, in cast order.
        # Prefer chip-composed v2 prose (richer appearance), fall back
        # to base_natlang for legacy chars without appearance_chip_tags.
        try:
            from core.tag_builder import compose_character_natlang_v2
        except Exception:
            compose_character_natlang_v2 = None
        for cm in cast:
            tag = (cm.get("tag") or "").lower()
            display = cm.get("display") or tag
            bio = bios_by_tag.get(tag) or {}
            series = bio.get("series") or ""
            header = (
                f"// Character: {display} ({series})" if series
                else f"// Character: {display}"
            )
            body = ""
            if compose_character_natlang_v2:
                try:
                    v2 = compose_character_natlang_v2(conn, tag)
                    if v2:
                        intro = (f"{display} from {series}, "
                                 if series else f"{display}, ")
                        body = intro + v2
                except Exception:
                    body = ""
            if not body:
                body = (bio.get("base_natlang") or display).strip()
            sections.append(f"{header}\n{body}")

        # 2. // Outfit sections — one per cast member
        for cm in cast:
            tag = (cm.get("tag") or "").lower()
            display = cm.get("display") or tag
            pc = per_char_by_tag.get(tag, {})
            outfit_text = pc.get("outfit_text") or ""
            outfit_source = pc.get("outfit_source") or "canon"
            resolved = _resolve_outfit_for_char(
                conn, tag, display, outfit_text, outfit_source, bios_by_tag,
            )
            name = resolved["header_outfit_name"]
            src_disp = resolved["source_char_display"]
            if name and src_disp and src_disp != display:
                # Borrow: "// Outfit: Killer Bee from Character: Cammy White"
                header = f"// Outfit: {name} from Character: {src_disp} (worn by {display})"
            elif name and src_disp:
                # Canon self: "// Outfit: Killer Bee from Character: Cammy White"
                header = f"// Outfit: {name} from Character: {src_disp}"
            elif name:
                header = f"// Outfit: {name} (worn by {display})"
            else:
                header = f"// Outfit (worn by {display})"
            body = (resolved["body"] or "").strip()
            if not body:
                body = outfit_text or "(no outfit description)"
            sections.append(f"{header}\n{body}")

        # 3. // Pose section — combine per-character pose_text + the
        # global interaction verb. When the interaction is set
        # (fighting/dancing/etc.) we emit a single Pose section that
        # describes both subjects in active motion.
        interaction = (plan_dict.get("interaction") or "").strip()
        per_char_poses = []
        for cm in cast:
            tag = (cm.get("tag") or "").lower()
            display = cm.get("display") or tag
            pc = per_char_by_tag.get(tag, {})
            pt = (pc.get("pose_text") or "").strip()
            if pt:
                per_char_poses.append(f"{display} {pt}")
        if per_char_poses:
            pose_body = ". ".join(per_char_poses).rstrip(".") + "."
            if interaction:
                pose_body = f"{interaction.capitalize()}: " + pose_body
            sections.append(f"// Pose\n{pose_body}")
        elif interaction:
            sections.append(f"// Pose\n{interaction.capitalize()}.")

        # 4. // Scene
        scene = (plan_dict.get("scene_text") or "").strip()
        if scene:
            sections.append(f"// Scene\n{scene.rstrip('.')}.")

        # 5. // Style
        style = (plan_dict.get("style_text") or "").strip()
        if style:
            sections.append(f"// Style\n{style.rstrip('.')}.")

        # 6. // Lighting (optional, separate from style)
        lighting = (plan_dict.get("lighting_text") or "").strip()
        if lighting:
            sections.append(f"// Lighting\n{lighting.rstrip('.')}.")

        body = "\n\n".join(sections)
        if default_negative_block:
            body += "\n\n" + default_negative_block
        return body
    finally:
        conn.close()


if __name__ == "__main__":
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

    from scripts.natlang_planner_probe import plan  # noqa: E402

    DB_PATH = os.path.join(ROOT, "data", "tag-builder", "tag-builder.db")

    def _load_bio(tag: str) -> dict:
        c = _open_db(DB_PATH)
        try:
            r = c.execute(
                "SELECT tag, display, series, base_natlang FROM characters "
                "WHERE tag = ?", (tag,),
            ).fetchone()
            return dict(r) if r else {"tag": tag, "display": tag}
        finally:
            c.close()

    async def main():
        cases = [
            (
                "cammy white in a blue bikini and chun-li in cammy white's outfit fighting on a beach",
                [
                    {"tag": "cammy_white", "display": "Cammy White"},
                    {"tag": "chun-li", "display": "Chun-Li"},
                ],
            ),
            (
                "cammy white in killer bee outfit",
                [{"tag": "cammy_white", "display": "Cammy White"}],
            ),
        ]
        for req, profiles in cases:
            print("=" * 78)
            print("REQUEST:", req)
            p, _ = await plan(req, profiles)
            print("\nPLAN:")
            print(json.dumps(p, indent=2))
            bios = [_load_bio(pf["tag"]) for pf in profiles]
            structured = compose_from_plan(p or {}, bios, DB_PATH)
            print("\nSTRUCTURED OUTPUT:")
            print(structured)
            print()

    asyncio.run(main())
