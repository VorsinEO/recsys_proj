import json
import random
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Set

import polars as pl

from config import CANDIDATE_POOL_SIZE, EXPLORATION_RATE, MMR_LAMBDA, TOP_K


def normalize_interactions(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.col('user_id').cast(pl.Utf8),
        pl.col('item_id').cast(pl.Utf8),
        pl.col('action').cast(pl.Utf8),
        pl.col('timestamp').cast(pl.Float64),
    ])


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
        weight = 1.0 if action == 'like' else -1.5
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
) -> float:
    genres = catalog.get(item_id, [])
    relevance = sum(profile.get(genre, 0.0) for genre in genres)
    if relevance == 0 and genres:
        relevance = 0.01
    popularity_boost = 0.15 * popularity.get(item_id, 0.0)
    return relevance + popularity_boost


def mmr_select(
    scored_items: List[tuple[str, float]],
    catalog: Dict[str, List[str]],
    pool_size: int = CANDIDATE_POOL_SIZE,
    lambda_param: float = MMR_LAMBDA,
) -> List[str]:
    remaining = sorted(scored_items, key=lambda item: item[1], reverse=True)
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


def build_cold_start_candidates(
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    pool_size: int = CANDIDATE_POOL_SIZE,
) -> List[str]:
    if not catalog:
        return []

    by_genre: Dict[str, List[tuple[str, float]]] = defaultdict(list)
    for item_id, genres in catalog.items():
        score = popularity.get(item_id, random.random() * 0.1)
        target_genres = genres or ['Unknown']
        for genre in target_genres:
            by_genre[genre].append((item_id, score))

    for genre in by_genre:
        by_genre[genre].sort(key=lambda item: item[1], reverse=True)

    selected: List[str] = []
    seen: Set[str] = set()
    genres = list(by_genre.keys())
    random.shuffle(genres)

    while len(selected) < pool_size:
        progressed = False
        for genre in genres:
            bucket = by_genre[genre]
            while bucket and bucket[0][0] in seen:
                bucket.pop(0)
            if bucket:
                item_id = bucket.pop(0)[0]
                selected.append(item_id)
                seen.add(item_id)
                progressed = True
                if len(selected) >= pool_size:
                    break
        if not progressed:
            break

    if len(selected) < pool_size:
        for item_id in catalog:
            if item_id not in seen:
                selected.append(item_id)
                seen.add(item_id)
            if len(selected) >= pool_size:
                break

    return selected[:pool_size]


def build_user_candidates(
    user_id: str,
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
    profiles: Dict[str, Dict[str, float]],
    dislikes: Dict[str, Set[str]],
) -> List[str]:
    profile = profiles.get(user_id, {})
    excluded = dislikes.get(user_id, set())
    scored_items = []

    for item_id in catalog:
        if item_id in excluded:
            continue
        score = score_item(item_id, catalog, popularity, profile)
        if random.random() < EXPLORATION_RATE:
            score += random.random() * 0.05
        scored_items.append((item_id, score))

    if not scored_items:
        return build_cold_start_candidates(catalog, popularity)

    if not profile:
        return build_cold_start_candidates(catalog, popularity)

    return mmr_select(scored_items, catalog)


def build_global_candidates(
    catalog: Dict[str, List[str]],
    popularity: Dict[str, float],
) -> List[str]:
    scored_items = [
        (item_id, popularity.get(item_id, random.random() * 0.1))
        for item_id in catalog
    ]
    if not scored_items:
        return build_cold_start_candidates(catalog, popularity)
    return mmr_select(scored_items, catalog)


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
