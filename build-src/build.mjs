import { build } from "esbuild";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const bundles = [
  {
    entry: "codemirror-entry.mjs",
    out: "codemirror.bundle.js",
    globalName: "PromptChainCM",
  },
  {
    entry: "three-entry.mjs",
    out: "three.bundle.js",
    globalName: "PromptChainThree",
  },
];

await Promise.all(
  bundles.map(({ entry, out, globalName }) =>
    build({
      entryPoints: [resolve(__dirname, entry)],
      bundle: true,
      format: "iife",
      globalName,
      outfile: resolve(__dirname, "..", "js", out),
      minify: true,
      target: ["es2020"],
    }).then(() => {
      console.log(`${out} built`);
    })
  )
);
