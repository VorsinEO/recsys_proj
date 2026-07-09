import json
from collections import defaultdict
from hashlib import md5
from typing import Dict, Iterable, List, Set

import redis

from catalog_store import get_items_genres
from config import (
    CANDIDATE_POOL_SIZE,
    DISLIKE_WEIGHT,
    GENRE_INDEX_PREFIX,
    LIKE_WEIGHT,
    TOP_GENRES_PER_USER,
    USER_CANDIDATES_PREFIX,
    USER_DISLIKED_PREFIX,
)
from watched_filter import WatchedFilter

USER_PROFILE_PREFIX = 'user_profile:'
POPULAR_LIKES_KEY = 'popular_likes'
CO_LIKE_PREFIX = 'co_like:'
ONLINE_POPULAR_SCAN = 350
ONLINE_CO_LIKE_PER_ITEM = 50
ONLINE_RANDOM_PER_GENRE = 80
FORCED_HEAD_SIZE = 5
TOP_POPULAR_SLOTS = 50
SOFT_CO_LIKE_WEIGHT = 0.5
SOFT_CO_LIKE_NEIGHBORS = 15


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _load_profile(redis_connection: redis.Redis, user_id: str) -> Dict[str, float]:
    payload = redis_connection.get(f'{USER_PROFILE_PREFIX}{user_id}')
    if not payload:
        return {}
    raw = json.loads(payload)
    return {str(genre): float(weight) for genre, weight in raw.items()}


def _load_disliked(redis_connection: redis.Redis, user_id: str) -> set[str]:
    payload = redis_connection.get(f'{USER_DISLIKED_PREFIX}{user_id}')
    if not payload:
        return set()
    return {str(item_id) for item_id in json.loads(payload)}


def _sample_genre_items(redis_connection: redis.Redis, genre: str, count: int) -> List[str]:
    key = f'{GENRE_INDEX_PREFIX}{genre}'
    sampled = redis_connection.srandmember(key, count)
    if not sampled:
        return []
    if not isinstance(sampled, list):
        sampled = [sampled]
    return [_decode(item_id) for item_id in sampled]


def get_popular_item_ids(redis_connection: redis.Redis, count: int = 50) -> List[str]:
    try:
        rows = redis_connection.zrevrange(POPULAR_LIKES_KEY, 0, max(0, count - 1))
    except redis.exceptions.ConnectionError:
        return []
    return [_decode(item_id) for item_id in rows]


def get_popular_scores(redis_connection: redis.Redis, count: int = 250) -> Dict[str, float]:
    try:
        rows = redis_connection.zrevrange(POPULAR_LIKES_KEY, 0, max(0, count - 1), withscores=True)
    except redis.exceptions.ConnectionError:
        return {}
    return {_decode(item_id): float(score) for item_id, score in rows}


def get_co_liked_items(redis_connection: redis.Redis, item_id: str, count: int = 30) -> List[tuple[str, float]]:
    try:
        rows = redis_connection.zrevrange(f'{CO_LIKE_PREFIX}{item_id}', 0, max(0, count - 1), withscores=True)
    except redis.exceptions.ConnectionError:
        return []
    return [(_decode(other_id), float(score)) for other_id, score in rows]


def record_co_likes(redis_connection: redis.Redis, liked_item_ids: Iterable[str], weight: float = 1.0) -> None:
    liked = [str(item_id) for item_id in liked_item_ids]
    if len(liked) < 2:
        return
    pipe = redis_connection.pipeline(transaction=False)
    for left in liked:
        for right in liked:
            if left == right:
                continue
            pipe.zincrby(f'{CO_LIKE_PREFIX}{left}', weight, right)
    pipe.execute()


def soft_link_item_to_popular(
    redis_connection: redis.Redis,
    item_id: str,
    popular_in_genre: List[str],
    weight: float = SOFT_CO_LIKE_WEIGHT,
) -> int:
    neighbors = [other for other in popular_in_genre if other != item_id][:SOFT_CO_LIKE_NEIGHBORS]
    if not neighbors:
        return 0
    pipe = redis_connection.pipeline(transaction=False)
    for other in neighbors:
        pipe.zincrby(f'{CO_LIKE_PREFIX}{item_id}', weight, other)
        pipe.zincrby(f'{CO_LIKE_PREFIX}{other}', weight * 0.5, item_id)
    pipe.execute()
    return len(neighbors)


def rebuild_co_likes_from_pairs(
    redis_connection: redis.Redis,
    user_liked_items: Dict[str, Set[str]],
) -> int:
    updated_users = 0
    pipe = redis_connection.pipeline(transaction=False)
    ops = 0
    for liked in user_liked_items.values():
        items = list(liked)
        if len(items) < 2:
            continue
        updated_users += 1
        for left in items:
            for right in items:
                if left == right:
                    continue
                pipe.zincrby(f'{CO_LIKE_PREFIX}{left}', 1, right)
                ops += 1
                if ops >= 2000:
                    pipe.execute()
                    pipe = redis_connection.pipeline(transaction=False)
                    ops = 0
    if ops:
        pipe.execute()
    return updated_users


def rotate_list(item_ids: List[str], seed: str) -> List[str]:
    if not item_ids:
        return []
    offset = int(md5(seed.encode()).hexdigest(), 16) % len(item_ids)
    return item_ids[offset:] + item_ids[:offset]


def _score_candidate(
    item_id: str,
    genres: List[str],
    profile: Dict[str, float],
    popular_scores: Dict[str, float],
    co_like_scores: Dict[str, float],
) -> float:
    if not genres and item_id not in co_like_scores:
        return float('-inf')

    positive_hits = [genre for genre in genres if profile.get(genre, 0.0) > 0]
    relevance = sum(profile.get(genre, 0.0) for genre in genres)
    if relevance > 0:
        relevance *= 2.5 + 0.5 * len(positive_hits)
    elif positive_hits:
        relevance = 0.5
    else:
        relevance = 0.0

    pop = popular_scores.get(item_id, 0.0)
    pop_boost = 1.0 * (pop ** 0.5) if pop > 0 else 0.0
    co_boost = 14.0 * co_like_scores.get(item_id, 0.0)
    return relevance + pop_boost + co_boost


def update_user_from_interact(
    redis_connection: redis.Redis,
    user_id: str,
    item_ids: List[str],
    actions: List[str],
) -> dict:
    if not item_ids:
        return {'updated': False, 'reason': 'empty'}

    item_ids = [str(item_id) for item_id in item_ids]
    catalog = get_items_genres(redis_connection, item_ids)
    profile = _load_profile(redis_connection, user_id)
    disliked = _load_disliked(redis_connection, user_id)
    shown = WatchedFilter(redis_connection).get_shown(user_id)
    excluded = set(item_ids) | disliked

    liked_now = [item_id for item_id, action in zip(item_ids, actions) if action == 'like']

    pipe = redis_connection.pipeline(transaction=False)
    for item_id, action in zip(item_ids, actions):
        weight = LIKE_WEIGHT if action == 'like' else -DISLIKE_WEIGHT
        for genre in catalog.get(item_id, []):
            profile[genre] = profile.get(genre, 0.0) + weight
        if action == 'dislike':
            disliked.add(item_id)
        if action == 'like':
            pipe.zincrby(POPULAR_LIKES_KEY, 1, item_id)
    pipe.execute()

    if len(liked_now) >= 2:
        record_co_likes(redis_connection, liked_now)

    positive = sorted(
        ((genre, weight) for genre, weight in profile.items() if weight > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:TOP_GENRES_PER_USER]
    positive_genres = {genre for genre, _ in positive}

    popular_scores = get_popular_scores(redis_connection, count=ONLINE_POPULAR_SCAN)
    popular_ids = list(popular_scores.keys())
    popular_catalog = get_items_genres(redis_connection, popular_ids)
    popular_in_genre: List[str] = []
    for item_id in popular_ids:
        if item_id in excluded:
            continue
        item_genres = set(popular_catalog.get(item_id, []))
        if positive_genres and not (item_genres & positive_genres):
            continue
        popular_in_genre.append(item_id)

    soft_links = 0
    if liked_now and popular_in_genre:
        for item_id in liked_now:
            soft_links += soft_link_item_to_popular(redis_connection, item_id, popular_in_genre)

    co_like_scores: Dict[str, float] = defaultdict(float)
    for item_id in liked_now:
        for neighbor_id, score in get_co_liked_items(redis_connection, item_id, ONLINE_CO_LIKE_PER_ITEM):
            if neighbor_id in excluded:
                continue
            co_like_scores[neighbor_id] += score

    random_fill: List[str] = []
    seen_random = set(excluded) | set(co_like_scores) | set(popular_in_genre)
    for genre, _ in positive:
        for item_id in _sample_genre_items(redis_connection, genre, ONLINE_RANDOM_PER_GENRE):
            if item_id in seen_random:
                continue
            seen_random.add(item_id)
            random_fill.append(item_id)

    # Prefer UNSEEN items in the head — critical for NDCG after cold-start already showed hits.
    co_ranked = [item_id for item_id, _ in sorted(co_like_scores.items(), key=lambda item: item[1], reverse=True)]
    popular_pool = popular_in_genre[:]

    def take_unseen(pool: List[str], limit: int, used: Set[str]) -> List[str]:
        picked: List[str] = []
        for item_id in pool:
            if item_id in used or item_id in shown or item_id in excluded:
                continue
            picked.append(item_id)
            used.add(item_id)
            if len(picked) >= limit:
                break
        return picked

    used: Set[str] = set()
    head = take_unseen(co_ranked, FORCED_HEAD_SIZE, used)
    if len(head) < FORCED_HEAD_SIZE:
        head += take_unseen(popular_pool, FORCED_HEAD_SIZE - len(head), used)
    # Prefer unseen long-tail over re-showing cold-start hits in the head.
    if len(head) < FORCED_HEAD_SIZE:
        head += take_unseen(random_fill, FORCED_HEAD_SIZE - len(head), used)
    # Last resort: already-shown items.
    if len(head) < FORCED_HEAD_SIZE:
        for item_id in co_ranked + popular_pool + random_fill:
            if item_id in used or item_id in excluded:
                continue
            head.append(item_id)
            used.add(item_id)
            if len(head) >= FORCED_HEAD_SIZE:
                break

    mid = [item_id for item_id in popular_pool if item_id not in used][:TOP_POPULAR_SLOTS]
    used.update(mid)
    # Exploration tail: prioritize unseen long-tail for coverage.
    unseen_tail = [item_id for item_id in random_fill if item_id not in used and item_id not in shown]
    seen_tail = [item_id for item_id in random_fill if item_id not in used and item_id in shown]
    tail = unseen_tail + seen_tail
    ordered_ids = head + mid + tail

    candidate_catalog = get_items_genres(redis_connection, ordered_ids)
    scored = {
        item_id: _score_candidate(
            item_id,
            candidate_catalog.get(item_id, []),
            profile,
            popular_scores,
            co_like_scores,
        )
        for item_id in ordered_ids
    }

    head_final = [item_id for item_id in head if scored.get(item_id, float('-inf')) > float('-inf')]
    rest = [item_id for item_id in ordered_ids if item_id not in head_final]
    # Unseen rest first, then by score — keeps NDCG head clean and coverage in the tail.
    rest.sort(
        key=lambda item_id: (
            0 if item_id not in shown else 1,
            -scored.get(item_id, float('-inf')),
        )
    )
    candidates = (head_final + rest)[:CANDIDATE_POOL_SIZE]

    pipe = redis_connection.pipeline(transaction=False)
    pipe.set(f'{USER_PROFILE_PREFIX}{user_id}', json.dumps(profile))
    pipe.set(f'{USER_DISLIKED_PREFIX}{user_id}', json.dumps(sorted(disliked)))
    if candidates:
        pipe.set(f'{USER_CANDIDATES_PREFIX}{user_id}', json.dumps(candidates))
    pipe.execute()

    return {
        'updated': bool(candidates),
        'candidate_count': len(candidates),
        'top_genres': [genre for genre, _ in positive],
        'co_like_seed': len(liked_now),
        'head_size': len(head_final),
        'head_co_like': sum(1 for item_id in head_final if item_id in co_like_scores),
        'head_unseen': sum(1 for item_id in head_final if item_id not in shown),
        'soft_links': soft_links,
    }
