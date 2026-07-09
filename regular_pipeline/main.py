import asyncio
import json
import sys
from pathlib import Path
import time

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import aio_pika
import polars as pl
import redis

from catalog_store import get_catalog
from config import (
    FLUSH_INTERVAL_SEC,
    GLOBAL_CANDIDATES_KEY,
    INTERACTIONS_PATH,
    RECOMPUTE_INTERVAL_SEC,
    USER_CANDIDATES_PREFIX,
    USER_DISLIKED_PREFIX,
    DATA_DIR,
)
from scoring import (
    build_global_candidates,
    build_user_candidates,
    build_user_dislikes,
    build_user_profiles,
    build_popularity,
    normalize_interactions,
)

redis_connection = redis.Redis('localhost')


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
    if INTERACTIONS_PATH.exists():
        existing_data = normalize_interactions(pl.read_csv(INTERACTIONS_PATH))
        all_data = pl.concat([existing_data, new_data], how='vertical')
    else:
        all_data = new_data
    all_data.write_csv(INTERACTIONS_PATH)


async def collect_messages():
    connection = await aio_pika.connect_robust(
        "amqp://guest:guest@127.0.0.1/",
        loop=asyncio.get_event_loop()
    )

    queue_name = "user_interactions"
    routing_key = "user.interact.message"

    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=10)
        queue = await channel.declare_queue(queue_name)
        exchange = await channel.declare_exchange("user.interact", type='direct')
        await queue.bind(exchange, routing_key)

        t_start = time.time()
        data = []
        while True:
            message = await queue.get(timeout=1, fail=False)
            if message is not None:
                async with message.process():
                    message = json.loads(message.body.decode())
                    data.append(message)

            if data and time.time() - t_start > FLUSH_INTERVAL_SEC:
                print('saving events from rabbitmq')
                flush_interactions(data)
                data = []
                t_start = time.time()


def recompute_recommendations() -> None:
    catalog = get_catalog(redis_connection)
    if not catalog:
        return

    if INTERACTIONS_PATH.exists():
        interactions = normalize_interactions(pl.read_csv(INTERACTIONS_PATH))
    else:
        interactions = normalize_interactions(pl.DataFrame({
            'user_id': [],
            'item_id': [],
            'action': [],
            'timestamp': [],
        }))

    popularity = build_popularity(interactions)
    profiles = build_user_profiles(interactions, catalog)
    dislikes = build_user_dislikes(interactions)

    global_candidates = build_global_candidates(catalog, popularity)
    redis_connection.set(GLOBAL_CANDIDATES_KEY, json.dumps(global_candidates))

    user_ids = set(profiles.keys()) | set(dislikes.keys())
    if len(interactions) > 0:
        user_ids.update(str(user_id) for user_id in interactions['user_id'].to_list())

    for user_id in user_ids:
        candidates = build_user_candidates(
            user_id=user_id,
            catalog=catalog,
            popularity=popularity,
            profiles=profiles,
            dislikes=dislikes,
        )
        redis_connection.set(f'{USER_CANDIDATES_PREFIX}{user_id}', json.dumps(candidates))
        redis_connection.set(
            f'{USER_DISLIKED_PREFIX}{user_id}',
            json.dumps(sorted(dislikes.get(user_id, set()))),
        )

    print(f'calculated recommendations for {len(user_ids)} users')


async def calculate_recommendations_loop():
    while True:
        print('calculating recommendations')
        recompute_recommendations()
        await asyncio.sleep(RECOMPUTE_INTERVAL_SEC)


async def main():
    await asyncio.gather(
        collect_messages(),
        calculate_recommendations_loop(),
    )


if __name__ == '__main__':
    asyncio.run(main())
