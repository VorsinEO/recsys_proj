import asyncio
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import aio_pika
import polars as pl
import redis

from catalog_store import (
    ensure_genre_index,
    get_catalog_sample,
    get_full_catalog_genres,
    get_items_genres,
    select_user_scoring_catalog,
)
from config import (
    CATALOG_CACHE_TTL_SEC,
    FLUSH_BATCH_SIZE,
    FLUSH_INTERVAL_SEC,
    GLOBAL_CANDIDATES_KEY,
    GLOBAL_REBUILD_EVERY_N_CYCLES,
    INTERACTIONS_PATH,
    RECOMPUTE_INTERVAL_SEC,
    USER_CANDIDATES_PREFIX,
    USER_DISLIKED_PREFIX,
    DATA_DIR,
)
from impression_store import get_impression_counts
from online_update import rebuild_co_likes_from_pairs, record_co_likes
from recommendations.service import refresh_global_candidates
from scoring import (
    build_user_candidates,
    build_user_dislikes,
    build_user_profiles,
    build_popularity,
    normalize_interactions,
)

redis_connection = redis.Redis('localhost')
message_buffer: list[dict] = []
buffer_lock = asyncio.Lock()
recompute_cycle = 0
dirty_users: set[str] = set()
MAX_DIRTY_PER_CYCLE = 40

_catalog_cache: dict | None = None
_genre_index_cache: dict | None = None
_catalog_cache_ts = 0.0


def _empty_interactions() -> pl.DataFrame:
    return normalize_interactions(pl.DataFrame({
        'user_id': [],
        'item_id': [],
        'action': [],
        'timestamp': [],
    }))


def _read_interactions() -> pl.DataFrame:
    if not INTERACTIONS_PATH.exists():
        return _empty_interactions()
    return normalize_interactions(pl.read_csv(INTERACTIONS_PATH))


def _get_cached_catalog_and_index(force: bool = False):
    global _catalog_cache, _genre_index_cache, _catalog_cache_ts

    now = time.time()
    if (
        not force
        and _catalog_cache is not None
        and _genre_index_cache is not None
        and now - _catalog_cache_ts < CATALOG_CACHE_TTL_SEC
    ):
        return _catalog_cache, _genre_index_cache

    full_catalog = get_full_catalog_genres(redis_connection)
    genre_index = ensure_genre_index(redis_connection, full_catalog)
    _catalog_cache = full_catalog
    _genre_index_cache = genre_index
    _catalog_cache_ts = now
    return full_catalog, genre_index


def flush_interactions(buffer: list[dict]) -> None:
    if not buffer:
        return

    new_data = (
        pl.DataFrame(buffer)
        .explode(['item_ids', 'actions'])
        .rename({
            'item_ids': 'item_id',
            'actions': 'action',
        })
    )
    new_data = normalize_interactions(new_data)

    if len(new_data) == 0:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dirty_users.update(str(user_id) for user_id in new_data['user_id'].unique().to_list())

    # Maintain co-like graph from multi-like events in this flush batch.
    for message in buffer:
        liked = [
            str(item_id)
            for item_id, action in zip(message.get('item_ids', []), message.get('actions', []))
            if action == 'like'
        ]
        if len(liked) >= 2:
            record_co_likes(redis_connection, liked)

    write_header = not INTERACTIONS_PATH.exists()
    with INTERACTIONS_PATH.open('a', encoding='utf-8') as interactions_file:
        interactions_file.write(new_data.write_csv(has_header=write_header))


async def append_message(message: dict) -> bool:
    async with buffer_lock:
        message_buffer.append(message)
        return len(message_buffer) >= FLUSH_BATCH_SIZE


async def drain_buffer() -> list[dict]:
    async with buffer_lock:
        if not message_buffer:
            return []
        buffered = list(message_buffer)
        message_buffer.clear()
        return buffered


async def collect_messages():
    connection = await aio_pika.connect_robust(
        "amqp://guest:guest@127.0.0.1/",
        loop=asyncio.get_event_loop()
    )

    queue_name = "user_interactions"
    routing_key = "user.interact.message"

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=200)
        queue = await channel.declare_queue(queue_name)
        exchange = await channel.declare_exchange("user.interact", type='direct')
        await queue.bind(exchange, routing_key)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    payload = json.loads(message.body.decode())
                    should_flush = await append_message(payload)
                    if should_flush:
                        buffered = await drain_buffer()
                        if buffered:
                            print(f'saving events from rabbitmq: {len(buffered)}')
                            flush_interactions(buffered)


async def collect_messages_loop():
    while True:
        try:
            await collect_messages()
        except Exception as exc:
            print(f'pipeline consumer error: {exc!r}, reconnecting in 3s')
            await asyncio.sleep(3)


async def flush_messages_loop():
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SEC)
            buffered = await drain_buffer()
            if buffered:
                print(f'saving events from rabbitmq: {len(buffered)}')
                flush_interactions(buffered)
        except Exception as exc:
            print(f'pipeline flush error: {exc!r}')


def recompute_recommendations() -> None:
    global recompute_cycle
    global dirty_users

    recompute_cycle += 1
    full_rebuild = recompute_cycle % GLOBAL_REBUILD_EVERY_N_CYCLES == 1

    if not dirty_users and not full_rebuild:
        print('calculated recommendations for 0 users (full_rebuild=False, dirty_left=0)')
        return

    started = time.time()
    interactions = _read_interactions()
    popularity = build_popularity(interactions)
    impressions = get_impression_counts(redis_connection)

    # Idle / periodic: refresh hit-heavy global pool + co-like graph.
    if full_rebuild and not dirty_users:
        refresh_global_candidates(redis_connection)
        if len(interactions) > 0:
            likes = interactions.filter(pl.col('action') == 'like')
            user_liked: dict[str, set[str]] = {}
            for row in likes.iter_rows(named=True):
                user_liked.setdefault(str(row['user_id']), set()).add(str(row['item_id']))
            rebuild_co_likes_from_pairs(redis_connection, user_liked)
        elapsed = time.time() - started
        print(
            f'calculated recommendations for 0 users '
            f'(full_rebuild=True, dirty_left=0, {elapsed:.2f}s)'
        )
        return

    target_user_ids = set(list(dirty_users)[:MAX_DIRTY_PER_CYCLE])
    if not target_user_ids:
        print('calculated recommendations for 0 users (full_rebuild=False, dirty_left=0)')
        return

    full_catalog, genre_index = _get_cached_catalog_and_index(force=False)
    if not full_catalog:
        return

    cold_start_catalog = get_catalog_sample(redis_connection)
    interacted_item_ids = (
        interactions['item_id'].unique().to_list()
        if len(interactions) > 0
        else []
    )
    profile_catalog = get_items_genres(redis_connection, interacted_item_ids)
    for item_id, genres in profile_catalog.items():
        full_catalog.setdefault(item_id, genres)

    profiles = build_user_profiles(interactions, profile_catalog)
    dislikes = build_user_dislikes(interactions)

    if full_rebuild:
        refresh_global_candidates(redis_connection)

    user_interacted: dict[str, list[str]] = {}
    if len(interactions) > 0:
        for row in interactions.iter_rows(named=True):
            user_id = str(row['user_id'])
            user_interacted.setdefault(user_id, []).append(str(row['item_id']))

    pipe = redis_connection.pipeline(transaction=False)
    for user_id in target_user_ids:
        profile = profiles.get(user_id, {})
        catalog = select_user_scoring_catalog(
            full_catalog=full_catalog,
            genre_index=genre_index,
            profile=profile,
            interacted_item_ids=user_interacted.get(user_id, []),
            cold_start_catalog=cold_start_catalog,
        )
        candidates = build_user_candidates(
            user_id=user_id,
            catalog=catalog,
            popularity=popularity,
            profiles=profiles,
            dislikes=dislikes,
            impressions=impressions,
        )
        pipe.set(f'{USER_CANDIDATES_PREFIX}{user_id}', json.dumps(candidates))
        pipe.set(
            f'{USER_DISLIKED_PREFIX}{user_id}',
            json.dumps(sorted(dislikes.get(user_id, set()))),
        )
    pipe.execute()

    dirty_users.difference_update(target_user_ids)
    elapsed = time.time() - started
    print(
        f'calculated recommendations for {len(target_user_ids)} users '
        f'(full_rebuild={full_rebuild}, dirty_left={len(dirty_users)}, '
        f'{elapsed:.2f}s)'
    )


async def calculate_recommendations_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            print('calculating recommendations')
            # Keep RabbitMQ consumer responsive while scoring runs.
            await loop.run_in_executor(None, recompute_recommendations)
        except Exception as exc:
            print(f'pipeline recompute error: {exc!r}')
        await asyncio.sleep(RECOMPUTE_INTERVAL_SEC)


async def main():
    await asyncio.gather(
        collect_messages_loop(),
        flush_messages_loop(),
        calculate_recommendations_loop(),
    )


if __name__ == '__main__':
    asyncio.run(main())
