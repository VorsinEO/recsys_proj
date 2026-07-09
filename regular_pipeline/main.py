import asyncio
import json
from pathlib import Path
import time

import aio_pika
import polars as pl
import redis

redis_connection = redis.Redis('localhost')
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
INTERACTIONS_PATH = DATA_DIR / 'interactions.csv'


def normalize_interactions(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.col('user_id').cast(pl.Utf8),
        pl.col('item_id').cast(pl.Utf8),
        pl.col('action').cast(pl.Utf8),
        pl.col('timestamp').cast(pl.Float64),
    ])


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
        # Creating channel
        channel = await connection.channel()

        # Will take no more than 10 messages in advance
        await channel.set_qos(prefetch_count=10)

        # Declaring queue
        queue = await channel.declare_queue(queue_name)

        # Declaring exchange
        exchange = await channel.declare_exchange("user.interact", type='direct')
        await queue.bind(exchange, routing_key)

        t_start = time.time()
        data = []
        while True:
            message = await queue.get(timeout=1, fail=False)
            if message is not None:
                async with message.process():
                    message = message.body.decode()
                    message = json.loads(message)
                    data.append(message)

            if data and time.time() - t_start > 10:
                print('saving events from rabbitmq')
                flush_interactions(data)
                data = []
                t_start = time.time()


async def calculate_top_recommendations():
    while True:
        if INTERACTIONS_PATH.exists():
            print('calculating top recommendations')
            interactions = normalize_interactions(pl.read_csv(INTERACTIONS_PATH))
            top_items = (
                interactions
                .sort('timestamp')
                .unique(['user_id', 'item_id'], keep='last')
                .filter(pl.col('action') == 'like')
                .groupby('item_id')
                .count()
                .sort('count', descending=True)
                .head(100)
            )['item_id'].to_list()

            top_items = [str(item_id) for item_id in top_items]

            redis_connection.set('top_items', json.dumps(top_items))
        await asyncio.sleep(10)


async def main():
    await asyncio.gather(
        collect_messages(),
        calculate_top_recommendations(),
    )


if __name__ == '__main__':
    asyncio.run(main())
