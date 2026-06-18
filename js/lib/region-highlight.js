// $region highlight — colors `$name { ... }` markers (regional-conditioning blocks)
// so it's visually obvious they pin a section to a mannequin. Self-contained CM6
// extension, no node/app imports. Regions don't nest, so the matching close brace
// is simply the next `}` after the opener.

export function regionHighlightExtension(CM) {
  const nameDeco = CM.Decoration.mark({ class: "pcr-region-name" });
  const braceDeco = CM.Decoration.mark({ class: "pcr-region-brace" });
  const OPEN = /\$[A-Za-z]\w*\s*\{/g;

  const build = (view) => {
    const ranges = [];
    const text = view.state.doc.toString();
    let m;
    OPEN.lastIndex = 0;
    while ((m = OPEN.exec(text)) !== null) {
      const nameLen = m[0].match(/^\$[A-Za-z]\w*/)[0].length;
      ranges.push(nameDeco.range(m.index, m.index + nameLen)); // $name
      const open = m.index + m[0].length - 1;
      ranges.push(braceDeco.range(open, open + 1)); // {
      const close = text.indexOf("}", open + 1);
      if (close !== -1) ranges.push(braceDeco.range(close, close + 1)); // }
    }
    ranges.sort((a, b) => a.from - b.from);
    return CM.Decoration.set(ranges, true);
  };

  const plugin = CM.ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = build(view); }
    update(u) { if (u.docChanged || u.viewportChanged) this.decorations = build(u.view); }
  }, { decorations: (v) => v.decorations });

  return [plugin];
}
