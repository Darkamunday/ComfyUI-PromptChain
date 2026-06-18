// Region → mask-row binding for the Edit isolate workflow. JS port of
// core/compiler.py region_figure_indices — keep the two in lockstep: mask
// rows are REGION ENTITIES (figures in figure order, then named props,
// stamped in pose_state v3 regionEntities), name match first, then the
// legacy fallbacks (trailing-int id, block order) clamped into figure space.

export function regionFigureIndices(regionList, poseJson) {
  let names = [];
  let numFigures = null; // null = no entity list -> legacy unclamped fallbacks
  if (poseJson && poseJson.trim()) {
    try {
      const pose = JSON.parse(poseJson);
      const ents = Array.isArray(pose.regionEntities) && pose.regionEntities.length
        ? pose.regionEntities : null;
      if (ents) {
        names = ents.map((e) => String((e && e.name) || "").toLowerCase());
        numFigures = ents.filter((e) => e && e.kind === "figure").length;
      } else if (Array.isArray(pose.figures)) {
        names = pose.figures.map((f, i) => String((f && f.name) || `mannequin${i + 1}`).toLowerCase());
      }
    } catch { /* fall through to the positional fallbacks */ }
  }
  return regionList.map((r, n) => {
    const rname = String(r?.name || "").toLowerCase();
    const hit = rname ? names.indexOf(rname) : -1;
    if (hit >= 0) return hit;
    let idx = parseInt(r?.id, 10);
    idx = Number.isFinite(idx) ? idx - 1 : n;
    if (numFigures !== null) idx = Math.min(Math.max(idx, 0), Math.max(numFigures, 1) - 1);
    return idx;
  });
}

// Which region does a probe shape (the painted inpaint mask, or a filled
// scene-rect for a moved copy) sit on? Loads each region's silhouette PNG,
// scales it to the document, and scores overlap = (probe∩region)/probe. The
// best-scoring region wins, but only past a floor — painting the background
// or a gap between figures matches nothing, so the caller keeps the global
// prompt. This is the inpaint analog of RegionalConditioning's spatial
// masking: in a regioned workflow you paint a figure and get that figure's
// prompt with no manual pick.
export async function matchRegionByOverlap(probeCanvas, regions, width, height) {
  if (!probeCanvas || !regions?.length || !(width > 0) || !(height > 0)) return null;
  const pdata = probeCanvas.getContext("2d").getImageData(0, 0, width, height).data;
  let probeCount = 0;
  for (let i = 3; i < pdata.length; i += 4) if (pdata[i] > 127) probeCount++;
  if (!probeCount) return null;

  let best = null, bestScore = 0;
  for (const r of regions) {
    if (!r.maskUrl || !r.text) continue;
    let bmp;
    try {
      const resp = await fetch(r.maskUrl);
      if (!resp.ok) continue;
      bmp = await createImageBitmap(await resp.blob());
    } catch { continue; }
    const rc = document.createElement("canvas");
    rc.width = width; rc.height = height;
    const rctx = rc.getContext("2d");
    rctx.drawImage(bmp, 0, 0, width, height);
    const rdata = rctx.getImageData(0, 0, width, height).data;
    let overlap = 0;
    for (let i = 0; i < pdata.length; i += 4) {
      if (pdata[i + 3] > 127 && rdata[i] > 64) overlap++;
    }
    const score = overlap / probeCount;
    if (score > bestScore) { bestScore = score; best = r; }
  }
  return bestScore >= 0.2 ? best : null;
}

// Assemble the per-region rows the Edit isolate UI consumes: each region of
// the displayed image with its prompt text (DB regions JSON) and the URL of
// its on-disk silhouette mask (content-addressed beside the pose control
// map). Empty when the image has no pose scene, no DB regions, or the pose
// files are known missing.
export function buildFigureRegions({ workflow, regionsRaw, poseFilesOk, apiURL }) {
  try {
    if (!workflow || poseFilesOk === false) return [];
    let regions = regionsRaw;
    if (typeof regions === "string") { try { regions = JSON.parse(regions); } catch { return []; } }
    const list = Array.isArray(regions?.regions) ? regions.regions : [];
    if (!list.length) return [];
    const poser = (workflow.nodes || []).find((n) => n.type === "PromptChain_PoseStudio");
    const values = poser?.widgets_values || [];
    // widget order: control_map, pose_state, width, height — the map ref can
    // carry ComfyUI's " [input]" annotation suffix.
    const mapRef = typeof values[0] === "string" ? values[0].trim().replace(/\s*\[\w+\]$/, "") : "";
    if (!mapRef) return [];
    const poseState = typeof values[1] === "string" ? values[1] : "";
    const indices = regionFigureIndices(list, poseState);
    // The map ref is input-dir-relative and can carry a subfolder
    // (promptchain_pose/...); /view wants subfolder and filename SPLIT.
    const slash = mapRef.lastIndexOf("/");
    const subfolder = slash >= 0 ? mapRef.slice(0, slash) : "";
    const baseName = (slash >= 0 ? mapRef.slice(slash + 1) : mapRef).replace(/\.[^.]+$/, "");
    return list.map((r, i) => ({
      name: r.name || `region ${i + 1}`,
      text: (r.text || "").trim(),
      maskUrl: apiURL(`/view?filename=${encodeURIComponent(`${baseName}_mask${indices[i]}.png`)}&type=input&subfolder=${encodeURIComponent(subfolder)}`),
    }));
  } catch {
    return [];
  }
}
