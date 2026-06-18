import hashlib
import threading

# ── state storage ───────────────────────────────────────────────────

_iterate_state: dict[str, dict] = {}
# {content_hash: {"index": int, "cycle": int, "total": int}}

_iterate_lock = threading.Lock()

# ── subordinate registry ────────────────────────────────────────────

_subordinate_nodes: set[str] = set()
# node IDs that should NOT auto-advance during execute


def set_subordinate_nodes(node_ids: list[str]):
    global _subordinate_nodes
    _subordinate_nodes = set(str(nid) for nid in node_ids)


def is_subordinate_node(unique_id: str) -> bool:
    return str(unique_id) in _subordinate_nodes


# ── hash generation ─────────────────────────────────────────────────

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parent_hash(node_id: str, total: int) -> str:
    key = f"parent_iterate:{node_id}:{total}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ── state machine ──────────────────────────────────────────────────

def get_iterate_state(
    hash_key: str,
    client_index: int,
    client_cycle: int,
    total: int,
    advance: bool,
) -> tuple[int, int, int, int, bool]:
    """Get current iterate state, optionally advancing for next execution.

    On first call (hash_key not seen): initializes from client values.
    On subsequent calls: uses server state (client may be stale from batch).

    Returns: (current_index, next_index, current_cycle, next_cycle, wrapped)
    """
    with _iterate_lock:
        if hash_key not in _iterate_state:
            # first execution with this content — seed from client
            _iterate_state[hash_key] = {
                "index": client_index % total if total > 0 else 0,
                "cycle": max(1, client_cycle),
                "total": total,
            }

        state = _iterate_state[hash_key]

        # total may have changed (user edited labels)
        if state["total"] != total:
            state["total"] = total
            if total > 0 and state["index"] >= total:
                state["index"] = 0
                state["cycle"] = 1

        current_index = state["index"]
        current_cycle = state["cycle"]

        if advance and total > 0:
            next_index = (current_index + 1) % total
            wrapped = next_index == 0
            next_cycle = current_cycle + 1 if wrapped else current_cycle

            state["index"] = next_index
            state["cycle"] = next_cycle

            return current_index, next_index, current_cycle, next_cycle, wrapped

        # no advance — return current state unchanged
        return current_index, current_index, current_cycle, current_cycle, False


def advance_iterate_state(hash_key: str) -> tuple[int, int, bool, int, int] | None:
    """Manually advance state (called by JS for subordinate nodes post-execution).

    Returns: (new_index, new_cycle, wrapped, prev_index, prev_cycle)
    Returns None if hash_key not found.
    """
    with _iterate_lock:
        state = _iterate_state.get(hash_key)
        if not state:
            return None

        total = state["total"]
        if total <= 0:
            return None

        prev_index = state["index"]
        prev_cycle = state["cycle"]

        new_index = (prev_index + 1) % total
        wrapped = new_index == 0
        new_cycle = prev_cycle + 1 if wrapped else prev_cycle

        state["index"] = new_index
        state["cycle"] = new_cycle

        return new_index, new_cycle, wrapped, prev_index, prev_cycle


def set_iterate_state(hash_key: str, index: int, cycle: int):
    # Creates entry if missing (handles server restart between execution and revert).
    with _iterate_lock:
        if hash_key not in _iterate_state:
            _iterate_state[hash_key] = {"index": index, "cycle": cycle, "total": 0}
        else:
            _iterate_state[hash_key]["index"] = index
            _iterate_state[hash_key]["cycle"] = cycle


def reset_iterate_state(hash_key: str | None = None):
    with _iterate_lock:
        if hash_key is None:
            _iterate_state.clear()
        elif hash_key in _iterate_state:
            del _iterate_state[hash_key]
