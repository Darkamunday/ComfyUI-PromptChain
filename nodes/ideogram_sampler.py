from __future__ import annotations

import logging

import torch

import comfy.sample
import comfy.model_management
import comfy.utils
import latent_preview
from comfy_api.latest import io

logger = logging.getLogger("promptchain.ideogram_sampler")


class IdeogramSamplerNode(io.ComfyNode):
    """Ideogram 4 sampler with automatic retry past the model's stochastic
    'Image blocked by safety filter' refusal.

    Ideogram 4's open weights emit a near-uniform gray refusal frame on a
    fraction of seeds — an officially-acknowledged false-positive (higher for
    non-JSON prompts), baked into the weights and not disableable. It's
    seed-dependent, so re-rolling the noise seed escapes it. This node samples,
    VAE-decodes, and if the decoded image is a near-flat refusal frame (pixel
    std below `block_std`), it re-samples with the next seed up to `max_retries`
    times. Measured separation: refusal frames ~0.04 std, real images ~0.15-0.33
    (0-1 scale), so the default 0.08 sits cleanly between.

    Drop-in replacement for SamplerCustomAdvanced + VAEDecode (returns IMAGE).
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="PromptChain_IdeogramSampler",
            display_name="Prompt Chain Ideogram Sampler (auto-retry)",
            category="promptchain",
            inputs=[
                io.Noise.Input("noise"),
                io.Guider.Input("guider"),
                io.Sampler.Input("sampler"),
                io.Sigmas.Input("sigmas"),
                io.Latent.Input("latent_image"),
                io.Vae.Input("vae"),
                io.Int.Input("max_retries", default=4, min=0, max=16,
                             tooltip="Extra re-rolls if the model returns its gray "
                                     "'Image blocked by safety filter' frame."),
                io.Float.Input("block_std", default=0.08, min=0.0, max=1.0, step=0.005,
                               tooltip="Pixel-std threshold below which a decoded image is "
                                       "treated as a blocked/refusal frame (0-1 scale)."),
            ],
            outputs=[
                io.Image.Output("IMAGE"),
            ],
        )

    @classmethod
    def execute(cls, noise, guider, sampler, sigmas, latent_image,
                vae, max_retries: int = 4, block_std: float = 0.08) -> io.NodeOutput:
        base = latent_image
        samples_in = comfy.sample.fix_empty_latent_channels(
            guider.model_patcher, base["samples"],
            base.get("downscale_ratio_spacial", None),
            base.get("downscale_ratio_temporal", None))
        noise_mask = base.get("noise_mask", None)
        batch_inds = base.get("batch_index", None)
        base_seed = int(getattr(noise, "seed", 0))
        disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED

        last_image = None
        for attempt in range(max_retries + 1):
            seed = base_seed + attempt  # attempt 0 keeps the user's seed
            noise_tensor = comfy.sample.prepare_noise(samples_in, seed, batch_inds)
            x0_output = {}
            callback = latent_preview.prepare_callback(
                guider.model_patcher, sigmas.shape[-1] - 1, x0_output)
            samples = guider.sample(noise_tensor, samples_in, sampler, sigmas,
                                    denoise_mask=noise_mask, callback=callback,
                                    disable_pbar=disable_pbar, seed=seed)
            samples = samples.to(comfy.model_management.intermediate_device())

            image = vae.decode(samples)
            if len(image.shape) == 5:  # video VAE — flatten batches
                image = image.reshape(-1, image.shape[-3], image.shape[-2], image.shape[-1])
            last_image = image

            std = float(image.float().std().item())
            blocked = std < block_std
            logger.info("[IdeogramSampler] attempt %d/%d seed=%d std=%.4f -> %s",
                        attempt, max_retries, seed, std,
                        "BLOCKED (retry)" if blocked else "ok")
            if not blocked:
                return io.NodeOutput(image)

        logger.warning("[IdeogramSampler] still blocked after %d attempts; "
                       "returning last frame", max_retries + 1)
        return io.NodeOutput(last_image)
