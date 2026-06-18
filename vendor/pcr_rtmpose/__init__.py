"""PromptChain-native OpenPose preprocessor (no comfyui_controlnet_aux needed).

Wraps the vendored, Apache-2.0 rtmlib RTMPose/RTMW wholebody estimator
(see ../rtmlib/_VENDORED.txt) to produce a DWPose-grade OpenPose skeleton image
— body + feet + face + both hands — with a clean, redistributable license.

rtmlib is reached only through the relative package path `..rtmlib`, never the
top-level name `rtmlib`, so a user's pip-installed rtmlib can never be shadowed.

onnxruntime (the inference backend) is imported LAZILY inside execute(), so this
node REGISTERS even on a machine where onnxruntime hasn't been pip-installed yet
(the installer adds it later). Missing onnxruntime raises a clear, actionable error.
"""
import os

import numpy as np
import torch


def _resolve_model_dir():
    """ComfyUI-managed dir for rtmlib's auto-downloaded ONNX checkpoints.

    Returns <models_dir>/rtmpose, falling back to rtmlib's own cache only if
    folder_paths is somehow unavailable (it always is, inside ComfyUI).
    """
    try:
        import folder_paths
        base = os.path.join(folder_paths.models_dir, "rtmpose")
    except Exception:
        base = None
    if base:
        os.makedirs(base, exist_ok=True)
    return base


class PromptChain_OpenPose:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "detect_hands": ("BOOLEAN", {"default": True}),
                "detect_face": ("BOOLEAN", {"default": True}),
                "resolution": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 64}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "execute"
    CATEGORY = "PromptChain/Preprocessors"

    def execute(self, image, detect_hands=True, detect_face=True, resolution=512, **kwargs):
        # Lazy backend import — keeps the node registerable without onnxruntime.
        try:
            import onnxruntime  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "PromptChain OpenPose needs the 'onnxruntime' package for inference. "
                "Install it with `pip install onnxruntime` (CPU) or "
                "`pip install onnxruntime-gpu` (CUDA), then retry."
            ) from exc

        import cv2

        from ..rtmlib import Wholebody, draw_skeleton
        from ..rtmlib.tools import file as _rtm_file

        device = "cuda" if torch.cuda.is_available() else "cpu"

        model_dir = _resolve_model_dir()
        _original_hub_dir = _rtm_file._get_rtmhub_dir
        if model_dir:
            # rtmlib appends '/checkpoints' to this; redirect the whole cache
            # into <models_dir>/rtmpose for the duration of model construction.
            _rtm_file._get_rtmhub_dir = lambda: model_dir
        try:
            estimator = Wholebody(
                to_openpose=True,
                mode="balanced",
                backend="onnxruntime",
                device=device,
            )
        except Exception as exc:
            raise RuntimeError(
                "PromptChain OpenPose failed to load the RTMW wholebody model. "
                "This usually means the ONNX checkpoints could not be downloaded "
                "(network) or onnxruntime could not initialise. Original error: "
                f"{exc}"
            ) from exc
        finally:
            _rtm_file._get_rtmhub_dir = _original_hub_dir

        out_frames = []
        for frame in image:
            # ComfyUI IMAGE: (H, W, 3) float RGB in [0,1] -> uint8 BGR for rtmlib/cv2.
            rgb = (frame.detach().cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

            keypoints, scores = estimator(bgr)

            if detect_face is False or detect_hands is False:
                keypoints, scores = self._mask_parts(
                    keypoints, scores, keep_hands=detect_hands, keep_face=detect_face
                )

            canvas = np.zeros_like(bgr)
            canvas = draw_skeleton(
                canvas, keypoints, scores, openpose_skeleton=True, kpt_thr=0.3
            )

            canvas = self._resize_long_side(canvas, resolution)

            out_rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            out_frames.append(torch.from_numpy(out_rgb))

        return (torch.stack(out_frames, dim=0),)

    @staticmethod
    def _mask_parts(keypoints, scores, keep_hands, keep_face):
        """Suppress face/hand keypoints by zeroing their confidence.

        134-keypoint OpenPose wholebody layout (rtmlib convert_coco_to_openpose):
          0..17  body (+neck),  18..23 feet,  24..91 face,  92..112 left hand,
          113..133 right hand.  Zeroed scores fall below kpt_thr and are not drawn.
        """
        scores = np.array(scores, copy=True)
        if scores.ndim == 2:
            face_slice = slice(24, 92)
            hand_slice = slice(92, 134)
            if not keep_face and scores.shape[1] > 24:
                scores[:, face_slice] = 0.0
            if not keep_hands and scores.shape[1] > 92:
                scores[:, hand_slice] = 0.0
        return keypoints, scores

    @staticmethod
    def _resize_long_side(img, resolution):
        h, w = img.shape[:2]
        if h == 0 or w == 0:
            return img
        import cv2
        scale = float(resolution) / float(max(h, w))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        return cv2.resize(img, (new_w, new_h), interpolation=interp)


NODE_CLASS_MAPPINGS = {
    "PromptChain_OpenPose": PromptChain_OpenPose,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptChain_OpenPose": "Prompt Chain OpenPose",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
