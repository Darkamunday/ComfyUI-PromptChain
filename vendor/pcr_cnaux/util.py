"""Trimmed copy of comfyui_controlnet_aux/src/custom_controlnet_aux/util.py.

Only the pieces DepthAnything-V2 + Canny actually touch are kept: HWC3,
common_input_validate, resize_image_with_pad (and its helpers), custom_hf_download,
and DEPTH_ANYTHING_V2_MODEL_NAME_DICT. Everything else (DWPose/densepose/etc model
name tables, torch-hub downloaders, ADE palette, NMS) was dropped.

Renamed from `custom_controlnet_aux` to `pcr_cnaux` so this never collides with a
real comfyui_controlnet_aux install. The model-download base dir is routed through
ComfyUI's `folder_paths` (models/controlnet_aux) instead of the upstream env-var /
sibling-`ckpts` scheme, so depth weights land in the PromptChain-managed models tree.
"""
import os
import tempfile
import warnings
from contextlib import suppress
from pathlib import Path

import cv2
import numpy as np
from huggingface_hub import constants, hf_hub_download

# TEED soft-edge weights (7_model.pth) live in this HF repo under Annotators/.
BDS_MODEL_NAME = "bdsqlsz/qinglong_controlnet-lllite"

DEPTH_ANYTHING_V2_MODEL_NAME_DICT = {
    "depth_anything_v2_vits.pth": "depth-anything/Depth-Anything-V2-Small",
    "depth_anything_v2_vitb.pth": "depth-anything/Depth-Anything-V2-Base",
    "depth_anything_v2_vitl.pth": "depth-anything/Depth-Anything-V2-Large",
    "depth_anything_v2_vitg.pth": "depth-anything/Depth-Anything-V2-Giant",
    "depth_anything_v2_metric_vkitti_vitl.pth": "depth-anything/Depth-Anything-V2-Metric-VKITTI-Large",
    "depth_anything_v2_metric_hypersim_vitl.pth": "depth-anything/Depth-Anything-V2-Metric-Hypersim-Large"
}

USE_SYMLINKS = False
temp_dir = tempfile.gettempdir()


def _annotator_ckpts_path():
    """Download base dir for preprocessor weights, inside ComfyUI's models tree.

    Resolved lazily so it follows the running ComfyUI's models path (and any
    --base-directory override) rather than being frozen at import time.
    """
    try:
        import folder_paths
        base = os.path.join(folder_paths.models_dir, "controlnet_aux")
    except Exception:
        base = os.path.join(tempfile.gettempdir(), "pcr_cnaux_ckpts")
    os.makedirs(base, exist_ok=True)
    return base


def HWC3(x):
    assert x.dtype == np.uint8
    if x.ndim == 2:
        x = x[:, :, None]
    assert x.ndim == 3
    H, W, C = x.shape
    assert C == 1 or C == 3 or C == 4
    if C == 3:
        return x
    if C == 1:
        return np.concatenate([x, x, x], axis=2)
    if C == 4:
        color = x[:, :, 0:3].astype(np.float32)
        alpha = x[:, :, 3:4].astype(np.float32) / 255.0
        y = color * alpha + 255.0 * (1.0 - alpha)
        y = y.clip(0, 255).astype(np.uint8)
        return y


def safer_memory(x):
    # Fix many MAC/AMD problems
    return np.ascontiguousarray(x.copy()).copy()


def safe_step(x, step=2):
    y = x.astype(np.float32) * float(step + 1)
    y = y.astype(np.int32).astype(np.float32) / float(step)
    return y


UPSCALE_METHODS = ["INTER_NEAREST", "INTER_LINEAR", "INTER_AREA", "INTER_CUBIC", "INTER_LANCZOS4"]


def get_upscale_method(method_str):
    assert method_str in UPSCALE_METHODS, f"Method {method_str} not found in {UPSCALE_METHODS}"
    return getattr(cv2, method_str)


def pad64(x):
    return int(np.ceil(float(x) / 64.0) * 64 - x)


# https://github.com/Mikubill/sd-webui-controlnet/blob/main/scripts/processor.py#L17
def resize_image_with_pad(input_image, resolution, upscale_method="", skip_hwc3=False, mode='edge'):
    if skip_hwc3:
        img = input_image
    else:
        img = HWC3(input_image)
    H_raw, W_raw, _ = img.shape
    if resolution == 0:
        return img, lambda x: x
    k = float(resolution) / float(min(H_raw, W_raw))
    H_target = int(np.round(float(H_raw) * k))
    W_target = int(np.round(float(W_raw) * k))
    img = cv2.resize(img, (W_target, H_target), interpolation=get_upscale_method(upscale_method) if k > 1 else cv2.INTER_AREA)
    H_pad, W_pad = pad64(H_target), pad64(W_target)
    img_padded = np.pad(img, [[0, H_pad], [0, W_pad], [0, 0]], mode=mode)

    def remove_pad(x):
        return safer_memory(x[:H_target, :W_target, ...])

    return safer_memory(img_padded), remove_pad


def common_input_validate(input_image, output_type, **kwargs):
    if "img" in kwargs:
        warnings.warn("img is deprecated, please use `input_image=...` instead.", DeprecationWarning)
        input_image = kwargs.pop("img")

    if "return_pil" in kwargs:
        warnings.warn("return_pil is deprecated. Use output_type instead.", DeprecationWarning)
        output_type = "pil" if kwargs["return_pil"] else "np"

    if type(output_type) is bool:
        warnings.warn("Passing `True` or `False` to `output_type` is deprecated and will raise an error in future versions")
        if output_type:
            output_type = "pil"

    if input_image is None:
        raise ValueError("input_image must be defined.")

    if not isinstance(input_image, np.ndarray):
        input_image = np.array(input_image, dtype=np.uint8)
        output_type = output_type or "pil"
    else:
        output_type = output_type or "np"

    return (input_image, output_type)


def custom_hf_download(pretrained_model_or_path, filename, cache_dir=None, ckpts_dir=None, subfolder='', use_symlinks=USE_SYMLINKS, repo_type="model"):
    if cache_dir is None:
        cache_dir = temp_dir
    if ckpts_dir is None:
        ckpts_dir = _annotator_ckpts_path()

    local_dir = os.path.join(ckpts_dir, pretrained_model_or_path)
    model_path = Path(local_dir).joinpath(*subfolder.split('/'), filename).__str__()

    if len(str(model_path)) >= 255:
        warnings.warn(f"Path {model_path} is too long, \n please change annotator_ckpts_path in config.yaml")

    if not os.path.exists(model_path):
        print(f"Failed to find {model_path}.\n Downloading from huggingface.co")
        print(f"cacher folder is {cache_dir}, you can change it by custom_tmp_path in config.yaml")
        if use_symlinks:
            cache_dir_d = constants.HF_HUB_CACHE    # use huggingface newer env variables `HF_HUB_CACHE`
            if cache_dir_d is None:
                import platform
                if platform.system() == "Windows":
                    cache_dir_d = Path(os.getenv("USERPROFILE")).joinpath(".cache", "huggingface", "hub").__str__()
                else:
                    cache_dir_d = os.path.join(os.getenv("HOME"), ".cache", "huggingface", "hub")
            try:
                # test_link
                Path(cache_dir_d).mkdir(parents=True, exist_ok=True)
                Path(ckpts_dir).mkdir(parents=True, exist_ok=True)
                (Path(cache_dir_d) / f"linktest_{filename}.txt").touch()
                # symlink instead of link avoid `invalid cross-device link` error.
                os.symlink(os.path.join(cache_dir_d, f"linktest_{filename}.txt"), os.path.join(ckpts_dir, f"linktest_{filename}.txt"))
                print("Using symlinks to download models. \n",
                      "Make sure you have enough space on your cache folder. \n",
                      "And do not purge the cache folder after downloading.\n",
                      "Otherwise, you will have to re-download the models every time you run the script.\n",
                      "You can use USE_SYMLINKS: False in config.yaml to avoid this behavior.")
            except Exception:
                print("Maybe not able to create symlink. Disable using symlinks.")
                use_symlinks = False
                cache_dir_d = Path(cache_dir).joinpath("ckpts", pretrained_model_or_path).__str__()
            finally:    # always remove test link files
                with suppress(FileNotFoundError):
                    os.remove(os.path.join(ckpts_dir, f"linktest_{filename}.txt"))
                    os.remove(os.path.join(cache_dir_d, f"linktest_{filename}.txt"))
        else:
            cache_dir_d = os.path.join(cache_dir, "ckpts", pretrained_model_or_path)

        try:
            model_path = hf_hub_download(repo_id=pretrained_model_or_path,
                cache_dir=cache_dir_d,
                local_dir=local_dir,
                subfolder=subfolder,
                filename=filename,
                local_dir_use_symlinks=use_symlinks,
                resume_download=True,
                etag_timeout=100,
                repo_type=repo_type
            )
        except Exception as exc:
            # Lazy first-use download of a preprocessor model (depth / soft-edge /
            # line-art). Unwrapped, a network/HF/disk failure surfaces as a raw
            # traceback in the ComfyUI error box — turn it into something the user
            # can act on instead.
            raise RuntimeError(
                f"Couldn't download the ControlNet preprocessor model '{filename}' "
                f"from Hugging Face ({pretrained_model_or_path}). Check your internet "
                f"connection, or pre-install it from ComfyUI Settings → PromptChain "
                f"→ Features → Install features (ControlNet). "
                f"[{type(exc).__name__}: {exc}]"
            ) from exc
        if not use_symlinks:
            try:
                import shutil
                shutil.rmtree(os.path.join(cache_dir, "ckpts"))
            except Exception as e:
                print(e)

    print(f"model_path is {model_path}")

    return model_path
