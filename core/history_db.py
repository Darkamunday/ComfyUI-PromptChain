import hashlib
import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

import folder_paths
from PIL import Image

logger = logging.getLogger("promptchain.history")


def get_data_dir() -> Path:
    return Path(folder_paths.get_user_directory()) / "PromptChain"


_write_lock = threading.RLock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(data_dir / "history.db"), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        _local.conn = conn
    return _local.conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS images (
            hash            TEXT PRIMARY KEY,
            filename        TEXT NOT NULL,
            subfolder       TEXT NOT NULL DEFAULT '',
            source_type     TEXT NOT NULL DEFAULT 'output',
            width           INTEGER,
            height          INTEGER,
            format          TEXT,
            file_size       INTEGER,
            created_at      INTEGER NOT NULL,
            -- generation metadata (populated at record time)
            prompt          TEXT,
            negative        TEXT,
            seed            INTEGER,
            model           TEXT,
            steps           INTEGER,
            cfg             REAL,
            sampler         TEXT,
            scheduler       TEXT,
            denoise         REAL,
            -- Layer 3 fields (empty until cache is enabled)
            parent_hash     TEXT,
            cached          INTEGER NOT NULL DEFAULT 0,
            cached_path     TEXT,
            FOREIGN KEY (parent_hash) REFERENCES images(hash)
        );

        CREATE TABLE IF NOT EXISTS image_workflows (
            hash            TEXT NOT NULL,
            workflow_id     TEXT NOT NULL,
            created_at      INTEGER NOT NULL,
            PRIMARY KEY (hash, workflow_id),
            FOREIGN KEY (hash) REFERENCES images(hash)
        );

        CREATE TABLE IF NOT EXISTS workflows (
            workflow_id     TEXT PRIMARY KEY,
            filepath        TEXT,
            first_seen      INTEGER NOT NULL,
            last_used       INTEGER NOT NULL
        );

        -- sidebar browser stars, keyed by scope-relative path; rename/move/
        -- delete endpoints re-key or prune via the move/remove helpers below
        CREATE TABLE IF NOT EXISTS browse_favorites (
            scope           TEXT NOT NULL,
            path            TEXT NOT NULL,
            created_at      INTEGER NOT NULL,
            PRIMARY KEY (scope, path)
        );

        -- perceptual-hash cache for duplicate detection; mtime_ns+size guard
        -- invalidates entries when the file changes in place
        CREATE TABLE IF NOT EXISTS browse_phash (
            scope           TEXT NOT NULL,
            path            TEXT NOT NULL,
            mtime_ns        INTEGER NOT NULL,
            size            INTEGER NOT NULL,
            phash           INTEGER NOT NULL,
            PRIMARY KEY (scope, path)
        );

        CREATE INDEX IF NOT EXISTS idx_iw_workflow ON image_workflows(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_created ON images(created_at DESC);
        -- get_workflow_images filters by workflow_id and sorts by created_at DESC;
        -- without this composite, each query scanned the join and sorted in memory.
        CREATE INDEX IF NOT EXISTS idx_iw_workflow_created ON image_workflows(workflow_id, created_at DESC);
    """)
    # migrations: add columns if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(images)").fetchall()}
    if "orphaned" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN orphaned INTEGER NOT NULL DEFAULT 0")
    if "deleted" not in cols:
        # User-deletion tombstone. The same content hash can live at several
        # paths at once (output + cached copy + promptchain_source_<hash12>
        # staging copies), and resolve_image_path heals a record onto any
        # surviving copy — so unlinking one file isn't deletion. This flag is the
        # authority: resolve/heal/list all honor it so read-repair can't resurrect
        # a deleted image.
        conn.execute("ALTER TABLE images ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    for col, typedef in [
        ("generation_time", "INTEGER"),
        ("prompt_id", "TEXT"),
        ("source_files", "TEXT"),
        ("lora", "TEXT"),
        ("vae", "TEXT"),
        # JSON {global, regions:[{id,name,text}], negative} for regional gens —
        # the viewer's prompt panel shows the per-region breakdown instead of
        # only the flattened compiled prompt.
        ("regions", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE images ADD COLUMN {col} {typedef}")

    # Backfill: derived images join their parents' workflows (record_image
    # attaches new ones since 2026-06; earlier inpaint/upscale/edit results
    # recorded only into their own fresh-id timeline). Loop to fixpoint so
    # chains propagate through intermediates; steady state is one no-op pass.
    while True:
        inserted = conn.execute("""
            INSERT OR IGNORE INTO image_workflows (hash, workflow_id, created_at)
            SELECT i.hash, pw.workflow_id, i.created_at
            FROM images i JOIN image_workflows pw ON pw.hash = i.parent_hash
            WHERE i.parent_hash IS NOT NULL
        """).rowcount
        if inserted <= 0:
            break
    conn.commit()


# ── hashing ──────────────────────────────────────────────────────

def compute_hash(filepath: str | Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── resolve paths ────────────────────────────────────────────────

def _resolve_output_path(filename: str, subfolder: str = "", source_type: str = "output") -> Path | None:
    if source_type == "temp":
        base = Path(folder_paths.get_temp_directory())
    elif source_type == "input":
        base = Path(folder_paths.get_input_directory())
    else:
        base = Path(folder_paths.get_output_directory())
    path = base / subfolder / filename if subfolder else base / filename
    return path if path.is_file() else None


def resolve_image_path(image_hash: str) -> Path | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT filename, subfolder, source_type, cached, cached_path, deleted FROM images WHERE hash = ?",
        (image_hash,),
    ).fetchone()
    if not row:
        return None
    if row["deleted"]:
        # tombstoned by the user — refuse to resolve. Otherwise the heal-at-read
        # fallthrough below would re-point the record onto a surviving replica
        # and the deleted image would come back on the next request.
        return None
    # Layer 3: prefer cached copy
    if row["cached"] and row["cached_path"]:
        p = Path(row["cached_path"])
        if p.is_file():
            return p
    path = _resolve_output_path(row["filename"], row["subfolder"], row["source_type"])
    return path or _try_reattach_input_ref(image_hash)


def reattach_record(image_hash: str, filename: str, subfolder: str, source_type: str) -> None:
    """Re-point a record at a digest-verified copy of its content. Callers must
    have proven the file's sha256 equals image_hash — the hash IS the content,
    so a match makes re-pointing unconditionally safe."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE images SET filename = ?, subfolder = ?, source_type = ?, orphaned = 0 WHERE hash = ? AND deleted = 0",
            (filename, subfolder, source_type, image_hash),
        )
        conn.commit()


def _try_reattach_input_ref(image_hash: str) -> Path | None:
    """A record whose stored path went stale may still have a content-addressed
    copy staged for LoadImage by image-workflow (promptchain_source_<hash12>.*).
    Digest-verify the candidate before healing — the 12-char name prefix alone
    isn't proof, the file could have been replaced."""
    try:
        input_dir = Path(folder_paths.get_input_directory())
    except Exception:
        return None
    for candidate in input_dir.glob(f"promptchain_source_{image_hash[:12]}.*"):
        try:
            if candidate.is_file() and compute_hash(candidate) == image_hash:
                reattach_record(image_hash, candidate.name, "", "input")
                return candidate
        except OSError:
            # a match that's unreadable (locked / mid-delete) must not 500 the
            # image route — skip and let resolution fail cleanly
            continue
    return None


def delete_image(image_hash: str) -> dict:
    """Permanently delete an image. Content is addressed by hash, so the same
    bytes live at several paths at once — the primary output/input/temp file, a
    cached copy, and the promptchain_source_<hash12> staging copies LoadImage
    uses for lineage. resolve_image_path heals a record onto any surviving copy,
    so unlinking one file let the image reappear on the next read. Purge EVERY
    replica, then tombstone the record so heal-at-read can't bring it back."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT filename, subfolder, source_type, cached, cached_path FROM images WHERE hash = ?",
        (image_hash,),
    ).fetchone()
    if not row:
        return {"deleted": False, "reason": "not tracked"}

    victims: list[Path] = []
    if row["cached"] and row["cached_path"]:
        victims.append(Path(row["cached_path"]))
    primary = _resolve_output_path(row["filename"], row["subfolder"], row["source_type"])
    if primary:
        victims.append(primary)
    # the content-addressed staging copies — digest-verify before unlinking so a
    # 12-hex prefix collision can never delete an unrelated source file
    try:
        input_dir = Path(folder_paths.get_input_directory())
        for cand in input_dir.glob(f"promptchain_source_{image_hash[:12]}.*"):
            try:
                if cand.is_file() and compute_hash(cand) == image_hash:
                    victims.append(cand)
            except OSError:
                continue
    except Exception:
        logger.debug("delete: could not scan input dir for %s", image_hash, exc_info=True)

    removed = []
    for p in victims:
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
        except OSError:
            logger.debug("delete: could not unlink %s", p, exc_info=True)

    # Keep the row (tombstoned) and its image_workflows links so the lineage
    # graph stays traversable through this node — the list/family queries below
    # filter deleted=1 out of every display.
    with _write_lock:
        conn = _get_conn()
        conn.execute("UPDATE images SET deleted = 1, orphaned = 1 WHERE hash = ?", (image_hash,))
        conn.commit()

    return {"deleted": True, "removed": removed}


# ── record ───────────────────────────────────────────────────────

def record_image(
    filename: str,
    subfolder: str = "",
    source_type: str = "output",
    workflow_id: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    path = _resolve_output_path(filename, subfolder, source_type)
    if not path:
        return None

    image_hash = compute_hash(path)
    now = int(time.time())
    meta = metadata or {}

    # read dimensions + format from image header
    width, height, fmt = None, None, None
    try:
        with Image.open(path) as img:
            width, height = img.size
            fmt = (img.format or "").lower()
    except Exception:
        logger.debug("failed to read image header %s", path, exc_info=True)

    file_size = path.stat().st_size

    # resolve parent lineage (img2img input)
    parent_hash = None
    parent_filename = meta.get("parent_filename")
    if parent_filename:
        parent_hash = _resolve_and_register_parent(parent_filename)

    with _write_lock:
        conn = _get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO images
                (hash, filename, subfolder, source_type, width, height, format, file_size, created_at,
                 prompt, negative, seed, model, steps, cfg, sampler, scheduler, denoise, parent_hash,
                 generation_time, prompt_id, source_files, lora, vae, regions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            image_hash, filename, subfolder, source_type,
            width, height, fmt, file_size, now,
            meta.get("prompt"), meta.get("negative"),
            meta.get("seed"), meta.get("model"),
            meta.get("steps"), meta.get("cfg"),
            meta.get("sampler"), meta.get("scheduler"),
            meta.get("denoise"),
            parent_hash,
            meta.get("generation_time"), meta.get("prompt_id"),
            meta.get("source_files"), meta.get("lora"), meta.get("vae"),
            meta.get("regions"),
        ))

        # A previously-deleted image that's generated again should return — clear
        # its tombstone now that real bytes for this content exist on disk. This
        # is the ONLY path that revives a deleted hash (read-repair cannot).
        conn.execute("UPDATE images SET deleted = 0 WHERE hash = ? AND deleted = 1", (image_hash,))

        if workflow_id:
            conn.execute("""
                INSERT OR IGNORE INTO image_workflows (hash, workflow_id, created_at)
                VALUES (?, ?, ?)
            """, (image_hash, workflow_id, now))

            conn.execute("""
                INSERT INTO workflows (workflow_id, first_seen, last_used)
                VALUES (?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET last_used = excluded.last_used
            """, (workflow_id, now, now))

        # A derived image (upscale/inpaint/edit) also joins its parent's
        # workflows so the source panel's lineage families pick it up and
        # reorder — its own fresh-id timeline coexists. Chains propagate
        # without a transitive walk: each parent joined ITS parent's
        # workflows when it was recorded.
        attached = [workflow_id] if workflow_id else []
        if parent_hash:
            parent_wids = conn.execute(
                "SELECT workflow_id FROM image_workflows WHERE hash = ?", (parent_hash,)
            ).fetchall()
            for row in parent_wids:
                wid = row["workflow_id"]
                if wid in attached:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO image_workflows (hash, workflow_id, created_at)
                    VALUES (?, ?, ?)
                """, (image_hash, wid, now))
                attached.append(wid)

        conn.commit()

    return {
        "hash": image_hash,
        "filename": filename,
        "subfolder": subfolder,
        "width": width,
        "height": height,
        "format": fmt,
        "file_size": file_size,
        "created_at": now,
        "parent_hash": parent_hash,
        "workflows": attached,
    }


def _resolve_and_register_parent(parent_filename: str) -> str | None:
    for base_fn, source_type in [
        (folder_paths.get_input_directory, "input"),
        (folder_paths.get_output_directory, "output"),
    ]:
        try:
            base = Path(base_fn())
        except Exception:
            continue
        candidate = base / parent_filename
        if candidate.is_file():
            # parent_filename may be subfolder-prefixed (e.g. a scoped inpaint ref
            # "promptchain_inpaint/promptchain_ref_xxx.png"); record its REAL
            # subfolder so the parent stays resolvable later — not "" as if it
            # lived in the input root.
            try:
                rel_sub = candidate.parent.relative_to(base)
                sub = "" if str(rel_sub) == "." or ".." in rel_sub.parts else rel_sub.as_posix()
            except ValueError:
                sub = ""
            parent_hash = compute_hash(candidate)
            pw, ph, pf = None, None, None
            try:
                with Image.open(candidate) as img:
                    pw, ph = img.size
                    pf = (img.format or "").lower()
            except Exception:
                pass
            psize = candidate.stat().st_size
            pmtime = int(candidate.stat().st_mtime)
            with _write_lock:
                conn = _get_conn()
                existing = conn.execute(
                    "SELECT filename, subfolder, source_type FROM images WHERE hash = ?",
                    (parent_hash,),
                ).fetchone()
                if not existing:
                    conn.execute("""
                        INSERT OR IGNORE INTO images
                            (hash, filename, subfolder, source_type, width, height, format, file_size, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        parent_hash, candidate.name, sub, source_type,
                        pw, ph, pf, psize, pmtime,
                    ))
                    conn.commit()
                elif not _resolve_output_path(existing["filename"], existing["subfolder"], existing["source_type"]):
                    # The stored path went stale (file moved/deleted) while the
                    # content in hand is digest-identical — re-point the record
                    # instead of leaving the family's reference dead.
                    conn.execute(
                        "UPDATE images SET filename = ?, subfolder = ?, source_type = ?, orphaned = 0 WHERE hash = ? AND deleted = 0",
                        (candidate.name, sub, source_type, parent_hash),
                    )
                    conn.commit()
            # A scoped inpaint ref that became a lineage parent must outlive the
            # age sweep, or the family's reference would later orphan.
            try:
                from . import inpaint_files
                inpaint_files.pin(str(candidate))
            except Exception:
                pass
            return parent_hash
    return None


# ── queries ──────────────────────────────────────────────────────

def get_workflow_images(workflow_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT i.* FROM images i
        JOIN image_workflows iw ON i.hash = iw.hash
        WHERE iw.workflow_id = ? AND i.deleted = 0
        ORDER BY i.created_at DESC
        LIMIT ? OFFSET ?
    """, (workflow_id, limit, offset)).fetchall()
    return [dict(row) for row in rows]


def get_workflow_image_count(workflow_id: str) -> int:
    conn = _get_conn()
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM image_workflows iw
        JOIN images i ON i.hash = iw.hash
        WHERE iw.workflow_id = ? AND i.deleted = 0
    """, (workflow_id,)).fetchone()
    return row["cnt"] if row else 0


def check_orphans(hashes: list[str]) -> list[str]:
    orphaned = []
    for h in hashes:
        path = resolve_image_path(h)
        if not path or not path.is_file():
            orphaned.append(h)

    orphaned_set = set(orphaned)
    with _write_lock:
        conn = _get_conn()
        for h in hashes:
            conn.execute("UPDATE images SET orphaned = ? WHERE hash = ?", (1 if h in orphaned_set else 0, h))
        conn.commit()
    return orphaned


def get_image_meta(image_hash: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM images WHERE hash = ? AND deleted = 0", (image_hash,)).fetchone()
    return dict(row) if row else None


# ── lineage ──────────────────────────────────────────────────────

def get_ancestors(image_hash: str) -> list[dict]:
    conn = _get_conn()
    ancestors = []
    current = image_hash
    visited = set()
    while current and current not in visited:
        visited.add(current)
        row = conn.execute("SELECT * FROM images WHERE hash = ?", (current,)).fetchone()
        if not row:
            break
        # climb through deleted intermediates so the chain stays connected, but
        # don't surface a deleted node as a card
        if current != image_hash and not row["deleted"]:
            ancestors.append(dict(row))
        current = row["parent_hash"]
    ancestors.reverse()
    return ancestors


def get_descendants(image_hash: str) -> list[dict]:
    conn = _get_conn()
    descendants = []
    visited = set()
    queue = deque([image_hash])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        children = conn.execute(
            "SELECT * FROM images WHERE parent_hash = ? ORDER BY created_at ASC",
            (current,),
        ).fetchall()
        for child in children:
            d = dict(child)
            if not d["deleted"]:
                descendants.append(d)
            queue.append(d["hash"])
    return descendants


def get_family(image_hash: str) -> list[dict]:
    """The image's full provenance tree, root-first.

    Ancestors/descendants only walk the vertical line, so siblings (two
    upscales of the same source) are invisible from each other. The viewer's
    up/down navigation wants the whole family: climb to the root, then DFS so
    each branch reads contiguously, siblings in creation order.
    """
    conn = _get_conn()
    current = image_hash
    climbed = set()
    while current not in climbed:
        climbed.add(current)
        row = conn.execute("SELECT parent_hash FROM images WHERE hash = ?", (current,)).fetchone()
        if not row or not row["parent_hash"]:
            break
        current = row["parent_hash"]
    root = current

    family = []
    seen = set()
    stack = [root]
    while stack:
        h = stack.pop()
        if h in seen:
            continue
        seen.add(h)
        row = conn.execute("SELECT * FROM images WHERE hash = ?", (h,)).fetchone()
        if not row:
            continue
        if not row["deleted"]:
            family.append(dict(row))
        children = conn.execute(
            # DESC because the stack pops last-first, visiting children ASC
            "SELECT hash FROM images WHERE parent_hash = ? ORDER BY created_at DESC",
            (h,),
        ).fetchall()
        stack.extend(c["hash"] for c in children)
    return family


def get_lineage(image_hash: str) -> dict:
    image = get_image_meta(image_hash)
    if not image:
        return {"image": None, "ancestors": [], "descendants": [], "family": []}
    return {
        "image": image,
        "ancestors": get_ancestors(image_hash),
        "descendants": get_descendants(image_hash),
        "family": get_family(image_hash),
    }


# ── workflow UUID duplicate detection ────────────────────────────

def try_register_workflow_atomic(workflow_id: str, filepath: str) -> tuple[bool, str | None]:
    """Atomically check-and-register a workflow path.

    Returns:
        (True, None) — new workflow, registered
        (True, filepath) — same path, timestamp updated
        (False, existing_path) — different path, potential duplicate
    """
    import os
    normalized = os.path.normpath(os.path.abspath(filepath))
    now = int(time.time())

    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT filepath FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()

        if row:
            existing = row["filepath"]
            try:
                normalized_existing = os.path.normpath(os.path.abspath(existing))
            except (OSError, TypeError):
                normalized_existing = existing

            if normalized_existing == normalized:
                conn.execute(
                    "UPDATE workflows SET last_used = ? WHERE workflow_id = ?",
                    (now, workflow_id),
                )
                conn.commit()
                return (True, filepath)
            else:
                return (False, existing)

        conn.execute(
            "INSERT INTO workflows (workflow_id, filepath, first_seen, last_used) VALUES (?, ?, ?, ?)",
            (workflow_id, filepath, now, now),
        )
        conn.commit()
        return (True, None)


def clone_and_register_workflow_atomic(
    from_workflow_id: str,
    to_workflow_id: str,
    to_filepath: str,
) -> tuple[bool, int]:
    """Clone image attachments from one workflow to another and register the new one."""
    now = int(time.time())

    with _write_lock:
        conn = _get_conn()
        try:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO image_workflows (hash, workflow_id, created_at)
                SELECT hash, ?, ? FROM image_workflows WHERE workflow_id = ?
            """, (to_workflow_id, now, from_workflow_id))
            cloned = cursor.rowcount

            conn.execute(
                """INSERT INTO workflows (workflow_id, filepath, first_seen, last_used) VALUES (?, ?, ?, ?)
                   ON CONFLICT(workflow_id) DO UPDATE SET filepath = excluded.filepath, last_used = excluded.last_used""",
                (to_workflow_id, to_filepath, now, now),
            )
            conn.commit()
            return (True, cloned)
        except Exception:
            conn.rollback()
            return (False, 0)


def register_workflow(workflow_id: str, filepath: str):
    now = int(time.time())
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE workflows SET filepath = ?, last_used = ? WHERE workflow_id = ?",
            (filepath, now, workflow_id),
        )
        conn.commit()


def update_image_path(image_hash: str, filename: str, subfolder: str = ""):
    """Update the stored path for an image (used by watcher on file rename/move)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE images SET filename = ?, subfolder = ? WHERE hash = ?",
            (filename, subfolder, image_hash),
        )
        conn.commit()


def detach_workflow_images(workflow_id: str) -> int:
    """Remove all image associations for a workflow. Images stay in DB and on disk."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM image_workflows WHERE workflow_id = ?", (workflow_id,))
        conn.commit()
        return cur.rowcount


def detach_images(workflow_id: str, hashes: list[str]) -> int:
    """Remove specific image associations from a workflow."""
    if not hashes:
        return 0
    with _write_lock:
        conn = _get_conn()
        placeholders = ",".join("?" * len(hashes))
        cur = conn.execute(
            f"DELETE FROM image_workflows WHERE workflow_id = ? AND hash IN ({placeholders})",
            [workflow_id] + list(hashes),
        )
        conn.commit()
        return cur.rowcount


def find_image_by_path(filename: str, subfolder: str = "",
                       source_type: str = "output") -> dict | None:
    """Look up an image by its file path (not hash)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM images WHERE filename = ? AND subfolder = ? AND source_type = ? AND deleted = 0",
        (filename, subfolder, source_type),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Browse favorites
# ---------------------------------------------------------------------------

def get_favorites(scope: str) -> set[str]:
    conn = _get_conn()
    rows = conn.execute("SELECT path FROM browse_favorites WHERE scope = ?", (scope,)).fetchall()
    return {row["path"] for row in rows}


def set_favorite(scope: str, path: str, on: bool):
    import time
    with _write_lock:
        conn = _get_conn()
        if on:
            conn.execute(
                "INSERT OR IGNORE INTO browse_favorites (scope, path, created_at) VALUES (?, ?, ?)",
                (scope, path, int(time.time())),
            )
        else:
            conn.execute("DELETE FROM browse_favorites WHERE scope = ? AND path = ?", (scope, path))
        conn.commit()


def move_favorites(src_scope: str, src_path: str, dst_scope: str, dst_path: str):
    """Re-key stars when a file or folder moves — the exact path plus any
    descendants (a moved folder carries its starred contents).  Prefix
    matching happens in Python; favorites are few and LIKE-escaping isn't
    worth the fragility."""
    import time
    with _write_lock:
        conn = _get_conn()
        prefix = src_path + "/"
        rows = conn.execute("SELECT path FROM browse_favorites WHERE scope = ?", (src_scope,)).fetchall()
        now = int(time.time())
        for row in rows:
            p = row["path"]
            if p != src_path and not p.startswith(prefix):
                continue
            new_path = dst_path + p[len(src_path):]
            conn.execute("DELETE FROM browse_favorites WHERE scope = ? AND path = ?", (src_scope, p))
            conn.execute(
                "INSERT OR REPLACE INTO browse_favorites (scope, path, created_at) VALUES (?, ?, ?)",
                (dst_scope, new_path, now),
            )
        conn.commit()


def remove_favorites(scope: str, paths: list[str]):
    """Prune stars for deleted paths and their descendants."""
    with _write_lock:
        conn = _get_conn()
        rows = conn.execute("SELECT path FROM browse_favorites WHERE scope = ?", (scope,)).fetchall()
        for row in rows:
            p = row["path"]
            if any(p == victim or p.startswith(victim + "/") for victim in paths):
                conn.execute("DELETE FROM browse_favorites WHERE scope = ? AND path = ?", (scope, p))
        conn.commit()


# ---------------------------------------------------------------------------
# Perceptual-hash cache (duplicate detection)
# ---------------------------------------------------------------------------

def get_phashes(scope: str) -> dict[str, tuple[int, int, int]]:
    """{path: (mtime_ns, size, phash)} for every cached hash in a scope."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT path, mtime_ns, size, phash FROM browse_phash WHERE scope = ?", (scope,)
    ).fetchall()
    # normalize the signed storage back to unsigned 64-bit
    return {r["path"]: (r["mtime_ns"], r["size"], r["phash"] & 0xFFFFFFFFFFFFFFFF) for r in rows}


def upsert_phashes(scope: str, rows: list[tuple[str, int, int, int]]):
    """rows: (path, mtime_ns, size, phash). SQLite stores the 64-bit hash as
    a signed int; callers convert back with & 0xFFFFFFFFFFFFFFFF."""
    if not rows:
        return
    with _write_lock:
        conn = _get_conn()
        conn.executemany(
            """INSERT OR REPLACE INTO browse_phash (scope, path, mtime_ns, size, phash)
               VALUES (?, ?, ?, ?, ?)""",
            [(scope, p, m, s, h - 0x10000000000000000 if h >= 0x8000000000000000 else h)
             for p, m, s, h in rows],
        )
        conn.commit()
