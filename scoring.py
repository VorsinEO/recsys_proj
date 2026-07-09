import hashlib
from collections import defaultdict
from typing import Dict, Iterable, List, Set

import polars as pl

from config import (
    CANDIDATE_POOL_SIZE,
    COLD_START_SLICE_SIZE,
    DISLIKE_WEIGHT,
    GLOBAL_CANDIDATE_STORE_SIZE,
    LIKE_WEIGHT,
    MMR_LAMBDA,
    POPULARITY_BOOST,
    SCORING_MMR_INPUT_SIZE,
    TOP_K,
)
from impression_store import impression_penalty


def normalize_interactions(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.col('user_id').cast(pl.Utf8),
        pl.col('item_id').cast(pl.Utf8),
        pl.col('action').cast(pl.Utf8),
        pl.col('timestamp').cast(pl.Float64),
    ])


def user_seed(user_id: str) -> int:
    return int(hashlib.md5(user_id.encode()).hexdigest(), 16)


def genre_jaccard(genres_a: List[str], genres_b: List[str]) -> float:
    set_a = set(genres_a)
    set_b = set(genres_b)
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def build_popularity(interactions: pl.DataFrame) -> Dict[str, float]:
    if len(interactions) == 0:
        return {}

    latest = (
        interactions
        .sort('timestamp')
        .unique(['user_id', 'item_id'], keep='last')
        .filter(pl.col('action') == 'like')
    )
    counts = latest.groupby('item_id').count()
    if len(counts) == 0:
        return {}

    max_count = counts['count'].max()
    return {
        str(row['item_id']): row['count'] / max_count
        for row in counts.iter_rows(named=True)
    }


def build_user_profiles(interactions: pl.DataFrame, catalog: Dict[str, List[str]]) -> Dict[str, Dict[str, float]]:
    profiles: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    if len(interactions) == 0:
        return {}

    latest = interactions.sort('timestamp').unique(['user_id', 'item_id'], keep='last')
    for row in latest.iter_rows(named=True):
        user_id = str(row['user_id'])
        item_id = str(row['item_id'])
        action = row['action']
        weight = LIKE_WEIGHT if action == 'like' else -DISLIKE_WEIGHT
        for genre in catalog.get(item_id, []):
            profiles[user_id][genre] += weight
    return {user_id: dict(genres) for user_id, genres in profiles.items()}


def build_user_dislikes(interactions: pl.DataFrame) -> Dict[str, Set[str]]:
    dislikes: Dict[str, Set[str]] = defaultdict(set)
    if len(interactions) == 0:
        return {}

    latest = interactions.sort('timestamp').unique(['user_id', 'item_id'], keep='last')
    for row in latest.iter_rows(named=True):
        if row['action'] == 'dislike':
            dislikes[str(row['user_id'])].add(str(row['item_id']))
    return dict(dislikes)


def score_item(
    item_id: str,
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    profile: Dict[str, float],
    impressions: Dict[str, int],
) -> float:
    genres = catalog.get(item_id, [])
    positive_hits = sum(1 for genre in genres if profile.get(genre, 0.0) > 0)
    relevance = sum(profile.get(genre, 0.0) for genre in genres)
    if relevance > 0:
        relevance *= 2.0 + 0.5 * positive_hits
    popularity_boost = POPULARITY_BOOST * popularity.get(item_id, 0.0)
    # Keep impression penalty mild so relevance dominates.
    penalty = 0.05 * impression_penalty(item_id, impressions)
    return relevance + popularity_boost - penalty


def mmr_select(
    scored_items: List[tuple[str, float]],
    catalog: Dict[str, List[str]],
    pool_size: int = CANDIDATE_POOL_SIZE,
    lambda_param: float = MMR_LAMBDA,
) -> List[str]:
    remaining = sorted(scored_items, key=lambda item: item[1], reverse=True)
    if lambda_param >= 0.999:
        return [item_id for item_id, _ in remaining[:pool_size]]

    selected: List[str] = []
    while remaining and len(selected) < pool_size:
        best_idx = 0
        best_value = float('-inf')
        for idx, (item_id, relevance) in enumerate(remaining):
            if not selected:
                mmr_score = relevance
            else:
                max_similarity = max(
                    genre_jaccard(catalog.get(item_id, []), catalog.get(selected_id, []))
                    for selected_id in selected
                )
                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_similarity
            if mmr_score > best_value:
                best_value = mmr_score
                best_idx = idx
        selected.append(remaining.pop(best_idx)[0])

    return selected


def _cold_start_score(
    item_id: str,
    user_id: str | None,
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    impressions: Dict[str, int],
) -> float:
    genres = catalog.get(item_id, [])
    seed_bonus = 0.0
    if user_id:
        genre_key = genres[0] if genres else item_id
        seed_bonus = (user_seed(f'{user_id}:{genre_key}') % 1000) / 5000.0
    penalty = 0.05 * impression_penalty(item_id, impressions)
    # Prefer globally liked items in cold start instead of penalizing them.
    pop_boost = POPULARITY_BOOST * popularity.get(item_id, 0.0)
    return seed_bonus + pop_boost - penalty


def build_cold_start_candidates(
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    user_id: str | None = None,
    impressions: Dict[str, int] | None = None,
    pool_size: int = CANDIDATE_POOL_SIZE,
) -> List[str]:
    if not catalog:
        return []

    impressions = impressions or {}
    slice_size = min(COLD_START_SLICE_SIZE, len(catalog))
    ranked_items = sorted(
        catalog.keys(),
        key=lambda item_id: _cold_start_score(item_id, user_id, catalog, popularity, impressions),
        reverse=True,
    )

    if user_id:
        offset = user_seed(user_id) % max(1, len(ranked_items) - slice_size + 1)
        ranked_items = ranked_items[offset:offset + slice_size]
    else:
        ranked_items = ranked_items[:slice_size]

    scored_items = [
        (item_id, _cold_start_score(item_id, user_id, catalog, popularity, impressions))
        for item_id in ranked_items
    ]
    return mmr_select(scored_items, catalog, pool_size=pool_size)


def build_user_candidates(
    user_id: str,
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    profiles: Dict[str, Dict[str, float]],
    dislikes: Dict[str, Set[str]],
    impressions: Dict[str, int] | None = None,
) -> List[str]:
    impressions = impressions or {}
    profile = profiles.get(user_id, {})
    excluded = dislikes.get(user_id, set())

    if not profile:
        return build_cold_start_candidates(
            catalog,
            popularity,
            user_id=user_id,
            impressions=impressions,
        )

    scored_items = []
    for item_id in catalog:
        if item_id in excluded:
            continue
        score = score_item(item_id, catalog, popularity, profile, impressions)
        scored_items.append((item_id, score))

    if not scored_items:
        return build_cold_start_candidates(
            catalog,
            popularity,
            user_id=user_id,
            impressions=impressions,
        )

    scored_items.sort(key=lambda item: item[1], reverse=True)
    scored_items = scored_items[:SCORING_MMR_INPUT_SIZE]
    return mmr_select(scored_items, catalog)


def build_global_candidates(
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    impressions: Dict[str, int] | None = None,
) -> List[str]:
    impressions = impressions or {}
    scored_items = [
        (
            item_id,
            -impression_penalty(item_id, impressions) - POPULARITY_BOOST * popularity.get(item_id, 0.0),
        )
        for item_id in catalog
    ]
    if not scored_items:
        return build_cold_start_candidates(catalog, popularity, impressions=impressions)

    scored_items.sort(key=lambda item: item[1], reverse=True)
    scored_items = scored_items[:200]
    return mmr_select(
        scored_items,
        catalog,
        pool_size=min(GLOBAL_CANDIDATE_STORE_SIZE, len(scored_items)),
    )


def select_recommendations(
    candidates: Iterable[str],
    shown: Set[str],
    disliked: Set[str],
    fallback: Iterable[str],
    top_k: int = TOP_K,
) -> List[str]:
    selected: List[str] = []
    seen = set(shown) | set(disliked)

    for item_id in candidates:
        if item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= top_k:
            return selected

    for item_id in fallback:
        if item_id in seen:
            continue
        selected.append(item_id)
        seen.add(item_id)
        if len(selected) >= top_k:
            return selected

    if len(selected) < top_k:
        for item_id in list(candidates) + list(fallback):
            if item_id in disliked or item_id in selected:
                continue
            selected.append(item_id)
            if len(selected) >= top_k:
                break

    return selected
