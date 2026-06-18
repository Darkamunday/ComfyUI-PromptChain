from __future__ import annotations

import json

import torch

import node_helpers
from comfy_api.latest import io

from ..core.compiler import region_figure_indices, region_orphans
from .attention_couple import _dilate


class RegionalConditioningNode(io.ComfyNode):
    """Regions JSON + figure masks -> ComfyUI native mask conditioning.

    The attention couple's model patch can't survive tiled samplers — each
    tile re-interpolates the full-canvas masks into its own latent. ComfyUI's
    cond masks travel INSIDE the conditioning instead, and tile-aware samplers
    (Ultimate SD Upscale's crop_cond) crop them per tile. This node emits the
    global text unmasked plus each region's text masked to its figure, so an
    upscale tile over a figure samples with that figure's own prompt.
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PromptChain_RegionalConditioning",
            display_name="Prompt Chain Regional Conditioning",
            category="promptchain",
            inputs=[
                io.Clip.Input("clip"),
                io.String.Input("regions", default="", force_input=True,
                                tooltip="Wire to Prompt Chain's 'regions' output (4th)."),
                io.Mask.Input("masks", optional=True,
                              tooltip="Per-figure masks from the 3D Poser MASKS output."),
                io.String.Input("pose", default="", optional=True, force_input=True,
                                tooltip="Wire to the 3D Poser's POSE_JSON output — carries the "
                                        "figure names so renamed $blocks bind to the right mask."),
                io.Int.Input("mask_dilation", default=22, min=0, max=128, step=1,
                             tooltip="Grow each region mask so ribbons/hair/props aren't clipped."),
            ],
            outputs=[
                io.Conditioning.Output("positive"),
                io.Conditioning.Output("negative"),
            ],
        )

    @classmethod
    def execute(cls, clip, regions: str = "", masks: torch.Tensor = None,
                pose: str = "", mask_dilation: int = 22) -> io.NodeOutput:
        def encode(text):
            tokens = clip.tokenize(text or "")
            return clip.encode_from_tokens_scheduled(tokens)

        data = {}
        if regions and regions.strip():
            try:
                data = json.loads(regions)
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(
                    "[RegionalConditioning] 'regions' must be PromptChain's regions JSON "
                    "(4th output). Got " + repr(regions[:160])) from e

        region_list = data.get("regions", [])
        positive = encode(data.get("global", ""))
        negative = encode(data.get("negative", ""))

        # Same region->entity binding as the couple/detailer (name first, then
        # trailing-int id, then block order). Orphan $blocks (mannequin deleted,
        # block left in the prompt) bind to no figure — drop them so a deleted
        # character's tags don't bleed onto a present figure's mask.
        if region_list and masks is not None and masks.shape[0] > 0:
            num_masks = masks.shape[0]  # mask rows = region entities (figures, then named props)
            orphans = region_orphans(region_list, pose)
            for n, (r, idx) in enumerate(zip(region_list, region_figure_indices(region_list, pose))):
                if orphans[n]:
                    continue
                text = (r.get("text") or "").strip()
                if not text:
                    continue  # an empty block has nothing to paint; the global cond still covers it
                idx = min(max(idx, 0), num_masks - 1)
                mask = _dilate(masks[idx:idx + 1], mask_dilation)
                positive += node_helpers.conditioning_set_values(
                    encode(text),
                    {"mask": mask, "set_area_to_bounds": False, "mask_strength": 1.0})

        return io.NodeOutput(positive, negative)
