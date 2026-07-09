import redis
from fastapi import FastAPI

from models import InteractEvent, NewItemsEvent, RecommendationsResponse
from recommendations.service import add_catalog_items, build_recommendations
from state_cleanup import archive_interactions_csv, purge_rabbitmq_queue
from watched_filter import WatchedFilter

app = FastAPI()

redis_connection = redis.Redis('localhost')
watched_filter = WatchedFilter(redis_connection)


@app.get('/healthcheck')
def healthcheck():
    return 200


@app.get('/cleanup')
def cleanup():
    archive_interactions_csv()
    try:
        redis_connection.flushdb()
    except redis.exceptions.ConnectionError:
        pass
    purge_rabbitmq_queue()
    return 200


@app.post('/add_items')
def add_movie(request: NewItemsEvent):
    add_catalog_items(redis_connection, request.item_ids, request.genres)
    return 200


@app.get('/recs/{user_id}')
def get_recs(user_id: str):
    item_ids = build_recommendations(redis_connection, watched_filter, user_id)
    return RecommendationsResponse(item_ids=item_ids)


@app.post('/interact')
async def interact(request: InteractEvent):
    for item_id in request.item_ids:
        watched_filter.add(request.user_id, item_id)
    return 200
