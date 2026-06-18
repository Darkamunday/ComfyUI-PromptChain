"""PromptState v2 — structured representation of a natlang prompt.

The /ai/patch render flow keeps this dataclass as the source of truth
between turns. node_prompt in the editor is the rendered prose VIEW;
PromptState is the model.

v2 schema rationale (replaces v1's disjoint cleared_slots / extra_phrases /
kept_slot_filter fields with a single per-slot SlotState map):

  - The four ad-hoc helpers (`apply_reverse_displacement`,
    `_ensure_active_slots_mentioned`, `_filter_outfit_extras`,
    `scrub_displaced_modifier_phrases`) all existed because v1 split
    each slot's truth across multiple fields. A single typed SlotState
    per slot collapses those reconciliation seams.
  - `mode` is gone. "stripped" is emergent (non-kept slots are
    `cleared(by_modifier="strip")`), and "named" vs "custom" is
    derivable from `bio_outfit_id`.
  - `pose.descriptive_facts` separates carry-over user posture facts
    (`["sitting", "legs up"]`) from `pose_modifiers` (canonical
    pose-domain slot_modifiers like `presenting_foot`). Both concepts
    were collapsed into v1's single `extra_phrases` and leaked into
    each other.

Frontend persists this in `node.properties.pcrPromptState` (JSON), passes
it in /ai/patch body, and stores the returned updated state on Apply.

This file is Phase A of the vibrant-rendering-loom plan: data + (de)serialization
+ v1→v2 migration shim + v1 backward-compat property shims.

The compat shims (`slots`, `cleared_slots`, `kept_slot_filter`,
`extra_phrases`, `mode`) are attached to OutfitState AFTER the dataclass
decorator runs, so they don't clobber InitVar defaults during class-body
evaluation. Removed in Phase G after callers migrate to slot_states-native
access.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict, InitVar
from typing import Any, Optional


# Defensive strip for natlang fields. Curated DB rows or prior-turn
# persisted states sometimes carry a leading `// Section:` line in the
# prose body; the render flow emits its own header so the duplicate
# would otherwise stack.
_LEADING_SECTION_HEADER_RE = re.compile(r"^\s*//\s*[A-Za-z][^\n]*\n+")


def _strip_leading_section_header(text: str) -> str:
    if not text:
        return text
    return _LEADING_SECTION_HEADER_RE.sub("", text).strip()


# ── slot taxonomy ──────────────────────────────────────────────────
SLOT_NAMES: tuple[str, ...] = (
    "tops", "bottoms", "dresses",
    "headwear", "footwear", "legwear",
    "handwear", "lingerie", "swimwear",
    "neckwear", "accessories", "modifiers",
)

SLOT_STATE_FILLED = "filled"
SLOT_STATE_CLEARED = "cleared"

ORIGIN_BIO = "bio"
ORIGIN_USER = "user"
ORIGIN_SWAP = "swap"
ORIGIN_NONE = ""

# v1 mode constants, kept for compat with callers that still import them.
OUTFIT_MODE_NAMED = "named"
OUTFIT_MODE_STRIPPED = "stripped"
OUTFIT_MODE_CUSTOM = "custom"

# Static seed list used during v1→v2 migration when DB classification isn't
# available. Runtime classification consults the DB.
_KNOWN_OUTFIT_MODIFIERS: frozenset[str] = frozenset({
    "barefoot", "topless", "bottomless", "nude", "naked",
    "fully_clothed", "exposed_chest", "exposed_breasts",
    "no_bra", "no_panties", "underwear_only", "lingerie_only",
    "stripped", "undressed",
})

_KNOWN_POSE_MODIFIERS: frozenset[str] = frozenset({
    "presenting_foot", "presenting_feet",
    "presenting_chest", "presenting_breasts",
    "presenting_pussy",
    "spreading_legs", "spread_legs",
    "kneeling_pose", "lying_pose",
})


# ── dataclasses ────────────────────────────────────────────────────

@dataclass
class SlotState:
    """Per-slot truth. One per slot in OutfitState.slot_states.

    state="filled" → item populated, optionally color, origin tracks where
    it came from. state="cleared" → by_modifier names the canonical that
    cleared it (e.g. "barefoot"), or "strip" for kept_slot_filter strips,
    or "" if it was simply never set."""
    state: str = SLOT_STATE_CLEARED
    item: str = ""
    color: str = ""
    origin: str = ORIGIN_NONE
    by_modifier: str = ""
    bio_sentence: str = ""


def _empty_slot_states() -> dict[str, SlotState]:
    return {name: SlotState() for name in SLOT_NAMES}


@dataclass
class OutfitState:
    """The character's currently-worn outfit, expressed as a per-slot map.

    No `mode` field: "stripped" is emergent from cleared(by_modifier="strip")
    states; "named" vs "custom" is derivable from bio_outfit_id presence.

    The v1 compat shims (`slots`, `cleared_slots`, `kept_slot_filter`,
    `extra_phrases`, `mode`) are attached as @properties AFTER this class
    is decorated — see _attach_outfit_compat_shims at module bottom."""
    name: str = ""
    bio_outfit_id: Optional[int] = None
    natlang_anchor: str = ""
    slot_states: dict[str, SlotState] = field(default_factory=_empty_slot_states)
    active_modifiers: list[str] = field(default_factory=list)
    user_extra_phrases: list[str] = field(default_factory=list)
    # When the outfit was borrowed from another character (e.g. user
    # said `cammy white in chun-li's outfit`), source_display names
    # the bio that owns it. Drives the `// Outfit: <Name> from
    # Character: <Source>` header so readers see the outfit's true
    # origin, not the subject. Empty when the outfit is the subject's own.
    source_display: str = ""
    # InitVars accept v1 constructor kwargs and route them through the
    # property setters in __post_init__. None of these become instance fields.
    slots: InitVar[Optional[list[dict]]] = None
    cleared_slots: InitVar[Optional[list[str]]] = None
    kept_slot_filter: InitVar[Optional[list[str]]] = None
    extra_phrases: InitVar[Optional[list[str]]] = None
    mode: InitVar[Optional[str]] = None

    def __post_init__(self,
                      slots: Optional[list[dict]],
                      cleared_slots: Optional[list[str]],
                      kept_slot_filter: Optional[list[str]],
                      extra_phrases: Optional[list[str]],
                      mode: Optional[str]):
        for n in SLOT_NAMES:
            if n not in self.slot_states:
                self.slot_states[n] = SlotState()
        del mode  # advisory in v2; silently accepted for compat
        # Order matters: extras first (to populate active_modifiers for clear
        # attribution), then slot fills, then clear declarations, then strip.
        if extra_phrases is not None:
            _outfit_set_extra_phrases(self, extra_phrases)
        if slots is not None:
            _outfit_set_slots(self, slots)
        if cleared_slots is not None:
            _outfit_set_cleared_slots(self, cleared_slots)
        if kept_slot_filter is not None:
            _outfit_set_kept_slot_filter(self, kept_slot_filter)


@dataclass
class PoseState:
    """The character's pose. descriptive_facts holds user-supplied posture
    facts that should carry across turns ("sitting", "legs up"). They
    are separate from pose_modifiers (canonical pose-domain slot_modifiers
    like "presenting_foot") so the two cannot leak into each other —
    the cause of v1's `_filter_outfit_extras` leak.

    `extra_phrases` v1 compat property attached after class decoration."""
    name: str = ""
    bio_pose_id: Optional[int] = None
    natlang_anchor: str = ""
    is_signature: bool = False
    descriptive_facts: list[str] = field(default_factory=list)
    pose_modifiers: list[str] = field(default_factory=list)
    # When the pose was borrowed from another character (`tifa lockhart
    # in cammy's victory pose`), source_display names the bio that owns
    # it. Drives the `// Pose: <Name> ... from Character: <Source>`
    # header. Empty when the pose belongs to the subject character.
    source_display: str = ""
    extra_phrases: InitVar[Optional[list[str]]] = None

    def __post_init__(self, extra_phrases: Optional[list[str]]):
        if extra_phrases is not None:
            _pose_set_extra_phrases(self, extra_phrases)


@dataclass
class CharacterState:
    tag: str
    display: str
    series: str = ""
    base_natlang: str = ""
    base_tags: str = ""
    outfit: OutfitState = field(default_factory=OutfitState)
    pose: PoseState = field(default_factory=PoseState)


@dataclass
class StyleState:
    template_id: str = ""
    name: str = ""


@dataclass
class PromptState:
    """v2 structured truth behind a natlang prompt. Persisted in
    node.properties.pcrPromptState and passed through /ai/patch."""
    characters: list[CharacterState] = field(default_factory=list)
    expression: str = ""
    setting: str = ""
    style: Optional[StyleState] = None
    schema_version: int = 2

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "PromptState":
        """Deserialize. v1 states are detected by the presence of
        outfit-level `cleared_slots`, `extra_phrases`, or `kept_slot_filter`
        keys, and migrated to v2 in-place."""
        if not data or not isinstance(data, dict):
            return cls()
        chars: list[CharacterState] = []
        for c in (data.get("characters") or []):
            if not isinstance(c, dict) or not c.get("tag"):
                continue
            outfit_d = c.get("outfit") or {}
            pose_d = c.get("pose") or {}
            chars.append(CharacterState(
                tag=c.get("tag", ""),
                display=c.get("display", ""),
                series=c.get("series", ""),
                base_natlang=_strip_leading_section_header(c.get("base_natlang", "")),
                base_tags=c.get("base_tags", ""),
                outfit=_outfit_from_dict(outfit_d),
                pose=_pose_from_dict(pose_d),
            ))
        style_d = data.get("style")
        style = None
        if isinstance(style_d, dict) and (style_d.get("template_id") or style_d.get("name")):
            style = StyleState(
                template_id=style_d.get("template_id", ""),
                name=style_d.get("name", ""),
            )
        return cls(
            characters=chars,
            expression=data.get("expression", ""),
            setting=data.get("setting", ""),
            style=style,
            schema_version=2,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "PromptState":
        if not raw:
            return cls()
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls()

    def is_empty(self) -> bool:
        return (
            not self.characters
            and not self.expression
            and not self.setting
            and self.style is None
        )

    def primary_character(self) -> Optional[CharacterState]:
        return self.characters[0] if self.characters else None


# ── dict → dataclass helpers (with v1 migration) ────────────────────

def _is_v1_outfit(d: dict) -> bool:
    return any(k in d for k in ("cleared_slots", "extra_phrases", "kept_slot_filter"))


def _slot_state_from_dict(d: Any) -> SlotState:
    if not isinstance(d, dict):
        return SlotState()
    return SlotState(
        state=d.get("state", SLOT_STATE_CLEARED),
        item=d.get("item", ""),
        color=d.get("color", ""),
        origin=d.get("origin", ORIGIN_NONE),
        by_modifier=d.get("by_modifier", ""),
        bio_sentence=d.get("bio_sentence", ""),
    )


def _outfit_from_dict(d: dict) -> OutfitState:
    if not isinstance(d, dict):
        return OutfitState()
    if _is_v1_outfit(d):
        return _migrate_outfit_v1(d)
    raw_states = d.get("slot_states") or {}
    slot_states: dict[str, SlotState] = {}
    for n in SLOT_NAMES:
        slot_states[n] = _slot_state_from_dict(raw_states.get(n))
    return OutfitState(
        name=d.get("name", ""),
        bio_outfit_id=d.get("bio_outfit_id"),
        natlang_anchor=_strip_leading_section_header(d.get("natlang_anchor", "")),
        slot_states=slot_states,
        active_modifiers=list(d.get("active_modifiers") or []),
        user_extra_phrases=list(d.get("user_extra_phrases") or []),
    )


def _pose_from_dict(d: dict) -> PoseState:
    if not isinstance(d, dict):
        return PoseState()
    if "extra_phrases" in d and "descriptive_facts" not in d:
        return _migrate_pose_v1(d)
    return PoseState(
        name=d.get("name", ""),
        bio_pose_id=d.get("bio_pose_id"),
        natlang_anchor=_strip_leading_section_header(d.get("natlang_anchor", "")),
        is_signature=bool(d.get("is_signature")),
        descriptive_facts=list(d.get("descriptive_facts") or []),
        pose_modifiers=list(d.get("pose_modifiers") or []),
    )


# ── v1 → v2 migration ──────────────────────────────────────────────

def _migrate_outfit_v1(d: dict) -> OutfitState:
    """Convert a v1 OutfitState dict to v2.

    v1 fields → v2 mapping:
      - slots: list[{slot_type, item, color, ...}]
          → slot_states[slot_type] = filled(item, color, origin="bio", bio_sentence)
      - cleared_slots: list[str]
          → slot_states[s] = cleared(by_modifier=<inferred from extras>)
      - kept_slot_filter: list[str] | None
          → all non-kept slots become cleared(by_modifier="strip")
      - extra_phrases: list[str]
          → outfit-domain modifier canonicals → active_modifiers
            pose-domain modifier canonicals → DROPPED (v1 leak fix)
            free-form prose → user_extra_phrases
    """
    slot_states = _empty_slot_states()

    for s in (d.get("slots") or []):
        if not isinstance(s, dict):
            continue
        slot_type = (s.get("slot_type") or "").strip().lower()
        if slot_type not in SLOT_NAMES:
            continue
        item = (s.get("item") or "").strip()
        if not item:
            continue
        slot_states[slot_type] = SlotState(
            state=SLOT_STATE_FILLED,
            item=item,
            color=(s.get("color") or "").strip(),
            origin=ORIGIN_BIO,
            bio_sentence=(s.get("bio_sentence") or "").strip(),
        )

    active_modifiers: list[str] = []
    user_extras: list[str] = []
    for raw in (d.get("extra_phrases") or []):
        if not isinstance(raw, str):
            continue
        canon = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if not canon:
            continue
        if canon in _KNOWN_OUTFIT_MODIFIERS:
            if canon not in active_modifiers:
                active_modifiers.append(canon)
        elif canon in _KNOWN_POSE_MODIFIERS:
            continue
        else:
            user_extras.append(raw.strip())

    for s in (d.get("cleared_slots") or []):
        slot_name = (s or "").strip().lower()
        if slot_name not in SLOT_NAMES:
            continue
        attribution = ""
        for mod in active_modifiers:
            if mod == "barefoot" and slot_name in ("footwear", "legwear"):
                attribution = mod
                break
            if mod in ("topless", "exposed_chest", "exposed_breasts", "no_bra") and slot_name in ("tops", "lingerie"):
                attribution = mod
                break
            if mod in ("bottomless", "no_panties") and slot_name in ("bottoms", "lingerie"):
                attribution = mod
                break
            if mod in ("nude", "naked", "stripped", "undressed"):
                attribution = mod
                break
        slot_states[slot_name] = SlotState(
            state=SLOT_STATE_CLEARED,
            by_modifier=attribution,
        )

    kept = d.get("kept_slot_filter")
    if isinstance(kept, list):
        kept_set = {(k or "").strip().lower() for k in kept}
        for slot_name in SLOT_NAMES:
            if slot_name in kept_set:
                continue
            cur = slot_states[slot_name]
            if cur.state == SLOT_STATE_FILLED:
                continue
            slot_states[slot_name] = SlotState(
                state=SLOT_STATE_CLEARED,
                by_modifier=cur.by_modifier or "strip",
            )

    return OutfitState(
        name=d.get("name", ""),
        bio_outfit_id=d.get("bio_outfit_id"),
        natlang_anchor=_strip_leading_section_header(d.get("natlang_anchor", "")),
        slot_states=slot_states,
        active_modifiers=active_modifiers,
        user_extra_phrases=user_extras,
    )


def _migrate_pose_v1(d: dict) -> PoseState:
    descriptive_facts: list[str] = []
    pose_modifiers: list[str] = []
    for raw in (d.get("extra_phrases") or []):
        if not isinstance(raw, str):
            continue
        phrase = raw.strip()
        if not phrase:
            continue
        canon = phrase.lower().replace(" ", "_").replace("-", "_")
        if canon in _KNOWN_POSE_MODIFIERS:
            if canon not in pose_modifiers:
                pose_modifiers.append(canon)
        else:
            descriptive_facts.append(phrase)
    return PoseState(
        name=d.get("name", ""),
        bio_pose_id=d.get("bio_pose_id"),
        natlang_anchor=_strip_leading_section_header(d.get("natlang_anchor", "")),
        is_signature=bool(d.get("is_signature")),
        descriptive_facts=descriptive_facts,
        pose_modifiers=pose_modifiers,
    )


# ── v1 backward-compat shims (attached as properties after class decoration) ──

def _outfit_get_slots(self: OutfitState) -> list[dict]:
    out: list[dict] = []
    for n in SLOT_NAMES:
        s = self.slot_states[n]
        if s.state != SLOT_STATE_FILLED or not s.item:
            continue
        row = {"slot_type": n, "item": s.item}
        if s.color:
            row["color"] = s.color
        if s.bio_sentence:
            row["bio_sentence"] = s.bio_sentence
        out.append(row)
    return out


def _outfit_set_slots(self: OutfitState, rows: Optional[list[dict]]) -> None:
    new_filled: set[str] = set()
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        slot_type = (row.get("slot_type") or row.get("slot") or "").strip().lower()
        if slot_type not in SLOT_NAMES:
            continue
        item = (row.get("item") or "").strip()
        if not item:
            continue
        self.slot_states[slot_type] = SlotState(
            state=SLOT_STATE_FILLED,
            item=item,
            color=(row.get("color") or "").strip(),
            origin=row.get("origin") or self.slot_states[slot_type].origin or ORIGIN_BIO,
            bio_sentence=(row.get("bio_sentence") or "").strip(),
        )
        new_filled.add(slot_type)
    # Slots that were previously filled but aren't in the new list become
    # absent (caller is replacing the active fill set; they didn't ask to
    # clear-with-modifier).
    for n in SLOT_NAMES:
        if n in new_filled:
            continue
        cur = self.slot_states[n]
        if cur.state == SLOT_STATE_FILLED:
            self.slot_states[n] = SlotState()


def _outfit_get_cleared_slots(self: OutfitState) -> list[str]:
    return sorted(
        n for n in SLOT_NAMES
        if self.slot_states[n].state == SLOT_STATE_CLEARED
        and self.slot_states[n].by_modifier
        and self.slot_states[n].by_modifier != "strip"
    )


def _outfit_set_cleared_slots(self: OutfitState, names: Optional[list[str]]) -> None:
    """v1 cleared_slots names slots cleared by a modifier. We always force a
    non-empty by_modifier attribution so the readback list isn't empty."""
    wanted = {(n or "").strip().lower() for n in (names or [])}
    wanted.discard("")
    fallback = self.active_modifiers[0] if self.active_modifiers else "legacy"
    for n in SLOT_NAMES:
        cur = self.slot_states[n]
        if n in wanted:
            if cur.state == SLOT_STATE_FILLED:
                continue  # explicit fills survive
            attrib = cur.by_modifier if (cur.by_modifier and cur.by_modifier != "strip") else fallback
            self.slot_states[n] = SlotState(state=SLOT_STATE_CLEARED, by_modifier=attrib)
        else:
            if cur.state == SLOT_STATE_CLEARED and cur.by_modifier and cur.by_modifier != "strip":
                self.slot_states[n] = SlotState()


def _outfit_get_kept_slot_filter(self: OutfitState) -> Optional[list[str]]:
    strip_active = any(self.slot_states[n].by_modifier == "strip" for n in SLOT_NAMES)
    if not strip_active:
        return None
    return sorted(
        n for n in SLOT_NAMES
        if self.slot_states[n].state == SLOT_STATE_FILLED
        or self.slot_states[n].by_modifier != "strip"
    )


def _outfit_set_kept_slot_filter(self: OutfitState, names: Optional[list[str]]) -> None:
    if names is None:
        for n in SLOT_NAMES:
            if self.slot_states[n].by_modifier == "strip":
                self.slot_states[n] = SlotState()
        return
    kept = {(k or "").strip().lower() for k in names}
    kept.discard("")
    for n in SLOT_NAMES:
        if n in kept:
            continue
        cur = self.slot_states[n]
        if cur.state == SLOT_STATE_FILLED:
            continue
        self.slot_states[n] = SlotState(
            state=SLOT_STATE_CLEARED,
            by_modifier=cur.by_modifier or "strip",
        )


def _outfit_get_extra_phrases(self: OutfitState) -> list[str]:
    return list(self.active_modifiers) + list(self.user_extra_phrases)


def _outfit_set_extra_phrases(self: OutfitState, phrases: Optional[list[str]]) -> None:
    active: list[str] = []
    user: list[str] = []
    for raw in (phrases or []):
        if not isinstance(raw, str) or not raw.strip():
            continue
        canon = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if canon in _KNOWN_OUTFIT_MODIFIERS:
            if canon not in active:
                active.append(canon)
        elif canon in _KNOWN_POSE_MODIFIERS:
            continue
        else:
            user.append(raw.strip())
    self.active_modifiers = active
    self.user_extra_phrases = user


def _outfit_get_mode(self: OutfitState) -> str:
    if any(self.slot_states[n].by_modifier == "strip" for n in SLOT_NAMES):
        return OUTFIT_MODE_STRIPPED
    if self.bio_outfit_id is not None:
        return OUTFIT_MODE_NAMED
    return OUTFIT_MODE_CUSTOM


def _outfit_set_mode(self: OutfitState, _value: str) -> None:
    return  # advisory only


def _pose_get_extra_phrases(self: PoseState) -> list[str]:
    return list(self.descriptive_facts) + list(self.pose_modifiers)


def _pose_set_extra_phrases(self: PoseState, phrases: Optional[list[str]]) -> None:
    descriptive: list[str] = []
    modifiers: list[str] = []
    for raw in (phrases or []):
        if not isinstance(raw, str) or not raw.strip():
            continue
        canon = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if canon in _KNOWN_POSE_MODIFIERS:
            if canon not in modifiers:
                modifiers.append(canon)
        else:
            descriptive.append(raw.strip())
    self.descriptive_facts = descriptive
    self.pose_modifiers = modifiers


# Attach property descriptors AFTER @dataclass decoration. Doing it inside
# the class body would clobber the InitVar class attrs and break dataclass's
# default-value detection.
OutfitState.slots = property(_outfit_get_slots, _outfit_set_slots)
OutfitState.cleared_slots = property(_outfit_get_cleared_slots, _outfit_set_cleared_slots)
OutfitState.kept_slot_filter = property(_outfit_get_kept_slot_filter, _outfit_set_kept_slot_filter)
OutfitState.extra_phrases = property(_outfit_get_extra_phrases, _outfit_set_extra_phrases)
OutfitState.mode = property(_outfit_get_mode, _outfit_set_mode)
PoseState.extra_phrases = property(_pose_get_extra_phrases, _pose_set_extra_phrases)
