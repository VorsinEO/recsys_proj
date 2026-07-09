import random

from catalog_store import (
    build_inverted_index,
    ensure_genre_index,
    get_catalog_for_scoring,
    get_user_scoring_pool,
    load_genre_index,
    save_items,
    select_user_scoring_catalog,
)


def test_get_catalog_for_scoring_includes_required_items(redis_client):
    item_ids = [str(i) for i in range(100)]
    genres = [[f'Genre{i % 7}'] for i in range(100)]
    save_items(redis_client, item_ids, genres)

    catalog = get_catalog_for_scoring(redis_client, ['99'], sample_size=10)

    assert '99' in catalog
    assert catalog['99'] == ['Genre1']


def test_save_items_builds_genre_index(redis_client):
    save_items(
        redis_client,
        ['1', '2', '3'],
        [['Action'], ['Action', 'Sci-Fi'], ['Comedy']],
    )

    index = load_genre_index(redis_client)

    assert index['Action'] == {'1', '2'}
    assert index['Sci-Fi'] == {'2'}
    assert index['Comedy'] == {'3'}


def test_get_user_scoring_pool_uses_top_genres(redis_client):
    item_ids = [str(i) for i in range(30)]
    genres = []
    for i in range(30):
        if i < 10:
            genres.append(['Action'])
        elif i < 20:
            genres.append(['Comedy'])
        else:
            genres.append(['Drama'])
    save_items(redis_client, item_ids, genres)
    full_catalog = {item_id: genre for item_id, genre in zip(item_ids, genres)}
    genre_index = build_inverted_index(full_catalog)

    pool = get_user_scoring_pool(
        profile={'Action': 4.0, 'Comedy': 1.0},
        genre_index=genre_index,
        interacted_item_ids=['5'],
        all_item_ids=item_ids,
    )

    assert '5' in pool
    assert len(pool & set(map(str, range(10)))) >= 8
    assert len(pool) >= 15


def test_select_user_scoring_catalog_prefers_genre_neighbors(redis_client):
    item_ids = [str(i) for i in range(30)]
    genres = [['Action'] if i < 10 else ['Comedy'] for i in range(30)]
    save_items(redis_client, item_ids, genres)
    full_catalog = {item_id: genre for item_id, genre in zip(item_ids, genres)}
    genre_index = ensure_genre_index(redis_client, full_catalog)
    cold_start = {item_id: full_catalog[item_id] for item_id in map(str, range(20, 30))}

    catalog = select_user_scoring_catalog(
        full_catalog=full_catalog,
        genre_index=genre_index,
        profile={'Action': 3.0},
        interacted_item_ids=['1'],
        cold_start_catalog=cold_start,
    )

    assert '1' in catalog
    assert len(catalog) >= 10
    assert sum(1 for genres in catalog.values() if 'Action' in genres) >= 8


def test_get_user_scoring_pool_backfills_small_genre(redis_client):
    save_items(redis_client, ['1'], [['Obscure']])
    genre_index = {'Obscure': {'1'}}
    all_ids = [str(i) for i in range(1000)]

    random.seed(0)
    pool = get_user_scoring_pool(
        profile={'Obscure': 1.0},
        genre_index=genre_index,
        interacted_item_ids=['1'],
        all_item_ids=all_ids,
    )

    assert len(pool) >= 200


def test_get_user_scoring_pool_caps_large_genres():
    genre_index = {
        'Drama': {str(i) for i in range(12000)},
        'Comedy': {str(i) for i in range(12000, 20000)},
    }
    random.seed(1)
    pool = get_user_scoring_pool(
        profile={'Drama': 4.0, 'Comedy': 2.0},
        genre_index=genre_index,
        interacted_item_ids=['1'],
        all_item_ids=[str(i) for i in range(20000)],
    )
    assert '1' in pool
    assert len(pool) <= 400
