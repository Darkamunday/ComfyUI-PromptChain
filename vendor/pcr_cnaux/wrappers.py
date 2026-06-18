"""PromptChain-native preprocessor nodes (no comfyui_controlnet_aux install needed).

V1-style node classes replicating comfyui_controlnet_aux's
Depth_Anything_V2_Preprocessor and Canny_Edge_Preprocessor INPUT_TYPES/execute,
but registered under PromptChain ids against the renamed `pcr_cnaux` slice so they
coexist with — and never overwrite — a genuine controlnet_aux install.

Imported via the top-level `pcr_cnaux` name (see pcr_cnaux/__init__.py) so the
absolute `pcr_cnaux.*` imports below bind to the same vendored package objects.
"""
from pcr_cnaux.utils import common_annotator_call, INPUT, define_preprocessor_inputs
import comfy.model_management as model_management


def _require_skimage():
    try:
        from skimage import morphology
        return morphology
    except ImportError as exc:
        raise RuntimeError(
            "Prompt Chain Line Art needs scikit-image for edge clean-up. "
            "Install it into ComfyUI's environment with: pip install scikit-image"
        ) from exc


class PromptChain_DepthAnything:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            ckpt_name=INPUT.COMBO(
                ["depth_anything_v2_vitg.pth", "depth_anything_v2_vitl.pth", "depth_anything_v2_vitb.pth", "depth_anything_v2_vits.pth"],
                default="depth_anything_v2_vitl.pth"
            ),
            resolution=INPUT.RESOLUTION()
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, ckpt_name="depth_anything_v2_vitl.pth", resolution=512, **kwargs):
        from pcr_cnaux.depth_anything_v2 import DepthAnythingV2Detector

        model = DepthAnythingV2Detector.from_pretrained(filename=ckpt_name).to(model_management.get_torch_device())
        out = common_annotator_call(model, image, resolution=resolution, max_depth=1)
        del model
        return (out, )


class PromptChain_Canny:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            low_threshold=INPUT.INT(default=100, max=255),
            high_threshold=INPUT.INT(default=200, max=255),
            resolution=INPUT.RESOLUTION()
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, low_threshold=100, high_threshold=200, resolution=512, **kwargs):
        from pcr_cnaux.canny import CannyDetector

        return (common_annotator_call(CannyDetector(), image, low_threshold=low_threshold, high_threshold=high_threshold, resolution=resolution), )


class PromptChain_Tile:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            pyrUp_iters=INPUT.INT(default=3, min=1, max=10),
            resolution=INPUT.RESOLUTION()
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, pyrUp_iters=3, resolution=512, **kwargs):
        from pcr_cnaux.native_simple import TileDetector
        return (common_annotator_call(TileDetector(), image, pyrUp_iters=pyrUp_iters, resolution=resolution), )


class PromptChain_Luminance:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(resolution=INPUT.RESOLUTION())

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, resolution=512, **kwargs):
        from pcr_cnaux.native_simple import LuminanceDetector
        return (common_annotator_call(LuminanceDetector(), image, resolution=resolution), )


class PromptChain_Scribble:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            thr_a=INPUT.INT(default=32, min=1, max=255),
            resolution=INPUT.RESOLUTION()
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, thr_a=32, resolution=512, **kwargs):
        from pcr_cnaux.native_simple import ScribbleXDoGDetector
        return (common_annotator_call(ScribbleXDoGDetector(), image, thr_a=thr_a, resolution=resolution), )


class PromptChain_SoftEdge:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            safe=INPUT.BOOLEAN(default=True),
            resolution=INPUT.RESOLUTION()
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, safe=True, resolution=512, **kwargs):
        from pcr_cnaux.teed import TEDDetector

        model = TEDDetector.from_pretrained().to(model_management.get_torch_device())
        out = common_annotator_call(model, image, resolution=resolution, safe_steps=2 if safe else 0)
        del model
        return (out, )


class PromptChain_LineArt:
    @classmethod
    def INPUT_TYPES(s):
        return define_preprocessor_inputs(
            resolution=INPUT.RESOLUTION(default=1280, step=8),
            object_min_size=INPUT.INT(default=36, min=1),
            object_connectivity=INPUT.INT(default=1, min=1)
        )

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, resolution=1280, object_min_size=36, object_connectivity=1, **kwargs):
        import torch
        from pcr_cnaux.teed import TEDDetector
        morphology = _require_skimage()

        # AnyLine recipe: MistoAI's MTEED (MIT, TEED-based) is the line extractor.
        model = TEDDetector.from_pretrained("TheMistoAI/MistoLine", "MTEED.pth", subfolder="Anyline").to(model_management.get_torch_device())
        result = common_annotator_call(model, image, resolution=resolution).numpy()
        del model

        cleaned = []
        for frame in result:
            keep = morphology.remove_small_objects(
                frame[:, :, 0].astype(bool), min_size=object_min_size, connectivity=object_connectivity
            )
            cleaned.append(torch.from_numpy(frame * keep[:, :, None]))
        return (torch.stack(cleaned), )


NODE_CLASS_MAPPINGS = {
    "PromptChain_DepthAnything": PromptChain_DepthAnything,
    "PromptChain_Canny": PromptChain_Canny,
    "PromptChain_Tile": PromptChain_Tile,
    "PromptChain_Luminance": PromptChain_Luminance,
    "PromptChain_Scribble": PromptChain_Scribble,
    "PromptChain_SoftEdge": PromptChain_SoftEdge,
    "PromptChain_LineArt": PromptChain_LineArt,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptChain_DepthAnything": "Prompt Chain Depth Anything",
    "PromptChain_Canny": "Prompt Chain Canny",
    "PromptChain_Tile": "Prompt Chain Tile",
    "PromptChain_Luminance": "Prompt Chain Luminance",
    "PromptChain_Scribble": "Prompt Chain Scribble",
    "PromptChain_SoftEdge": "Prompt Chain Soft Edge",
    "PromptChain_LineArt": "Prompt Chain Line Art",
}
