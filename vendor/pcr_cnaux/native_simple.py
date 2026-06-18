"""Clean-room preprocessor detectors — tile, luminance, scribble (XDoG).

These are standard, model-free OpenCV image operations written fresh for
PromptChain (NOT copied from comfyui_controlnet_aux, whose tile/recolor/scribble
modules ship without a license file). They reuse this package's shared helpers
only (HWC3 / resize_image_with_pad / common_input_validate), matching the
detector call convention common_annotator_call expects.
"""
import cv2
import numpy as np
from PIL import Image

from pcr_cnaux.util import HWC3, resize_image_with_pad, common_input_validate


class TileDetector:
    """Blur-tile hint: downscale by 2**iters then pyramid-up back, so only the
    coarse structure survives — the standard ControlNet 'tile' guidance map."""

    def __call__(self, input_image=None, pyrUp_iters=3, detect_resolution=512,
                 output_type=None, upscale_method="INTER_AREA", **kwargs):
        input_image, output_type = common_input_validate(input_image, output_type, **kwargs)
        detected_map, remove_pad = resize_image_with_pad(input_image, detect_resolution, upscale_method)
        H, W = detected_map.shape[:2]
        small = cv2.resize(
            detected_map,
            (max(1, W >> pyrUp_iters), max(1, H >> pyrUp_iters)),
            interpolation=cv2.INTER_AREA,
        )
        for _ in range(pyrUp_iters):
            small = cv2.pyrUp(small)
        out = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
        detected_map = HWC3(remove_pad(out))
        if output_type == "pil":
            detected_map = Image.fromarray(detected_map)
        return detected_map


class LuminanceDetector:
    """Perceptual luminance (CIELAB L channel) as a greyscale recolor hint."""

    def __call__(self, input_image=None, detect_resolution=512,
                 output_type=None, upscale_method="INTER_CUBIC", **kwargs):
        input_image, output_type = common_input_validate(input_image, output_type, **kwargs)
        detected_map, remove_pad = resize_image_with_pad(input_image, detect_resolution, upscale_method)
        lab = cv2.cvtColor(HWC3(detected_map), cv2.COLOR_RGB2LAB)
        gray = cv2.cvtColor(lab[:, :, 0], cv2.COLOR_GRAY2RGB)
        detected_map = HWC3(remove_pad(gray))
        if output_type == "pil":
            detected_map = Image.fromarray(detected_map)
        return detected_map


class ScribbleXDoGDetector:
    """XDoG (eXtended Difference-of-Gaussians) edges -> white scribble on black.
    A difference of two Gaussian blurs, thresholded. No model."""

    def __call__(self, input_image=None, thr_a=32, detect_resolution=512,
                 output_type=None, upscale_method="INTER_CUBIC", **kwargs):
        input_image, output_type = common_input_validate(input_image, output_type, **kwargs)
        detected_map, remove_pad = resize_image_with_pad(input_image, detect_resolution, upscale_method)
        gray = cv2.cvtColor(HWC3(detected_map), cv2.COLOR_RGB2GRAY).astype(np.float32)
        dog = cv2.GaussianBlur(gray, (0, 0), 0.5) - cv2.GaussianBlur(gray, (0, 0), 5.0)
        result = np.zeros_like(gray, dtype=np.uint8)
        result[dog > thr_a] = 255
        detected_map = HWC3(remove_pad(result))
        if output_type == "pil":
            detected_map = Image.fromarray(detected_map)
        return detected_map
