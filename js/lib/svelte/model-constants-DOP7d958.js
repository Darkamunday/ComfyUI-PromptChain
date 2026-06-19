function extractPrecisions(files) {
  const precisions = /* @__PURE__ */ new Set();
  for (const f of files) if (f.variants) for (const v of f.variants) precisions.add(v.precision);
  return [...precisions];
}
function resolveFilesForPrecision(files, precision) {
  return files.map((f) => {
    if (f.variants) {
      const match = f.variants.find((v) => v.precision === precision);
      return match ? { label: f.label, folder: f.folder, filename: match.filename, size_bytes: match.size_bytes, source: match.source } : null;
    }
    return f;
  }).filter(Boolean);
}
const ARCHITECTURES = [
  { id: "sdxl", label: "SDXL" },
  { id: "sd15", label: "SD 1.5" },
  { id: "flux", label: "Flux" },
  { id: "flux2", label: "Flux 2" },
  { id: "sd3", label: "SD3" },
  { id: "zimage", label: "Z-Image" },
  { id: "qwen_image", label: "Qwen Image" },
  { id: "qwen_edit", label: "Qwen Edit" },
  { id: "wan22", label: "Wan 2.2" },
  { id: "ltx", label: "LTX Video" },
  { id: "hunyuan_video", label: "HunyuanVideo" },
  { id: "hidream", label: "HiDream" },
  { id: "ernie", label: "ERNIE Image" },
  { id: "ideogram", label: "Ideogram" }
];
const FAMILIES = {
  sdxl: [
    { id: "base_sdxl", label: "Base SDXL" },
    { id: "pony", label: "Pony" },
    { id: "illustrious", label: "Illustrious" },
    { id: "noobai", label: "NoobAI" }
  ],
  sd15: [
    { id: "base_sd15", label: "Base SD 1.5" },
    { id: "nai", label: "NAI" }
  ],
  flux: [
    { id: "flux_dev", label: "Flux Dev" },
    { id: "flux_schnell", label: "Flux Schnell" },
    { id: "flux_fill", label: "Flux Fill" },
    { id: "flux_kontext", label: "Flux Kontext" }
  ],
  flux2: [
    { id: "flux2", label: "Flux 2" },
    { id: "flux2_gguf", label: "Flux 2 GGUF" },
    { id: "flux2_klein", label: "Flux 2 Klein" }
  ],
  sd3: [
    { id: "sd3", label: "SD3" },
    { id: "sd3.5", label: "SD 3.5" }
  ],
  zimage: [
    { id: "zimage_base", label: "Z-Image Base" },
    { id: "zimage_turbo", label: "Z-Image Turbo" }
  ],
  qwen_image: [
    { id: "qwen_image", label: "Qwen Image" }
  ],
  qwen_edit: [
    { id: "qwen_edit", label: "Qwen Edit" },
    { id: "qwen_aio", label: "Qwen AIO" }
  ],
  wan22: [
    { id: "wan22_t2v", label: "Wan T2V 14B" },
    { id: "wan22_i2v", label: "Wan I2V 14B" },
    { id: "wan22_5b", label: "Wan 5B" }
  ],
  ltx: [
    { id: "ltx23", label: "LTX 2.3" }
  ],
  hunyuan_video: [
    { id: "hunyuan_video_15", label: "HunyuanVideo 1.5" }
  ],
  hidream: [
    { id: "hidream", label: "HiDream-I1" }
  ],
  ernie: [
    { id: "ernie_base", label: "ERNIE Image" },
    { id: "ernie_turbo", label: "ERNIE Image Turbo" }
  ],
  ideogram: [
    { id: "ideogram4", label: "Ideogram 4" }
  ]
};
export {
  ARCHITECTURES as A,
  FAMILIES as F,
  extractPrecisions as e,
  resolveFilesForPrecision as r
};
//# sourceMappingURL=model-constants-DOP7d958.js.map
