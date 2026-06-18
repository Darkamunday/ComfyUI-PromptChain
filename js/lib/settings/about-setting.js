// Settings entry for the About modal + Help link — a second entry point to the
// same modal the sidebar kebab opens (the splash re-opened, dismissible).
import { openAboutModal } from "../onboarding.js";

const BTN = "padding:5px 14px;border:none;border-radius:4px;background:var(--comfy-input-bg,#333);color:var(--input-text,#ddd);font-size:12px;cursor:pointer;white-space:nowrap;";
const README_URL = "https://github.com/mobcat40/ComfyUI-PromptChain#readme";

export function renderAboutSetting() {
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex;align-items:center;gap:10px;";

  const about = document.createElement("button");
  about.textContent = "About PromptChain";
  about.style.cssText = BTN;
  about.addEventListener("click", () => openAboutModal());

  const help = document.createElement("button");
  help.textContent = "Help";
  help.style.cssText = BTN;
  help.addEventListener("click", () => window.open(README_URL, "_blank", "noopener"));

  wrap.append(about, help);
  return wrap;
}
