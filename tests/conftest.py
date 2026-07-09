import json
from pathlib import Path

import pytest
import redis

from catalog_store import get_all_item_ids, get_catalog, save_items
from config import CATALOG_ALL_KEY, GLOBAL_CANDIDATES_KEY, USER_CANDIDATES_PREFIX
from recommendations.service import add_catalog_items, build_recommendations
from watched_filter import WatchedFilter


@pytest.fixture
def redis_client():
    client = redis.Redis('localhost')
    try:
        client.ping()
    except redis.exceptions.ConnectionError as exc:
        pytest.skip(f'Redis unavailable: {exc}')
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def watched_filter(redis_client):
    return WatchedFilter(redis_client)
