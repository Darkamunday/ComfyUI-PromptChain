"""Standalone tests for ai_agent._compact_history and its helpers.

ai_agent.py binds `server.PromptServer.instance.routes` at import time, so it
can't be imported outside a running ComfyUI. The compaction logic is pure
(stdlib `json` + module constants only), so we lift just those nodes out of
the source via AST and exec them in a clean namespace. This runs the real
source text — no copy-paste drift — without the heavy module imports.

Run directly:  python tests/test_compact_history.py
(also collectable by pytest if available).
"""
import ast
import json
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "core" / "ai_agent.py"
_WANT_FUNCS = {"_estimate_message_tokens", "_summarize_dropped_messages", "_compact_history"}
_WANT_CONST_PREFIX = "_HISTORY_"


def _load_pure_namespace() -> dict:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    picked: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith(_WANT_CONST_PREFIX)
            for t in node.targets
        ):
            picked.append(node)
    missing = _WANT_FUNCS - {n.name for n in picked if isinstance(n, ast.FunctionDef)}
    if missing:
        raise AssertionError(f"functions not found in source: {sorted(missing)}")
    module = ast.Module(body=picked, type_ignores=[])
    ns: dict = {"json": json}
    exec(compile(module, str(_SRC), "exec"), ns)
    return ns


_NS = _load_pure_namespace()
_compact_history = _NS["_compact_history"]
_estimate_message_tokens = _NS["_estimate_message_tokens"]
BUDGET_LOCAL = _NS["_HISTORY_BUDGET_LOCAL"]
SUMMARY_MAX = _NS["_HISTORY_SUMMARY_MAX_CHARS"]

LOCAL_PROV = {"is_ollama": True}            # keyless → tight local budget
CLOUD_PROV = {"kind": "claude", "api_key": "sk-test"}  # large budget


# ── canonical-message builders ─────────────────────────────────────

def _user_text(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_patch(narration, request, tool_id):
    return {"role": "assistant", "content": [
        {"type": "text", "text": narration},
        {"type": "tool_use", "id": tool_id, "name": "apply_prompt_patch",
         "input": {"request": request}},
    ]}


def _tool_result(tool_id, body):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": body},
    ]}


def _assistant_text(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _exchange(i, *, result_pad=5000):
    """One full edit round: user ask → assistant tool_use → tool_result →
    assistant narration. result_pad bloats the tool_result (which is dropped,
    never summarized) to push token weight without inflating the summary."""
    tid = f"toolu_{i:04d}"
    return [
        _user_text(f"edit request number {i}: " + ("detail " * 30)),
        _assistant_patch("Sure, applying that.", f"add item_{i} to scene", tid),
        _tool_result(tid, "applied:\n" + ("x" * result_pad)),
        _assistant_text(f"Done — added item_{i}."),
    ]


def _big_history(n):
    h = []
    for i in range(n):
        h.extend(_exchange(i))
    return h


def _head_is_user_text(history):
    if not history:
        return True
    m = history[0]
    if m.get("role") != "user":
        return False
    c = m.get("content")
    return isinstance(c, str) or (
        isinstance(c, list) and any(b.get("type") == "text" for b in c)
    )


# ── tests ──────────────────────────────────────────────────────────

def test_below_budget_is_noop():
    h = _big_history(2)
    assert sum(_estimate_message_tokens(m) for m in h) <= BUDGET_LOCAL
    out, summary = _compact_history(h, "", LOCAL_PROV)
    assert out is h, "below budget must return the same list object untouched"
    assert summary == ""


def test_over_budget_local_compacts():
    h = _big_history(14)
    assert sum(_estimate_message_tokens(m) for m in h) > BUDGET_LOCAL
    out, summary = _compact_history(h, "", LOCAL_PROV)
    assert len(out) < len(h), "should have dropped oldest turns"
    assert sum(_estimate_message_tokens(m) for m in out) <= BUDGET_LOCAL
    assert summary, "dropped turns must produce a summary"
    assert "You:" in summary and "Applied edit:" in summary


def test_retained_tail_starts_at_user_text_boundary():
    # The whole point of boundary-cutting: never leave an orphaned
    # tool_result as the first message (would break Claude's API).
    out, _ = _compact_history(_big_history(14), "", LOCAL_PROV)
    assert _head_is_user_text(out), f"head not a user-text turn: {out[0]!r}"
    # And no tool_result rides at the head.
    head_types = [b.get("type") for b in out[0]["content"]]
    assert "tool_result" not in head_types


def test_summary_excludes_narration_and_results():
    _, summary = _compact_history(_big_history(14), "", LOCAL_PROV)
    # Assistant narration ("Sure, applying that." / "Done — added...") and
    # tool_result bodies must NOT leak into the recap.
    assert "Sure, applying that" not in summary
    assert "Done — added" not in summary
    assert "applied:" not in summary


def test_cloud_budget_does_not_trigger_on_local_sized_history():
    h = _big_history(14)
    out, summary = _compact_history(h, "", CLOUD_PROV)
    assert out is h, "history that trips the local budget stays whole on cloud"
    assert summary == ""


def test_existing_summary_is_preserved_and_extended():
    prior = '- You: "an earlier ask"'
    out, summary = _compact_history(_big_history(14), prior, LOCAL_PROV)
    assert summary.startswith(prior), "prior summary must be kept at the front"
    assert len(summary) > len(prior), "new dropped turns must be appended"


def test_summary_is_capped():
    prior = "- You: \"pad\"\n" * 400  # ~5k chars, over the cap
    assert len(prior) > SUMMARY_MAX
    _, summary = _compact_history(_big_history(14), prior, LOCAL_PROV)
    assert len(summary) <= SUMMARY_MAX
    assert not summary.startswith("\n"), "cap must not leave a leading newline"


def test_single_huge_user_turn_is_noop():
    # One user turn over budget: no safe boundary to cut at, so leave it.
    h = [_user_text("q " * 40000)]
    assert sum(_estimate_message_tokens(m) for m in h) > BUDGET_LOCAL
    out, summary = _compact_history(h, "", LOCAL_PROV)
    assert out is h
    assert summary == ""


def test_empty_history_is_noop():
    out, summary = _compact_history([], "carried", LOCAL_PROV)
    assert out == []
    assert summary == "carried"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
