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
# Mega-hits for cold head (precision/NDCG), then second-tier for mid coverage.
POPULAR_HEAD = 20
POPULAR_SECOND_TIER = 80
POPULAR_IN_GLOBAL = POPULAR_SECOND_TIER
# Coverage gap: 0.66 → 0.80 needs ~3.5k more unique. Explore must sample full catalog,
# not the ~220-item global long-tail. P/NDCG have huge headroom (0.93 / 0.10).
COLD_EXPLORE_SLOTS = 3
UC_EXPLORE_SLOTS = 1
CATALOG_EXPLORE_SAMPLE = 48


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
    # Mid: second-tier popular (after mega-hits), then long-tail — both rotated.
    second_tier_end = min(POPULAR_SECOND_TIER, len(item_ids))
    mid = item_ids[head_size:second_tier_end]
    tail = item_ids[second_tier_end:]
    return head + rotate_list(mid, f'{user_id}:mid') + rotate_list(tail, f'{user_id}:tail')


def build_fast_global_candidates(redis_connection: redis.Redis) -> List[str]:
    popular_n = min(POPULAR_IN_GLOBAL, GLOBAL_CANDIDATE_STORE_SIZE)
    popular = get_popular_item_ids(redis_connection, count=popular_n)
    # Keep mega-hits first, second-tier next (already ordered by zrevrange).
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


def _catalog_explore_candidates(
    redis_connection: redis.Redis,
    user_id: str,
    exclude: set[str],
    count: int = CATALOG_EXPLORE_SAMPLE,
) -> List[str]:
    """Sample explore items from the full catalog (coverage), not the tiny global pool."""
    raw = redis_connection.srandmember(CATALOG_ALL_KEY, count + len(exclude) + 20)
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    picked: List[str] = []
    for item_id in raw:
        decoded = _decode(item_id)
        if decoded in exclude:
            continue
        picked.append(decoded)
        if len(picked) >= count:
            break
    return rotate_list(picked, f'{user_id}:explore')


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
    explore_slots: int = COLD_EXPLORE_SLOTS,
) -> List[str]:
    """Keep first (top_k - explore_slots) relevant, last explore_slots from long-tail."""
    explore_slots = max(0, min(explore_slots, top_k - 1)) if top_k > 1 else 0
    relevant_budget = top_k - explore_slots
    selected: List[str] = []
    seen = set(shown) | set(disliked)

    for item_id in primary:
        if item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= relevant_budget:
            break

    if explore_slots > 0:
        for item_id in explore:
            if item_id in seen:
                continue
            selected.append(item_id)
            seen.add(item_id)
            if len(selected) >= top_k:
                break

    if len(selected) < top_k:
        # Prefer unseen leftovers; only re-show if catalog is exhausted.
        leftovers = list(primary) + list(explore)
        for item_id in leftovers:
            if item_id in seen or item_id in disliked or item_id in selected:
                continue
            selected.append(item_id)
            if len(selected) >= top_k:
                break
        if len(selected) < top_k:
            for item_id in leftovers:
                if item_id in disliked or item_id in selected:
                    continue
                selected.append(item_id)
                if len(selected) >= top_k:
                    break
    return selected[:top_k]


def build_recommendations(
    redis_connection: redis.Redis,
    watched_filter: WatchedFilter,
    user_id: str,
) -> Tuple[List[str], dict]:
    user_payload = redis_connection.get(f'{USER_CANDIDATES_PREFIX}{user_id}')
    global_pool = ensure_global_candidates(redis_connection)
    popular_n = min(POPULAR_IN_GLOBAL, len(global_pool))
    mega_hits = set(global_pool[: min(POPULAR_HEAD, len(global_pool))])

    if user_payload:
        candidates = [str(item_id) for item_id in json.loads(user_payload)]
        source = 'user_candidates'
        explore_slots = UC_EXPLORE_SLOTS
    else:
        candidates = rotate_head_and_tail(global_pool, user_id)
        source = 'cold_start'
        explore_slots = COLD_EXPLORE_SLOTS

    shown = watched_filter.get_shown(user_id)
    disliked = watched_filter.get_disliked(user_id)
    fallback = get_fallback_candidates(redis_connection, user_id)

    # Full-catalog SRANDMEMBER for coverage; skip mega-hits / shown / disliked.
    explore_candidates: List[str] = []
    if explore_slots > 0:
        explore_candidates = _catalog_explore_candidates(
            redis_connection,
            user_id,
            exclude=set(shown) | set(disliked) | mega_hits,
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
        explore_slots=explore_slots,
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
        'explore_slots': explore_slots,
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
