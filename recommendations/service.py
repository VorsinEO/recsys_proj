import json
from typing import List

import redis

from catalog_store import get_all_item_ids, get_catalog, save_items
from config import (
    GLOBAL_CANDIDATES_KEY,
    TOP_K,
    USER_CANDIDATES_PREFIX,
)
from scoring import build_cold_start_candidates, select_recommendations
from watched_filter import WatchedFilter


def get_candidates_from_redis(redis_connection: redis.Redis, user_id: str) -> List[str]:
    payload = redis_connection.get(f'{USER_CANDIDATES_PREFIX}{user_id}')
    if payload:
        return [str(item_id) for item_id in json.loads(payload)]

    payload = redis_connection.get(GLOBAL_CANDIDATES_KEY)
    if payload:
        return [str(item_id) for item_id in json.loads(payload)]

    catalog = get_catalog(redis_connection)
    return build_cold_start_candidates(catalog, {})


def get_fallback_candidates(redis_connection: redis.Redis) -> List[str]:
    payload = redis_connection.get(GLOBAL_CANDIDATES_KEY)
    if payload:
        return [str(item_id) for item_id in json.loads(payload)]

    catalog = get_catalog(redis_connection)
    return build_cold_start_candidates(catalog, {})


def build_recommendations(
    redis_connection: redis.Redis,
    watched_filter: WatchedFilter,
    user_id: str,
) -> List[str]:
    candidates = get_candidates_from_redis(redis_connection, user_id)
    shown = watched_filter.get_shown(user_id)
    disliked = watched_filter.get_disliked(user_id)
    fallback = get_fallback_candidates(redis_connection)
    all_catalog = get_all_item_ids(redis_connection)
    if not fallback:
        fallback = all_catalog

    item_ids = select_recommendations(
        candidates=candidates,
        shown=shown,
        disliked=disliked,
        fallback=fallback + all_catalog,
        top_k=TOP_K,
    )
    watched_filter.add(user_id, item_ids)
    return item_ids


def add_catalog_items(
    redis_connection: redis.Redis,
    item_ids: List[str],
    genres: List[List[str]],
) -> None:
    normalized_genres = genres if genres else [[] for _ in item_ids]
    save_items(redis_connection, item_ids, normalized_genres)

    catalog = get_catalog(redis_connection)
    cold_start = build_cold_start_candidates(catalog, {})
    redis_connection.set(GLOBAL_CANDIDATES_KEY, json.dumps(cold_start))
