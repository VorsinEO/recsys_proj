import json
import random
from typing import Dict, Iterable, List, Set

import redis

from config import (
    CATALOG_ALL_KEY,
    CATALOG_ITEM_PREFIX,
    GENRE_INDEX_PREFIX,
    GENRE_POOL_BACKFILL_SIZE,
    GLOBAL_CANDIDATE_POOL_SIZE,
    MAX_SCORING_POOL_SIZE,
    MIN_GENRE_POOL_SIZE,
    TOP_GENRES_PER_USER,
)


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _genre_index_key(genre: str) -> str:
    return f'{GENRE_INDEX_PREFIX}{genre}'


def build_inverted_index(catalog: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    index: Dict[str, Set[str]] = {}
    for item_id, genres in catalog.items():
        for genre in genres:
            index.setdefault(genre, set()).add(str(item_id))
    return index


def save_items(redis_connection: redis.Redis, item_ids: List[str], genres: List[List[str]]) -> None:
    normalized_item_ids = [str(item_id) for item_id in item_ids]
    if not normalized_item_ids:
        return

    payload_by_key = {}
    genre_members: Dict[str, List[str]] = {}
    for item_id, item_genres in zip(normalized_item_ids, genres):
        payload_by_key[f'{CATALOG_ITEM_PREFIX}{item_id}'] = json.dumps(item_genres)
        for genre in item_genres:
            genre_members.setdefault(genre, []).append(item_id)

    pipe = redis_connection.pipeline(transaction=False)
    pipe.sadd(CATALOG_ALL_KEY, *normalized_item_ids)
    pipe.mset(payload_by_key)
    for genre, members in genre_members.items():
        pipe.sadd(_genre_index_key(genre), *members)
    pipe.execute()


def get_catalog_size(redis_connection: redis.Redis) -> int:
    return int(redis_connection.scard(CATALOG_ALL_KEY))


def get_all_item_ids(redis_connection: redis.Redis) -> List[str]:
    return [_decode(item_id) for item_id in redis_connection.smembers(CATALOG_ALL_KEY)]


def get_catalog_sample(
    redis_connection: redis.Redis,
    sample_size: int = GLOBAL_CANDIDATE_POOL_SIZE,
) -> Dict[str, List[str]]:
    total = get_catalog_size(redis_connection)
    if total == 0:
        return {}

    sample_size = min(sample_size, total)
    sampled_ids = redis_connection.srandmember(CATALOG_ALL_KEY, sample_size)
    if sampled_ids is None:
        return {}
    if not isinstance(sampled_ids, list):
        sampled_ids = [sampled_ids]

    return get_items_genres(redis_connection, [_decode(item_id) for item_id in sampled_ids])


def get_items_genres(redis_connection: redis.Redis, item_ids: Iterable[str]) -> Dict[str, List[str]]:
    unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids))
    if not unique_ids:
        return {}

    pipe = redis_connection.pipeline()
    for item_id in unique_ids:
        pipe.get(f'{CATALOG_ITEM_PREFIX}{item_id}')
    payloads = pipe.execute()

    catalog: Dict[str, List[str]] = {}
    for item_id, payload in zip(unique_ids, payloads):
        if payload:
            catalog[item_id] = json.loads(payload)
        else:
            catalog[item_id] = []
    return catalog


def get_full_catalog_genres(redis_connection: redis.Redis) -> Dict[str, List[str]]:
    return get_items_genres(redis_connection, get_all_item_ids(redis_connection))


def load_genre_index(redis_connection: redis.Redis) -> Dict[str, Set[str]]:
    index: Dict[str, Set[str]] = {}
    prefix = GENRE_INDEX_PREFIX
    for raw_key in redis_connection.scan_iter(f'{prefix}*'):
        key = _decode(raw_key)
        genre = key[len(prefix):]
        members = redis_connection.smembers(raw_key)
        index[genre] = {_decode(item_id) for item_id in members}
    return index


def ensure_genre_index(
    redis_connection: redis.Redis,
    catalog: Dict[str, List[str]],
) -> Dict[str, Set[str]]:
    index = load_genre_index(redis_connection)
    if index:
        return index

    inverted = build_inverted_index(catalog)
    if not inverted:
        return {}

    pipe = redis_connection.pipeline()
    for genre, item_ids in inverted.items():
        if item_ids:
            pipe.sadd(_genre_index_key(genre), *sorted(item_ids))
    pipe.execute()
    return inverted


def get_user_scoring_pool(
    profile: Dict[str, float],
    genre_index: Dict[str, Set[str]],
    interacted_item_ids: Iterable[str],
    all_item_ids: Iterable[str],
) -> Set[str]:
    pool = {str(item_id) for item_id in interacted_item_ids}
    positive_genres = sorted(
        ((genre, weight) for genre, weight in profile.items() if weight > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:TOP_GENRES_PER_USER]

    if not positive_genres:
        return pool

    # Sample from each liked genre instead of taking the whole inverted list
    # (Drama alone is ~12k items and makes recompute explode).
    remaining = max(0, MAX_SCORING_POOL_SIZE - len(pool))
    per_genre = max(1, remaining // max(1, len(positive_genres)))
    for genre, _ in positive_genres:
        members = list(genre_index.get(genre, set()))
        if not members:
            continue
        if len(members) > per_genre:
            members = random.sample(members, per_genre)
        pool.update(members)
        if len(pool) >= MAX_SCORING_POOL_SIZE:
            break

    if len(pool) < MIN_GENRE_POOL_SIZE:
        backfill = [
            item_id for item_id in all_item_ids
            if str(item_id) not in pool
        ]
        random.shuffle(backfill)
        for item_id in backfill[:GENRE_POOL_BACKFILL_SIZE]:
            pool.add(str(item_id))
            if len(pool) >= MAX_SCORING_POOL_SIZE:
                break

    if len(pool) > MAX_SCORING_POOL_SIZE:
        keep = list(pool)
        random.shuffle(keep)
        # Always keep interacted items.
        interacted = {str(item_id) for item_id in interacted_item_ids}
        selected = list(interacted)
        for item_id in keep:
            if item_id in interacted:
                continue
            selected.append(item_id)
            if len(selected) >= MAX_SCORING_POOL_SIZE:
                break
        pool = set(selected)

    return pool


def select_user_scoring_catalog(
    full_catalog: Dict[str, List[str]],
    genre_index: Dict[str, Set[str]],
    profile: Dict[str, float],
    interacted_item_ids: Iterable[str],
    cold_start_catalog: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    if not profile:
        return dict(cold_start_catalog)

    pool = get_user_scoring_pool(
        profile=profile,
        genre_index=genre_index,
        interacted_item_ids=interacted_item_ids,
        all_item_ids=full_catalog.keys(),
    )
    return {
        item_id: full_catalog[item_id]
        for item_id in pool
        if item_id in full_catalog
    }


def get_catalog_for_scoring(
    redis_connection: redis.Redis,
    required_item_ids: Iterable[str],
    sample_size: int = GLOBAL_CANDIDATE_POOL_SIZE,
) -> Dict[str, List[str]]:
    catalog = get_items_genres(redis_connection, required_item_ids)
    extra = get_catalog_sample(redis_connection, sample_size=sample_size)
    for item_id, genres in extra.items():
        catalog.setdefault(item_id, genres)
    return catalog


def get_catalog(redis_connection: redis.Redis) -> Dict[str, List[str]]:
    return get_catalog_sample(redis_connection, sample_size=get_catalog_size(redis_connection))
