import json
import re
import random

from comfy_api.latest import io
from ..core.bundle import make_bundle, parse_bundle
from ..core.compiler import compile_prompt, compile_regions, combine_tags, deduplicate
from ..core.load_image_prompts import resolve_load_image_keywords
from ..core.iterate_state import (
    content_hash,
    parent_hash,
    get_iterate_state,
    is_subordinate_node,
)


def _autogrow_sort_key(k):
    try:
        return int(k.split("_")[-1])
    except (ValueError, IndexError):
        return 0


class PromptChainNode(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        chain_input = io.Autogrow.TemplatePrefix(
            input=io.String.Input("in", force_input=True),
            prefix="in_",
            min=0,
            max=10,
        )

        return io.Schema(
            node_id="PromptChain_PromptChain",
            display_name="Prompt Chain",
            category="promptchain",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                io.String.Input("prompt", multiline=True, default=""),
                io.Autogrow.Input("inputs", template=chain_input, optional=True),
                # mode state — serialized by JS from node.properties, not from widgets_values.
                # JS sets widget.serializeValue to return the correct property value,
                # bypassing the autogrow widget_values corruption issue.
                io.String.Input("mode", default="switch", socketless=True),
                io.Int.Input("switch_index", default=1, socketless=True),
                io.Boolean.Input("locked", default=False, socketless=True),
                io.Boolean.Input("disabled", default=False, socketless=True),
                io.String.Input("cached_output", default="", socketless=True),
                io.String.Input("cached_neg_output", default="", socketless=True),
                # iterate state — synced from JS properties like mode/switch_index
                io.Int.Input("iterate_index", default=0, socketless=True),
                io.Int.Input("iterate_cycle", default=1, socketless=True),
                io.Boolean.Input("collapsed", default=False, socketless=True),
                # per-wildcard mode overrides — JSON string serialized from
                # node.properties.pcrWildcardModes by JS serializeValue
                io.String.Input("wildcard_modes", default="", socketless=True),
                # Regions JSON captured at lock time (like cached_output) — locked
                # regional graphs must keep their per-figure split; returning ""
                # silently un-regionalized the couple/detailer/upscaler.
                io.String.Input("cached_regions", default="", socketless=True),
            ],
            outputs=[
                io.String.Output("out"),
                io.String.Output("positive"),
                io.String.Output("negative"),
                # JSON {global, regions:[{id,name,text}], negative} for regional
                # conditioning; empty string when no $mannequin{} groups exist.
                io.String.Output("regions"),
            ],
            hidden=[io.Hidden.unique_id, io.Hidden.extra_pnginfo],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Force re-execution so wildcards reshuffle and roll mode re-rolls.
        return float("nan")

    @classmethod
    def execute(cls, prompt: str = "", inputs: io.Autogrow.Type = None,
                mode: str = "combine", switch_index: int = 1,
                locked: bool = False, disabled: bool = False,
                cached_output: str = "", cached_neg_output: str = "",
                iterate_index: int = 0, iterate_cycle: int = 1,
                collapsed: bool = False,
                wildcard_modes: str = "",
                cached_regions: str = "") -> io.NodeOutput:

        # validate mode (safety net for corrupted widget values)
        if mode not in ("combine", "roll", "switch", "iterate"):
            mode = "combine"

        # parse per-wildcard mode overrides
        _wc_modes = None
        if wildcard_modes:
            try:
                _wc_modes = json.loads(wildcard_modes)
            except (json.JSONDecodeError, TypeError):
                _wc_modes = None

        if disabled:
            cls._stamp_workflow_metadata("", "")
            return io.NodeOutput(make_bundle("", ""), "", "", "",
                                 ui={"text": ["(disabled)"], "neg_text": [""]})

        if collapsed:
            # passthrough: skip own prompt but still respect mode settings
            prompt = ""

        if locked and cached_output:
            pos = cached_output
            neg = cached_neg_output or ""
            cls._stamp_workflow_metadata(pos, neg)
            # Regions ride the lock cache too — wildcards inside $blocks stay
            # frozen in step with the flat outputs. Locks cached before regions
            # were always emitted carry "" — rebuild a regions-less JSON from the
            # flat cache so a couple-routed graph doesn't sample with empty cond.
            regions_out = cached_regions or json.dumps(
                {"global": pos, "regions": [], "negative": neg})
            return io.NodeOutput(make_bundle(pos, neg), pos, neg, regions_out,
                                 ui={"text": [pos], "neg_text": [neg],
                                     "regions": [regions_out]})

        # re-seed from OS entropy so wildcards reshuffle even if something
        # upstream (e.g. a reproducibility plugin) set a fixed seed
        random.seed()

        # resolve __LoadImagePositive__ / __LoadImageNegative__ keywords
        load_image_prompts = resolve_load_image_keywords(prompt, cls.hidden.prompt)

        # collect dynamic inputs first to know if children are connected
        input_bundles = []
        if inputs:
            for key in sorted(inputs.keys(), key=_autogrow_sort_key):
                value = inputs[key]
                if value and isinstance(value, str) and value.strip():
                    pos_part, neg_part = parse_bundle(value)
                    input_bundles.append((pos_part.strip(), neg_part.strip()))
                else:
                    input_bundles.append(("", ""))

        has_children = any(b[0] or b[1] for b in input_bundles)

        # ── iterate mode ────────────────────────────────────────
        if mode == "iterate":
            unique_id = cls.hidden.unique_id or "0"
            subordinate = is_subordinate_node(unique_id)

            return cls._execute_iterate(
                prompt, input_bundles, has_children,
                iterate_index, iterate_cycle,
                unique_id, subordinate,
                load_image_prompts=load_image_prompts,
                wildcard_modes=_wc_modes,
            )

        # ── standard modes (combine, roll, switch) ─────────────

        # if children are connected, ignore ::Label:: lines in own content —
        # strip them out and compile the remainder as plain text.
        # mode applies to children, not inline wildcards.
        compile_text = prompt
        compile_mode = mode
        if has_children:
            compile_mode = "combine"
            # remove ::Label:: lines entirely so they don't leak into output
            compile_text = re.sub(r"^::([^:]+)::.*$", "", prompt, flags=re.MULTILINE)
        processed_pos, processed_neg, _metadata = compile_prompt(
            compile_text, mode=compile_mode, switch_index=switch_index,
            load_image_prompts=load_image_prompts,
            wildcard_modes=_wc_modes)

        pos_inputs = [b[0] for b in input_bundles if b[0]]
        neg_inputs = [b[1] for b in input_bundles if b[1]]

        if mode == "switch" and input_bundles:
            if switch_index == 0:
                selected_pos, selected_neg = "", ""
            elif 1 <= switch_index <= len(input_bundles):
                selected_pos, selected_neg = input_bundles[switch_index - 1]
            else:
                selected_pos, selected_neg = "", ""

            pos_parts = [p for p in [processed_pos, selected_pos] if p]
            neg_parts = [p for p in [processed_neg, selected_neg] if p]
            positive_output = ", ".join(pos_parts) if pos_parts else ""
            negative_output = ", ".join(neg_parts) if neg_parts else ""

        elif mode == "roll" and input_bundles:
            selected_idx = random.randint(0, len(input_bundles) - 1)
            roll_selected = selected_idx + 1  # 1-based for UI
            selected_pos, selected_neg = input_bundles[selected_idx]

            pos_parts = [p for p in [processed_pos, selected_pos] if p]
            neg_parts = [p for p in [processed_neg, selected_neg] if p]
            positive_output = ", ".join(pos_parts) if pos_parts else ""
            negative_output = ", ".join(neg_parts) if neg_parts else ""

        else:
            positive_output = combine_tags(processed_pos, pos_inputs)
            negative_output = combine_tags(processed_neg, neg_inputs)

        positive_output = deduplicate(positive_output)
        negative_output = deduplicate(negative_output)

        # Regional split — computed from the node's own compiled text (chain merge
        # stays in the flat outputs); region binding is this node's document for
        # Phase 1. ALWAYS emitted, even with no $name{} groups: in a regional graph
        # the AttentionCouple is the sampler's only conditioning source, and an
        # empty string there meant a $block-less prompt sampled with EMPTY
        # positive AND negative. Consumers branch on the regions LIST, so an
        # empty list still means "not regional" everywhere.
        regions_obj = compile_regions(
            compile_text, mode=compile_mode, switch_index=switch_index,
            load_image_prompts=load_image_prompts, wildcard_modes=_wc_modes)
        regions_json = json.dumps(regions_obj)

        cls._stamp_workflow_metadata(positive_output, negative_output)
        out_bundle = make_bundle(positive_output, negative_output)
        # regions in the ui so the frontend can capture it for the lock cache
        # (same mechanism as text/neg_text -> _pcrOutputText).
        ui = {"text": [positive_output], "neg_text": [negative_output],
              "regions": [regions_json]}
        if mode == "roll" and input_bundles:
            ui["roll_selected"] = [roll_selected]
        elif mode == "roll" and _metadata.get("roll_selected"):
            ui["roll_selected"] = [_metadata["roll_selected"]]
        if _metadata.get("wildcard_results"):
            ui["wildcard_results"] = [json.dumps(_metadata["wildcard_results"])]
        return io.NodeOutput(out_bundle, positive_output, negative_output,
                             regions_json, ui=ui)

    @classmethod
    def _execute_iterate(cls, prompt, input_bundles, has_children,
                         client_index, client_cycle, unique_id, subordinate,
                         load_image_prompts=None, wildcard_modes=None):
        if has_children:
            # ── parent iterate: cycle through connected inputs ──
            active_bundles = [(p, n) for p, n in input_bundles if p or n]
            total = len(active_bundles)

            if total == 0:
                return cls._make_output("", "", {})

            hash_key = parent_hash(unique_id, total)
            # advance if standalone parent; subordinate parents wait for JS cascade
            current_idx, next_idx, cur_cycle, next_cycle, wrapped = get_iterate_state(
                hash_key, client_index, client_cycle, total, advance=not subordinate,
            )

            selected_pos, selected_neg = active_bundles[current_idx % total]

            # compile own content as combine, stripping ::Label:: lines
            stripped = re.sub(r"^::([^:]+)::.*$", "", prompt, flags=re.MULTILINE)
            processed_pos, processed_neg, _iter_meta = compile_prompt(
                stripped, mode="combine", load_image_prompts=load_image_prompts,
                wildcard_modes=wildcard_modes)

            pos_parts = [p for p in [processed_pos, selected_pos] if p]
            neg_parts = [p for p in [processed_neg, selected_neg] if p]
            positive_output = deduplicate(", ".join(pos_parts)) if pos_parts else ""
            negative_output = deduplicate(", ".join(neg_parts)) if neg_parts else ""

            ui = cls._iterate_ui(positive_output, negative_output,
                                 current_idx, next_idx, cur_cycle, total,
                                 hash_key, wrapped, current_idx)
            ui["iterate_selected_input"] = [current_idx]
            if _iter_meta.get("wildcard_results"):
                ui["wildcard_results"] = [json.dumps(_iter_meta["wildcard_results"])]
            return cls._make_output(positive_output, negative_output, ui)

        # ── own-content iterate: cycle through ::Label:: lines ──
        processed_pos, processed_neg, _ = compile_prompt(
            prompt, mode="combine", load_image_prompts=load_image_prompts,
            wildcard_modes=wildcard_modes)

        # count labeled lines in the positive section
        labeled_lines = []
        for line in processed_pos.split("\n"):
            if re.match(r"^::([^:]+)::", line.strip()):
                labeled_lines.append(line.strip())

        total = len(labeled_lines)
        if total == 0:
            # no labels — fall through to combine
            positive_output = deduplicate(processed_pos)
            negative_output = deduplicate(processed_neg)
            return cls._make_output(positive_output, negative_output, {})

        # hash the labeled content for state keying
        labeled_content = "\n".join(labeled_lines)
        hash_key = content_hash(labeled_content)

        # subordinates don't auto-advance (JS cascades them post-execution)
        current_idx, next_idx, cur_cycle, next_cycle, wrapped = get_iterate_state(
            hash_key, client_index, client_cycle, total,
            advance=not subordinate,
        )

        # select the label at current_index using switch-mode logic (1-based)
        positive_output, negative_output, _sel_meta = compile_prompt(
            prompt, mode="switch", switch_index=current_idx + 1,
            load_image_prompts=load_image_prompts,
            wildcard_modes=wildcard_modes)

        positive_output = deduplicate(positive_output)
        negative_output = deduplicate(negative_output)

        ui = cls._iterate_ui(positive_output, negative_output,
                             current_idx, next_idx, cur_cycle, total,
                             hash_key, wrapped, current_idx)
        if _sel_meta.get("wildcard_results"):
            ui["wildcard_results"] = [json.dumps(_sel_meta["wildcard_results"])]
        return cls._make_output(positive_output, negative_output, ui)

    @staticmethod
    def _iterate_ui(pos, neg, current, next_idx, cycle, total, hash_key, wrapped, selected):
        return {
            "text": [pos],
            "neg_text": [neg],
            "iterate_current": [current],
            "iterate_next": [next_idx],
            "iterate_cycle": [cycle],
            "iterate_total": [total],
            "iterate_content_hash": [hash_key],
            "iterate_wrapped": [wrapped],
        }

    @classmethod
    def _stamp_workflow_metadata(cls, positive: str, negative: str):
        """Write compiled output into extra_pnginfo so PNG metadata reflects the actual prompt."""
        epng = cls.hidden.extra_pnginfo
        if not epng or not isinstance(epng, dict):
            return
        wf = epng.get("workflow")
        if not isinstance(wf, dict):
            return
        uid = str(cls.hidden.unique_id)
        for wf_node in wf.get("nodes", []):
            if str(wf_node.get("id", "")) == uid:
                props = wf_node.setdefault("properties", {})
                props["pcrCompiledOutput"] = positive
                props["pcrCompiledNegOutput"] = negative
                break

    @classmethod
    def _make_output(cls, pos, neg, ui, regions=""):
        if not ui:
            ui = {"text": [pos], "neg_text": [neg]}
        cls._stamp_workflow_metadata(pos, neg)
        return io.NodeOutput(make_bundle(pos, neg), pos, neg, regions, ui=ui)
