import json
import time

from catalog_store import save_items
from config import USER_CANDIDATES_PREFIX, USER_DISLIKED_PREFIX
from online_update import (
    get_co_liked_items,
    record_co_likes,
    rotate_list,
    update_user_from_interact,
)
from recommendations.service import (
    build_fast_global_candidates,
    build_recommendations,
    rotate_head_and_tail,
)
from watched_filter import WatchedFilter


def test_online_update_creates_user_candidates(redis_client):
    save_items(
        redis_client,
        [str(i) for i in range(1, 31)],
        [['Action'] if i <= 15 else ['Comedy'] for i in range(1, 31)],
    )

    started = time.time()
    meta = update_user_from_interact(
        redis_client,
        user_id='u-online',
        item_ids=['1'],
        actions=['like'],
    )
    elapsed_ms = (time.time() - started) * 1000

    assert meta['updated'] is True
    assert elapsed_ms < 120
    payload = redis_client.get(f'{USER_CANDIDATES_PREFIX}u-online')
    assert payload is not None
    candidates = json.loads(payload)
    assert len(candidates) >= 3
    assert '1' not in candidates


def test_online_update_tracks_dislikes(redis_client):
    save_items(redis_client, ['1', '2', '3'], [['Action'], ['Action'], ['Comedy']])
    update_user_from_interact(redis_client, 'u1', ['2'], ['dislike'])
    disliked = json.loads(redis_client.get(f'{USER_DISLIKED_PREFIX}u1'))
    assert '2' in disliked


def test_co_like_and_popular_bias(redis_client):
    save_items(
        redis_client,
        [str(i) for i in range(1, 21)],
        [['Action'] for _ in range(20)],
    )
    record_co_likes(redis_client, ['1', '7'])
    record_co_likes(redis_client, ['1', '7'])
    redis_client.zincrby('popular_likes', 20, '7')

    meta = update_user_from_interact(redis_client, 'u-co', ['1'], ['like'])
    assert meta['updated'] is True
    candidates = json.loads(redis_client.get(f'{USER_CANDIDATES_PREFIX}u-co'))
    assert '7' in candidates[:5]
    assert get_co_liked_items(redis_client, '1', 5)[0][0] == '7'


def test_unseen_preferred_in_head_after_shown(redis_client):
    save_items(
        redis_client,
        [str(i) for i in range(1, 41)],
        [['Action'] for _ in range(40)],
    )
    for item_id, score in [('5', 50), ('6', 40), ('7', 30), ('8', 20), ('9', 10)]:
        redis_client.zincrby('popular_likes', score, item_id)

    # Mark top hits as already shown during cold start.
    WatchedFilter(redis_client).add('u-shown', ['5', '6', '7'])
    meta = update_user_from_interact(redis_client, 'u-shown', ['1'], ['like'])
    assert meta['updated'] is True
    assert meta['head_unseen'] >= 3
    candidates = json.loads(redis_client.get(f'{USER_CANDIDATES_PREFIX}u-shown'))
    assert '5' not in candidates[:5]
    assert '6' not in candidates[:5]
    assert '7' not in candidates[:5]


def test_recs_keep_explore_slots(redis_client):
    save_items(
        redis_client,
        [str(i) for i in range(1, 101)],
        [['Drama'] for _ in range(100)],
    )
    for item_id in map(str, range(1, 20)):
        redis_client.zincrby('popular_likes', 30, item_id)
    update_user_from_interact(redis_client, 'u-exp', ['1'], ['like'])
    items, meta = build_recommendations(redis_client, WatchedFilter(redis_client), 'u-exp')
    assert len(items) == 10
    assert meta['explore_slots'] == 2


def test_global_candidates_prefer_popular(redis_client):
    save_items(redis_client, [str(i) for i in range(1, 101)], [['Drama'] for _ in range(100)])
    for item_id in ['3', '5', '8']:
        redis_client.zincrby('popular_likes', 50, item_id)
    candidates = build_fast_global_candidates(redis_client)
    assert '3' in candidates[:10]
    assert '5' in candidates[:10]


def test_cold_start_rotates_head(redis_client):
    items = [str(i) for i in range(20)]
    a = rotate_head_and_tail(items, 'user-a', head_size=5)
    b = rotate_head_and_tail(items, 'user-b', head_size=5)
    assert set(a[:5]) == set(items[:5])
    assert set(b[:5]) == set(items[:5])
    assert a[:5] != b[:5] or a[5:] != b[5:]
    assert rotate_list(['1', '2', '3'], 'x') != rotate_list(['1', '2', '3'], 'y')
