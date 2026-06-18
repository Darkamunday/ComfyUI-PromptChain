"""Natlang section assembly.

The v1 render pipeline (intent application, prose composition, snippet
retrieval, displacement) was retired in Phase G. The v2 path lives in
`natlang_render_v2.py`. Only the section-list-to-output-text serializer
is shared with v2 and remains here.
"""
from __future__ import annotations


def assemble_output_text(sections: list[dict]) -> str:
    """Join the rendered sections into a single output_text string for
    the editor. Mirrors the natlang assembly path in the existing
    /ai/patch flow — body_text per section, separated by blank lines,
    Negative Prompt section gets comma-joined tokens."""
    out_lines: list[str] = []
    for s in sections:
        header = s.get("header") or ""
        if s.get("is_negative"):
            tokens = s.get("tokens") or []
            body = ", ".join(tokens)
        else:
            body = (s.get("body_text") or "").strip()
            if not body:
                body = ", ".join(s.get("tokens") or [])
        if not body:
            continue
        out_lines.append(f"{header}\n{body}")
    return "\n\n".join(out_lines)
