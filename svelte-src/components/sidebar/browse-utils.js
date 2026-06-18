// Pure utility functions extracted from AssetBrowser —
// sorting, grouping, and item mutation helpers.

export function sortCompare(a, b, sortField, sortDirection) {
  const af = a.type === "folder" ? 0 : 1;
  const bf = b.type === "folder" ? 0 : 1;
  if (af !== bf) return af - bf;
  const desc = sortDirection === "desc";
  let cmp = 0;
  if (sortField === "name") cmp = a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  else if (sortField === "modified") cmp = (a.modified || 0) - (b.modified || 0);
  else if (sortField === "size") cmp = (a.size || 0) - (b.size || 0);
  else if (sortField === "type") cmp = (a.extension || a.type).localeCompare(b.extension || b.type);
  return desc ? -cmp : cmp;
}

export function insertSorted(items, item, sortField, sortDirection) {
  const a = [...items];
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (sortCompare(item, a[mid], sortField, sortDirection) > 0) lo = mid + 1;
    else hi = mid;
  }
  a.splice(lo, 0, item);
  return a;
}

export function patchItemInList(items, oldPath, data, sortField, sortDirection) {
  const idx = items.findIndex(i => i.path === oldPath);
  if (idx < 0) return items;
  const updated = { ...items[idx], ...data };
  if (data.name !== undefined || data.modified !== undefined || data.size !== undefined) {
    const filtered = items.filter((_, i) => i !== idx);
    return insertSorted(filtered, updated, sortField, sortDirection);
  }
  const a = [...items];
  a[idx] = updated;
  return a;
}

export function dropItemsByPath(items, paths) {
  const s = new Set(paths);
  return items.filter(i => !s.has(i.path));
}

export function buildGroups(items, mode) {
  if (mode === "none" || !mode) return [{ label: null, items }];
  const d = new Date();
  const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
  const buckets = new Map();
  const order = [];
  for (const item of items) {
    let key;
    if (mode === "time") {
      const t = item.modified || 0;
      if (t >= dayStart) key = "Today";
      else if (t >= dayStart - 86400) key = "Yesterday";
      else if (t >= dayStart - 7 * 86400) key = "This Week";
      else if (t >= dayStart - 30 * 86400) key = "This Month";
      else key = "Older";
    } else if (mode === "type") {
      key = item.type === "folder" ? "Folders"
          : item.type === "image" ? "Images"
          : item.type === "video" ? "Videos"
          : item.type === "workflow" ? "Workflows"
          : "Other";
    } else if (mode === "size") {
      const s = item.size || 0;
      if (item.type === "folder") key = "Folders";
      else if (s >= 50 * 1048576) key = "Huge (50+ MB)";
      else if (s >= 5 * 1048576) key = "Large (5-50 MB)";
      else if (s >= 307200) key = "Medium (300 KB-5 MB)";
      else key = "Small (< 300 KB)";
    }
    if (!buckets.has(key)) { buckets.set(key, []); order.push(key); }
    buckets.get(key).push(item);
  }
  return order.map(key => ({ label: key, items: buckets.get(key) }));
}
