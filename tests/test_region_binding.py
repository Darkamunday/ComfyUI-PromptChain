"""Standalone tests for compiler.region_figure_indices / region_figure_count.

compiler.py imports yaml at module level; the binder is pure (stdlib json/re),
so lift just those functions out of the source via AST and exec them in a clean
namespace — same pattern as test_compact_history.py. Runs the real source text.

Run directly:  python tests/test_region_binding.py
(also collectable by pytest if available).
"""
import ast
import json
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "core" / "compiler.py"
_WANT_FUNCS = {"region_figure_indices", "region_figure_count", "region_orphans",
               "_pose_entity_index", "_region_fallback_index"}


def _load_pure_namespace() -> dict:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in _WANT_FUNCS]
    missing = _WANT_FUNCS - {n.name for n in picked}
    if missing:
        raise AssertionError(f"functions not found in source: {sorted(missing)}")
    module = ast.Module(body=picked, type_ignores=[])
    import re
    ns: dict = {"json": json, "re": re}
    exec(compile(module, str(_SRC), "exec"), ns)
    return ns


_NS = _load_pure_namespace()
indices = _NS["region_figure_indices"]
fig_count = _NS["region_figure_count"]
orphans = _NS["region_orphans"]


def _regions(*names):
    """Region list the compiler would emit for $name{} blocks in order:
    id = the name's trailing integer, else 1-based block order."""
    import re as _re
    return [{"id": int(m.group(1)) if (m := _re.search(r"(\d+)$", name)) else n + 1,
             "name": name, "text": "x"}
            for n, name in enumerate(names)]


def _pose_v2(*fig_names):
    return json.dumps({"version": 2,
                       "figures": [({"name": n} if n else {}) for n in fig_names]})


def _pose_v3(entities, fig_names=None):
    pose = {"version": 3,
            "regionEntities": [{"kind": k, "name": n} for k, n in entities]}
    if fig_names is not None:
        pose["figures"] = [({"name": n} if n else {}) for n in fig_names]
    return json.dumps(pose)


# ── legacy (no regionEntities) — must be byte-identical to the old behavior ──

def test_legacy_name_match():
    assert indices(_regions("alice", "bob"), _pose_v2("bob", "alice")) == [1, 0]


def test_legacy_default_mannequin_names():
    assert indices(_regions("mannequin2", "mannequin1"), _pose_v2(None, None)) == [1, 0]


def test_legacy_trailing_int_no_pose():
    assert indices(_regions("mannequin2"), "") == [1]


def test_legacy_block_order_fallback():
    # Unmatched, no trailing int -> id was assigned block-order 1-based.
    assert indices([{"id": 1, "name": "ghost", "text": "x"}], _pose_v2("alice")) == [0]


def test_legacy_unclamped():
    # No entity list -> fallbacks stay UNclamped (callers clamp) — old contract.
    assert indices(_regions("mannequin7"), _pose_v2("alice")) == [6]


# ── entity mode (regionEntities present) ─────────────────────────────────────

ENTS_2F_1P = [("figure", "alice"), ("figure", "bob"), ("prop", "sword")]


def test_entity_name_match_full_list():
    assert indices(_regions("sword", "alice", "bob"), _pose_v3(ENTS_2F_1P)) == [2, 0, 1]


def test_entity_case_insensitive():
    assert indices(_regions("SWORD"), _pose_v3(ENTS_2F_1P)) == [2]


def test_entity_unmatched_block_order_clamps_to_figures():
    # $ghost at block position 3 (id 3) must NOT land on the prop row (2):
    # positional fallbacks are figure-space only.
    regs = _regions("alice", "bob") + [{"id": 3, "name": "ghost", "text": "x"}]
    assert indices(regs, _pose_v3(ENTS_2F_1P)) == [0, 1, 1]


def test_entity_unmatched_trailing_int_clamps_to_figures():
    assert indices(_regions("mannequin7"), _pose_v3(ENTS_2F_1P)) == [1]


def test_entity_one_fig_one_prop():
    ents = [("figure", "mannequin1"), ("prop", "desk")]
    assert indices(_regions("desk", "mannequin1"), _pose_v3(ents)) == [1, 0]
    # Unmatched clamps to the single figure row.
    assert indices([{"id": 5, "name": "ghost5", "text": "x"}], _pose_v3(ents)) == [0]


def test_entity_zero_figures_degenerate():
    # All-prop scene: unmatched positional blocks clamp to row 0 (best effort).
    ents = [("prop", "desk"), ("prop", "lamp")]
    assert indices(_regions("lamp"), _pose_v3(ents)) == [1]
    assert indices([{"id": 4, "name": "ghost4", "text": "x"}], _pose_v3(ents)) == [0]


def test_entity_empty_list_falls_back_to_figures():
    pose = json.dumps({"version": 3, "regionEntities": [],
                       "figures": [{"name": "alice"}]})
    assert indices(_regions("alice"), pose) == [0]


# ── region_orphans (drop $blocks whose mannequin was deleted) ────────────────

def test_orphan_deleted_mannequin():
    # The reported bug: $black + $guest in the prompt, only the black figure
    # remains. $guest (id 2 -> idx 1) has no figure -> orphan; $black binds.
    ents = [("figure", "black")]
    regs = _regions("black", "guest")
    assert orphans(regs, _pose_v3(ents)) == [False, True]


def test_orphan_unmatched_trailing_int():
    # $mannequin7 with 2 figures: clamped binding was the bleed bug -> orphan.
    assert orphans(_regions("mannequin7"), _pose_v3(ENTS_2F_1P)) == [True]


def test_orphan_unmatched_block_order():
    regs = _regions("alice", "bob") + [{"id": 3, "name": "ghost", "text": "x"}]
    assert orphans(regs, _pose_v3(ENTS_2F_1P)) == [False, False, True]


def test_orphan_prop_name_match_is_not_orphan():
    # A region bound to a named prop by NAME still paints its prop mask.
    assert orphans(_regions("sword", "alice"), _pose_v3(ENTS_2F_1P)) == [False, False]


def test_orphan_positional_within_figures_kept():
    # Unmatched but in figure range (e.g. a seeded $mannequin1 before rename)
    # is a real positional bind, NOT an orphan.
    ents = [("figure", "black")]
    assert orphans(_regions("mannequin1"), _pose_v3(ents)) == [False]


def test_orphan_legacy_pose_never_orphans():
    # No entity list -> can't tell stray from positional -> never orphan.
    assert orphans(_regions("mannequin7"), _pose_v2("alice")) == [False]
    assert orphans(_regions("guest"), "") == [False]


def test_orphan_all_prop_scene():
    ents = [("prop", "desk"), ("prop", "lamp")]
    assert orphans(_regions("lamp"), _pose_v3(ents)) == [False]          # prop match
    assert orphans([{"id": 4, "name": "ghost4", "text": "x"}],
                   _pose_v3(ents)) == [True]                              # no figure


# ── region_figure_count ──────────────────────────────────────────────────────

def test_count_v3():
    assert fig_count(_pose_v3(ENTS_2F_1P)) == 2


def test_count_v2_figures():
    assert fig_count(_pose_v2("a", None, "c")) == 3


def test_count_absent_or_garbage():
    assert fig_count("") is None
    assert fig_count("not json") is None
    assert fig_count(json.dumps({"version": 3})) is None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
