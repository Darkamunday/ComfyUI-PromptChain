// Re-pose recipes — the pose-transfer "patterns" the Re-pose modal can run.
// Each recipe IS an existing, working data/templates graph: CLIP, VAE, LoRA and
// node types all come from that template untouched. The ONLY thing the modal
// swaps is the base diffusion model (the pose-transfer ability lives in the
// template's LoRAs, which work across any compatible base UNET). The poser's
// control-map MODE is recipe-bound (clay render for AnyPose, true depth for
// RefControl — its LoRA is depth-trained).
//
// `promptDoc` is a PromptChain prompt-box document — NOT pre-split. `//` lines
// are section labels, a `Negative Prompt:` marker splits positive from negative,
// and tags/outfits/wildcards expand: all handled by compile_prompt SERVER-SIDE.
// buildReposeGraph wires a PromptChain_PromptChain node into the recipe graph
// per the template's anchorConnections, so the doc reaches that one compile —
// the same single source of truth inpaint and upscale use. These defaults mirror
// the user's proven workflows verbatim.

// AnyPose: the user's exact edit instruction + an empty `// Your Tags` section to
// append to. Qwen Edit reads the compiled positive as its edit command; the
// negative encode stays empty unless the user adds a `Negative Prompt:` section.
const ANYPOSE_DOC = [
  "// Pose Transfer - AnyPose",
  "Make the person in image 1 do a pose like the person in image 2. Changing the style and background of the image of the person in image 1 is undesirable, so don't do it. The new pose should be pixel accurate to the pose we are trying to copy. The position of the arms and head and legs should be the same as the pose we are trying to copy. Change the field of view and angle to match exactly image 2. Head tilt and eye gaze pose should match the person in image 2.",
  "",
  "// Your Tags",
  "",
].join("\n");

// RefControl: `refcontrol` is the LoRA's trigger word — it MUST lead the positive
// encode to activate the reference→depth fusion (author's model card). Scene /
// style tags go under `// Your Tags`; a `Negative Prompt:` section is optional
// (Klein 9B base is non-distilled, so CFG steers on a real positive/negative
// split). compile_prompt produces both encodes from this single doc.
const REFCONTROL_DOC = [
  "// Pose Transfer - RefControl",
  "refcontrol",
  "",
  "// Your Tags",
  "",
].join("\n");

export const REPOSE_RECIPES = [
  {
    id: "anypose",
    label: "Qwen Edit · AnyPose",
    blurb: "Qwen Edit re-renders the subject into the posed mannequin. Best identity/outfit retention.",
    templateId: "eb95f21b-67f1-4372-8bd7-c831d4d14621",
    // Base-model picker scope. The AnyPose LoRAs are trained for Qwen Image Edit
    // 2511 — lock to it (a 2509/other qwen-edit would mis-apply the pattern).
    archs: ["qwen_edit"],
    modelMatch: /2511/i,
    modelLabel: "Qwen Image Edit 2511",
    preferModel: /fp8/i, // default the picker to the fp8 file (native loader, faster on modern cards)
    poserMode: "white", // matches the template — the AnyPose LoRA's image2 is the white pose render, not the clay one
    loraStrength: 0.7,
    megapixels: 1.0, // input-image scale target (exposes the modal's Input scale control)
    promptDoc: ANYPOSE_DOC,
    sampler: { seed: 0, steps: 40, cfg: 4, sampler: "euler", scheduler: "simple", denoise: 1.0 },
  },
  {
    id: "refcontrol",
    label: "Flux2 Klein · RefControl (depth)",
    blurb: "Klein follows the poser's DEPTH render exactly — pose AND geometry. Identity from the subject image.",
    templateId: "6f8f583a-cde4-4762-838f-f5713912bbfd",
    // The RefControl-depth LoRA + Qwen3-8B CLIP are Klein 9B-specific — lock the
    // picker to Klein 9B (excludes Flux2 Dev's Mistral encoder and the 4B, whose
    // LoRA doesn't match). Matches "klein" AND "9b" anywhere in the name.
    archs: ["flux2", "flux2_klein"],
    modelMatch: /(?=.*klein)(?=.*9b)/i,
    modelLabel: "Flux2 Klein 9B",
    preferModel: /fp8/i, // default to the proven fp8 Base (the GGUF is the slower trial file)
    poserMode: "depth", // LoRA is depth-trained — locked
    loraStrength: 0.6,
    promptDoc: REFCONTROL_DOC,
    sampler: { seed: 0, steps: 30, cfg: 6, sampler: "euler", scheduler: "simple", denoise: 1.0 }, // user-tuned defaults
  },
];

export function reposeRecipeById(id) {
  return REPOSE_RECIPES.find((r) => r.id === id) || null;
}
