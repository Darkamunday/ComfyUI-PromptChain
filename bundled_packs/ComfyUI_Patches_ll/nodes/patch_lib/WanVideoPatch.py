import torch
from torch import Tensor

from comfy.ldm.wan.model import sinusoidal_embedding_1d
from ..patch_util import PatchKeys
from einops import repeat
import comfy.ldm.common_dit


def wan_forward(self, x, timestep, context, clip_fea=None, **kwargs):
    bs, c, t, h, w = x.shape
    x = comfy.ldm.common_dit.pad_to_patch_size(x, self.patch_size)
    patch_size = self.patch_size
    t_len = ((t + (patch_size[0] // 2)) // patch_size[0])
    h_len = ((h + (patch_size[1] // 2)) // patch_size[1])
    w_len = ((w + (patch_size[2] // 2)) // patch_size[2])
    img_ids = torch.zeros((t_len, h_len, w_len, 3), device=x.device, dtype=x.dtype)
    img_ids[:, :, :, 0] = img_ids[:, :, :, 0] + torch.linspace(0, t_len - 1, steps=t_len, device=x.device,
                                                               dtype=x.dtype).reshape(-1, 1, 1)
    img_ids[:, :, :, 1] = img_ids[:, :, :, 1] + torch.linspace(0, h_len - 1, steps=h_len, device=x.device,
                                                               dtype=x.dtype).reshape(1, -1, 1)
    img_ids[:, :, :, 2] = img_ids[:, :, :, 2] + torch.linspace(0, w_len - 1, steps=w_len, device=x.device,
                                                               dtype=x.dtype).reshape(1, 1, -1)
    img_ids = repeat(img_ids, "t h w c -> b (t h w) c", b=bs)

    freqs = self.rope_embedder(img_ids).movedim(1, 2)
    return self.forward_orig(x, timestep, context, clip_fea=clip_fea, freqs=freqs, **kwargs)[:, :, :t, :h, :w]

def wan_forward_orig(
    self,
    x,
    t,
    context,
    clip_fea=None,
    freqs=None,
    transformer_options={},
    **kwargs
) -> Tensor:
    # patches_replace = transformer_options.get("patches_replace", {})
    patches_point = transformer_options.get(PatchKeys.options_key, {})

    transformer_options[PatchKeys.running_net_model] = self

    patches_enter = patches_point.get(PatchKeys.dit_enter, [])
    if patches_enter is not None and len(patches_enter) > 0:
        for patch_enter in patches_enter:
            x, t, context = patch_enter(
                x,
                t,
                context,
                transformer_options
            )

    # embeddings
    x = self.patch_embedding(x.float()).to(x.dtype)
    grid_sizes = x.shape[2:]
    x = x.flatten(2).transpose(1, 2)

    # time embeddings
    e = self.time_embedding(
        sinusoidal_embedding_1d(self.freq_dim, t).to(dtype=x[0].dtype))
    e0 = self.time_projection(e).unflatten(1, (6, self.dim))

    # context
    context = self.text_embedding(context)

    if clip_fea is not None and self.img_emb is not None:
        context_clip = self.img_emb(clip_fea)  # bs x 257 x dim
        context = torch.concat([context_clip, context], dim=1)

    attention_mask = None

    patch_blocks_before = patches_point.get(PatchKeys.dit_blocks_before, [])
    if patch_blocks_before is not None and len(patch_blocks_before) > 0:
        for blocks_before in patch_blocks_before:
            x, context, e0, ids, freqs = blocks_before(img=x, txt=context, vec=e0, ids=None, pe=freqs, transformer_options=transformer_options, e=e)

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

    if patch_double_blocks_replace is not None:
        x, context = patch_double_blocks_replace({"img": x,
                                                  "txt": context,
                                                  "vec": e0,
                                                  "pe": freqs,
                                                  "control": None,
                                                  "attn_mask": attention_mask,
                                                  },
                                                 {
                                                     "original_blocks": double_blocks_wrap,
                                                     "transformer_options": transformer_options
                                                 })
    else:
        x, context = double_blocks_wrap(img=x,
                                        txt=context,
                                        vec=e0,
                                        pe=freqs,
                                        control=None,
                                        attn_mask=attention_mask,
                                        transformer_options=transformer_options
                                        )

    patches_double_blocks_after = patches_point.get(PatchKeys.dit_double_blocks_after, [])
    if patches_double_blocks_after is not None and len(patches_double_blocks_after) > 0:
        for patch_double_blocks_after in patches_double_blocks_after:
            x, context = patch_double_blocks_after(x, context, transformer_options)

    patch_blocks_transition = patches_point.get(PatchKeys.dit_blocks_transition_replace)

    def blocks_transition_wrap(**kwargs):
        x = kwargs["img"]
        return x

    if patch_blocks_transition is not None:
        x = patch_blocks_transition({"img": x, "txt": context, "vec": e0, "pe": freqs},
                                    {
                                        "original_func": blocks_transition_wrap,
                                        "transformer_options": transformer_options
                                    })
    else:
        x = blocks_transition_wrap(img=x, txt=context)

    patches_single_blocks_before = patches_point.get(PatchKeys.dit_single_blocks_before, [])
    if patches_single_blocks_before is not None and len(patches_single_blocks_before) > 0:
        for patch_single_blocks_before in patches_single_blocks_before:
            x, context = patch_single_blocks_before(x, context, transformer_options)

    def single_blocks_wrap(img, **kwargs):
        return img

    patch_single_blocks_replace = patches_point.get(PatchKeys.dit_single_blocks_replace)

    if patch_single_blocks_replace is not None:
        x, context = patch_single_blocks_replace({"img": x,
                                                  "txt": context,
                                                  "vec": e0,
                                                  "pe": freqs,
                                                  "control": None,
                                                  "attn_mask": attention_mask
                                                  },
                                                 {
                                                     "original_blocks": single_blocks_wrap,
                                                     "transformer_options": transformer_options
                                                 })
    else:
        x = single_blocks_wrap(img=x,
                               txt=context,
                               vec=e0,
                               pe=freqs,
                               control=None,
                               attn_mask=attention_mask,
                               transformer_options=transformer_options
                               )

    patch_blocks_exit = patches_point.get(PatchKeys.dit_blocks_after, [])
    if patch_blocks_exit is not None and len(patch_blocks_exit) > 0:
        for blocks_after in patch_blocks_exit:
            x, context = blocks_after(x, context, transformer_options)

    def final_transition_wrap(**kwargs):
        img = kwargs["img"]
        # img = self.head.norm(img)
        return img

    patch_blocks_after_transition_replace = patches_point.get(PatchKeys.dit_blocks_after_transition_replace)
    if patch_blocks_after_transition_replace is not None:
        x = patch_blocks_after_transition_replace({"img": x, "txt": context, "vec": e0, "pe": freqs},
                                                    {
                                                        "original_func": final_transition_wrap,
                                                        "transformer_options": transformer_options
                                                    })
    else:
        x = final_transition_wrap(img=x)

    patches_final_layer_before = patches_point.get(PatchKeys.dit_final_layer_before, [])
    if patches_final_layer_before is not None and len(patches_final_layer_before) > 0:
        for patch_final_layer_before in patches_final_layer_before:
            x = patch_final_layer_before(img=x, txt=context, transformer_options=transformer_options)

    # e = (comfy.model_management.cast_to(self.head.modulation, dtype=x.dtype, device=x.device) + e.unsqueeze(1)).chunk(2, dim=1)
    # x = (self.head.head(x * (1 + e[1]) + e[0]))
    # head
    x = self.head(x, e)

    # unpatchify
    x = self.unpatchify(x, grid_sizes)

    patches_exit = patches_point.get(PatchKeys.dit_exit, [])
    if patches_exit is not None and len(patches_exit) > 0:
        for patch_exit in patches_exit:
            x = patch_exit(x, transformer_options)

    del transformer_options[PatchKeys.running_net_model]

    return x

def double_block_and_control_replace(i, block, img, txt=None, vec=None, pe=None, control=None, attn_mask=None, transformer_options={}):
    blocks_replace = transformer_options.get("patches_replace", {}).get("dit", {})
    # arguments
    kwargs = dict(
        e=vec,
        freqs=pe,
        context=txt)
    if ("double_block", i) in blocks_replace:
        def block_wrap(args):
            out = {}
            out["img"] = block(x=args["img"],  **kwargs)
            return out

        out = blocks_replace[("double_block", i)]({"img": img,
                                                   "txt": txt,
                                                   "vec": vec,
                                                   "pe": pe,
                                                   "attention_mask": attn_mask,
                                                   },
                                                  {
                                                      "original_block": block_wrap,
                                                      "transformer_options": transformer_options
                                                  })
        img = out["img"]
    else:
        img = block(img, **kwargs)

    return img, txt
