import types
import comfy

class PatchKeys:
    ################## transformer_options patches ##################
    options_key = "patches_point"
    running_net_model = "running_net_model"
    # patches_point下支持设置的补丁
    dit_enter = "patch_dit_enter"
    dit_blocks_before = "patch_dit_blocks_before"
    dit_double_blocks_replace = "patch_dit_double_blocks_replace"
    dit_double_block_with_control_replace = "patch_dit_double_block_with_control_replace"
    dit_double_blocks_after = "patch_dit_double_blocks_after"
    dit_blocks_transition_replace = "patch_dit_blocks_transition_replace"
    dit_single_blocks_before = "patch_dit_single_blocks_before"
    dit_single_blocks_replace = "patch_dit_single_blocks_replace"
    dit_blocks_after = "patch_dit_blocks_after"
    dit_blocks_after_transition_replace = "patch_dit_final_layer_before_replace"
    dit_final_layer_before = "patch_dit_final_layer_before"
    dit_exit = "patch_dit_exit"
    ################## transformer_options patches ##################


def set_model_patch(model_patcher, options_key, patch, name):
    to = model_patcher.model_options["transformer_options"]
    if options_key not in to:
        to[options_key] = {}
    to[options_key][name] = to[options_key].get(name, []) + [patch]

def set_model_patch_replace(model_patcher, options_key, patch, name):
    to = model_patcher.model_options["transformer_options"]
    if options_key not in to:
        to[options_key] = {}
    to[options_key][name] = patch

def add_model_patch_option(model, patch_key):
    if 'transformer_options' not in model.model_options:
        model.model_options['transformer_options'] = {}
    to = model.model_options['transformer_options']
    if patch_key not in to:
        to[patch_key] = {}
    return to[patch_key]


def set_hook(diffusion_model, bak_method_name, new_method, old_method_name='forward_orig'):
    if new_method is not None:
        setattr(diffusion_model, bak_method_name, getattr(diffusion_model, old_method_name));
        setattr(diffusion_model ,old_method_name, types.MethodType(new_method, diffusion_model))


def clean_hook(diffusion_model, bak_method_name, old_method_name='forward_orig'):
    if hasattr(diffusion_model, bak_method_name):
        setattr(diffusion_model, old_method_name, getattr(diffusion_model, bak_method_name))
        delattr(diffusion_model, bak_method_name)


def is_hunyuan_video_model(model):
    if isinstance(model, comfy.ldm.hunyuan_video.model.HunyuanVideo):
        return True
    return False

def is_ltxv_video_model(model):
    if isinstance(model, comfy.ldm.lightricks.model.LTXVModel):
        return True
    return False

def is_flux_model(model):
    if isinstance(model, comfy.ldm.flux.model.Flux):
        return True
    return False

def is_mochi_video_model(model):
    if isinstance(model, comfy.ldm.genmo.joint_model.asymm_models_joint.AsymmDiTJoint):
        return True
    return False

def is_wan_video_model(model):
    if isinstance(model, comfy.ldm.wan.model.WanModel):
        return True
    return False