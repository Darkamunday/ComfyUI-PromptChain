from __future__ import annotations

import json
import logging

from comfy_api.latest import io

logger = logging.getLogger("promptchain.ideogram_caption")


def _envelope(text: str) -> str:
    """Wrap plain text in Ideogram's minimal JSON caption envelope.

    Ideogram 4 was trained on structured JSON captions; its docs state the
    'Image blocked by safety filter' false-positive rate is HIGHER for non-JSON
    prompts. Measured on a benign prompt: plain text blocked 4/5 seeds, this
    mechanical envelope (coherent text) blocked 1/5. The remaining ~20% is
    cleared by the auto-retry sampler. (A real per-element deconstruction via an
    LLM gets to 0/5 — that's the optional upgrade when a local LLM is set up.)
    """
    t = (text or "").strip()
    return json.dumps(
        {"high_level_description": t,
         "compositional_deconstruction": {"background": t, "elements": []}},
        ensure_ascii=False)


class IdeogramCaptionNode(io.ComfyNode):
    """Turn a PromptChain prompt into an Ideogram-friendly JSON caption.

    Sits between PromptChain's `positive` output and CLIPTextEncode in the
    Ideogram template. JSON-shaped prompts trip Ideogram's stochastic safety
    refusal far less often than plain text. If the user already wrote valid JSON,
    it's passed through untouched.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PromptChain_IdeogramCaption",
            display_name="Prompt Chain Ideogram Caption",
            category="promptchain",
            inputs=[
                io.String.Input("text", default="", force_input=True,
                                tooltip="Wire to Prompt Chain's 'positive' output."),
                io.Combo.Input("mode", options=["envelope", "off"], default="envelope",
                               tooltip="envelope = wrap plain text in Ideogram's JSON caption "
                                       "(reduces the model's false safety blocks); off = passthrough."),
            ],
            outputs=[
                io.String.Output("text"),
            ],
        )

    @classmethod
    def execute(cls, text: str = "", mode: str = "envelope") -> io.NodeOutput:
        s = (text or "").strip()
        if mode == "off" or not s:
            return io.NodeOutput(text)
        # Already valid JSON (user hand-wrote a caption) — leave it alone.
        if s.startswith("{") and s.endswith("}"):
            try:
                json.loads(s)
                return io.NodeOutput(text)
            except json.JSONDecodeError:
                pass
        return io.NodeOutput(_envelope(text))
