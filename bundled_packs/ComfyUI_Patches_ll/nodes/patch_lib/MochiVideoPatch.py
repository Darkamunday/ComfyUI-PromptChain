from typing import List, Dict

import torch
from einops import rearrange
import torch.nn.functional as F

from ..patch_util import PatchKeys

# copied from comfy.ldm.genmo.joint_model.asymm_models_joint.AsymmDiTJoint.forward
def mochi_forward(
        self,
        x: torch.Tensor,
        timestep: torch.Tensor,
        context: List[torch.Tensor],
        attention_mask: List[torch.Tensor],
        num_tokens=256,
        packed_indices: Dict[str, torch.Tensor] = None,
        rope_cos: torch.Tensor = None,
        rope_sin: torch.Tensor = None,
        control=None, transformer_options={}, **kwargs
):
    patches_replace = transformer_options.get("patches_replace", {})
    patches_point = transformer_options.get(PatchKeys.options_key, {})
    transformer_options[PatchKeys.running_net_model] = self

    patches_enter = patches_point.get(PatchKeys.dit_enter, [])
    if patches_enter is not None and len(patches_enter) > 0:
        for patch_enter in patches_enter:
            x, timestep, context, attention_mask, num_tokens = patch_enter(
                x,
                timestep,
                context,
                attention_mask,
                num_tokens,
                transformer_options
            )

    y_feat = context
    y_mask = attention_mask
    sigma = timestep
    """Forward pass of DiT.

    Args:
        x: (B, C, T, H, W) tensor of spatial inputs (images or latent representations of images)
        sigma: (B,) tensor of noise standard deviations
        y_feat: List((B, L, y_feat_dim) tensor of caption token features. For SDXL text encoders: L=77, y_feat_dim=2048)
        y_mask: List((B, L) boolean tensor indicating which tokens are not padding)
        packed_indices: Dict with keys for Flash Attention. Result of compute_packed_indices.
    """
    B, _, T, H, W = x.shape

    x, c, y_feat, rope_cos, rope_sin = self.prepare(
        x, sigma, y_feat, y_mask
    )
    del y_mask

    pe = [rope_cos, rope_sin]
    patch_blocks_before = patches_point.get(PatchKeys.dit_blocks_before, [])
    if patch_blocks_before is not None and len(patch_blocks_before) > 0:
        for blocks_before in patch_blocks_before:
            x, y_feat, c, ids, pe = blocks_before(img=x, txt=y_feat, vec=c, ids=None, pe=pe,
                                                          transformer_options=transformer_options)

    def double_blocks_wrap(img, txt, vec, pe, control=None, attn_mask=None, transformer_options={}):
        running_net_model = transformer_options[PatchKeys.running_net_model]
        patch_double_blocks_with_control_replace = patches_point.get(PatchKeys.dit_double_block_with_control_replace)
        for i, block in enumerate(running_net_model.blocks):
            if patch_double_blocks_with_control_replace is not None:
                img, txt = patch_double_blocks_with_control_replace({'i': i,
                                                                     'block': block,
                                                                     'img': img,
                                                                     'txt': txt,
                                                                     'vec': vec,
                                                                     'pe': pe,
                                                                     'control': control,
                                                                     'attn_mask': attn_mask
                                                                     },
                                                                    {
                                                                        "original_func": double_block_and_control_replace,
                                                                        "transformer_options": transformer_options
                                                                    })
            else:
                img, txt = double_block_and_control_replace(i=i,
                                                            block=block,
                                                            img=img,
                                                            txt=txt,
                                                            vec=vec,
                                                            pe=pe,
                                                            control=control,
                                                            attn_mask=attn_mask,
                                                            transformer_options=transformer_options
                                                            )
        del patch_double_blocks_with_control_replace
        return img, txt

    patch_double_blocks_replace = patches_point.get(PatchKeys.dit_double_blocks_replace)

    transformer_options["db_blocks_num_tokens"] = num_tokens
    if patch_double_blocks_replace is not None:
        x, y_feat = patch_double_blocks_replace({"img": x,
                                                  "txt": y_feat,
                                                  "vec": c,
                                                  "pe": pe,
                                                  "control": None,
                                                  "attn_mask": None,
                                                  },
                                                 {
                                                     "original_blocks": double_blocks_wrap,
                                                     "transformer_options": transformer_options
                                                 })
    else:
        x, y_feat = double_blocks_wrap(img=x,
                                       txt=y_feat,
                                       vec=c,
                                       pe=pe,
                                       control=None,
                                       attn_mask=None,
                                       transformer_options=transformer_options
                                       )
    del transformer_options["db_blocks_num_tokens"]

    # del y_feat  # Final layers don't use dense text features.

    patches_double_blocks_after = patches_point.get(PatchKeys.dit_double_blocks_after, [])
    if patches_double_blocks_after is not None and len(patches_double_blocks_after) > 0:
        for patch_double_blocks_after in patches_double_blocks_after:
            x, y_feat = patch_double_blocks_after(x, y_feat, transformer_options)

    patch_blocks_transition = patches_point.get(PatchKeys.dit_blocks_transition_replace)

    def blocks_transition_wrap(**kwargs):
        img = kwargs["img"]
        return img

    if patch_blocks_transition is not None:
        x = patch_blocks_transition({"img": x, "txt": y_feat, "vec": c, "pe": pe},
                                    {
                                        "original_func": blocks_transition_wrap,
                                        "transformer_options": transformer_options
                                    })
    else:
        x = blocks_transition_wrap(img=x, txt=y_feat)

    patches_single_blocks_before = patches_point.get(PatchKeys.dit_single_blocks_before, [])
    if patches_single_blocks_before is not None and len(patches_single_blocks_before) > 0:
        for patch_single_blocks_before in patches_single_blocks_before:
            x, y_feat = patch_single_blocks_before(x, y_feat, transformer_options)

    def single_blocks_wrap(img, **kwargs):
        return img

    patch_single_blocks_replace = patches_point.get(PatchKeys.dit_single_blocks_replace)

    if patch_single_blocks_replace is not None:
        x, y_feat = patch_single_blocks_replace({"img": x,
                                                  "txt": y_feat,
                                                  "vec": c,
                                                  "pe": pe,
                                                  "control": None,
                                                  "attn_mask": None
                                                  },
                                                 {
                                                     "original_blocks": single_blocks_wrap,
                                                     "transformer_options": transformer_options
                                                 })
    else:
        x = single_blocks_wrap(img=x,
                               txt=y_feat,
                               vec=c,
                               pe=pe,
                               control=None,
                               attn_mask=None,
                               transformer_options=transformer_options
                               )

    patch_blocks_exit = patches_point.get(PatchKeys.dit_blocks_after, [])
    if patch_blocks_exit is not None and len(patch_blocks_exit) > 0:
        for blocks_after in patch_blocks_exit:
            x, y_feat = blocks_after(x, y_feat, transformer_options)

    def final_transition_wrap(**kwargs):
        # pipe => x = normal_out(x)
        img = kwargs["img"]
        _c = kwargs["vec"]
        temp_model = transformer_options[PatchKeys.running_net_model]
        shift, scale = temp_model.final_layer.mod(F.silu(_c)).chunk(2, dim=1)
        # comfyui => x = modulate(self.norm_final(x), shift, scale)
        img = temp_model.final_layer.norm_final(img) * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)
        del temp_model
        return img

    patch_blocks_after_transition_replace = patches_point.get(PatchKeys.dit_blocks_after_transition_replace)
    if patch_blocks_after_transition_replace is not None:
        x = patch_blocks_after_transition_replace({"img": x, "txt": y_feat, "vec": c, "pe": pe},
                                                    {
                                                        "original_func": final_transition_wrap,
                                                        "transformer_options": transformer_options
                                                    })
    else:
        x = final_transition_wrap(img=x, txt=y_feat, vec=c)

    patches_final_layer_before = patches_point.get(PatchKeys.dit_final_layer_before, [])
    if patches_final_layer_before is not None and len(patches_final_layer_before) > 0:
        for patch_final_layer_before in patches_final_layer_before:
            x = patch_final_layer_before(x, y_feat, transformer_options)

    del y_feat

    # pipe => x = proj_out(x)
    x = self.final_layer.linear(x)  # (B, M, patch_size ** 2 * out_channels)
    # x = self.final_layer(x, c)  # (B, M, patch_size ** 2 * out_channels)
    x = rearrange(
        x,
        "B (T hp wp) (p1 p2 c) -> B c T (hp p1) (wp p2)",
        T=T,
        hp=H // self.patch_size,
        wp=W // self.patch_size,
        p1=self.patch_size,
        p2=self.patch_size,
        c=self.out_channels,
    )

    patches_exit = patches_point.get(PatchKeys.dit_exit, [])
    if patches_exit is not None and len(patches_exit) > 0:
        for patch_exit in patches_exit:
            x = patch_exit(x, transformer_options)

    del transformer_options[PatchKeys.running_net_model]

    return -x

def double_block_and_control_replace(i, block, img, txt=None, vec=None, pe=None, control=None, attn_mask=None,
                                     transformer_options={}):
    blocks_replace = transformer_options.get("patches_replace", {}).get("dit", {})
    _num_tokens = transformer_options["db_blocks_num_tokens"]
    if ("double_block", i) in blocks_replace:
        def block_wrap(args):
            out = {}
            out["img"], out["txt"] = block(args["img"],
                                           args["vec"],
                                           args["txt"],
                                           rope_cos=args["rope_cos"],
                                           rope_sin=args["rope_sin"],
                                           crop_y=args["num_tokens"])
            return out

        out = blocks_replace[("double_block", i)]({"img": img,
                                                   "txt": txt,
                                                   "vec": vec,
                                                   "pe": pe,
                                                   "rope_cos": pe[0],
                                                   "rope_sin": pe[1],
                                                   "crop_y": _num_tokens
                                                   },
                                                  {
                                                      "original_block": block_wrap,
                                                      "transformer_options": transformer_options
                                                  })
        txt = out["txt"]
        img = out["img"]
    else:
        img, txt = block(img,
                         vec,
                         txt,
                         rope_cos=pe[0],
                         rope_sin=pe[1],
                         crop_y=_num_tokens)  # (B, M, D), (B, L, D)

    return img, txt
