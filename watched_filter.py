import json
import os
from typing import List, Set

import redis

from config import USER_DISLIKED_PREFIX


class WatchedFilter:
    SHOWN_PREFIX = 'shown:'

    def __init__(self, redis_connection: redis.Redis | None = None):
        self.redis_connection = redis_connection or redis.Redis(
            os.environ.get('REDIS_HOST', 'localhost'),
            port=int(os.environ.get('REDIS_PORT', 6379)),
        )

    def _key(self, user_id: str) -> str:
        return f'{self.SHOWN_PREFIX}{user_id}'

    def add(self, user_id: str, item_ids: List[str] | str) -> None:
        if isinstance(item_ids, str):
            item_ids = [item_ids]
        if not item_ids:
            return
        try:
            self.redis_connection.sadd(self._key(user_id), *item_ids)
        except redis.exceptions.ConnectionError:
            pass

    def _decode_item_id(self, item_id) -> str:
        if isinstance(item_id, bytes):
            return item_id.decode()
        return str(item_id)

    def get_shown(self, user_id: str) -> Set[str]:
        try:
            return {
                self._decode_item_id(item_id)
                for item_id in self.redis_connection.smembers(self._key(user_id))
            }
        except redis.exceptions.ConnectionError:
            return set()

    def get_disliked(self, user_id: str) -> Set[str]:
        try:
            payload = self.redis_connection.get(f'{USER_DISLIKED_PREFIX}{user_id}')
            if not payload:
                return set()
            return set(json.loads(payload))
        except (redis.exceptions.ConnectionError, TypeError, json.JSONDecodeError):
            return set()
