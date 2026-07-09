import asyncio
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import aio_pika
import redis
from aio_pika import Message
from aio_pika.abc import AbstractRobustExchange, AbstractRobustConnection
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from models import InteractEvent
from online_update import update_user_from_interact
from request_logging import log_request

app = FastAPI()

queue_name = "user_interactions"
routing_key = "user.interact.message"

_rabbitmq_connection: AbstractRobustConnection = None
_rabbitmq_exchange = None
_redis_connection = redis.Redis('localhost')

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get('/healthcheck')
def read_root():
    return True


@app.post('/interact')
async def interact(message: InteractEvent):
    message.timestamp = time.time()
    update_meta = {'updated': False}
    try:
        update_meta = update_user_from_interact(
            _redis_connection,
            message.user_id,
            message.item_ids,
            message.actions,
        )
    except Exception as exc:
        update_meta = {'updated': False, 'error': repr(exc)}

    await publish_message(Message(
        bytes(json.dumps(message.model_dump()), "utf-8"),
        content_type="text/json",
    ))
    log_request(
        'collector',
        'interact',
        user_id=message.user_id,
        item_ids=message.item_ids,
        actions=message.actions,
        online_update=update_meta,
    )
    return 200


async def create_rabbitmq_exchange() -> AbstractRobustExchange:
    global _rabbitmq_exchange, _rabbitmq_connection
    if _rabbitmq_exchange is None or _rabbitmq_connection.is_closed:
        _rabbitmq_connection = await aio_pika.connect_robust(
            "amqp://guest:guest@localhost/",
            loop=asyncio.get_event_loop()
        )
        channel = await _rabbitmq_connection.channel()
        _rabbitmq_exchange = await channel.declare_exchange("user.interact", type='direct')
        queue = await channel.declare_queue(queue_name)
        await queue.bind(_rabbitmq_exchange, routing_key)
    return _rabbitmq_exchange


async def publish_message(message: Message):
    rabbitmq_exchange = await create_rabbitmq_exchange()
    await rabbitmq_exchange.publish(
        message,
        routing_key,
    )
