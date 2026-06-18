import comfy
from .VideoPatchNode import video_outer_sample_function_wrapper
from .FluxPatchNode import flux_outer_sample_function_wrapper
from .patch_util import is_flux_model, is_hunyuan_video_model, is_ltxv_video_model, is_mochi_video_model, \
    is_wan_video_model


class DitForwardOverrider:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
            }
        }

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply_patch"
    CATEGORY = "patches/dit"
    DESCRIPTION = "Support Flux, HunYuanVideo, LTXVideo, MochiVideo"

    def apply_patch(self, model):

        model = model.clone()
        patch_key = "dit_forward_override_wrapper"
        diffusion_model = model.get_model_object('diffusion_model')
        if is_flux_model(diffusion_model):
            if len(model.get_wrappers(comfy.patcher_extension.WrappersMP.OUTER_SAMPLE, patch_key)) == 0:
                # Just add it once when connecting in series
                model.add_wrapper_with_key(comfy.patcher_extension.WrappersMP.OUTER_SAMPLE,
                                           patch_key,
                                           flux_outer_sample_function_wrapper
                                           )
        elif is_hunyuan_video_model(diffusion_model) or is_ltxv_video_model(diffusion_model) or is_mochi_video_model(diffusion_model)\
                or is_wan_video_model(diffusion_model):
            if len(model.get_wrappers(comfy.patcher_extension.WrappersMP.OUTER_SAMPLE, patch_key)) == 0:
                # Just add it once when connecting in series
                model.add_wrapper_with_key(comfy.patcher_extension.WrappersMP.OUTER_SAMPLE,
                                           patch_key,
                                           video_outer_sample_function_wrapper
                                           )
        return (model, )


NODE_CLASS_MAPPINGS = {
    "DitForwardOverrider": DitForwardOverrider,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DitForwardOverrider": "DitForwardOverrider",
}
