import json
from pathlib import Path

import polars as pl

from impression_store import get_impression_counts, increment_impressions, impression_penalty
from scoring import (
    build_cold_start_candidates,
    build_popularity,
    build_user_candidates,
    build_user_dislikes,
    build_user_profiles,
    genre_jaccard,
    select_recommendations,
)


CATALOG = {
    '1': ['Action', 'Sci-Fi'],
    '2': ['Comedy'],
    '3': ['Drama', 'Romance'],
    '4': ['Action'],
    '5': ['Horror'],
    '6': ['Comedy', 'Romance'],
    '7': ['Documentary'],
    '8': ['Sci-Fi'],
    '9': ['Drama'],
    '10': ['Action', 'Comedy'],
    '11': ['Romance'],
    '12': ['Horror', 'Thriller'],
}


def make_interactions(rows):
    return pl.DataFrame(rows).with_columns([
        pl.col('user_id').cast(pl.Utf8),
        pl.col('item_id').cast(pl.Utf8),
        pl.col('action').cast(pl.Utf8),
        pl.col('timestamp').cast(pl.Float64),
    ])


def test_genre_jaccard():
    assert genre_jaccard(['Action', 'Sci-Fi'], ['Action']) == 0.5
    assert genre_jaccard([], []) == 0.0


def test_impression_penalty_increases_with_count():
    assert impression_penalty('1', {'1': 0}) == 0.0
    assert impression_penalty('1', {'1': 100}) > impression_penalty('1', {'1': 10})


def test_build_user_profiles_and_dislikes():
    interactions = make_interactions([
        {'user_id': 'u1', 'item_id': '1', 'action': 'like', 'timestamp': 1.0},
        {'user_id': 'u1', 'item_id': '2', 'action': 'dislike', 'timestamp': 2.0},
    ])
    profiles = build_user_profiles(interactions, CATALOG)
    dislikes = build_user_dislikes(interactions)

    assert profiles['u1']['Action'] > 0
    assert profiles['u1']['Comedy'] < 0
    assert dislikes['u1'] == {'2'}


def test_build_user_candidates_prefers_matching_genres():
    interactions = make_interactions([
        {'user_id': 'u1', 'item_id': '1', 'action': 'like', 'timestamp': 1.0},
    ])
    popularity = build_popularity(interactions)
    profiles = build_user_profiles(interactions, CATALOG)
    dislikes = build_user_dislikes(interactions)

    candidates = build_user_candidates('u1', CATALOG, popularity, profiles, dislikes)

    assert '4' in candidates or '10' in candidates or '8' in candidates


def test_cold_start_differs_per_user():
    big_catalog = {str(i): [f'Genre{i % 5}'] for i in range(100)}
    user_a = build_cold_start_candidates(big_catalog, {}, user_id='user-a')
    user_b = build_cold_start_candidates(big_catalog, {}, user_id='user-b')
    assert user_a[:10] != user_b[:10]


def test_cold_start_penalizes_impressions():
    catalog = {str(i): ['Action'] for i in range(20)}
    impressions = {'0': 100, '1': 0}
    candidates = build_cold_start_candidates(catalog, {}, user_id='u1', impressions=impressions)
    assert candidates[0] != '0'


def test_select_recommendations_filters_shown_and_disliked():
    selected = select_recommendations(
        candidates=['1', '2', '3', '4', '5'],
        shown={'1', '2'},
        disliked={'3'},
        fallback=['6', '7', '8', '9', '10', '11', '12'],
        top_k=5,
    )
    assert selected == ['4', '5', '6', '7', '8']
    assert '1' not in selected
    assert '3' not in selected


def test_select_recommendations_backfills_when_catalog_exhausted():
    selected = select_recommendations(
        candidates=['1', '2', '3'],
        shown={'1', '2', '3', '4', '5', '6', '7', '8', '9', '10'},
        disliked={'11'},
        fallback=['1', '2', '3', '4', '5', '12', '13', '14', '15', '16'],
        top_k=10,
    )
    assert len(selected) == 10
    assert '11' not in selected


def test_impression_store_roundtrip(redis_client):
    increment_impressions(redis_client, ['1', '1', '2'])
    counts = get_impression_counts(redis_client)
    assert counts['1'] == 2
    assert counts['2'] == 1


def test_archive_interactions_csv(tmp_path):
    from state_cleanup import archive_interactions_csv

    interactions_path = tmp_path / 'interactions.csv'
    interactions_path.write_text('user_id,item_id,action,timestamp\nu,1,like,1.0\n')

    archive_path = archive_interactions_csv(interactions_path, tmp_path)

    assert archive_path is not None
    assert archive_path.exists()
    assert not interactions_path.exists()
    assert archive_path.name.startswith('interactions_')
