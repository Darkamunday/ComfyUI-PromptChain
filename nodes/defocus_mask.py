from __future__ import annotations

import logging

import torch
import torch.nn.functional as F

from comfy_api.latest import io

log = logging.getLogger("promptchain.defocus_mask")


# ── Background mask from a depth map ────────────────────────────────────────
# Upscaling a render with shallow depth of field stretches the generator's
# sub-acuity mottle in defocused regions into visible "wrinkles", and any
# detail-adding stage amplifies it further — measured on real outputs. Local
# frequency statistics CANNOT find those regions (the mottle itself carries
# more energy than smooth in-focus surfaces — calibrated and rejected), so
# the discriminator is depth: background = the far class of an Otsu split on
# a monocular depth map (DepthAnythingV2 upstream).

def _box_blur(x: torch.Tensor, radius: int) -> torch.Tensor:
    """[B,1,H,W] box blur; cheap separable stand-in for a gaussian feather."""
    if radius < 1:
        return x
    k = radius * 2 + 1
    kernel = torch.ones(1, 1, 1, k, device=x.device) / k
    x = F.conv2d(F.pad(x, (radius, radius, 0, 0), mode="replicate"), kernel)
    x = F.conv2d(F.pad(x, (0, 0, radius, radius), mode="replicate"), kernel.transpose(2, 3))
    return x


def _otsu_threshold(depth: torch.Tensor) -> float:
    """Classic Otsu two-class split on a [H,W] 0..1 depth map."""
    hist = torch.histc(depth.float(), bins=256, min=0.0, max=1.0)
    p = hist / hist.sum().clamp(min=1)
    centers = (torch.arange(256, device=depth.device) + 0.5) / 256.0
    omega = torch.cumsum(p, 0)
    mu = torch.cumsum(p * centers, 0)
    mu_t = mu[-1]
    denom = (omega * (1.0 - omega)).clamp(min=1e-9)
    sigma_b = (mu_t * omega - mu) ** 2 / denom
    return float(centers[int(sigma_b.argmax())])


class DefocusMaskNode(io.ComfyNode):
    """Background mask from a depth map (1.0 = far/background, 0.0 = subject).
    Feed it a monocular depth render (e.g. DepthAnythingV2, near = bright);
    the subject/background split is found per-image with Otsu, so there is no
    absolute-depth tuning. Built for bokeh-preserving upscales: composite a
    plain resample over the detail-generating path wherever this mask is high.
    """

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="PromptChain_DefocusMask",
            display_name="Prompt Chain Background Mask (Depth)",
            category="promptchain",
            inputs=[
                io.Image.Input("image", tooltip="Depth map image, near = bright "
                                                "(DepthAnythingV2Preprocessor output)."),
                io.Float.Input("bias", default=0.0, min=-0.5, max=0.5, step=0.01,
                               tooltip="Shifts the auto (Otsu) subject/background split. "
                                       "Negative masks less (only the farthest planes), "
                                       "positive masks more."),
                io.Float.Input("softness", default=0.05, min=0.0, max=0.5, step=0.01,
                               tooltip="Depth range around the split that fades instead of "
                                       "cutting hard."),
                io.Int.Input("feather", default=24, min=0, max=512, step=4,
                             tooltip="Spatial falloff (px, at the depth map's resolution) "
                                     "applied after thresholding."),
            ],
            outputs=[
                io.Mask.Output("MASK"),
            ],
        )

    @classmethod
    def execute(cls, image: torch.Tensor, bias: float = 0.0,
                softness: float = 0.05, feather: int = 24) -> io.NodeOutput:
        depth = image.mean(dim=-1)  # [B,H,W], near = bright
        masks = []
        for b in range(depth.shape[0]):
            d = depth[b]
            thr = _otsu_threshold(d) + bias
            half = max(softness * 0.5, 1e-4)
            # far (small depth value) → 1.0, with a soft shoulder around thr
            m = ((thr + half - d) / (2.0 * half)).clamp(0.0, 1.0)
            m = m * m * (3.0 - 2.0 * m)
            masks.append(m)
            log.info("background mask %d: otsu %.3f bias %+.2f → %.1f%% of frame",
                     b, thr - bias, bias, float(m.mean()) * 100.0)
        mask = torch.stack(masks)
        if feather > 0:
            mask = _box_blur(mask.unsqueeze(1), max(1, feather // 2)).squeeze(1)
        return io.NodeOutput(mask.clamp(0.0, 1.0))
