from packaging import version as version
import comfyui_version
from .patch_lib.FluxPatch import flux_forward_orig
comfyui_ver = version.parse(comfyui_version.__version__)

if comfyui_ver >= version.parse('0.3.25'):
    from .patch_lib.HunYuanVideoPatch import hunyuan_forward_orig
else:
    from .patch_lib.old.HunYuanVideoPatch import hunyuan_forward_orig

if comfyui_ver > version.parse('0.3.19'):
    # support LTXV 0.9.5
    from .patch_lib.LTXVideoPatch import ltx_forward_orig
else:
    from .patch_lib.old.LTXVideoPatch import ltx_forward_orig
from .patch_lib.MochiVideoPatch import mochi_forward
from .patch_lib.WanVideoPatch import wan_forward_orig
from .patch_util import is_hunyuan_video_model, is_ltxv_video_model, is_flux_model, is_mochi_video_model, \
    is_wan_video_model


def get_new_forward_orig(diffusion_model):
    if is_hunyuan_video_model(diffusion_model):
        return hunyuan_forward_orig
    if is_ltxv_video_model(diffusion_model):
        return ltx_forward_orig
    if is_flux_model(diffusion_model):
        return flux_forward_orig
    if is_mochi_video_model(diffusion_model):
        return mochi_forward
    if is_wan_video_model(diffusion_model):
        return wan_forward_orig
    return None

def get_old_method_name(diffusion_model):
    if is_flux_model(diffusion_model) or is_hunyuan_video_model(diffusion_model) or is_wan_video_model(diffusion_model):
        return 'forward_orig'
    if is_ltxv_video_model(diffusion_model) or is_mochi_video_model(diffusion_model):
        return 'forward'
    return None