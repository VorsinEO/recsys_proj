import json
from typing import List, Tuple

import redis

from catalog_store import save_items
from config import (
    CATALOG_ALL_KEY,
    GLOBAL_CANDIDATE_STORE_SIZE,
    GLOBAL_CANDIDATES_KEY,
    TOP_K,
    USER_CANDIDATES_PREFIX,
)
from impression_store import increment_impressions
from online_update import get_popular_item_ids, rotate_list
from scoring import select_recommendations
from watched_filter import WatchedFilter

COLD_START_HEAD_SIZE = 5
POPULAR_IN_GLOBAL = 35
EXPLORE_SLOTS = 2


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def rotate_head_and_tail(
    item_ids: List[str],
    user_id: str,
    head_size: int = COLD_START_HEAD_SIZE,
) -> List[str]:
    if not item_ids:
        return []
    if len(item_ids) <= head_size:
        return rotate_list(item_ids, f'{user_id}:head')

    head = rotate_list(item_ids[:head_size], f'{user_id}:head')
    tail = item_ids[head_size:]
    return head + rotate_list(tail, f'{user_id}:tail')


def build_fast_global_candidates(redis_connection: redis.Redis) -> List[str]:
    popular_n = min(POPULAR_IN_GLOBAL, GLOBAL_CANDIDATE_STORE_SIZE)
    popular = get_popular_item_ids(redis_connection, count=popular_n)
    need = max(0, GLOBAL_CANDIDATE_STORE_SIZE - len(popular))
    sampled: List[str] = []
    if need:
        raw = redis_connection.srandmember(CATALOG_ALL_KEY, need + len(popular) + 100)
        if raw:
            if not isinstance(raw, list):
                raw = [raw]
            popular_set = set(popular)
            for item_id in raw:
                decoded = _decode(item_id)
                if decoded in popular_set:
                    continue
                sampled.append(decoded)
                if len(sampled) >= need:
                    break
    return popular + sampled


def ensure_global_candidates(redis_connection: redis.Redis) -> List[str]:
    payload = redis_connection.get(GLOBAL_CANDIDATES_KEY)
    if payload:
        return [str(item_id) for item_id in json.loads(payload)]

    global_candidates = build_fast_global_candidates(redis_connection)
    if global_candidates:
        redis_connection.set(GLOBAL_CANDIDATES_KEY, json.dumps(global_candidates))
    return global_candidates


def refresh_global_candidates(redis_connection: redis.Redis) -> List[str]:
    global_candidates = build_fast_global_candidates(redis_connection)
    if global_candidates:
        redis_connection.set(GLOBAL_CANDIDATES_KEY, json.dumps(global_candidates))
    else:
        redis_connection.delete(GLOBAL_CANDIDATES_KEY)
    return global_candidates


def get_fallback_candidates(redis_connection: redis.Redis, user_id: str) -> List[str]:
    return rotate_head_and_tail(ensure_global_candidates(redis_connection), user_id)


def _pick_with_explore(
    primary: List[str],
    explore: List[str],
    shown: set[str],
    disliked: set[str],
    top_k: int = TOP_K,
    explore_slots: int = EXPLORE_SLOTS,
) -> List[str]:
    """Keep first (top_k - explore_slots) relevant, last explore_slots from long-tail."""
    relevant_budget = max(1, top_k - explore_slots)
    selected: List[str] = []
    seen = set(shown) | set(disliked)

    for item_id in primary:
        if item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= relevant_budget:
            break

    for item_id in explore:
        if item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= top_k:
            break

    if len(selected) < top_k:
        for item_id in list(primary) + list(explore):
            if item_id in disliked or item_id in selected:
                continue
            selected.append(item_id)
            if len(selected) >= top_k:
                break
    return selected


def build_recommendations(
    redis_connection: redis.Redis,
    watched_filter: WatchedFilter,
    user_id: str,
) -> Tuple[List[str], dict]:
    user_payload = redis_connection.get(f'{USER_CANDIDATES_PREFIX}{user_id}')
    global_pool = ensure_global_candidates(redis_connection)
    popular_n = min(POPULAR_IN_GLOBAL, len(global_pool))
    explore_pool = global_pool[popular_n:]

    if user_payload:
        candidates = [str(item_id) for item_id in json.loads(user_payload)]
        source = 'user_candidates'
    else:
        candidates = rotate_head_and_tail(global_pool, user_id)
        source = 'cold_start'

    shown = watched_filter.get_shown(user_id)
    disliked = watched_filter.get_disliked(user_id)
    fallback = get_fallback_candidates(redis_connection, user_id)

    explore_candidates = rotate_list(
        list(dict.fromkeys(candidates[8:] + explore_pool + fallback)),
        f'{user_id}:explore',
    )
    unseen_primary = [
        item_id for item_id in candidates
        if item_id not in shown and item_id not in disliked
    ]
    item_ids = _pick_with_explore(
        primary=candidates,
        explore=explore_candidates,
        shown=shown,
        disliked=disliked,
        top_k=TOP_K,
        explore_slots=EXPLORE_SLOTS,
    )

    if len(item_ids) < TOP_K:
        item_ids = select_recommendations(
            candidates=candidates,
            shown=shown,
            disliked=disliked,
            fallback=fallback,
            top_k=TOP_K,
        )

    backfill_count = max(0, len(item_ids) - len(unseen_primary[:TOP_K]))
    watched_filter.add(user_id, item_ids)
    increment_impressions(redis_connection, item_ids)

    meta = {
        'source': source,
        'shown_count': len(shown),
        'disliked_count': len(disliked),
        'backfill_count': backfill_count,
        'explore_slots': EXPLORE_SLOTS,
    }
    return item_ids, meta


def add_catalog_items(
    redis_connection: redis.Redis,
    item_ids: List[str],
    genres: List[List[str]],
) -> None:
    normalized_genres = genres if genres else [[] for _ in item_ids]
    save_items(redis_connection, item_ids, normalized_genres)
    refresh_global_candidates(redis_connection)
