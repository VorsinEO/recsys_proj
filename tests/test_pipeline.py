import json
import time

import polars as pl
import pytest

from catalog_store import save_items
from config import GLOBAL_CANDIDATES_KEY, USER_CANDIDATES_PREFIX, USER_DISLIKED_PREFIX
from regular_pipeline.main import flush_interactions, recompute_recommendations
from scoring import normalize_interactions


@pytest.fixture
def sample_catalog(redis_client):
    item_ids = [str(i) for i in range(1, 16)]
    genres = [
        ['Action'],
        ['Comedy'],
        ['Drama'],
        ['Romance'],
        ['Sci-Fi'],
        ['Horror'],
        ['Thriller'],
        ['Documentary'],
        ['Animation'],
        ['Fantasy'],
        ['Action', 'Sci-Fi'],
        ['Comedy', 'Romance'],
        ['Drama', 'Romance'],
        ['Action', 'Thriller'],
        ['Horror', 'Thriller'],
    ]
    save_items(redis_client, item_ids, genres)
    return item_ids


def test_pipeline_flush_and_recompute(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)

    flush_interactions([{
        'user_id': 'grader-user',
        'item_ids': ['1', '11'],
        'actions': ['like', 'like'],
        'timestamp': time.time(),
    }])

    assert interactions_path.exists()
    rows = pl.read_csv(interactions_path)
    assert len(rows) == 2

    recompute_recommendations()

    global_candidates = json.loads(redis_client.get(GLOBAL_CANDIDATES_KEY))
    user_candidates = json.loads(redis_client.get(f'{USER_CANDIDATES_PREFIX}grader-user'))

    assert len(global_candidates) > 0
    assert len(user_candidates) > 0
    action_items = {'1', '4', '10', '11', '14'}
    assert action_items & set(user_candidates)


def test_dislikes_excluded_from_user_state(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)

    flush_interactions([{
        'user_id': 'grader-user',
        'item_ids': ['2'],
        'actions': ['dislike'],
        'timestamp': time.time(),
    }])

    recompute_recommendations()

    disliked = json.loads(redis_client.get(f'{USER_DISLIKED_PREFIX}grader-user'))
    assert '2' in disliked


def test_flush_interactions_appends_rows(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)
    pipeline_main.dirty_users.clear()

    flush_interactions([{
        'user_id': 'u1',
        'item_ids': ['1'],
        'actions': ['like'],
        'timestamp': time.time(),
    }])
    flush_interactions([{
        'user_id': 'u2',
        'item_ids': ['2'],
        'actions': ['dislike'],
        'timestamp': time.time(),
    }])

    rows = pl.read_csv(interactions_path)
    assert len(rows) == 2
    assert set(rows['user_id'].to_list()) == {'u1', 'u2'}


def test_incremental_recompute_updates_only_dirty_users(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)
    pipeline_main.recompute_cycle = 1
    pipeline_main.dirty_users.clear()

    flush_interactions([{
        'user_id': 'u1',
        'item_ids': ['1'],
        'actions': ['like'],
        'timestamp': time.time(),
    }])
    recompute_recommendations()

    first_u1 = redis_client.get(f'{USER_CANDIDATES_PREFIX}u1')
    assert first_u1 is not None
    assert redis_client.get(f'{USER_CANDIDATES_PREFIX}u2') is None

    flush_interactions([{
        'user_id': 'u2',
        'item_ids': ['2'],
        'actions': ['dislike'],
        'timestamp': time.time(),
    }])
    recompute_recommendations()

    second_u1 = redis_client.get(f'{USER_CANDIDATES_PREFIX}u1')
    first_u2 = redis_client.get(f'{USER_CANDIDATES_PREFIX}u2')
    assert first_u2 is not None
    assert second_u1 == first_u1
