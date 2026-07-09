import redis

from config import IMPRESSIONS_HASH_KEY


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def get_impression_counts(redis_connection: redis.Redis) -> dict[str, int]:
    try:
        raw = redis_connection.hgetall(IMPRESSIONS_HASH_KEY)
    except redis.exceptions.ConnectionError:
        return {}
    return {_decode(item_id): int(count) for item_id, count in raw.items()}


def increment_impressions(redis_connection: redis.Redis, item_ids: list[str]) -> None:
    if not item_ids:
        return
    try:
        pipe = redis_connection.pipeline()
        for item_id in item_ids:
            pipe.hincrby(IMPRESSIONS_HASH_KEY, str(item_id), 1)
        pipe.execute()
    except redis.exceptions.ConnectionError:
        pass


def impression_penalty(item_id: str, impressions: dict[str, int]) -> float:
    count = impressions.get(item_id, 0)
    if count <= 0:
        return 0.0
    return 0.15 * (count ** 0.5)
