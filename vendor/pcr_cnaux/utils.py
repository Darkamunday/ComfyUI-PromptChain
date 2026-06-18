"""Trimmed copy of comfyui_controlnet_aux/utils.py (the top-level one).

Only the node-wrapper helpers DepthAnything + Canny need are kept:
common_annotator_call, the INPUT enum, and define_preprocessor_inputs. The
config.yaml / env-var ckpts bootstrap, log setup, run_script installer, NMS, and
pixel-perfect helpers were dropped — none are reachable from the two wrappers.
"""
import numpy as np
import torch
from enum import Enum
import comfy

# Sync with theoretical limit from Comfy base
MAX_RESOLUTION = 16384


def common_annotator_call(model, tensor_image, input_batch=False, show_pbar=True, **kwargs):
    if "detect_resolution" in kwargs:
        del kwargs["detect_resolution"]  # Prevent weird case?

    if "resolution" in kwargs:
        detect_resolution = kwargs["resolution"] if type(kwargs["resolution"]) == int and kwargs["resolution"] >= 64 else 512
        del kwargs["resolution"]
    else:
        detect_resolution = 512

    if input_batch:
        np_images = np.asarray(tensor_image * 255., dtype=np.uint8)
        np_results = model(np_images, output_type="np", detect_resolution=detect_resolution, **kwargs)
        return torch.from_numpy(np_results.astype(np.float32) / 255.0)

    batch_size = tensor_image.shape[0]
    if show_pbar:
        pbar = comfy.utils.ProgressBar(batch_size)
    out_tensor = None
    for i, image in enumerate(tensor_image):
        np_image = np.asarray(image.cpu() * 255., dtype=np.uint8)
        np_result = model(np_image, output_type="np", detect_resolution=detect_resolution, **kwargs)
        out = torch.from_numpy(np_result.astype(np.float32) / 255.0)
        if out_tensor is None:
            out_tensor = torch.zeros(batch_size, *out.shape, dtype=torch.float32)
        out_tensor[i] = out
        if show_pbar:
            pbar.update(1)
    return out_tensor


def define_preprocessor_inputs(**arguments):
    return dict(
        required=dict(image=INPUT.IMAGE()),
        optional=arguments
    )


class INPUT(Enum):
    def IMAGE():
        return ("IMAGE",)

    def LATENT():
        return ("LATENT",)

    def MASK():
        return ("MASK",)

    def SEED(default=0):
        return ("INT", dict(default=default, min=0, max=0xffffffffffffffff))

    def RESOLUTION(default=512, min=64, max=MAX_RESOLUTION, step=64):
        return ("INT", dict(default=default, min=min, max=max, step=step))

    def INT(default=0, min=0, max=MAX_RESOLUTION, step=1):
        return ("INT", dict(default=default, min=min, max=max, step=step))

    def FLOAT(default=0, min=0, max=1, step=0.01):
        return ("FLOAT", dict(default=default, min=min, max=max, step=step))

    def STRING(default='', multiline=False):
        return ("STRING", dict(default=default, multiline=multiline))

    def COMBO(values, default=None):
        return (values, dict(default=values[0] if default is None else default))

    def BOOLEAN(default=True):
        return ("BOOLEAN", dict(default=default))
