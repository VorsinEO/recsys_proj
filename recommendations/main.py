import time

import redis
from fastapi import FastAPI

from models import InteractEvent, NewItemsEvent, RecommendationsResponse
from recommendations.service import add_catalog_items, build_recommendations
from request_logging import archive_request_log, log_request
from state_cleanup import archive_interactions_csv, purge_rabbitmq_queue
from watched_filter import WatchedFilter

app = FastAPI()

redis_connection = redis.Redis('localhost')
watched_filter = WatchedFilter(redis_connection)


@app.get('/healthcheck')
def healthcheck():
    return True


@app.get('/cleanup')
def cleanup():
    archived_interactions = archive_interactions_csv()
    archived_requests = archive_request_log()
    try:
        redis_connection.flushdb()
    except redis.exceptions.ConnectionError:
        pass
    log_request(
        'recs',
        'cleanup',
        archived_interactions=str(archived_interactions) if archived_interactions else None,
        archived_requests=str(archived_requests) if archived_requests else None,
    )
    return True


@app.post('/add_items')
def add_movie(request: NewItemsEvent):
    add_catalog_items(redis_connection, request.item_ids, request.genres)
    log_request(
        'recs',
        'add_items',
        item_count=len(request.item_ids),
        sample_item_ids=request.item_ids[:5],
    )
    return 200


@app.get('/recs/{user_id}')
def get_recs(user_id: str):
    started = time.time()
    item_ids, meta = build_recommendations(redis_connection, watched_filter, user_id)
    log_request(
        'recs',
        'recs',
        user_id=user_id,
        returned_count=len(item_ids),
        item_ids=item_ids,
        latency_ms=round((time.time() - started) * 1000, 2),
        **meta,
    )
    return RecommendationsResponse(item_ids=item_ids)


@app.post('/interact')
async def interact(request: InteractEvent):
    for item_id in request.item_ids:
        watched_filter.add(request.user_id, item_id)
    return 200
