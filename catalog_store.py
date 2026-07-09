import json
from typing import Dict, List

import redis

from config import CATALOG_ALL_KEY, CATALOG_ITEM_PREFIX


def save_items(redis_connection: redis.Redis, item_ids: List[str], genres: List[List[str]]) -> None:
    for item_id, item_genres in zip(item_ids, genres):
        item_id = str(item_id)
        redis_connection.sadd(CATALOG_ALL_KEY, item_id)
        redis_connection.set(f'{CATALOG_ITEM_PREFIX}{item_id}', json.dumps(item_genres))


def get_all_item_ids(redis_connection: redis.Redis) -> List[str]:
    item_ids = []
    for item_id in redis_connection.smembers(CATALOG_ALL_KEY):
        if isinstance(item_id, bytes):
            item_ids.append(item_id.decode())
        else:
            item_ids.append(str(item_id))
    return item_ids


def get_catalog(redis_connection: redis.Redis) -> Dict[str, List[str]]:
    catalog = {}
    for item_id in get_all_item_ids(redis_connection):
        payload = redis_connection.get(f'{CATALOG_ITEM_PREFIX}{item_id}')
        if payload:
            catalog[item_id] = json.loads(payload)
        else:
            catalog[item_id] = []
    return catalog
