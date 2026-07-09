import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import INTERACTIONS_PATH, TOP_K
from recommendations.main import app


@pytest.fixture
def client(redis_client):
    with TestClient(app) as test_client:
        yield test_client


SAMPLE_ITEMS = {
    'item_ids': [str(i) for i in range(1, 21)],
    'genres': [
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
        ['Comedy'],
        ['Drama'],
        ['Sci-Fi'],
        ['Romance'],
        ['Fantasy', 'Action'],
    ],
}


def test_healthcheck(client):
    response = client.get('/healthcheck')
    assert response.status_code == 200
    assert response.json() is True


def test_add_items_requires_matching_genres(client):
    response = client.post('/add_items', json={
        'item_ids': ['1', '2'],
        'genres': [['Action']],
    })
    assert response.status_code == 422


def test_add_items_and_recs_return_top_k(client):
    response = client.post('/add_items', json=SAMPLE_ITEMS)
    assert response.status_code == 200

    response = client.get('/recs/grader-user-1')
    assert response.status_code == 200
    item_ids = response.json()['item_ids']
    assert len(item_ids) == TOP_K
    assert len(set(item_ids)) == TOP_K


def test_recs_do_not_repeat_shown_items(client):
    client.post('/add_items', json=SAMPLE_ITEMS)

    first = client.get('/recs/grader-user-2').json()['item_ids']
    second = client.get('/recs/grader-user-2').json()['item_ids']

    assert len(first) == TOP_K
    assert len(second) == TOP_K
    assert not set(first) & set(second)


def test_cleanup_resets_redis(client, redis_client):
    client.post('/add_items', json=SAMPLE_ITEMS)
    assert redis_client.dbsize() > 0

    response = client.get('/cleanup')
    assert response.status_code == 200
    assert redis_client.dbsize() == 0


def test_cleanup_archives_csv_file(tmp_path):
    from state_cleanup import archive_interactions_csv

    interactions_path = tmp_path / 'interactions.csv'
    interactions_path.write_text('user_id,item_id,action,timestamp\nu,1,like,1.0\n')

    archive_path = archive_interactions_csv(interactions_path, tmp_path)
    assert archive_path is not None
    assert not interactions_path.exists()
    assert list(tmp_path.glob('interactions_*.csv'))
