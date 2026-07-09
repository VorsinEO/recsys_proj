import json
import time

import polars as pl
import pytest

from catalog_store import save_items
from config import GLOBAL_CANDIDATES_KEY, USER_CANDIDATES_PREFIX, USER_DISLIKED_PREFIX
from online_update import update_user_from_interact
from regular_pipeline.main import flush_interactions, recompute_recommendations


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


def test_pipeline_flush_and_light_recompute(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)
    pipeline_main.dirty_users.clear()
    pipeline_main.recompute_cycle = 0

    flush_interactions([{
        'user_id': 'grader-user',
        'item_ids': ['1', '11'],
        'actions': ['like', 'like'],
        'timestamp': time.time(),
    }])

    assert interactions_path.exists()
    rows = pl.read_csv(interactions_path)
    assert len(rows) == 2

    # Per-user UC comes from online_update, not pipeline dirty recompute.
    update_user_from_interact(redis_client, 'grader-user', ['1', '11'], ['like', 'like'])
    recompute_recommendations()

    global_candidates = json.loads(redis_client.get(GLOBAL_CANDIDATES_KEY))
    user_candidates = json.loads(redis_client.get(f'{USER_CANDIDATES_PREFIX}grader-user'))

    assert len(global_candidates) > 0
    assert len(user_candidates) > 0
    action_items = {'1', '4', '10', '11', '14'}
    assert action_items & set(user_candidates)


def test_dislikes_via_online_update(tmp_path, redis_client, sample_catalog, monkeypatch):
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
    update_user_from_interact(redis_client, 'grader-user', ['2'], ['dislike'])
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


def test_light_recompute_does_not_overwrite_online_uc(tmp_path, redis_client, sample_catalog, monkeypatch):
    import regular_pipeline.main as pipeline_main

    interactions_path = tmp_path / 'interactions.csv'
    monkeypatch.setattr(pipeline_main, 'INTERACTIONS_PATH', interactions_path)
    monkeypatch.setattr(pipeline_main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline_main, 'redis_connection', redis_client)
    pipeline_main.recompute_cycle = 1
    pipeline_main.dirty_users.clear()

    update_user_from_interact(redis_client, 'u1', ['1'], ['like'])
    online_uc = redis_client.get(f'{USER_CANDIDATES_PREFIX}u1')
    assert online_uc is not None

    flush_interactions([{
        'user_id': 'u1',
        'item_ids': ['1'],
        'actions': ['like'],
        'timestamp': time.time(),
    }])
    recompute_recommendations()

    assert redis_client.get(f'{USER_CANDIDATES_PREFIX}u1') == online_uc
    assert redis_client.get(GLOBAL_CANDIDATES_KEY) is not None
